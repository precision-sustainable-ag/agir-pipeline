"""
orchestrator/submit_jobs.py
============================

Claim a lease for each batch and submit one Slurm job.

The login node does exactly two things:
  1. Claim an exclusive lease in SQLite (prevents duplicate submissions).
  2. Render a per-batch Slurm script and call sbatch.

Input staging happens before submission. Everything downstream — stage CLI,
promote, artifact copy, ingest — happens inside the Slurm job on the compute
node.

Stage identity and the CLI invocation are declared in the config YAML
under a ``stage:`` block so this module works for any pipeline stage
without modification:

  stage:
    name: raw_to_jpg
    cli_module: stages.raw_to_jpg.cli
    cli_args: >-
      --c $CONFIG_PATH
      --i $TMPDIR/input
      --o $TMPDIR/output
      --t $CPUS
      --batch-id $BATCH_ID
    output_subdir: raw_to_jpg
    promote_script: stages/raw_to_jpg/promote.py
    promote_args: "--run-dir $RUN_DIR --dest $FINAL_DEST"

  stage:
    name: jpg_to_det
    cli_module: stages.jpg_to_det.cli
    cli_args: >-
      --c $CONFIG_PATH
      --m $MODEL_PATH
      --i $TMPDIR/input
      --o $TMPDIR/output
      --t $CPUS
      --batch-id $BATCH_ID
      --device cuda
    output_subdir: jpg_to_det
    promote_script: stages/jpg_to_det/promote.py
    promote_args: "--run-dir $RUN_DIR --dest $FINAL_DEST"

``cli_args`` and ``promote_args`` are embedded verbatim into the bash
script and may reference any variable set earlier in that script
($BATCH_ID, $CONFIG_PATH, $MODEL_PATH, $TMPDIR, $CPUS, $RUN_DIR,
$FINAL_DEST, $AGIR_DIR, ...).

Generated job script lifecycle (on compute node):
  1. validate staged inputs already exist
  2. rsync           staged inputs → $TMPDIR/input
  3. python -m       stage CLI
  4. python          promote script
  5. rsync           $TMPDIR/output → 90daydata/stage_runs/<run_id>/
  6. python          scripts/job/ingest_and_release.py (writes stage_runs row,
                     releases lease)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import List, NamedTuple, Optional

from orchestrator.config import load_stage_config
from orchestrator.sqlite_db import (
    open_db,
    claim_stage_lease,
    update_lease_slurm_job_id,
)

logger = logging.getLogger(__name__)

ORCHESTRATOR_ID = f"orchestrator.{os.getpid()}"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class JobResult(NamedTuple):
    batch_id: str
    status: str       # submitted | lease_conflict | sbatch_failed | dry_run
    slurm_job_id: Optional[str]
    lease_id: Optional[str]
    error: Optional[str]


# ---------------------------------------------------------------------------
# Slurm script rendering
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_JOB_TEMPLATE = "slurm_job.sh.j2"


def _template_env():
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_template(template_name: str, context: dict) -> str:
    template = _template_env().get_template(template_name)
    return template.render(**context)


def _job_template_name(cfg: dict) -> str:
    render_cfg = cfg.get("render", {})
    return render_cfg.get("template", DEFAULT_JOB_TEMPLATE)


def _visualization_command(stage_name: str, visualization_cfg: Optional[dict]) -> str:
    if not visualization_cfg or not visualization_cfg.get("enabled", False):
        return f'log "No visualization configured for stage {stage_name}"'

    mode = visualization_cfg.get("mode", stage_name)
    args = visualization_cfg.get("args", {})
    parts = [("mode", mode), *args.items()]
    lines = ['python "$AGIR_DIR/scripts/job/visualize.py" \\']
    for index, (name, value) in enumerate(parts):
        suffix = " \\" if index < len(parts) - 1 else ""
        flag = str(name).replace("_", "-")
        lines.append(f"    --{flag} {value}{suffix}")
    return "\n".join(lines)


def _render_slurm_script(
    *,
    stage_name: str,
    cli_module: str,
    cli_args: str,
    output_subdir: str,
    viz_cmd: str,
    viz_dest: str,
    batch_id: str,
    lease_id: str,
    orchestrator_id: str,
    agir_pipeline_dir: str,
    stage_config: str,
    model_path: str,
    uv_env: str,
    db_path: str,
    src_endpoint: str,
    dst_endpoint: str,
    src_batch_path: str,
    input_staging_dir: str,
    output_stage_runs: str,
    final_dest: str,
    log_dir: str,
    account: str,
    partition: str,
    gres: Optional[str],
    cpus: int,
    mem: str,
    time_limit: str,
    template_name: str = DEFAULT_JOB_TEMPLATE,
) -> str:
    context = {
        "stage_name": stage_name,
        "cli_module": cli_module,
        "cli_args": cli_args,
        "output_subdir": output_subdir,
        "viz_cmd": viz_cmd,
        "viz_dest": viz_dest,
        "batch_id": batch_id,
        "lease_id": lease_id,
        "orchestrator_id": orchestrator_id,
        "agir_pipeline_dir": agir_pipeline_dir,
        "stage_config": stage_config,
        "model_path": model_path,
        "uv_env": uv_env,
        "db_path": db_path,
        "input_staging_dir": input_staging_dir,
        "output_stage_runs": output_stage_runs,
        "final_dest": final_dest,
        "log_dir": log_dir,
        "account": account,
        "partition": partition,
        "gres": gres,
        "cpus": cpus,
        "mem": mem,
        "time_limit": time_limit,
    }
    return _render_template(template_name, context)


# ---------------------------------------------------------------------------
# Main submission loop
# ---------------------------------------------------------------------------

def submit_jobs(
    batch_ids: List[str],
    config_path: str,
    *,
    dry_run: bool = False,
) -> List[JobResult]:
    """
    Claim leases and submit one Slurm job per batch.

    Parameters
    ----------
    batch_ids : list[str]
        Batches to submit.
    config_path : str
        Path to the stage YAML config.
    dry_run : bool
        If True, render scripts but do not call sbatch.

    Returns
    -------
    list[JobResult]
    """
    cfg = load_stage_config(config_path)
    template_name = _job_template_name(cfg)

    # ── Stage block ───────────────────────────────────────────────────────────
    stage_cfg      = cfg["stage"]
    stage_name     = stage_cfg["name"]
    cli_module     = stage_cfg["cli_module"]
    cli_args       = stage_cfg["cli_args"].strip()
    output_subdir  = stage_cfg.get("output_subdir", stage_name)
    visualization_cfg = cfg.get("visualization")

    # ── Paths block ───────────────────────────────────────────────────────────
    paths          = cfg["paths"]
    agir_dir       = paths["agir_pipeline_dir"]
    stage_config   = paths["stage_config"]
    model_path     = paths.get("det_model_path", paths.get("model_path", ""))
    uv_env         = paths["uv_env"]
    db_path        = paths["db"]
    input_root     = paths["input_staging_root"]
    output_runs    = paths["output_stage_runs"]
    final_dest_root = paths["final_dest_root"]
    log_dir        = paths["log_dir"]
    script_dir     = Path(paths.get("script_dir", log_dir)) / "scripts"

    # ── Transfer block ────────────────────────────────────────────────────────
    transfer       = cfg["transfer"]
    src_endpoint   = transfer["juno_endpoint"]
    dst_endpoint   = transfer.get("atlas_endpoint") or transfer["ceres_endpoint"]
    route          = transfer["routes"][stage_name]
    src_root       = route["source_root_juno"]
    # input_subdir: optional subdirectory appended to <input_staging_root>/<batch_id>
    # e.g. jpg_to_det reads from .../semifield-developed-images/<batch_id>/images/
    #      raw_to_jpg reads from .../semifield-upload/<batch_id>/  (no subdir)
    input_subdir   = route.get("input_subdir", "")

    # ── Slurm block ───────────────────────────────────────────────────────────
    slurm          = cfg["slurm"]
    account        = slurm["account"]
    partition      = slurm["partition"]
    gres           = slurm.get("gres")          # optional — only gpu stages need it
    cpus           = int(slurm["cpus_per_task"])
    mem            = slurm["mem"]
    time_limit     = slurm["time"]

    # TTL for the lease: parse time_limit (HH:MM:SS or D-HH:MM:SS) → seconds
    ttl_seconds    = _parse_time_to_seconds(time_limit)

    script_dir.mkdir(parents=True, exist_ok=True)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    results: List[JobResult] = []
    conn = open_db(db_path)
    try:
        for batch_id in batch_ids:
            lease = claim_stage_lease(
                conn,
                batch_id=batch_id,
                stage=stage_name,
                orchestrator_id=ORCHESTRATOR_ID,
                ttl_seconds=ttl_seconds,
            )
            if not lease.get("claimed"):
                logger.warning(
                    "[%s] Lease conflict — already claimed (lease_id=%s)",
                    batch_id, lease.get("lease_id"),
                )
                results.append(JobResult(batch_id, "lease_conflict", None, None, None))
                continue

            lease_id = str(lease["lease_id"])
            input_staging_dir = f"{input_root}/{batch_id}"
            if input_subdir:
                input_staging_dir = f"{input_staging_dir}/{input_subdir}"
            # final_dest suffix is stage-specific — read from config or default to stage name
            dest_suffix = stage_cfg.get("final_dest_suffix", stage_name)
            final_dest = f"{final_dest_root}/{batch_id}/{dest_suffix}"

            # Visualization destination: <final_dest_root>/<batch_id>/<stage>/
            viz_dest = f"{final_dest_root}/{batch_id}/{stage_name}"

            viz_cmd = _visualization_command(stage_name, visualization_cfg)

            # src_batch_path: per-stage subpath on Juno where inputs live
            src_batch_path = f"{src_root}/{batch_id}"

            script_text = _render_slurm_script(
                stage_name=stage_name,
                cli_module=cli_module,
                cli_args=cli_args,
                output_subdir=output_subdir,
                viz_cmd=viz_cmd,
                viz_dest=viz_dest,
                batch_id=batch_id,
                lease_id=lease_id,
                orchestrator_id=ORCHESTRATOR_ID,
                agir_pipeline_dir=agir_dir,
                stage_config=stage_config,
                model_path=model_path,
                uv_env=uv_env,
                db_path=db_path,
                src_endpoint=src_endpoint,
                dst_endpoint=dst_endpoint,
                src_batch_path=src_batch_path,
                input_staging_dir=input_staging_dir,
                output_stage_runs=output_runs,
                final_dest=final_dest,
                log_dir=log_dir,
                account=account,
                partition=partition,
                gres=gres,
                cpus=cpus,
                mem=mem,
                time_limit=time_limit,
                template_name=template_name,
            )

            script_path = script_dir / f"{stage_name}_{batch_id}.sh"
            script_path.write_text(script_text)
            script_path.chmod(0o755)

            if dry_run:
                logger.info("[DRY-RUN] Would submit: %s", script_path)
                results.append(JobResult(batch_id, "dry_run", None, lease_id, None))
                continue

            try:
                proc = subprocess.run(
                    ["sbatch", str(script_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                slurm_job_id = proc.stdout.strip().split()[-1]
                logger.info(
                    "[%s] Submitted job %s (lease %s)", batch_id, slurm_job_id, lease_id
                )
                try:
                    update_lease_slurm_job_id(conn, lease_id, slurm_job_id)
                except Exception as exc:
                    logger.warning("[%s] Could not record slurm_job_id on lease: %s", batch_id, exc)
                results.append(JobResult(batch_id, "submitted", slurm_job_id, lease_id, None))

            except subprocess.CalledProcessError as exc:
                logger.error(
                    "[%s] sbatch failed (exit %d): %s",
                    batch_id, exc.returncode, exc.stderr.strip(),
                )
                results.append(
                    JobResult(batch_id, "sbatch_failed", None, lease_id, exc.stderr.strip())
                )
    finally:
        conn.close()

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time_to_seconds(time_str: str) -> int:
    """
    Parse a Slurm time string to seconds.

    Accepts: HH:MM:SS  or  D-HH:MM:SS
    Adds a 10-minute buffer so leases outlast the job.
    """
    try:
        if "-" in time_str:
            days_part, hms = time_str.split("-", 1)
            days = int(days_part)
        else:
            days, hms = 0, time_str
        h, m, s = (int(x) for x in hms.split(":"))
        return days * 86400 + h * 3600 + m * 60 + s + 600
    except Exception:
        logger.warning("Could not parse time_limit %r; defaulting to 24h TTL", time_str)
        return 86400
