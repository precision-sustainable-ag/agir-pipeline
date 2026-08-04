#!/usr/bin/env python3
"""Receive and register Atlas result-sync requests on Ceres.

This first result-sync phase performs one Globus transfer from the Atlas
request outbox to the Ceres inbox, waits for that transfer to finish, validates
each received ``*.result-sync.json`` file, and records it idempotently in the
canonical Ceres ``result_syncs`` table.

Run-bundle transfer, artifact verification, promotion, and canonical stage-run
ingestion are intentionally handled by later phases.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.globus_transfer import (
    TransferPollResult,
    TransferRequest,
    TransferSubmitResult,
    poll_task,
    submit_transfer,
)
from orchestrator.logging_utils import command_logging
from orchestrator.result_sync_request import (
    ResultSyncRequestError,
    load_result_sync_request,
)
from orchestrator.sqlite_db import open_db, register_result_sync

logger = logging.getLogger(__name__)

REQUEST_SUFFIX = ".result-sync.json"
MAX_REQUEST_BYTES = 1024 * 1024
REQUEST_TRANSFER_LABEL = "agir:result_sync:request_delivery"
REQUIRED_RESULT_SYNC_COLUMNS = frozenset(
    {
        "run_id",
        "request_json",
        "status",
        "src_endpoint",
        "dst_endpoint",
        "src_path",
        "dst_path",
    }
)

SubmitFunc = Callable[..., TransferSubmitResult]
PollFunc = Callable[..., TransferPollResult]


class ResultSyncConfigError(ValueError):
    """Raised when the Ceres result-sync configuration is unsafe or incomplete."""


class RequestTransferError(RuntimeError):
    """Raised when the Atlas-outbox transfer cannot complete successfully."""


class RequestTransferTimeout(RequestTransferError):
    """Raised when the Atlas-outbox transfer exceeds its configured timeout."""


@dataclass(frozen=True)
class CeresResultSyncConfig:
    """Validated settings needed to receive and trust Atlas requests."""

    db_path: Path
    log_dir: Path
    atlas_endpoint: str
    ceres_endpoint: str
    atlas_outbox: str
    ceres_inbox: Path
    atlas_run_root: str
    ceres_run_root: str
    poll_interval_seconds: float
    poll_timeout_seconds: float


@dataclass(frozen=True)
class InboxRequestResult:
    """Outcome of validating and optionally registering one inbox file."""

    request_path: Path
    run_id: str | None
    status: str
    message: str


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultSyncConfigError(f"{field} must be an object")
    return value


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultSyncConfigError(f"{field} must be a non-empty string")
    return value


def _validate_transfer_path(value: Any, field: str) -> str:
    text = _require_nonempty_string(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute() or len(path.parts) < 3 or path.parts[1] != "90daydata":
        raise ResultSyncConfigError(f"{field} must be an absolute path below /90daydata")
    if ".." in path.parts:
        raise ResultSyncConfigError(f"{field} must not contain '..'")
    return text


def _require_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ResultSyncConfigError(f"{field} must be greater than zero")
    return float(value)


def load_ceres_result_sync_config(path: Path) -> CeresResultSyncConfig:
    """Load and validate one Ceres result-sync YAML configuration."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ResultSyncConfigError(f"Unable to load result-sync config {path}: {exc}") from exc

    config = _require_object(raw, "config")
    if config.get("site") != "CERES":
        raise ResultSyncConfigError("site must be 'CERES'")

    paths = _require_object(config.get("paths"), "paths")
    sync = _require_object(config.get("result_sync"), "result_sync")

    db_path = Path(_require_nonempty_string(paths.get("db"), "paths.db"))
    log_dir = Path(_require_nonempty_string(paths.get("log_dir"), "paths.log_dir"))
    atlas_endpoint = _require_nonempty_string(
        sync.get("atlas_endpoint"),
        "result_sync.atlas_endpoint",
    )
    ceres_endpoint = _require_nonempty_string(
        sync.get("ceres_endpoint"),
        "result_sync.ceres_endpoint",
    )
    if atlas_endpoint == ceres_endpoint:
        raise ResultSyncConfigError("Atlas and Ceres endpoints must differ")

    atlas_outbox = _validate_transfer_path(
        sync.get("atlas_outbox"),
        "result_sync.atlas_outbox",
    )
    ceres_inbox = Path(
        _validate_transfer_path(
            sync.get("ceres_inbox"),
            "result_sync.ceres_inbox",
        )
    )
    atlas_run_root = _validate_transfer_path(
        sync.get("atlas_run_root"),
        "result_sync.atlas_run_root",
    )
    ceres_run_root = _validate_transfer_path(
        sync.get("ceres_run_root"),
        "result_sync.ceres_run_root",
    )

    polling = _require_object(sync.get("polling", {}), "result_sync.polling")
    poll_interval_seconds = _require_positive_number(
        polling.get("interval_seconds", 30),
        "result_sync.polling.interval_seconds",
    )
    poll_timeout_seconds = _require_positive_number(
        polling.get("timeout_seconds", 3600),
        "result_sync.polling.timeout_seconds",
    )

    return CeresResultSyncConfig(
        db_path=db_path,
        log_dir=log_dir,
        atlas_endpoint=atlas_endpoint,
        ceres_endpoint=ceres_endpoint,
        atlas_outbox=atlas_outbox,
        ceres_inbox=ceres_inbox,
        atlas_run_root=atlas_run_root,
        ceres_run_root=ceres_run_root,
        poll_interval_seconds=poll_interval_seconds,
        poll_timeout_seconds=poll_timeout_seconds,
    )


def receive_request_outbox(
    config: CeresResultSyncConfig,
    *,
    dry_run: bool = False,
    submit_func: SubmitFunc = submit_transfer,
    poll_func: PollFunc = poll_task,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic_func: Callable[[], float] = time.monotonic,
) -> str:
    """Transfer Atlas's outbox to Ceres and wait for the Globus task."""
    request = TransferRequest(
        src_endpoint=config.atlas_endpoint,
        dst_endpoint=config.ceres_endpoint,
        src_path=config.atlas_outbox,
        dst_path=str(config.ceres_inbox),
        label=REQUEST_TRANSFER_LABEL,
    )
    logger.info(
        "Submitting result-sync request transfer src=%s:%s dst=%s:%s dry_run=%s",
        request.src_endpoint,
        request.src_path,
        request.dst_endpoint,
        request.dst_path,
        dry_run,
    )
    submitted = submit_func(
        request,
        dry_run=dry_run,
        recursive=True,
        sync_level="checksum",
    )
    if submitted.status != "submitted" or not submitted.globus_task_id:
        raise RequestTransferError(
            f"Unable to submit Atlas request transfer: {submitted.details}"
        )

    task_id = submitted.globus_task_id
    started_at = monotonic_func()
    while True:
        polled = poll_func(task_id, dry_run=dry_run)
        logger.info(
            "Polled result-sync request transfer task_id=%s status=%s globus_status=%s",
            task_id,
            polled.status,
            polled.globus_status,
        )
        if polled.status == "completed":
            return task_id
        if polled.status in {"failed", "canceled"}:
            raise RequestTransferError(
                f"Atlas request transfer {task_id} ended as {polled.status}: {polled.details}"
            )

        elapsed = monotonic_func() - started_at
        if elapsed >= config.poll_timeout_seconds:
            raise RequestTransferTimeout(
                f"Timed out after {config.poll_timeout_seconds:g} seconds waiting for "
                f"Atlas request transfer {task_id}"
            )
        sleep_func(min(config.poll_interval_seconds, config.poll_timeout_seconds - elapsed))


def _require_bundle_below_root(bundle_path: str, root_path: str, field: str) -> None:
    bundle = PurePosixPath(bundle_path)
    root = PurePosixPath(root_path)
    try:
        relative = bundle.relative_to(root)
    except ValueError as exc:
        raise ResultSyncRequestError(
            f"{field} must be below configured root {root_path!r}"
        ) from exc
    if not relative.parts:
        raise ResultSyncRequestError(f"{field} must identify a run below {root_path!r}")
    if len(relative.parts) != 1:
        raise ResultSyncRequestError(
            f"{field} must identify a direct child of configured root {root_path!r}"
        )


def validate_request_route(
    request: Mapping[str, Any],
    config: CeresResultSyncConfig,
) -> None:
    """Enforce Ceres-owned roots and one-directory-per-run semantics."""
    run_id = str(request["run"]["run_id"])
    bundle = request["run_bundle"]
    if bundle["recursive"] is not True:
        raise ResultSyncRequestError("run_bundle.recursive must be true")

    _require_bundle_below_root(
        bundle["src_path"],
        config.atlas_run_root,
        "run_bundle.src_path",
    )
    _require_bundle_below_root(
        bundle["dst_path"],
        config.ceres_run_root,
        "run_bundle.dst_path",
    )
    if PurePosixPath(bundle["src_path"]).name != run_id:
        raise ResultSyncRequestError("run_bundle.src_path must end with run.run_id")
    if PurePosixPath(bundle["dst_path"]).name != run_id:
        raise ResultSyncRequestError("run_bundle.dst_path must end with run.run_id")


def discover_inbox_requests(inbox: Path) -> list[Path]:
    """Return stable, lexically ordered result-sync request paths."""
    if not inbox.exists():
        return []
    if not inbox.is_dir():
        raise ResultSyncRequestError(f"Ceres inbox is not a directory: {inbox}")
    return sorted(inbox.glob(f"*{REQUEST_SUFFIX}"), key=lambda path: path.name)


def require_result_sync_schema(conn) -> None:
    """Fail clearly when the canonical DB has not received the result-sync schema."""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(result_syncs)").fetchall()
    }
    missing = sorted(REQUIRED_RESULT_SYNC_COLUMNS - columns)
    if missing:
        raise ResultSyncConfigError(
            "Ceres database is missing the result_syncs schema or required columns "
            f"{missing}; apply schemas/sqlite/pipeline.sql before running result sync"
        )


def process_inbox_requests(
    conn,
    config: CeresResultSyncConfig,
    *,
    dry_run: bool = False,
) -> list[InboxRequestResult]:
    """Validate and idempotently register every request currently in the inbox."""
    results: list[InboxRequestResult] = []
    for request_path in discover_inbox_requests(config.ceres_inbox):
        run_id: str | None = None
        try:
            if request_path.is_symlink():
                raise ResultSyncRequestError("request file must not be a symbolic link")
            if not request_path.is_file():
                raise ResultSyncRequestError("request path must be a regular file")
            size_bytes = request_path.stat().st_size
            if size_bytes > MAX_REQUEST_BYTES:
                raise ResultSyncRequestError(
                    f"request exceeds {MAX_REQUEST_BYTES} byte limit"
                )

            request = load_result_sync_request(
                request_path,
                allowed_source_endpoints={config.atlas_endpoint},
                allowed_destination_endpoints={config.ceres_endpoint},
            )
            run_id = request["run"]["run_id"]
            expected_name = f"{run_id}{REQUEST_SUFFIX}"
            if request_path.name != expected_name:
                raise ResultSyncRequestError(
                    f"request filename must be {expected_name!r}"
                )
            validate_request_route(request, config)

            if dry_run:
                result = InboxRequestResult(
                    request_path=request_path,
                    run_id=run_id,
                    status="would_register",
                    message="validated; database write skipped",
                )
            else:
                registered = register_result_sync(
                    conn,
                    run_id=run_id,
                    batch_id=request["run"]["batch_id"],
                    stage=request["run"]["stage"],
                    run_status=request["run"]["status"],
                    promotion_succeeded=request["promotion"]["succeeded"],
                    src_endpoint=request["run_bundle"]["src_endpoint"],
                    dst_endpoint=request["run_bundle"]["dst_endpoint"],
                    src_path=request["run_bundle"]["src_path"],
                    dst_path=request["run_bundle"]["dst_path"],
                    recursive=request["run_bundle"]["recursive"],
                    request_path=str(request_path),
                    request_json=request,
                    request_created_at=request["request_created_at"],
                    source_site=request["source_site"],
                    destination_site=request["destination_site"],
                )
                status = "registered" if registered["accepted"] else "existing"
                result = InboxRequestResult(
                    request_path=request_path,
                    run_id=run_id,
                    status=status,
                    message=registered["status"],
                )
            logger.info(
                "Processed result-sync request path=%s run_id=%s status=%s",
                result.request_path,
                result.run_id,
                result.status,
            )
        except (OSError, ValueError, ResultSyncRequestError) as exc:
            result = InboxRequestResult(
                request_path=request_path,
                run_id=run_id,
                status="invalid",
                message=str(exc),
            )
            logger.error(
                "Rejected result-sync request path=%s run_id=%s error=%s",
                request_path,
                run_id,
                exc,
            )
        results.append(result)
    return results


def print_results(results: Sequence[InboxRequestResult]) -> bool:
    """Print a compact operator summary and return whether any request failed."""
    print("\n-- Atlas result-sync requests ----------------------------------")
    any_error = False
    for result in results:
        marker = "+"
        if result.status == "invalid":
            marker = "!"
            any_error = True
        run_id = result.run_id or "unknown"
        print(f"  {marker} {run_id:<36} {result.status}: {result.message}")
    if not results:
        print("  No result-sync request files found.")
    print()
    return any_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receive and register Atlas result-sync requests on Ceres."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not contact Globus or write SQLite; validate requests already in the inbox.",
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


def main() -> int:
    args = _parser().parse_args()
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

    log_dir = args.log_dir or config.log_dir
    with command_logging(
        base_log_dir=log_dir,
        command_name="sync_atlas_results",
        category="result_sync",
        log_level=args.log_level,
    ) as log_paths:
        logger.info("Writing command logs under %s", log_paths.directory)
        logger.info(
            "Starting Atlas request synchronization config=%s db=%s dry_run=%s",
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
        try:
            if not args.dry_run:
                conn = open_db(config.db_path)
                require_result_sync_schema(conn)
            results = process_inbox_requests(conn, config, dry_run=args.dry_run)
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.error("Unable to process Ceres result-sync inbox: %s", exc)
            return 1
        finally:
            if conn is not None:
                conn.close()

        any_error = print_results(results)
        exit_code = 1 if any_error else 0
        logger.info(
            "Finished Atlas request synchronization requests=%d exit_code=%d",
            len(results),
            exit_code,
        )
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
