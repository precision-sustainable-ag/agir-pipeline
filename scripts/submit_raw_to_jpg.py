#!/usr/bin/env python3
"""
Submit raw_to_jpg SLURM jobs for a list of batches.

Reads the same batch list file used by stage_raw_inputs.py, verifies that each
batch's input-stage transfer is confirmed complete in the DB, claims an exclusive
lease, generates a per-batch SLURM job script, and submits it via sbatch.

Run AFTER stage_raw_inputs.py has confirmed all transfers.

Usage
-----
python scripts/submit_raw_to_jpg.py \\
    --batches path/to/batch_list.txt \\
    --config  configs/scinet_raw_to_jpg.yaml \\
    [--skip-transfer-check]
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.batch_list import parse_batch_list
from orchestrator.submit_jobs import submit_raw_to_jpg_jobs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Claim leases and submit raw_to_jpg SLURM jobs for each batch."
    )
    parser.add_argument(
        "--batches", required=True, type=Path,
        help="Batch list file (same format as stage_raw_inputs.py).",
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="SciNet job config YAML (e.g. configs/scinet_raw_to_jpg.yaml).",
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

    results = submit_raw_to_jpg_jobs(
        batch_entries=entries,
        config_path=str(args.config),
        require_transfer_complete=not args.skip_transfer_check,
    )

    any_failed = False
    print("\n── Job submission results ────────────────────────────────")
    for r in results:
        if r.status == "submitted":
            print(f"  ✓  {r.batch_id:<25}  job={r.slurm_job_id}  lease={r.lease_id}")
        else:
            print(f"  ✗  {r.batch_id:<25}  {r.status}", end="")
            if r.error:
                print(f"  ({r.error[:80]})", end="")
            print()
            any_failed = True
    print()

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())