#!/usr/bin/env python3
"""
Refresh SQLite input-staging statuses from existing Globus tasks.

This command does not plan new staging requests and does not submit transfers.
It only polls staged_inputs rows that already have Globus task ids.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import load_stage_config
from orchestrator.input_staging_planner import STAGE_INPUT_SPECS
from orchestrator.polling import (
    StagedInputPollResult,
    has_pollable_staged_inputs,
    poll_staged_inputs,
)
from orchestrator.sqlite_db import open_db

logger = logging.getLogger(__name__)


def print_results(results: Sequence[StagedInputPollResult]) -> bool:
    print("\n-- input staging poll results -----------------------------------")
    any_error = False
    for result in results:
        if result.status in {"submitted", "active", "completed"}:
            marker = "+"
        else:
            marker = "!"
            any_error = True

        globus = f" globus={result.globus_status}" if result.globus_status else ""
        task = f" task={result.globus_task_id}" if result.globus_task_id else ""
        print(
            f"  {marker} {result.batch_id:<24} "
            f"{result.previous_status}->{result.status}{globus}{task}"
        )
    print()
    return any_error


def poll_once(conn, *, stage: str, limit: int) -> bool:
    """Poll one batch of staged-input rows. Returns True if any error status was seen."""
    results = poll_staged_inputs(
        conn,
        stage=stage,
        limit=limit,
    )
    if not results:
        logger.info(
            "No submitted/active input-staging rows with Globus task ids found for stage=%s",
            stage,
        )
        return False
    return print_results(results)


def run_poll_loop(
    conn,
    *,
    stage: str,
    limit: int,
    interval_seconds: float,
    forever: bool,
    timeout_seconds: float | None,
) -> int:
    """Run repeated polling for --wait or --forever."""
    started_at = time.monotonic()
    any_error = False

    while True:
        any_error = poll_once(conn, stage=stage, limit=limit) or any_error

        if not forever and not has_pollable_staged_inputs(conn, stage=stage):
            logger.info("No pollable input-staging rows remain for stage=%s", stage)
            return 1 if any_error else 0

        sleep_seconds = interval_seconds
        if timeout_seconds is not None:
            elapsed_seconds = time.monotonic() - started_at
            remaining_seconds = timeout_seconds - elapsed_seconds
            if remaining_seconds <= 0:
                logger.error(
                    "Timed out after %.0f second(s) waiting for input staging for stage=%s",
                    timeout_seconds,
                    stage,
                )
                return 124
            sleep_seconds = min(interval_seconds, remaining_seconds)

        time.sleep(sleep_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll existing input-staging Globus tasks and update SQLite."
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=sorted(STAGE_INPUT_SPECS),
        help="Pipeline stage whose input-staging tasks should be polled.",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Stage config YAML.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum staged_inputs rows to poll (default: 50).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--wait",
        action="store_true",
        help="Poll repeatedly until no submitted/active task-backed rows remain.",
    )
    mode.add_argument(
        "--forever",
        action="store_true",
        help="Poll repeatedly until interrupted.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between polling passes for --wait/--forever (default: 60).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Maximum seconds to wait when using --wait.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be greater than 0")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.timeout is not None and not args.wait:
        parser.error("--timeout can only be used with --wait")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    cfg = load_stage_config(args.config)
    conn = open_db(cfg["paths"]["db"])
    try:
        if args.wait or args.forever:
            return run_poll_loop(
                conn,
                stage=args.stage,
                limit=args.limit,
                interval_seconds=args.interval,
                forever=args.forever,
                timeout_seconds=args.timeout,
            )

        any_error = poll_once(conn, stage=args.stage, limit=args.limit)
    finally:
        conn.close()
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
