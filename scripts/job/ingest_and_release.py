#!/usr/bin/env python3
"""
Ingest a stage run_report into the DB and release the stage lease.

This script runs at the end of each SLURM job (inside the compute node) after
artifacts have been copied back to 90daydata.  It is intentionally minimal:
just DB writes and a clean exit so the SLURM job's overall exit code is
unambiguous.

Usage
-----
# Normal completion path (run_report available):
python scripts/ingest_and_release.py \\
    --run-report /90daydata/.../stage_runs/<run_id>/run_report.json \\
    --lease-id   <uuid> \\
    --orchestrator-id orchestrator.manual \\
    --release-reason "stage_exit_0_promote_exit_0"

# Error path (no run_report was produced):
python scripts/ingest_and_release.py \\
    --lease-id   <uuid> \\
    --orchestrator-id orchestrator.manual \\
    --release-reason "no_run_report"
"""

import argparse
import logging
from pathlib import Path

from agir_db import AgirDB

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest run_report into DB and release the stage lease."
    )
    parser.add_argument(
        "--run-report", type=Path, default=None,
        help="Path to run_report.json on 90daydata.  Omit if no report was produced.",
    )
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--orchestrator-id", required=True)
    parser.add_argument("--release-reason", required=True)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    exit_code = 0
    with AgirDB() as db:
        window_key = db.orchestration.get_lease_window_key(args.lease_id)

        if args.run_report:
            if not args.run_report.exists():
                logger.error("run_report not found at %s", args.run_report)
                exit_code = 1
            else:
                try:
                    result = db.orchestration.ingest_run_report(
                        str(args.run_report), window_key=window_key
                    )
                    logger.info(
                        "Ingested run_report: run_id=%s status=%s window_key=%s",
                        result.get("run_id"), result.get("status"), result.get("window_key"),
                    )
                except Exception as exc:
                    logger.error("Failed to ingest run_report: %s", exc)
                    exit_code = 1

        release = db.orchestration.release_stage_lease(
            lease_id=args.lease_id,
            orchestrator_id=args.orchestrator_id,
            release_reason=args.release_reason,
        )
        if release.get("released"):
            logger.info("Lease %s released (%s)", args.lease_id, args.release_reason)
        else:
            logger.warning(
                "Lease %s was not released (already released or not found)", args.lease_id
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())