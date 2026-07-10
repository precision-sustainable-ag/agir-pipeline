#!/usr/bin/env python3
"""
Plan and execute input staging for SQLite-backed orchestration.

This is the operator-facing wrapper around:
  * orchestrator.input_staging_planner  - find what should move
  * orchestrator.sqlite_db              - record staged_inputs state
  * orchestrator.globus_transfer        - submit Globus transfers
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import load_stage_config
from orchestrator.globus_transfer import (
    TransferSubmitResult,
    submit_transfer,
)
from orchestrator.input_staging_planner import (
    STAGE_INPUT_SPECS,
    StagingRequest,
    plan_input_staging,
    requests_as_dicts,
)
from orchestrator.logging_utils import command_logging
from orchestrator.sqlite_db import (
    mark_input_staging_status,
    open_db,
    request_input_staging,
)

logger = logging.getLogger(__name__)

SubmitFunc = Callable[..., TransferSubmitResult]


@dataclass(frozen=True)
class StageInputResult:
    batch_id: str
    stage: str
    status: str
    staging_id: Optional[str]
    globus_task_id: Optional[str]
    message: str


def load_batch_file(path: Path) -> List[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def process_requests(
    conn,
    requests: Sequence[StagingRequest],
    *,
    requested_by: str,
    dry_run: bool,
    submit_func: SubmitFunc = submit_transfer,
) -> List[StageInputResult]:
    """
    Record and optionally submit planned staging requests.

    Dry-run mode prints/plans only and does not write SQLite or contact Globus.
    Non-dry-run mode records each request in ``staged_inputs``. Newly created
    or reopened requests are submitted through ``submit_func``.
    """
    results: List[StageInputResult] = []

    for req in requests:
        if dry_run:
            results.append(
                StageInputResult(
                    batch_id=req.batch_id,
                    stage=req.stage,
                    status="planned",
                    staging_id=None,
                    globus_task_id=None,
                    message=f"{req.src_endpoint}:{req.src_path} -> {req.dst_endpoint}:{req.dst_path}",
                )
            )
            continue

        request_result = request_input_staging(
            conn,
            batch_id=req.batch_id,
            stage=req.stage,
            src_endpoint=req.src_endpoint,
            dst_endpoint=req.dst_endpoint,
            src_path=req.src_path,
            dst_path=req.dst_path,
            requested_by=requested_by,
            priority=req.priority,
        )
        staging_id = request_result["staging_id"]

        if not request_result["accepted"]:
            results.append(
                StageInputResult(
                    batch_id=req.batch_id,
                    stage=req.stage,
                    status=request_result["state"],
                    staging_id=staging_id,
                    globus_task_id=request_result.get("globus_task_id"),
                    message=request_result["status"],
                )
            )
            continue

        submit_result = submit_func(req)
        if submit_result.status == "submitted":
            updated = mark_input_staging_status(
                conn,
                staging_id=staging_id,
                status="submitted",
                globus_task_id=submit_result.globus_task_id,
            )
            status = updated["status"]
            message = submit_result.details
            globus_task_id = submit_result.globus_task_id
        else:
            updated = mark_input_staging_status(
                conn,
                staging_id=staging_id,
                status="failed",
                error_summary=submit_result.details,
            )
            status = updated["status"]
            message = submit_result.details
            globus_task_id = None

        results.append(
            StageInputResult(
                batch_id=req.batch_id,
                stage=req.stage,
                status=status,
                staging_id=staging_id,
                globus_task_id=globus_task_id,
                message=message,
            )
        )

    return results


def print_planned_requests(requests: Sequence[StagingRequest]) -> None:
    for row in requests_as_dicts(requests):
        print(row)


def print_results(results: Sequence[StageInputResult]) -> bool:
    print("\n-- input staging results ----------------------------------------")
    any_error = False
    for result in results:
        if result.status in {"planned", "submitted", "active", "completed", "already_active", "already_completed"}:
            marker = "+"
        else:
            marker = "!"
            any_error = True

        task = f" task={result.globus_task_id}" if result.globus_task_id else ""
        staging = f" staging_id={result.staging_id}" if result.staging_id else ""
        print(f"  {marker} {result.batch_id:<24} {result.status}{task}{staging}")
    print()
    return any_error


def _resolve_log_dir(cfg: dict, override: Optional[Path], *, stage: str) -> Path:
    if override is not None:
        return override
    configured = cfg.get("paths", {}).get("log_dir")
    if configured:
        return Path(configured)
    return Path.cwd() / "logs" / stage


def _log_results(results: Sequence[StageInputResult]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        logger.info(
            "Input staging result batch_id=%s stage=%s status=%s staging_id=%s globus_task_id=%s message=%s",
            result.batch_id,
            result.stage,
            result.status,
            result.staging_id,
            result.globus_task_id,
            result.message,
        )
    logger.info("Input staging summary: %s", counts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan and stage inputs for one pipeline stage."
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=sorted(STAGE_INPUT_SPECS),
        help="Pipeline stage whose inputs should be staged.",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Stage config YAML.",
    )
    parser.add_argument(
        "--batches",
        type=Path,
        default=None,
        help="Optional batch list file. Limits planning to these batch_ids.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned requests only. Does not write SQLite or call Globus.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum readiness rows to plan (default: 50).",
    )
    parser.add_argument(
        "--site",
        default="JUNO",
        help="Site filter for readiness query (default: JUNO).",
    )
    parser.add_argument(
        "--requested-by",
        default="stage_inputs.py",
        help="Value stored in staged_inputs.requested_by.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Optional base directory for command logs. Defaults to paths.log_dir from config.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    cfg = load_stage_config(args.config)
    batch_ids = load_batch_file(args.batches) if args.batches else None
    log_dir = _resolve_log_dir(cfg, args.log_dir, stage=args.stage)

    with command_logging(
        base_log_dir=log_dir,
        command_name="stage_inputs",
        log_level=args.log_level,
    ) as log_paths:
        logger.info("Writing command logs under %s", log_paths.directory)
        logger.info(
            "Starting input staging command stage=%s config=%s db=%s site=%s limit=%s batches=%s dry_run=%s requested_by=%s",
            args.stage,
            args.config,
            cfg["paths"]["db"],
            args.site,
            args.limit,
            args.batches,
            args.dry_run,
            args.requested_by,
        )

        conn = open_db(cfg["paths"]["db"], readonly=args.dry_run)
        try:
            requests = plan_input_staging(
                conn,
                cfg,
                stage=args.stage,
                site=args.site,
                limit=args.limit,
                batch_ids=batch_ids,
            )
            logger.info("Planned %d input staging request(s)", len(requests))

            if not requests:
                print(f"No input staging requests found for stage={args.stage} site={args.site}")
                logger.info("No input staging requests found for stage=%s site=%s", args.stage, args.site)
                return 0

            if args.dry_run:
                print_planned_requests(requests)

            results = process_requests(
                conn,
                requests,
                requested_by=args.requested_by,
                dry_run=args.dry_run,
            )
        finally:
            conn.close()

        _log_results(results)
        any_error = print_results(results)
        logger.info("Finished input staging command exit_code=%s", 1 if any_error else 0)
        return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
