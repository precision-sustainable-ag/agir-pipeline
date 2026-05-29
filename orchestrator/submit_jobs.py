"""
orchestrator/submit_jobs.py
============================

Claim a lease for each batch and submit one Slurm job.

The login node does exactly two things:
  1. Claim an exclusive lease in SQLite (prevents duplicate submissions).
  2. Render a per-batch Slurm script and call sbatch.

Everything else — Globus transfer, stage CLI, promote, ingest — happens
*inside* the Slurm job on the compute node.

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
  1. Globus transfer  Juno → 90daydata
  2. cp              90daydata → $TMPDIR/input
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

_SLURM_TEMPLATE = """\
#!/usr/bin/env bash
#SBATCH --job-name={stage_name}_{batch_id}
#SBATCH --account={account}
#SBATCH --partition={partition}
{gres_line}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem}
#SBATCH --time={time_limit}
#SBATCH -o {log_dir}/{batch_id}-%j.out
#SBATCH -e {log_dir}/{batch_id}-%j.err

# ── Job-level constants ───────────────────────────────────────────────────────
BATCH_ID="{batch_id}"
LEASE_ID="{lease_id}"
ORCHESTRATOR_ID="{orchestrator_id}"
AGIR_DIR="{agir_pipeline_dir}"
CONFIG_PATH="{stage_config}"
MODEL_PATH="{model_path}"
CPUS={cpus}
DB_PATH="{db_path}"

SRC_ENDPOINT="{src_endpoint}"
DST_ENDPOINT="{dst_endpoint}"
# Source is the per-batch subdirectory on Juno LTS
SRC_BATCH_PATH="{src_batch_path}"
INPUT_STAGING_DIR="{input_staging_dir}"

OUTPUT_STAGE_RUNS="{output_stage_runs}"
FINAL_DEST="{final_dest}"

# ── Logging helpers ───────────────────────────────────────────────────────────
JOB_LOG="{log_dir}/{batch_id}-${{SLURM_JOB_ID}}.log"

log() {{
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$JOB_LOG"
}}
log_section() {{
    log "════════════════════════════════════════"
    log "$*"
    log "════════════════════════════════════════"
}}
on_exit() {{
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        log "[ERROR] Job exited with code $rc"
    fi
    log "Job finished — exit=$rc"
}}
trap on_exit EXIT
set -euo pipefail

# ── Environment ───────────────────────────────────────────────────────────────
source "{uv_env}"
cd "$AGIR_DIR"
export PYTHONPATH="$AGIR_DIR${{PYTHONPATH:+:$PYTHONPATH}}"

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

log_section "{stage_name} starting"
log "Batch:   $BATCH_ID"
log "Node:    $(hostname -f)"
log "Job ID:  $SLURM_JOB_ID"

# ── Step 1: Globus transfer Juno → 90daydata ──────────────────────────────────
log_section "[1/5] Globus transfer"
log "Source: $SRC_BATCH_PATH"
log "Dest:   $DST_ENDPOINT:$INPUT_STAGING_DIR"

mkdir -p "$INPUT_STAGING_DIR"

set +e
EXISTING=$(find "$INPUT_STAGING_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)
set -e

if [[ "$EXISTING" -gt 0 ]]; then
    log "Already staged — $EXISTING file(s) found — skipping Globus transfer"
else
    log "No files found locally — initiating Globus transfer ..."

    TRANSFER_OUT=$(mktemp)
    set +e
    globus transfer \\
        --recursive \\
        --sync-level mtime \\
        --label "{stage_name}-$BATCH_ID-$SLURM_JOB_ID" \\
        --format json \\
        "$SRC_ENDPOINT:$SRC_BATCH_PATH" \\
        "$DST_ENDPOINT:$INPUT_STAGING_DIR" \\
        > "$TRANSFER_OUT" 2>&1
    TRANSFER_RC=$?
    set -e

    cat "$TRANSFER_OUT" | tee -a "$JOB_LOG"

    if [[ $TRANSFER_RC -ne 0 ]]; then
        log "[ERROR] globus transfer failed (exit=$TRANSFER_RC)"
        rm -f "$TRANSFER_OUT"
        python "$AGIR_DIR/scripts/job/ingest_and_release.py" \\
            --db "$DB_PATH" \\
            --lease-id "$LEASE_ID" \\
            --orchestrator-id "$ORCHESTRATOR_ID" \\
            --release-reason "globus_transfer_failed"
        exit 1
    fi

    TASK_ID=$(python3 -c "import json,sys; print(json.load(open('$TRANSFER_OUT'))['task_id'])")
    rm -f "$TRANSFER_OUT"
    log "Transfer submitted: task_id=$TASK_ID"

    set +e
    globus task wait --timeout 7200 --polling-interval 30 "$TASK_ID"
    WAIT_RC=$?
    set -e

    if [[ $WAIT_RC -ne 0 ]]; then
        log "[ERROR] globus task wait failed (exit=$WAIT_RC)"
        python "$AGIR_DIR/scripts/job/ingest_and_release.py" \\
            --db "$DB_PATH" \\
            --lease-id "$LEASE_ID" \\
            --orchestrator-id "$ORCHESTRATOR_ID" \\
            --release-reason "globus_transfer_failed"
        exit 1
    fi
    log "Transfer complete"
fi

FILE_COUNT=$(find "$INPUT_STAGING_DIR" -maxdepth 1 -type f | wc -l)
log "Files available: $FILE_COUNT"
if [[ "$FILE_COUNT" -eq 0 ]]; then
    log "[ERROR] No input files found at $INPUT_STAGING_DIR"
    python "$AGIR_DIR/scripts/job/ingest_and_release.py" \\
        --db "$DB_PATH" \\
        --lease-id "$LEASE_ID" \\
        --orchestrator-id "$ORCHESTRATOR_ID" \\
        --release-reason "no_input_files"
    exit 1
fi

# ── Step 2: Copy inputs to TMPDIR ─────────────────────────────────────────────
log_section "[2/6] Copy inputs to TMPDIR"
TMPINPUT="$TMPDIR/input"
TMPOUTPUT="$TMPDIR/output"
mkdir -p "$TMPINPUT" "$TMPOUTPUT"

cp -r "$INPUT_STAGING_DIR"/. "$TMPINPUT"/
log "Copied $FILE_COUNT file(s) to $TMPINPUT"

# ── Step 3: Run stage CLI ─────────────────────────────────────────────────────
log_section "[3/6] Running {stage_name}"

set +e
python -m {cli_module} \\
    {cli_args} \\
    --batch-id "$BATCH_ID"
STAGE_EXIT=$?
set -e
log "Stage exit code: $STAGE_EXIT"

# Resolve run dir from output
RUN_DIR=$(ls -td "$TMPOUTPUT/{output_subdir}"/*/  2>/dev/null | head -1)
RUN_ID=$(basename "$RUN_DIR" 2>/dev/null || echo "unknown")

if [[ -z "$RUN_DIR" || ! -d "$RUN_DIR" ]]; then
    log "[ERROR] Could not resolve run directory under $TMPOUTPUT/{output_subdir}"
    python "$AGIR_DIR/scripts/job/ingest_and_release.py" \\
        --db "$DB_PATH" \\
        --lease-id "$LEASE_ID" \\
        --orchestrator-id "$ORCHESTRATOR_ID" \\
        --release-reason "no_run_dir"
    exit 1
fi
log "Run dir: $RUN_DIR"
log "Run ID:  $RUN_ID"

# ── Step 4: Visualize sample ──────────────────────────────────────────────────
log_section "[4/6] Generating visualization sample"
VIZ_DIR="$TMPDIR/visualizations"
mkdir -p "$VIZ_DIR"
VIZ_EXIT=0

if [[ "$STAGE_EXIT" -le 1 ]]; then
    set +e
    {viz_cmd}
    VIZ_EXIT=$?
    set -e
    if [[ $VIZ_EXIT -eq 0 ]]; then
        log "Visualization sample written to $VIZ_DIR"
    else
        log "[WARN] Visualization step exited $VIZ_EXIT — continuing"
        VIZ_EXIT=0   # non-fatal
    fi
else
    log "Skipping visualization — stage_exit=$STAGE_EXIT"
fi

# ── Step 5: Promote outputs ───────────────────────────────────────────────────
log_section "[5/6] Promoting outputs"
PROMOTE_EXIT=0
VIZ_DEST="{viz_dest}"

if [[ "$STAGE_EXIT" -le 1 ]]; then
    mkdir -p "$FINAL_DEST"
    set +e
    python "$AGIR_DIR/scripts/job/promote.py" \\
        --run-dir "$RUN_DIR" \\
        --dest "$FINAL_DEST"
    PROMOTE_EXIT=$?
    set -e
    log "Promote exit: $PROMOTE_EXIT"

    # Promote visualization sample (non-fatal)
    if [[ -d "$VIZ_DIR" && "$(ls -A "$VIZ_DIR" 2>/dev/null)" ]]; then
        mkdir -p "$VIZ_DEST"
        VIZ_ZIP="$TMPDIR/{stage_name}_sample_${{BATCH_ID}}.zip"
        zip -j "$VIZ_ZIP" "$VIZ_DIR"/*
        cp "$VIZ_ZIP" "$VIZ_DEST/"
        log "Visualization sample zipped and promoted to $VIZ_DEST"
    else
        log "No visualization files to promote"
    fi
else
    log "Skipping promotion — stage_exit=$STAGE_EXIT"
fi

# ── Step 6: Copy artifacts to 90daydata and ingest ───────────────────────────
log_section "[6/6] Copy artifacts + ingest"
ARTIFACT_DEST="$OUTPUT_STAGE_RUNS/$RUN_ID"
mkdir -p "$ARTIFACT_DEST"
rsync -a "$RUN_DIR/" "$ARTIFACT_DEST/"
log "Artifacts saved to $ARTIFACT_DEST"

RELEASE_REASON="stage_exit_${{STAGE_EXIT}}_promote_exit_${{PROMOTE_EXIT}}"
python "$AGIR_DIR/scripts/job/ingest_and_release.py" \\
    --db "$DB_PATH" \\
    --run-report "$ARTIFACT_DEST/run_report.json" \\
    --lease-id "$LEASE_ID" \\
    --orchestrator-id "$ORCHESTRATOR_ID" \\
    --release-reason "$RELEASE_REASON"

log "DONE — stage={stage_name} batch=$BATCH_ID stage_exit=$STAGE_EXIT promote_exit=$PROMOTE_EXIT"
"""


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
) -> str:
    gres_line = f"#SBATCH --gres={gres}" if gres else ""
    return _SLURM_TEMPLATE.format(
        stage_name=stage_name,
        cli_module=cli_module,
        cli_args=cli_args,
        output_subdir=output_subdir,
        viz_cmd=viz_cmd,
        viz_dest=viz_dest,
        batch_id=batch_id,
        lease_id=lease_id,
        orchestrator_id=orchestrator_id,
        agir_pipeline_dir=agir_pipeline_dir,
        stage_config=stage_config,
        model_path=model_path,
        uv_env=uv_env,
        db_path=db_path,
        src_endpoint=src_endpoint,
        dst_endpoint=dst_endpoint,
        src_batch_path=src_batch_path,
        input_staging_dir=input_staging_dir,
        output_stage_runs=output_stage_runs,
        final_dest=final_dest,
        log_dir=log_dir,
        account=account,
        partition=partition,
        gres_line=gres_line,
        cpus=cpus,
        mem=mem,
        time_limit=time_limit,
    )


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

    # ── Stage block ───────────────────────────────────────────────────────────
    stage_cfg      = cfg["stage"]
    stage_name     = stage_cfg["name"]
    cli_module     = stage_cfg["cli_module"]
    cli_args       = stage_cfg["cli_args"].strip()
    output_subdir  = stage_cfg.get("output_subdir", stage_name)

    # Visualization config — read from stage block with sensible defaults
    viz_sample_size = int(stage_cfg.get("viz_sample_size", 24))
    viz_scale       = float(stage_cfg.get("viz_scale", 0.15))
    viz_max_width   = int(stage_cfg.get("viz_max_width", 1800))

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

            # Build the visualize command for this stage
            if stage_name == "raw_to_jpg":
                viz_cmd = (
                    f'python "$AGIR_DIR/scripts/job/visualize.py" \\\n'
                    f'    --mode raw_to_jpg \\\n'
                    f'    --images "$RUN_DIR/artifacts" \\\n'
                    f'    --output "$VIZ_DIR" \\\n'
                    f'    --sample-size {viz_sample_size} \\\n'
                    f'    --scale {viz_scale}'
                )
            elif stage_name == "jpg_to_det":
                viz_cmd = (
                    f'python "$AGIR_DIR/scripts/job/visualize.py" \\\n'
                    f'    --mode jpg_to_det \\\n'
                    f'    --images "$TMPINPUT" \\\n'
                    f'    --detections "$RUN_DIR/artifacts" \\\n'
                    f'    --output "$VIZ_DIR" \\\n'
                    f'    --sample-size {viz_sample_size} \\\n'
                    f'    --max-width {viz_max_width}'
                )
            else:
                # Unknown stage — skip visualization gracefully
                viz_cmd = f'log "No visualization configured for stage {stage_name}"'

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