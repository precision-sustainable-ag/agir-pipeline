"""
Submit SLURM jobs for a pipeline stage across a list of batches.

For each batch:
  1. Verify the input-staging transfer is confirmed complete in the DB.
  2. Claim an exclusive stage lease (prevents duplicate submissions).
  3. Render a per-batch SLURM job script from the config template.
  4. Submit via sbatch and record the job ID on the lease row.

The generated job script handles the full compute-node lifecycle:
  copy-in → run stage → promote (optional) → copy-out artifacts → ingest + release lease

Stage identity and the compute-node CLI invocation are declared in the config YAML
under a ``stage:`` block, so this module works for any pipeline stage without
modification:

  stage:
    name: raw_to_jpg
    cli_module: stages.raw_to_jpg.cli
    cli_args: "--c $CONFIG_PATH --i $TMPDIR/input --o $TMPDIR/output --t $CPUS --batch-id $BATCH_ID"
    output_subdir: raw_to_jpg
    promote_script: stages/raw_to_jpg/promote.py   # omit if stage has no promote step
    promote_args: "--run-dir $RUN_DIR --dest $FINAL_DEST"

``cli_args`` and ``promote_args`` are embedded verbatim into the bash script, so
they may reference any bash variable set earlier in the script ($BATCH_ID,
$CONFIG_PATH, $TMPDIR, $CPUS, $RUN_DIR, $FINAL_DEST, $AGIR_DIR, etc.).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, NamedTuple, Optional

from agir_db import AgirDB

from ..input_manifest import load_input_manifest, validate_input_manifest
from ..batch_list import BatchEntry
from ..config import load_stage_config


logger = logging.getLogger(__name__)

ORCHESTRATOR_ID = f"orchestrator.manual.{os.getpid()}"


class JobResult(NamedTuple):
    batch_id: str
    status: str  # submitted | lease_conflict | no_input_manifest | sbatch_failed
    slurm_job_id: Optional[str]
    lease_id: Optional[str]
    error: Optional[str]

def submit_stage_jobs(
    batch_entries: List[BatchEntry],
    config_path: str,
    require_transfer_complete: bool = True,
) -> List[JobResult]:
    """Claim leases and submit one SLURM job per batch.

    The stage name and CLI invocation are read from the ``stage:`` block in the
    config YAML — no stage-specific code lives here.

    Returns a list of JobResult namedtuples (one per batch entry).
    """
    cfg = load_stage_config(config_path)

    stage_cfg = cfg.get("stage", {})
    stage_name = stage_cfg.get("name")
    if not stage_name:
        raise ValueError(f"Config '{config_path}' is missing required 'stage.name' field.")
    cli_module = stage_cfg.get("cli_module")
    if not cli_module:
        raise ValueError(f"Config '{config_path}' is missing required 'stage.cli_module' field.")
    cli_args = stage_cfg.get("cli_args", "")
    output_subdir = stage_cfg.get("output_subdir", stage_name)
    promote_script = stage_cfg.get("promote_script")
    promote_args = stage_cfg.get("promote_args", "")

    paths_cfg = cfg.get("paths", {})
    slurm_cfg = cfg.get("slurm", {})

    agir_pipeline_dir = paths_cfg["agir_pipeline_dir"]
    stage_config = paths_cfg["stage_config"]
    input_staging_root = paths_cfg["input_staging_root"]
    output_stage_runs = paths_cfg["output_stage_runs"]
    final_dest_root = paths_cfg["final_dest_root"]
    log_dir = paths_cfg["log_dir"]

    cpus = int(slurm_cfg.get("cpus_per_task", 16))
    mem = slurm_cfg.get("mem", "128G")
    time_limit = slurm_cfg.get("time", "4:00:00")

    # convert time_limit to seconds and add buffer for lease TTL
    time_match = re.match(r"(\d+):(\d{2}):(\d{2})", time_limit)
    if not time_match:
        raise ValueError(f"Invalid time format in config: {time_limit!r}")
    hours, minutes, seconds = map(int, time_match.groups())
    total_seconds = hours * 3600 + minutes * 60 + seconds

    account = slurm_cfg.get("account", "dash_agir")

    script_dir = Path(output_stage_runs) / "job_scripts"
    script_dir.mkdir(parents=True, exist_ok=True)

    results: List[JobResult] = []

    with AgirDB() as db:
        for entry in batch_entries:
            batch_id = entry.batch_id

            window_key = f"{entry.start_epoch}_{entry.end_epoch}"
            
            transfer = None
            input_manifest_path = None
            
            if require_transfer_complete:

                transfer = db.orchestration.get_completed_windowed_input_transfer(
                    batch_id=batch_id,
                    stage=stage_name,
                    window_key=window_key,
                )
                if not transfer or not transfer.get("input_manifest_ref"):
                    logger.warning(
                        "[%s] No completed input transfer with manifest for window %s — run stage_inputs first",
                        batch_id,
                        window_key,
                    )
                    results.append(
                        JobResult(
                            batch_id,
                            "no_input_manifest",
                            None,
                            None,
                            "No completed input transfer with input_manifest_ref",
                        )
                    )
                    continue

                input_manifest_path = str(transfer["input_manifest_ref"])
                try:
                    manifest = load_input_manifest(input_manifest_path)
                    validate_input_manifest(
                        manifest,
                        expected_stage=stage_name,
                        expected_batch_id=batch_id,
                        expected_window_key=window_key,
                    )
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "[%s] Input manifest %s is not usable: %s",
                        batch_id,
                        input_manifest_path,
                        exc,
                    )
                    results.append(
                        JobResult(batch_id, "no_input_manifest", None, None, str(exc))
                    )
            else:
                logger.info("[%s] Skipping transfer check (require_transfer_complete=False)", batch_id)
                input_manifest_path = ""
            lease = db.orchestration.claim_stage_lease(
                batch_id=batch_id,
                stage=stage_name,
                orchestrator_id=ORCHESTRATOR_ID,
                ttl_seconds=total_seconds,
                window_key=window_key,
            )
            if not lease.get("claimed"):
                logger.warning("[%s] Could not claim lease — active lease exists", batch_id)
                results.append(JobResult(batch_id, "lease_conflict", None, None, None))
                continue

            lease_id = str(lease["lease_id"])
            input_dir = f"{input_staging_root}/{batch_id}"
            final_dest = f"{final_dest_root}/{batch_id}/images"

            script_text = _render_slurm_script(
                stage_name=stage_name,
                cli_module=cli_module,
                cli_args=cli_args,
                output_subdir=output_subdir,
                promote_script=promote_script,
                promote_args=promote_args,
                batch_id=batch_id,
                lease_id=lease_id,
                window_key=window_key,
                stage_config=stage_config,
                input_dir=input_dir,
                input_manifest_path=input_manifest_path or "",
                output_stage_runs=output_stage_runs,
                final_dest=final_dest,
                agir_pipeline_dir=agir_pipeline_dir,
                log_dir=log_dir,
                account=account,
                cpus=cpus,
                mem=mem,
                time_limit=time_limit,
            )

            script_path = script_dir / f"{stage_name}_{batch_id}_{window_key}.sh"
            script_path.write_text(script_text)
            script_path.chmod(0o755)

            try:
                proc = subprocess.run(
                    ["sbatch", str(script_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                match = re.search(r"Submitted batch job (\d+)", proc.stdout)
                if not match:
                    raise RuntimeError(
                        f"Could not parse SLURM job ID from sbatch output: {proc.stdout!r}"
                    )
                slurm_job_id = match.group(1)
            except (subprocess.CalledProcessError, RuntimeError) as exc:
                err = getattr(exc, "stderr", str(exc)) or str(exc)
                logger.error("[%s] sbatch failed: %s", batch_id, err)
                db.orchestration.release_stage_lease(
                    lease_id=lease_id,
                    orchestrator_id=ORCHESTRATOR_ID,
                    release_reason="sbatch_failed",
                )
                results.append(JobResult(batch_id, "sbatch_failed", None, lease_id, err))
                continue

            db.orchestration.update_lease_slurm_job_id(lease_id, slurm_job_id)
            logger.info("[%s] Submitted SLURM job %s (lease %s)", batch_id, slurm_job_id, lease_id)
            results.append(JobResult(batch_id, "submitted", slurm_job_id, lease_id, None))

    return results


def _render_promote_block(
    agir_pipeline_dir: str,
    promote_script: Optional[str],
    promote_args: str,
    stage_exit_var: str = "$STAGE_EXIT",
) -> str:
    if not promote_script:
        return f"""\
PROMOTE_EXIT=0
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] No promote step for this stage"
"""
    return f"""\
PROMOTE_EXIT=1
if [ "{stage_exit_var}" -eq 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Promoting outputs"
  python "{agir_pipeline_dir}/{promote_script}" \\
    {promote_args}
  PROMOTE_EXIT=$?
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] promote exited $PROMOTE_EXIT"
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Skipping promotion (stage_exit={stage_exit_var})"
fi
"""


def _render_slurm_script(
    stage_name: str,
    cli_module: str,
    cli_args: str,
    output_subdir: str,
    promote_script: Optional[str],
    promote_args: str,
    batch_id: str,
    lease_id: str,
    window_key: str,
    stage_config: str,
    input_dir: str,
    input_manifest_path: str,
    output_stage_runs: str,
    final_dest: str,
    agir_pipeline_dir: str,
    log_dir: str,
    account: str,
    cpus: int,
    mem: str,
    time_limit: str,
) -> str:
    promote_block = _render_promote_block(
        agir_pipeline_dir=agir_pipeline_dir,
        promote_script=promote_script,
        promote_args=promote_args,
        stage_exit_var="$STAGE_EXIT",
    )
    return f"""\
#!/bin/bash
#SBATCH --job-name={stage_name}_{batch_id}
#SBATCH --account={account}
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem}
#SBATCH --time={time_limit}
#SBATCH --output={log_dir}/{batch_id}_%j.out
#SBATCH --error={log_dir}/{batch_id}_%j.err

set -uo pipefail

BATCH_ID="{batch_id}"
LEASE_ID="{lease_id}"
WINDOW_KEY="{window_key}"
ORCHESTRATOR_ID="{ORCHESTRATOR_ID}"
CONFIG_PATH="{stage_config}"
INPUT_DIR="{input_dir}"
INPUT_MANIFEST_PATH="{input_manifest_path}"
OUTPUT_STAGE_RUNS="{output_stage_runs}"
FINAL_DEST="{final_dest}"
AGIR_DIR="{agir_pipeline_dir}"
CPUS={cpus}

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] START stage={stage_name} batch=$BATCH_ID job=$SLURM_JOB_ID"

mkdir -p "$TMPDIR/input" "$TMPDIR/output"

# ── 1. Materialize exactly the manifest workset on local scratch ─────────────
if [ -n "$INPUT_MANIFEST_PATH" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Copying manifest-defined inputs from $INPUT_MANIFEST_PATH"
  cp "$INPUT_MANIFEST_PATH" "$TMPDIR/input_manifest.json"
  python - <<'PY'
import json
import os
import shutil
from pathlib import Path

manifest_path = Path(os.environ["TMPDIR"]) / "input_manifest.json"
input_dir = Path(os.environ["TMPDIR"]) / "input"
manifest = json.loads(manifest_path.read_text())
for idx, item in enumerate(manifest.get("items", [])):
    source = Path(item["staged_path"])
    dest = input_dir / item.get("file_name", source.name)
    if not source.exists():
        raise FileNotFoundError('manifest item %d source file not found: %s' % (idx, source))
    shutil.copy2(source, dest)
PY
  COPY_EXIT=$?
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: INPUT_MANIFEST_PATH empty; copying full input dir $INPUT_DIR"
  rsync -a --info=progress2 "$INPUT_DIR/" "$TMPDIR/input/"
  COPY_EXIT=$?
fi
if [ "$COPY_EXIT" -ne 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: input materialization failed with exit $COPY_EXIT"
  python "$AGIR_DIR/scripts/ingest_and_release.py" \
    --lease-id "$LEASE_ID" \
    --orchestrator-id "$ORCHESTRATOR_ID" \
    --release-reason "input_materialization_failed"
  exit "$COPY_EXIT"
fi

# ── 2. Run stage ──────────────────────────────────────────────────────────────
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running {stage_name}"
cd "$AGIR_DIR"
python -m {cli_module} \\
  {cli_args}
STAGE_EXIT=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] {stage_name} exited $STAGE_EXIT"

# ── 3. Locate run directory ───────────────────────────────────────────────────
RUN_REPORT=$(find "$TMPDIR/output/{output_subdir}" -name "run_report.json" 2>/dev/null | head -1)
if [ -z "$RUN_REPORT" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: no run_report.json found"
  python "$AGIR_DIR/scripts/ingest_and_release.py" \\
    --lease-id "$LEASE_ID" \\
    --orchestrator-id "$ORCHESTRATOR_ID" \\
    --release-reason "no_run_report"
  exit 1
fi
RUN_DIR=$(dirname "$RUN_REPORT")
RUN_ID=$(python -c "import json; print(json.load(open('$RUN_REPORT'))['run_id'])")
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run_id=$RUN_ID"

# ── 4. Promote outputs ────────────────────────────────────────────────────────
{promote_block}
# ── 5. Copy artifacts back to 90daydata ──────────────────────────────────────
ARTIFACT_DEST="$OUTPUT_STAGE_RUNS/$RUN_ID"
mkdir -p "$ARTIFACT_DEST"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Copying artifacts to $ARTIFACT_DEST"
rsync -a "$RUN_DIR/" "$ARTIFACT_DEST/"

# ── 6. Ingest run_report and release lease ────────────────────────────────────
RELEASE_REASON="stage_exit_${{STAGE_EXIT}}_promote_exit_${{PROMOTE_EXIT}}"
python "$AGIR_DIR/scripts/ingest_and_release.py" \\
  --run-report "$ARTIFACT_DEST/run_report.json" \\
  --lease-id "$LEASE_ID" \\
  --orchestrator-id "$ORCHESTRATOR_ID" \\
  --release-reason "$RELEASE_REASON"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE stage={stage_name} stage_exit=$STAGE_EXIT promote_exit=$PROMOTE_EXIT"
"""
