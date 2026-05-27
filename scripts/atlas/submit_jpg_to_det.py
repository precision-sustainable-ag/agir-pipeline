#!/usr/bin/env python3
"""
scripts/atlas/submit_jpg_to_det.py
=====================================

Read a batch list and submit one SLURM job per batch.

Each generated job handles its own:
  1. Globus transfer  — pull JPGs from JUNO LTS to Atlas /90daydata
  2. Detection        — python -m stages.jpg_to_det.cli
  3. Promotion        — stages/jpg_to_det/promote.py

The login node just submits and exits. SLURM manages the queue.
Jobs run in parallel up to however many GPUs are available.

Lockfiles at <locks_root>/jpg_to_det/<batch_id>.lock prevent
duplicate submissions if this script is run more than once.

Usage
-----
# Submit all batches from find script output:
python scripts/atlas/submit_jpg_to_det.py \
    --batches batches_needing_det.txt \
    --config  configs/atlas_jpg_to_det.yaml

# Limit to first N:
python scripts/atlas/submit_jpg_to_det.py \
    --batches batches_needing_det.txt \
    --config  configs/atlas_jpg_to_det.yaml \
    --limit 5

# Dry-run (generate scripts, no sbatch):
python scripts/atlas/submit_jpg_to_det.py \
    --batches batches_needing_det.txt \
    --config  configs/atlas_jpg_to_det.yaml \
    --dry-run

# Single batch:
python scripts/atlas/submit_jpg_to_det.py \
    --batch-id MD_2025-04-25 \
    --config   configs/atlas_jpg_to_det.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


# ── Config validation ─────────────────────────────────────────────────────────

_REQUIRED_CONFIG_KEYS = [
    "slurm.account",
    "slurm.partition",
    "slurm.gres",
    "slurm.cpus_per_task",
    "slurm.det_workers",
    "slurm.mem",
    "slurm.time",
    "transfer.juno_endpoint",
    "transfer.atlas_endpoint",
    "transfer.routes.jpg_to_det.source_root_juno",
    "transfer.routes.jpg_to_det.destination_root",
    "paths.agir_pipeline_dir",
    "paths.stage_config",
    "paths.det_model_path",
    "paths.uv_env",
    "paths.output_stage_runs",
    "paths.log_dir",
    "paths.db",
    "paths.locks_root",
]


def _get_nested(cfg: dict, dotted_key: str):
    keys = dotted_key.split(".")
    node = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def validate_config(cfg: dict, config_path: Path) -> None:
    missing = [k for k in _REQUIRED_CONFIG_KEYS if _get_nested(cfg, k) is None]
    if missing:
        lines = "\n  ".join(missing)
        raise SystemExit(
            f"Config file is missing required keys ({config_path}):\n  {lines}\n"
            f"Check configs/atlas_jpg_to_det.yaml for the expected structure."
        )


# ── Lockfile helpers ──────────────────────────────────────────────────────────

def _lock_path(locks_root: Path, batch_id: str) -> Path:
    return locks_root / "jpg_to_det" / f"{batch_id}.lock"

def _is_locked(locks_root: Path, batch_id: str) -> bool:
    return _lock_path(locks_root, batch_id).exists()

def _create_lock(locks_root: Path, batch_id: str) -> None:
    p = _lock_path(locks_root, batch_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "batch_id":     batch_id,
        "slurm_job_id": None,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node":         os.uname().nodename,
    }, indent=2))

def _update_lock(locks_root: Path, batch_id: str, slurm_job_id: str) -> None:
    p = _lock_path(locks_root, batch_id)
    if not p.exists():
        return
    data = json.loads(p.read_text())
    data["slurm_job_id"] = slurm_job_id
    p.write_text(json.dumps(data, indent=2))

def _remove_lock(locks_root: Path, batch_id: str) -> None:
    _lock_path(locks_root, batch_id).unlink(missing_ok=True)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class JobResult:
    batch_id: str
    status: str        # submitted | locked | sbatch_error | dry_run
    slurm_job_id: Optional[str] = None
    script_path: Optional[Path] = None
    stdout_log: Optional[str] = None
    stderr_log: Optional[str] = None
    job_log: Optional[str] = None
    error: Optional[str] = None


# ── SLURM script ──────────────────────────────────────────────────────────────

_SLURM_TEMPLATE = """\
#!/usr/bin/env bash
#SBATCH --job-name=jpg_to_det_{batch_id}
#SBATCH --account={account}
#SBATCH --partition={partition}
#SBATCH --gres={gres}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem}
#SBATCH --time={time}
#SBATCH -o {log_dir}/{batch_id}-%j.out
#SBATCH -e {log_dir}/{batch_id}-%j.err

# ── Job log (timestamped, separate from SLURM stdout/stderr) ─────────────────
# Written to a dedicated file so you can tail it while the job runs:
#   tail -f {log_dir}/{batch_id}-$SLURM_JOB_ID.log
JOB_LOG="{log_dir}/{batch_id}-${{SLURM_JOB_ID}}.log"

log() {{
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$JOB_LOG"
}}

log_section() {{
    log "════════════════════════════════════════"
    log "$*"
    log "════════════════════════════════════════"
}}

# Trap any unexpected exit and log it
on_exit() {{
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        log "[ERROR] Job exited unexpectedly with code $exit_code"
        log "[ERROR] Last command: $BASH_COMMAND"
    fi
    log "Job finished at $(date -u +%Y-%m-%dT%H:%M:%SZ) — exit=$exit_code"
}}
trap on_exit EXIT

set -euo pipefail
umask 002

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

BATCH_ID="{batch_id}"
AGIR_DIR="{agir_dir}"
CONFIG="{stage_config}"
MODEL="{model_path}"
CPUS="{cpus}"
DET_WORKERS="{det_workers}"
DST_IMAGES="{dst_images}"
FINAL_DET_DIR="{final_det_dir}"
STAGE_RUNS_DIR="{stage_runs_dir}"

log_section "jpg_to_det starting"
log "Batch:        $BATCH_ID"
log "Node:         $(hostname -f)"
log "Job ID:       $SLURM_JOB_ID"
log "Job log:      $JOB_LOG"
log "SLURM stdout: {log_dir}/{batch_id}-${{SLURM_JOB_ID}}.out"
log "SLURM stderr: {log_dir}/{batch_id}-${{SLURM_JOB_ID}}.err"
log "Agir dir:     $AGIR_DIR"
log "Config:       $CONFIG"
log "Model:        $MODEL"
log "Dst images:   $DST_IMAGES"

# ── Activate environment ──────────────────────────────────────────────────────
log "Activating Python environment: {uv_env}"
source "{uv_env}"
cd "$AGIR_DIR"
log "Python: $(which python) $(python --version 2>&1)"

# ── Step 1: Stage inputs via Globus ──────────────────────────────────────────
log_section "[1/3] Staging from JUNO"
log "Source: {juno_endpoint}:{juno_src}/{batch_id}/images"
log "Dest:   {atlas_endpoint}:$DST_IMAGES"

# Check if JPGs are already on disk — skip transfer entirely if so
set +e
EXISTING_JPGS=$(find "$DST_IMAGES" -maxdepth 1 \\( -iname '*.jpg' -o -iname '*.jpeg' \\) 2>/dev/null | wc -l)
set -e
if [[ "$EXISTING_JPGS" -gt 0 ]]; then
    log "Already staged — $EXISTING_JPGS JPGs found at $DST_IMAGES — skipping Globus transfer"
else
    log "No JPGs found locally — initiating Globus transfer ..."

    TRANSFER_OUT=$(mktemp)
set +e
globus transfer \\
    --recursive \\
    --sync-level mtime \\
    --label "jpg_to_det staging -- $BATCH_ID" \\
    --format json \\
    "{juno_endpoint}:{juno_src}/{batch_id}/images" \\
    "{atlas_endpoint}:$DST_IMAGES" \\
    > "$TRANSFER_OUT" 2>&1
TRANSFER_RC=$?
set -e

log "globus transfer output:"
cat "$TRANSFER_OUT" | tee -a "$JOB_LOG"

if [[ $TRANSFER_RC -ne 0 ]]; then
    log "[ERROR] globus transfer failed (exit=$TRANSFER_RC)"
    log "[ERROR] If you see PermissionDenied/LOGIN_DENIED, re-authenticate on the login node:"
    log "[ERROR]   globus session update"
    log "[ERROR]   (authenticate with your @scinet.usda.gov account)"
    rm -f "$TRANSFER_OUT"
    exit 1
fi

TASK_ID=$(python3 -c "import json; print(json.load(open('$TRANSFER_OUT'))['task_id'])")
rm -f "$TRANSFER_OUT"
log "Transfer submitted: task_id=$TASK_ID"
log "Monitor at: https://app.globus.org/activity/$TASK_ID"

set +e
globus task wait --timeout 7200 --polling-interval 30 "$TASK_ID"
WAIT_RC=$?
set -e
if [[ $WAIT_RC -ne 0 ]]; then
    log "[ERROR] globus task wait failed (exit=$WAIT_RC)"
    log "[ERROR] Check status: globus task show $TASK_ID"
    exit 1
fi
log "Transfer complete"
fi  # end: if not already staged

JPG_COUNT=$(find "$DST_IMAGES" -maxdepth 1 \\( -iname '*.jpg' -o -iname '*.jpeg' \\) | wc -l)
log "JPGs available: $JPG_COUNT"

if [[ "$JPG_COUNT" -eq 0 ]]; then
    log "[ERROR] No JPGs found at $DST_IMAGES"
    exit 1
fi

# ── Step 2: Run detection ─────────────────────────────────────────────────────
log_section "[2/3] Running detection"

TMPINPUT="$TMPDIR/input"
TMPOUTPUT="$TMPDIR/output"
mkdir -p "$TMPINPUT" "$TMPOUTPUT"
log "TMPDIR: $TMPDIR"

log "Copying JPGs to TMPDIR ..."
cp -r "$DST_IMAGES"/. "$TMPINPUT"/
log "Copy complete"

log "Running stages.jpg_to_det.cli ..."
set +e
python -m stages.jpg_to_det.cli \\
    --c "$CONFIG" \\
    --m "$MODEL" \\
    --i "$TMPINPUT" \\
    --o "$TMPOUTPUT" \\
    --t "$DET_WORKERS" \\
    --batch-id "$BATCH_ID" \\
    --device cuda
STAGE_EXIT=$?
set -e
log "Stage exit code: $STAGE_EXIT"

# ── Step 3: Promote outputs ───────────────────────────────────────────────────
log_section "[3/3] Promoting outputs"

RUN_REPORT=$(find "$TMPOUTPUT/jpg_to_det" -name "run_report.json" | head -1)
if [[ -z "$RUN_REPORT" ]]; then
    log "[ERROR] run_report.json not found under $TMPOUTPUT/jpg_to_det"
    log "Contents of TMPOUTPUT:"
    find "$TMPOUTPUT" -maxdepth 3 | tee -a "$JOB_LOG"
    exit 1
fi
log "run_report: $RUN_REPORT"

RUN_ID=$(python3 -c "import json; print(json.load(open('$RUN_REPORT'))['run_id'])")
RUN_DEST="$STAGE_RUNS_DIR/$RUN_ID"
mkdir -p "$RUN_DEST"
cp -r "$(dirname $RUN_REPORT)"/. "$RUN_DEST"/
log "Run artifacts: $RUN_DEST"

if [[ "$STAGE_EXIT" -le 1 ]]; then
    mkdir -p "$FINAL_DET_DIR"

    # Resolve artifacts dir (written by cli.py under TMPOUTPUT/jpg_to_det/<run_id>/artifacts)
    ARTIFACTS_DIR=$(ls -td "$TMPOUTPUT/jpg_to_det"/*/artifacts 2>/dev/null | head -1)
    if [[ -z "$ARTIFACTS_DIR" || ! -d "$ARTIFACTS_DIR" ]]; then
        log "[ERROR] Could not resolve artifacts dir under $TMPOUTPUT/jpg_to_det"
        find "$TMPOUTPUT" -maxdepth 4 | tee -a "$JOB_LOG"
        exit 1
    fi
    log "Artifacts dir: $ARTIFACTS_DIR"

    # Generate visualization sample (non-fatal if it fails)
    VIZ_DIR="$RUN_DEST/visualizations"
    mkdir -p "$VIZ_DIR"
    log "Rendering visualization sample to $VIZ_DIR ..."
    set +e
    python stages/jpg_to_det/visualize_detections.py \\
        --images      "$TMPINPUT" \\
        --detections  "$ARTIFACTS_DIR" \\
        --output      "$VIZ_DIR" \\
        --sample-size 24 \\
        --max-width   1800
    VIZ_EXIT=$?
    set -e
    [[ $VIZ_EXIT -eq 0 ]] && log "Visualizations done" || log "WARN: visualization failed (exit=$VIZ_EXIT) — continuing"

    log "Promoting to $FINAL_DET_DIR ..."
    python stages/jpg_to_det/promote.py \\
        --run-dir "$RUN_DEST" \\
        --dest    "$FINAL_DET_DIR" \\
        --viz-dir "$VIZ_DIR"
    log "Promotion done"
else
    log "Skipping promotion and visualization (stage_exit=$STAGE_EXIT)"
fi

exit $STAGE_EXIT
"""


def _generate_script(batch_id: str, cfg: dict) -> str:
    slurm    = cfg["slurm"]
    paths    = cfg["paths"]
    transfer = cfg["transfer"]
    route    = transfer["routes"]["jpg_to_det"]

    juno_src      = route["source_root_juno"]
    dst_root      = route["destination_root"]
    dst_images    = f"{dst_root}/{batch_id}/images"
    final_det_dir = f"{dst_root}/{batch_id}/detections"

    return _SLURM_TEMPLATE.format(
        batch_id      = batch_id,
        account       = slurm["account"],
        partition     = slurm["partition"],
        gres          = slurm["gres"],
        cpus          = slurm["cpus_per_task"],
        det_workers   = slurm["det_workers"],
        mem           = slurm["mem"],
        time          = slurm["time"],
        log_dir       = paths["log_dir"],
        agir_dir      = paths["agir_pipeline_dir"],
        stage_config  = paths["stage_config"],
        model_path    = paths["det_model_path"],
        uv_env        = paths["uv_env"],
        stage_runs_dir= paths["output_stage_runs"],
        dst_images    = dst_images,
        final_det_dir = final_det_dir,
        juno_endpoint = transfer["juno_endpoint"],
        atlas_endpoint= transfer["atlas_endpoint"],
        juno_src      = juno_src,
    )


# ── Core ──────────────────────────────────────────────────────────────────────

def check_globus_auth(juno_endpoint: str, atlas_endpoint: str) -> None:
    """
    Verify Globus session is valid and both endpoints are reachable.
    Raises SystemExit with clear instructions if auth has expired.

    Run this on the login node before submitting jobs — compute nodes
    can't do interactive browser-based re-authentication.
    """
    # Check we're logged in at all
    result = subprocess.run(
        ["globus", "whoami", "--format", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "Not logged in to Globus.\n"
            "Run on the login node:\n"
            "  globus login\n"
            "Then resubmit."
        )

    # Check each endpoint with a minimal ls call
    for name, endpoint_id in [("JUNO", juno_endpoint), ("Atlas", atlas_endpoint)]:
        result = subprocess.run(
            ["globus", "ls", "--format", "json", f"{endpoint_id}:/"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Parse the error to give a useful message
            error_msg = result.stderr.strip() or result.stdout.strip()
            if "session_required_single_domain" in error_msg or "PermissionDenied" in error_msg:
                raise SystemExit(
                    f"Globus session needs reauthentication for {name} endpoint "
                    f"({endpoint_id}).\n"
                    f"Run on the login node:\n"
                    f"  globus session update\n"
                    f"Make sure to authenticate with your @scinet.usda.gov account.\n"
                    f"Then resubmit.\n\n"
                    f"Raw error: {error_msg[:300]}"
                )
            raise SystemExit(
                f"Cannot reach {name} endpoint ({endpoint_id}).\n"
                f"Error: {error_msg[:300]}"
            )
        logger.info("Globus endpoint OK: %s (%s)", name, endpoint_id)


def submit_jobs(
    batch_ids: list[str],
    config_path: Path,
    locks_root: Path,
    dry_run: bool = False,
) -> list[JobResult]:
    cfg = yaml.safe_load(config_path.read_text())
    validate_config(cfg, config_path)

    transfer = cfg["transfer"]
    paths    = cfg["paths"]

    # Verify Globus auth before generating any scripts or lockfiles
    if not dry_run:
        logger.info("Checking Globus authentication ...")
        check_globus_auth(
            juno_endpoint  = transfer["juno_endpoint"],
            atlas_endpoint = transfer["atlas_endpoint"],
        )

    script_dir = Path(paths["output_stage_runs"]) / "job_scripts"
    log_dir    = Path(paths["log_dir"])
    script_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    results: list[JobResult] = []

    for batch_id in batch_ids:
        # Already submitted?
        if _is_locked(locks_root, batch_id):
            lock_data = json.loads(_lock_path(locks_root, batch_id).read_text())
            logger.info(
                "Skipping %s — locked (job=%s submitted=%s)",
                batch_id,
                lock_data.get("slurm_job_id", "?"),
                lock_data.get("submitted_at", "?"),
            )
            results.append(JobResult(batch_id=batch_id, status="locked"))
            continue

        # Write SLURM script
        script = script_dir / f"jpg_to_det_{batch_id}.sh"
        script.write_text(_generate_script(batch_id, cfg))
        script.chmod(0o755)
        logger.info("Script written: %s", script)

        if dry_run:
            logger.info("[DRY-RUN] Would submit %s", script)
            results.append(JobResult(
                batch_id=batch_id, status="dry_run", script_path=script,
            ))
            continue

        # Lock before sbatch
        _create_lock(locks_root, batch_id)

        try:
            proc = subprocess.run(
                ["sbatch", str(script)],
                capture_output=True, text=True, check=True,
            )
            m = re.search(r"(\d+)", proc.stdout)
            job_id = m.group(1) if m else None
            _update_lock(locks_root, batch_id, job_id or "")

            stdout_log = f"{paths['log_dir']}/{batch_id}-{job_id}.out"
            stderr_log = f"{paths['log_dir']}/{batch_id}-{job_id}.err"
            job_log    = f"{paths['log_dir']}/{batch_id}-{job_id}.log"

            logger.info("Submitted %s → job %s", batch_id, job_id)
            logger.info("  Script:      %s", script)
            logger.info("  Job log:     %s", job_log)
            logger.info("  SLURM out:   %s", stdout_log)
            logger.info("  SLURM err:   %s", stderr_log)
            logger.info("  Monitor:     squeue --job %s", job_id)

            results.append(JobResult(
                batch_id=batch_id,
                status="submitted",
                slurm_job_id=job_id,
                script_path=script,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                job_log=job_log,
            ))
        except subprocess.CalledProcessError as exc:
            _remove_lock(locks_root, batch_id)
            logger.error("sbatch failed for %s: %s", batch_id, exc.stderr[:200])
            results.append(JobResult(
                batch_id=batch_id, status="sbatch_error", error=exc.stderr[:200],
            ))

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Submit one SLURM jpg_to_det job per batch (staging inside each job)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--batches", type=Path,
                       help="Batch list file (one batch_id per line).")
    group.add_argument("--batch-id",
                       help="Submit a single batch_id directly.")

    parser.add_argument("--config", required=True, type=Path,
                        help="Stage config YAML (configs/atlas_jpg_to_det.yaml).")
    parser.add_argument("--locks-root", type=Path, default=None,
                        help="Lockfile root (default: from config paths.locks_root).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max batches to submit (default: all).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate scripts but do not sbatch.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # Load config first so we can pull locks_root from it
    cfg = yaml.safe_load(args.config.read_text())
    validate_config(cfg, args.config)
    locks_root = args.locks_root or Path(cfg["paths"]["locks_root"])

    if args.batch_id:
        batch_ids = [args.batch_id]
    else:
        lines = args.batches.read_text().splitlines()
        batch_ids = [l.strip() for l in lines if l.strip() and not l.startswith("#")]

    if args.limit:
        batch_ids = batch_ids[:args.limit]

    if not batch_ids:
        logger.warning("No batches to submit.")
        return 0

    logger.info("Submitting %d batch(es)", len(batch_ids))

    results = submit_jobs(
        batch_ids=batch_ids,
        config_path=args.config,
        locks_root=locks_root,
        dry_run=args.dry_run,
    )

    print("\n── submission results ──────────────────────────────────────────")
    any_error = False
    for r in results:
        icon = "✓" if r.status in {"submitted", "dry_run"} else "~" if r.status == "locked" else "✗"
        job  = f"  job={r.slurm_job_id}" if r.slurm_job_id else ""
        err  = f"  ({r.error[:60]})" if r.error else ""
        print(f"  {icon}  {r.batch_id:<24}  {r.status}{job}{err}")
        if r.slurm_job_id:
            print(f"       tail -f {r.job_log}")
        if r.status == "sbatch_error":
            any_error = True
    print()
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())