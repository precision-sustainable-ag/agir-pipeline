#!/usr/bin/env python3
"""
scripts/job/ingest_and_release.py
==============================

Called at the tail of every Slurm job after artifacts have been copied
back to 90daydata.

Responsibilities:
  1. Ingest run_report.json into the SQLite stage_runs table (idempotent).
  2. Release the stage lease from stage_leases.

Exit codes:
  0  — ingest (if requested) succeeded AND lease was released.
  1  — ingest failed (run_report missing or malformed). Lease is still released.

Usage
-----
# Normal completion (run_report available):
python scripts/job/ingest_and_release.py \\
    --db /path/to/globus_file_index.sqlite3 \\
    --run-report /90daydata/.../stage_runs/<run_id>/run_report.json \\
    --lease-id <uuid> \\
    --orchestrator-id orchestrator.<pid> \\
    --release-reason "stage_exit_0_promote_exit_0"

# Error path (no run_report produced):
python scripts/job/ingest_and_release.py \\
    --db /path/to/globus_file_index.sqlite3 \\
    --lease-id <uuid> \\
    --orchestrator-id orchestrator.<pid> \\
    --release-reason "no_run_report"
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from orchestrator.sqlite_db import open_db, ingest_run_report, release_stage_lease

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest run_report into SQLite stage_runs and release the stage lease."
    )
    parser.add_argument(
        "--db", required=True, type=Path,
        help="Path to the SQLite pipeline database.",
    )
    parser.add_argument(
        "--run-report", type=Path, default=None,
        help="Path to run_report.json. Omit if the stage produced no report.",
    )
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--orchestrator-id", required=True)
    parser.add_argument("--release-reason", required=True)
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

    logger.info(
        "ingest_and_release: db=%s lease_id=%s reason=%s",
        args.db, args.lease_id, args.release_reason,
    )

    exit_code = 0
    conn = open_db(args.db)
    try:
        # ── 1. Ingest run_report ──────────────────────────────────────────────
        if args.run_report:
            if not args.run_report.exists():
                logger.error("run_report not found: %s", args.run_report)
                exit_code = 1
            else:
                try:
                    result = ingest_run_report(conn, args.run_report)
                    conn.commit()
                    logger.info(
                        "Ingested: run_id=%s status=%s batch_id=%s",
                        result["run_id"], result["status"], result["batch_id"],
                    )
                except Exception as exc:
                    logger.error("Failed to ingest run_report: %s", exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    exit_code = 1
        else:
            logger.info("No --run-report provided; skipping ingest.")

        # ── 2. Release lease (always attempt, even if ingest failed) ──────────
        released = release_stage_lease(conn, args.lease_id, args.orchestrator_id)
        if released:
            logger.info("Lease %s released (%s)", args.lease_id, args.release_reason)
        else:
            logger.warning(
                "Lease %s not released — not found or wrong orchestrator-id", args.lease_id
            )
    finally:
        conn.close()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())