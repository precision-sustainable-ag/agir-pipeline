"""
Submit raw_to_jpg SLURM jobs for a list of batches.

For each batch:
  1. Verify the input-staging transfer is confirmed complete in the DB.
  2. Claim an exclusive stage lease (prevents duplicate submissions).
  3. Render a per-batch SLURM job script from the config template.
  4. Submit via sbatch and record the job ID on the lease row.

The generated job script handles the full compute-node lifecycle:
  copy-in → raw_to_jpg → promote → copy-out artifacts → ingest + release lease
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import yaml

from agir_db import AgirDB

from .batch_list import BatchEntry

logger = logging.getLogger(__name__)

ORCHESTRATOR_ID = "orchestrator.manual"
# 4-hour walltime + 10-minute buffer so the lease outlives the job
_LEASE_TTL_SECONDS = 4 * 3600 + 600


class JobResult(NamedTuple):
    batch_id: str
    status: str                   # submitted | lease_conflict | transfer_not_complete | sbatch_failed | no_transfer
    slurm_job_id: Optional[str]
    lease_id: Optional[str]
    error: Optional[str]


def load_job_config(config_path: str) -> Dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def submit_raw_to_jpg_jobs(
    batch_entries: List[BatchEntry],
    config_path: str,
    require_transfer_complete: bool = True,
) -> List[JobResult]:
    """Claim leases and submit one SLURM job per batch.

    Returns a list of JobResult namedtuples (one per batch entry).
    """
    cfg = load_job_config(config_path)
    paths_cfg = cfg.get("paths", {})
    slurm_cfg = cfg.get("slurm", {})

    agir_pipeline_dir = paths_cfg["agir_pipeline_dir"]
    raw_to_jpg_config = paths_cfg["raw_to_jpg_config"]
    input_staging_root = paths_cfg["input_staging_root"]
    output_stage_runs = paths_cfg["output_stage_runs"]
    final_dest_root = paths_cfg["final_dest_root"]
    log_dir = paths_cfg["log_dir"]

    cpus = int(slurm_cfg.get("cpus_per_task", 16))
    mem = slurm_cfg.get("mem", "128G")
    time_limit = slurm_cfg.get("time", "4:00:00")
    account = slurm_cfg.get("account", "dash_agir")

    script_dir = Path(output_stage_runs) / "job_scripts"
    script_dir.mkdir(parents=True, exist_ok=True)

    results: List[JobResult] = []

    with AgirDB() as db:
        for entry in batch_entries:
            batch_id = entry.batch_id

            if require_transfer_complete:
                if not db.orchestration.are_windowed_inputs_staged(
                    batch_id, entry.start_epoch, entry.end_epoch
                ):
                    logger.warning(
                        "[%s] Windowed inputs not found on 90daydata — run stage_raw_inputs first",
                        batch_id,
                    )
                    results.append(JobResult(batch_id, "no_staged_inputs", None, None, None))
                    continue
                else:
                    logger.info("[%s] Confirmed staged inputs on 90daydata", batch_id)
            else:
                logger.info("[%s] Skipping transfer check (require_transfer_complete=False)", batch_id)

            window_key = f"{entry.start_epoch}_{entry.end_epoch}"
            lease = db.orchestration.claim_stage_lease(
                batch_id=batch_id,
                stage="raw_to_jpg",
                orchestrator_id=ORCHESTRATOR_ID,
                ttl_seconds=_LEASE_TTL_SECONDS,
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
                batch_id=batch_id,
                lease_id=lease_id,
                config_path=raw_to_jpg_config,
                input_dir=input_dir,
                output_stage_runs=output_stage_runs,
                final_dest=final_dest,
                agir_pipeline_dir=agir_pipeline_dir,
                log_dir=log_dir,
                account=account,
                cpus=cpus,
                mem=mem,
                time_limit=time_limit,
            )

            script_path = script_dir / f"raw_to_jpg_{batch_id}_{window_key}.sh"
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


def _render_slurm_script(
    batch_id: str,
    lease_id: str,
    config_path: str,
    input_dir: str,
    output_stage_runs: str,
    final_dest: str,
    agir_pipeline_dir: str,
    log_dir: str,
    account: str,
    cpus: int,
    mem: str,
    time_limit: str,
) -> str:
    return f"""\
#!/bin/bash
#SBATCH --job-name=raw_to_jpg_{batch_id}
#SBATCH --account={account}
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem}
#SBATCH --time={time_limit}
#SBATCH --output={log_dir}/{batch_id}_%j.out
#SBATCH --error={log_dir}/{batch_id}_%j.err

set -uo pipefail

BATCH_ID="{batch_id}"
LEASE_ID="{lease_id}"
ORCHESTRATOR_ID="{ORCHESTRATOR_ID}"
CONFIG_PATH="{config_path}"
INPUT_DIR="{input_dir}"
OUTPUT_STAGE_RUNS="{output_stage_runs}"
FINAL_DEST="{final_dest}"
AGIR_DIR="{agir_pipeline_dir}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] START batch=$BATCH_ID job=$SLURM_JOB_ID"

mkdir -p "$TMPDIR/input" "$TMPDIR/output"

# ── 1. Copy staged inputs to local scratch ────────────────────────────────────
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Copying inputs from $INPUT_DIR"
rsync -a --info=progress2 "$INPUT_DIR/" "$TMPDIR/input/"

# ── 2. Run raw_to_jpg ─────────────────────────────────────────────────────────
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running raw_to_jpg"
cd "$AGIR_DIR"
python -m stages.raw_to_jpg.cli \\
  --c "$CONFIG_PATH" \\
  --i "$TMPDIR/input" \\
  --o "$TMPDIR/output" \\
  --t {cpus} \\
  --batch-id "$BATCH_ID"
STAGE_EXIT=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] raw_to_jpg exited $STAGE_EXIT"

# ── 3. Locate run directory ───────────────────────────────────────────────────
RUN_REPORT=$(find "$TMPDIR/output/raw_to_jpg" -name "run_report.json" 2>/dev/null | head -1)
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

# ── 4. Promote outputs (only when stage exited 0) ────────────────────────────
PROMOTE_EXIT=1
if [ "$STAGE_EXIT" -eq 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Promoting to $FINAL_DEST"
  python "$AGIR_DIR/stages/raw_to_jpg/promote.py" \\
    --run-dir "$RUN_DIR" \\
    --dest "$FINAL_DEST"
  PROMOTE_EXIT=$?
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] promote exited $PROMOTE_EXIT"
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Skipping promotion (stage_exit=$STAGE_EXIT)"
fi

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

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE stage_exit=$STAGE_EXIT promote_exit=$PROMOTE_EXIT"
"""