#!/usr/bin/env python3
"""
Run both pipeline phases for a stage in sequence:
  1. stage_inputs  — transfer time-windowed files from LTS to 90daydata
  2. submit_jobs   — claim leases and submit SLURM jobs

This is the standard operator entrypoint. Use the individual scripts
(stage_inputs.py, submit_jobs.py) only when you need to run a single phase.

Usage
-----
python scripts/run_stage.py \\
    --batches path/to/batch_list.txt \\
    --config  configs/scinet_raw_to_jpg.yaml

# Run only phase 1 (transfer):
python scripts/run_stage.py --batches ... --config ... --stage-only

# Run only phase 2 (submit), assuming staging is already done:
python scripts/run_stage.py --batches ... --config ... --submit-only
"""

import argparse
import logging
from pathlib import Path

from orchestrator.batch_list import parse_batch_list
from orchestrator.config import load_stage_config
from orchestrator.manual.stage_inputs import stage_inputs_for_batches
from orchestrator.manual.submit_jobs import submit_stage_jobs


def _read_stage_name(config_path: Path) -> str:
    cfg = load_stage_config(config_path)
    stage_name = cfg.get("stage", {}).get("name")
    if not stage_name:
        raise ValueError(
            f"Config '{config_path}' is missing required 'stage.name' field."
        )
    return stage_name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage inputs then submit SLURM jobs for a pipeline stage."
    )
    parser.add_argument(
        "--batches", required=True, type=Path,
        help="Batch list file (batch_id | start_time end_time, times in GMT).",
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="Stage config YAML (e.g. configs/scinet_raw_to_jpg.yaml).",
    )
    parser.add_argument(
        "--stage-only", action="store_true",
        help="Run phase 1 (input staging) only — do not submit jobs.",
    )
    parser.add_argument(
        "--submit-only", action="store_true",
        help="Run phase 2 (job submission) only — assumes staging is already done.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="Dry-run mode for Globus transfers (phase 1 only).",
    )
    parser.add_argument(
        "--skip-transfer-check", action="store_true",
        help="Submit jobs without verifying transfer completion (phase 2 only).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    if args.stage_only and args.submit_only:
        print("Error: --stage-only and --submit-only are mutually exclusive.")
        return 1

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    try:
        stage_name = _read_stage_name(args.config)
    except (ValueError, OSError) as exc:
        logging.error("Failed to read stage name from config: %s", exc)
        return 1

    try:
        entries = parse_batch_list(args.batches)
    except ValueError as exc:
        logging.error("Failed to parse batch list: %s", exc)
        return 1

    if not entries:
        logging.warning("Batch list is empty — nothing to do.")
        return 0

    # ── Phase 1: stage inputs ─────────────────────────────────────────────────
    if not args.submit_only:
        logging.info("[phase 1] Staging '%s' inputs for %d batch(es)", stage_name, len(entries))

        dry_run = True if args.dry_run else None
        stage_results = stage_inputs_for_batches(
            batch_entries=entries,
            config_path=str(args.config),
            stage=stage_name,
            requested_by="scripts.run_stage",
            dry_run=dry_run,
        )

        any_staging_failed = False
        print(f"\n── [phase 1] Transfer results [{stage_name}] ──────────────────")
        for r in stage_results:
            icon = "✓" if r.status in {"completed", "already_completed"} else "✗"
            print(f"  {icon}  {r.window_key:<40}  {r.status}")
            if r.status not in {"completed", "already_completed", "already_active"}:
                any_staging_failed = True
        print()

        if any_staging_failed:
            logging.error(
                "[phase 1] One or more transfers failed — skipping job submission. "
                "Fix the transfers above and rerun, or use --submit-only to skip phase 1."
            )
            return 1

        if args.stage_only:
            logging.info("[phase 1] --stage-only set — skipping job submission.")
            return 0

    # ── Phase 2: submit jobs ──────────────────────────────────────────────────
    logging.info("[phase 2] Submitting jobs for %d batch(es)", len(entries))

    try:
        job_results = submit_stage_jobs(
            batch_entries=entries,
            config_path=str(args.config),
            require_transfer_complete=not args.skip_transfer_check,
        )
    except ValueError as exc:
        logging.error("Config error: %s", exc)
        return 1

    any_submission_failed = False
    logging.info("[phase 2] Job submission results")
    for r in job_results:
        if r.status == "submitted":
            logging.info("  ✓  %-25s  job=%s  lease=%s", r.batch_id, r.slurm_job_id, r.lease_id)
        else:
            logging.warning("  ✗  %-25s  %s  (%s)", r.batch_id, r.status, (r.error or "")[:80])
            any_submission_failed = True

    return 1 if any_submission_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())