#!/usr/bin/env python3
"""
Submit SLURM jobs for a pipeline stage across a list of batches.

Reads the same batch list file used by stage_inputs.py, verifies that each
batch's input-stage transfer is confirmed complete in the DB, claims an
exclusive lease, generates a per-batch SLURM job script, and submits it via
sbatch.

The stage being submitted is determined by the ``stage.name`` field in the
config YAML, so this script works for any pipeline stage without modification.

Run AFTER stage_inputs.py has confirmed all transfers.

Usage
-----
python scripts/submit_jobs.py \\
    --batches path/to/batch_list.txt \\
    --config  configs/scinet_raw_to_jpg.yaml \\
    [--skip-transfer-check]
"""

import argparse
import logging
from pathlib import Path

from orchestrator.batch_list import parse_batch_list
from orchestrator.manual.submit_jobs import submit_stage_jobs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Claim leases and submit SLURM jobs for each batch."
    )
    parser.add_argument(
        "--batches", required=True, type=Path,
        help="Batch list file (same format as stage_inputs.py).",
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="Stage config YAML (e.g. configs/scinet_raw_to_jpg.yaml).",
    )
    parser.add_argument(
        "--skip-transfer-check", action="store_true",
        help="Submit jobs even if the input-stage transfer is not yet confirmed complete.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    try:
        entries = parse_batch_list(args.batches)
    except ValueError as exc:
        logging.error("Failed to parse batch list: %s", exc)
        return 1

    if not entries:
        logging.warning("Batch list is empty — nothing to do.")
        return 0

    logging.info("Submitting jobs for %d batch(es)", len(entries))

    try:
        results = submit_stage_jobs(
            batch_entries=entries,
            config_path=str(args.config),
            require_transfer_complete=not args.skip_transfer_check,
        )
    except ValueError as exc:
        logging.error("Config error: %s", exc)
        return 1

    any_failed = False
    no_manifest_count = 0
    logging.info("── Job submission results ────────────────────────────────")
    for r in results:
        if r.status == "submitted":
            logging.info("  ✓  %-25s  job=%s  lease=%s", r.batch_id, r.slurm_job_id, r.lease_id)
        else:
            logging.warning("  ✗  %-25s  %s  (%s)", r.batch_id, r.status, (r.error or "")[:80])
            any_failed = True
            if r.status == "no_input_manifest":
                no_manifest_count += 1

    if no_manifest_count == len(results):
        logging.error(
            "All batches are missing input manifests. "
            "Run stage_inputs.py first to transfer and stage inputs, then re-run this script. "
            "To skip the transfer check entirely, pass --skip-transfer-check."
        )
    elif no_manifest_count > 0:
        logging.warning(
            "%d batch(es) are missing input manifests. "
            "Run stage_inputs.py for those batches before resubmitting.",
            no_manifest_count,
        )

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
