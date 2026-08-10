#!/usr/bin/env python3
"""Publish a reconciled Ceres SQLite snapshot and optionally transfer it."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.logging_utils import command_logging
from orchestrator.result_sync import ResultSyncConfigError
from orchestrator.snapshot_publication import (
    SnapshotPublicationError,
    load_snapshot_publication_config,
    publish_reconciled_snapshot,
)

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a consistent Ceres database snapshot only when Atlas "
            "result synchronization is fully reconciled."
        )
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--transfer",
        action="store_true",
        help="Transfer the published snapshot from Ceres to Atlas with Globus.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reconcile an unpublished candidate snapshot without replacing or transferring it.",
    )
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def _print_reconciliation(report) -> None:
    state = "READY" if report.ready else "BLOCKED"
    print(f"\nSnapshot reconciliation: {state}")
    print(f"  result syncs checked: {report.checked_syncs}")
    print(f"  result syncs ingested: {report.ingested_syncs}")
    print(f"  latest inventory run: {report.latest_inventory_run_id or 'none'}")
    print(f"  expected promoted files: {report.expected_promoted_files}")
    for issue in report.issues:
        run = f" run_id={issue.run_id}" if issue.run_id else ""
        print(f"  ! {issue.code}{run}: {issue.message}")
    print()


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    try:
        config = load_snapshot_publication_config(args.config)
    except ResultSyncConfigError as exc:
        logger.error("%s", exc)
        return 2

    with command_logging(
        base_log_dir=args.log_dir or config.result_sync.log_dir,
        command_name="publish_ceres_db_snapshot",
        category="result_sync",
        log_level=args.log_level,
    ):
        try:
            result = publish_reconciled_snapshot(
                config,
                transfer=args.transfer,
                dry_run=args.dry_run,
            )
        except (OSError, sqlite3.Error, ValueError, SnapshotPublicationError) as exc:
            logger.error("Unable to publish Ceres database snapshot: %s", exc)
            return 1

        _print_reconciliation(result.reconciliation)
        if not result.reconciliation.ready:
            logger.error("Snapshot publication blocked by reconciliation failures")
            return 1
        if args.dry_run:
            print("Dry run passed; no snapshot was published or transferred.")
            return 0

        print(f"Published Ceres snapshot: {result.snapshot_path}")
        if result.globus_task_id:
            print(f"Transferred snapshot to Atlas: task={result.globus_task_id}")
        else:
            print("Atlas transfer not requested; run again with --transfer when ready.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
