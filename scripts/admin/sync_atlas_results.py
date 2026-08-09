#!/usr/bin/env python3
"""Receive, transfer, and verify Atlas run bundles on Ceres."""

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
from orchestrator.result_sync import (
    CeresResultSyncConfig,
    RequestTransferError,
    RequestTransferTimeout,
    ResultSyncConfigError,
    SyncOutcome,
    load_ceres_result_sync_config,
    poll_transferring_bundle_transfers,
    process_inbox_requests,
    receive_request_outbox,
    require_result_sync_schema,
    submit_requested_bundle_transfers,
    synchronize_run_bundles,
    verify_transferred_run_bundles,
)
from orchestrator.sqlite_db import open_db

logger = logging.getLogger(__name__)


def print_outcomes(
    title: str,
    outcomes: list[SyncOutcome],
    *,
    empty_message: str,
) -> bool:
    """Print one workflow phase and return whether it contains an error."""
    print(f"\n-- {title} " + "-" * max(1, 60 - len(title)))
    for outcome in outcomes:
        marker = "!" if outcome.is_error else "+"
        run_id = outcome.run_id or "unknown"
        transition = outcome.status
        if outcome.previous_status:
            transition = f"{outcome.previous_status}->{outcome.status}"
        task = (
            f" task={outcome.globus_task_id}"
            if outcome.globus_task_id
            else ""
        )
        print(f"  {marker} {run_id:<36} {transition}{task}: {outcome.message}")
    if not outcomes:
        print(f"  {empty_message}")
    print()
    return any(outcome.is_error for outcome in outcomes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receive Atlas requests and transfer their run bundles to Ceres."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not contact Globus or write SQLite; validate inbox requests and "
            "plan requested transfers and validate transferred bundles without "
            "changing their states."
        ),
    )
    parser.add_argument(
        "--bundle-limit",
        type=int,
        default=50,
        help="Maximum requested run-bundle transfers to submit (default: 50).",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Override paths.log_dir from the configuration.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def _print_summary(
    requests: list[SyncOutcome],
    transfers: list[SyncOutcome],
    verifications: list[SyncOutcome],
) -> bool:
    any_error = print_outcomes(
        "Atlas result-sync requests",
        requests,
        empty_message="No result-sync request files found.",
    )
    any_error = print_outcomes(
        "Atlas run-bundle transfers",
        transfers,
        empty_message="No requested or transferring run bundles found.",
    ) or any_error
    return print_outcomes(
        "Ceres run-bundle verification",
        verifications,
        empty_message="No transferred run bundles found for verification.",
    ) or any_error


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.bundle_limit <= 0:
        parser.error("--bundle-limit must be greater than zero")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    try:
        config = load_ceres_result_sync_config(args.config)
    except ResultSyncConfigError as exc:
        logger.error("%s", exc)
        return 2

    with command_logging(
        base_log_dir=args.log_dir or config.log_dir,
        command_name="sync_atlas_results",
        category="result_sync",
        log_level=args.log_level,
    ) as log_paths:
        logger.info("Writing command logs under %s", log_paths.directory)
        logger.info(
            "Starting Atlas result synchronization config=%s db=%s dry_run=%s",
            args.config,
            config.db_path,
            args.dry_run,
        )
        try:
            if not args.dry_run:
                config.ceres_inbox.mkdir(parents=True, exist_ok=True)
            task_id = receive_request_outbox(config, dry_run=args.dry_run)
            logger.info("Atlas request transfer completed task_id=%s", task_id)
        except RequestTransferTimeout as exc:
            logger.error("%s", exc)
            return 124
        except RequestTransferError as exc:
            logger.error("%s", exc)
            return 1

        conn = None
        requests: list[SyncOutcome] = []
        transfers: list[SyncOutcome] = []
        verifications: list[SyncOutcome] = []
        timed_out = False
        try:
            conn = open_db(config.db_path, readonly=args.dry_run)
            require_result_sync_schema(conn)
            requests = process_inbox_requests(conn, config, dry_run=args.dry_run)
            transfers, timed_out = synchronize_run_bundles(
                conn,
                config,
                limit=args.bundle_limit,
                dry_run=args.dry_run,
            )
            verifications = verify_transferred_run_bundles(
                conn,
                config,
                dry_run=args.dry_run,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.error("Unable to process Ceres result synchronization: %s", exc)
            return 1
        finally:
            if conn is not None:
                conn.close()

        any_error = _print_summary(requests, transfers, verifications)
        if timed_out:
            logger.error(
                "Timed out with run-bundle transfers still active; rerun the command "
                "to resume polling their recorded Globus task ids"
            )
            exit_code = 124
        else:
            exit_code = 1 if any_error else 0
        logger.info(
            "Finished Atlas result synchronization requests=%d bundles=%d "
            "verifications=%d exit_code=%d",
            len(requests),
            len(transfers),
            len(verifications),
            exit_code,
        )
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
