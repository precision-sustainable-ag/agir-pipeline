"""Ceres-side Atlas result synchronization service.

This module owns configuration validation, request intake, Globus run-bundle
delivery, bundle verification, and the associated SQLite state transitions.
The admin entry point is intentionally limited to CLI and presentation work.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from orchestrator.artifact_validation import validate_transferred_run_bundle
from orchestrator.globus_transfer import (
    TransferPollResult,
    TransferRequest,
    TransferSubmitResult,
    poll_task,
    submit_transfer,
)
from orchestrator.promotion import promote_run_bundle
from orchestrator.result_sync_request import (
    ResultSyncRequestError,
    load_result_sync_request,
    validate_result_sync_request,
)
from orchestrator.sqlite_db import (
    get_result_syncs,
    ingest_run_report,
    mark_result_sync_status,
    register_result_sync,
)

logger = logging.getLogger(__name__)

REQUEST_SUFFIX = ".result-sync.json"
MAX_REQUEST_BYTES = 1024 * 1024
REQUEST_TRANSFER_LABEL = "agir:result_sync:request_delivery"
REQUIRED_RESULT_SYNC_COLUMNS = frozenset(
    {
        "run_id",
        "request_json",
        "status",
        "attempt_count",
        "globus_task_id",
        "src_endpoint",
        "dst_endpoint",
        "src_path",
        "dst_path",
    }
)

SubmitFunc = Callable[..., TransferSubmitResult]
PollFunc = Callable[..., TransferPollResult]

__all__ = [
    "CeresResultSyncConfig",
    "finalize_verified_run_bundles",
    "RequestTransferError",
    "RequestTransferTimeout",
    "ResultSyncConfigError",
    "SyncOutcome",
    "load_ceres_result_sync_config",
    "poll_transferring_bundle_transfers",
    "process_inbox_requests",
    "receive_request_outbox",
    "require_result_sync_schema",
    "submit_requested_bundle_transfers",
    "synchronize_run_bundles",
    "verify_transferred_run_bundles",
]


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
    promotion_root: Path
    promotion_suffixes: dict[str, str]
    poll_interval_seconds: float
    poll_timeout_seconds: float


@dataclass(frozen=True)
class SyncOutcome:
    """One request, transfer, or verification result for logging and display."""

    phase: str
    run_id: str | None
    status: str
    message: str
    previous_status: str | None = None
    globus_task_id: str | None = None
    request_path: Path | None = None
    is_error: bool = False


# Configuration ---------------------------------------------------------------


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


def _load_promotion_suffixes(value: Any) -> dict[str, str]:
    suffixes = _require_object(value, "result_sync.promotion.stage_suffixes")
    if not suffixes:
        raise ResultSyncConfigError(
            "result_sync.promotion.stage_suffixes must not be empty"
        )
    result: dict[str, str] = {}
    for stage, suffix in suffixes.items():
        stage_name = _require_nonempty_string(stage, "promotion stage name")
        suffix_name = _require_nonempty_string(
            suffix,
            f"result_sync.promotion.stage_suffixes.{stage_name}",
        )
        if PurePosixPath(suffix_name).parts != (suffix_name,) or suffix_name in {".", ".."}:
            raise ResultSyncConfigError(
                f"promotion suffix for {stage_name!r} must be one directory name"
            )
        result[stage_name] = suffix_name
    return result


def load_ceres_result_sync_config(path: Path) -> CeresResultSyncConfig:
    """Load and validate one Ceres result-sync YAML configuration."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ResultSyncConfigError(
            f"Unable to load result-sync config {path}: {exc}"
        ) from exc

    config = _require_object(raw, "config")
    if config.get("site") != "CERES":
        raise ResultSyncConfigError("site must be 'CERES'")

    paths = _require_object(config.get("paths"), "paths")
    sync = _require_object(config.get("result_sync"), "result_sync")
    atlas_endpoint = _require_nonempty_string(
        sync.get("atlas_endpoint"), "result_sync.atlas_endpoint"
    )
    ceres_endpoint = _require_nonempty_string(
        sync.get("ceres_endpoint"), "result_sync.ceres_endpoint"
    )
    if atlas_endpoint == ceres_endpoint:
        raise ResultSyncConfigError("Atlas and Ceres endpoints must differ")

    polling = _require_object(sync.get("polling", {}), "result_sync.polling")
    promotion = _require_object(sync.get("promotion"), "result_sync.promotion")
    return CeresResultSyncConfig(
        db_path=Path(_require_nonempty_string(paths.get("db"), "paths.db")),
        log_dir=Path(_require_nonempty_string(paths.get("log_dir"), "paths.log_dir")),
        atlas_endpoint=atlas_endpoint,
        ceres_endpoint=ceres_endpoint,
        atlas_outbox=_validate_transfer_path(
            sync.get("atlas_outbox"), "result_sync.atlas_outbox"
        ),
        ceres_inbox=Path(
            _validate_transfer_path(sync.get("ceres_inbox"), "result_sync.ceres_inbox")
        ),
        atlas_run_root=_validate_transfer_path(
            sync.get("atlas_run_root"), "result_sync.atlas_run_root"
        ),
        ceres_run_root=_validate_transfer_path(
            sync.get("ceres_run_root"), "result_sync.ceres_run_root"
        ),
        promotion_root=Path(
            _validate_transfer_path(
                promotion.get("root"),
                "result_sync.promotion.root",
            )
        ),
        promotion_suffixes=_load_promotion_suffixes(
            promotion.get("stage_suffixes")
        ),
        poll_interval_seconds=_require_positive_number(
            polling.get("interval_seconds", 30),
            "result_sync.polling.interval_seconds",
        ),
        poll_timeout_seconds=_require_positive_number(
            polling.get("timeout_seconds", 3600),
            "result_sync.polling.timeout_seconds",
        ),
    )


# Request delivery and registration ------------------------------------------


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
                f"Atlas request transfer {task_id} ended as {polled.status}: "
                f"{polled.details}"
            )

        elapsed = monotonic_func() - started_at
        if elapsed >= config.poll_timeout_seconds:
            raise RequestTransferTimeout(
                f"Timed out after {config.poll_timeout_seconds:g} seconds waiting for "
                f"Atlas request transfer {task_id}"
            )
        sleep_func(
            min(config.poll_interval_seconds, config.poll_timeout_seconds - elapsed)
        )


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
        bundle["src_path"], config.atlas_run_root, "run_bundle.src_path"
    )
    _require_bundle_below_root(
        bundle["dst_path"], config.ceres_run_root, "run_bundle.dst_path"
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
    """Fail clearly when the canonical DB lacks the result-sync schema."""
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
) -> list[SyncOutcome]:
    """Validate and idempotently register every request currently in the inbox."""
    outcomes: list[SyncOutcome] = []
    for request_path in discover_inbox_requests(config.ceres_inbox):
        run_id: str | None = None
        try:
            if request_path.is_symlink():
                raise ResultSyncRequestError("request file must not be a symbolic link")
            if not request_path.is_file():
                raise ResultSyncRequestError("request path must be a regular file")
            if request_path.stat().st_size > MAX_REQUEST_BYTES:
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
                status = "would_register"
                message = "validated; database write skipped"
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
                message = registered["status"]
            outcome = SyncOutcome(
                phase="request",
                request_path=request_path,
                run_id=run_id,
                status=status,
                message=message,
            )
            logger.info(
                "Processed result-sync request path=%s run_id=%s status=%s",
                request_path,
                run_id,
                status,
            )
        except (OSError, ValueError) as exc:
            outcome = SyncOutcome(
                phase="request",
                request_path=request_path,
                run_id=run_id,
                status="invalid",
                message=str(exc),
                is_error=True,
            )
            logger.error(
                "Rejected result-sync request path=%s run_id=%s error=%s",
                request_path,
                run_id,
                exc,
            )
        outcomes.append(outcome)
    return outcomes


# Run-bundle transfer ---------------------------------------------------------


def _validate_sync_row_route(
    sync: Mapping[str, Any],
    config: CeresResultSyncConfig,
) -> None:
    """Revalidate a persisted transfer route before use."""
    run_id = str(sync["run_id"])
    if sync["source_site"] != "ATLAS" or sync["destination_site"] != "CERES":
        raise ResultSyncRequestError("result-sync row must route from ATLAS to CERES")
    if sync["src_endpoint"] != config.atlas_endpoint:
        raise ResultSyncRequestError(
            "result-sync source endpoint is not configured Atlas"
        )
    if sync["dst_endpoint"] != config.ceres_endpoint:
        raise ResultSyncRequestError(
            "result-sync destination endpoint is not configured Ceres"
        )
    if int(sync["recursive"]) != 1:
        raise ResultSyncRequestError(
            "result-sync run-bundle transfer must be recursive"
        )

    _require_bundle_below_root(sync["src_path"], config.atlas_run_root, "src_path")
    _require_bundle_below_root(sync["dst_path"], config.ceres_run_root, "dst_path")
    if PurePosixPath(sync["src_path"]).name != run_id:
        raise ResultSyncRequestError("result-sync src_path must end with run_id")
    if PurePosixPath(sync["dst_path"]).name != run_id:
        raise ResultSyncRequestError("result-sync dst_path must end with run_id")


def _bundle_transfer_label(sync: Mapping[str, Any]) -> str:
    return f"agir:{sync['stage']}:{sync['batch_id']}:{sync['run_id']}:result_sync"


def _error_summary(message: str, *, limit: int = 4000) -> str:
    text = str(message).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _log_outcome(outcome: SyncOutcome, action: str) -> None:
    log_func = logger.error if outcome.is_error else logger.info
    log_func(
        "Result-sync %s run_id=%s status=%s->%s task_id=%s message=%s",
        action,
        outcome.run_id,
        outcome.previous_status,
        outcome.status,
        outcome.globus_task_id,
        outcome.message,
    )


def submit_requested_bundle_transfers(
    conn,
    config: CeresResultSyncConfig,
    *,
    limit: int,
    dry_run: bool = False,
    submit_func: SubmitFunc = submit_transfer,
) -> list[SyncOutcome]:
    """Submit one checksum transfer for each requested result-sync row."""
    outcomes: list[SyncOutcome] = []
    for sync in get_result_syncs(conn, statuses=["requested"], limit=limit):
        run_id = str(sync["run_id"])
        previous_status = str(sync["status"])
        try:
            _validate_sync_row_route(sync, config)
            submitted = submit_func(
                TransferRequest(
                    src_endpoint=sync["src_endpoint"],
                    dst_endpoint=sync["dst_endpoint"],
                    src_path=sync["src_path"],
                    dst_path=sync["dst_path"],
                    label=_bundle_transfer_label(sync),
                ),
                dry_run=dry_run,
                recursive=bool(sync["recursive"]),
                sync_level="checksum",
            )
            if dry_run:
                outcome = SyncOutcome(
                    phase="transfer",
                    run_id=run_id,
                    previous_status=previous_status,
                    status="would_submit",
                    globus_task_id=submitted.globus_task_id,
                    message=submitted.details,
                )
            elif submitted.status == "submitted" and submitted.globus_task_id:
                updated = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="transferring",
                    globus_task_id=submitted.globus_task_id,
                )
                outcome = SyncOutcome(
                    phase="transfer",
                    run_id=run_id,
                    previous_status=previous_status,
                    status=updated["status"],
                    globus_task_id=updated["globus_task_id"],
                    message=submitted.details,
                )
            else:
                message = _error_summary(submitted.details)
                updated = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="failed",
                    error_summary=message,
                )
                outcome = SyncOutcome(
                    phase="transfer",
                    run_id=run_id,
                    previous_status=previous_status,
                    status=updated["status"],
                    message=message,
                    is_error=True,
                )
        except (OSError, ResultSyncRequestError) as exc:
            message = _error_summary(str(exc))
            if dry_run:
                status = "invalid"
            else:
                status = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="failed",
                    error_summary=message,
                )["status"]
            outcome = SyncOutcome(
                phase="transfer",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                message=message,
                is_error=True,
            )

        _log_outcome(outcome, "bundle submission")
        outcomes.append(outcome)
    return outcomes


def poll_transferring_bundle_transfers(
    conn,
    *,
    poll_func: PollFunc = poll_task,
) -> list[SyncOutcome]:
    """Poll all active bundle tasks once and persist terminal outcomes."""
    outcomes: list[SyncOutcome] = []
    for sync in get_result_syncs(conn, statuses=["transferring"], limit=10000):
        run_id = str(sync["run_id"])
        previous_status = str(sync["status"])
        task_id = sync.get("globus_task_id")
        if not task_id:
            message = "transferring result-sync row has no Globus task id"
            status = mark_result_sync_status(
                conn,
                run_id=run_id,
                status="failed",
                error_summary=message,
            )["status"]
            outcome = SyncOutcome(
                phase="transfer",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                message=message,
                is_error=True,
            )
        else:
            try:
                polled = poll_func(task_id)
            except OSError as exc:
                polled = TransferPollResult(
                    status="failed",
                    globus_status=None,
                    details=str(exc),
                    command=[],
                )

            is_error = False
            if polled.status == "completed":
                status = mark_result_sync_status(
                    conn, run_id=run_id, status="transferred"
                )["status"]
                message = polled.details
            elif polled.status == "canceled":
                message = _error_summary(polled.details)
                status = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="canceled",
                    error_summary=message,
                )["status"]
                is_error = True
            elif polled.status == "failed" and polled.globus_status is not None:
                message = _error_summary(polled.details)
                status = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="failed",
                    error_summary=message,
                )["status"]
                is_error = True
            elif polled.status == "failed":
                # A local query failure does not prove the remote task failed.
                status = "poll_error"
                message = _error_summary(polled.details)
                is_error = True
            else:
                status = "active"
                message = polled.details

            outcome = SyncOutcome(
                phase="transfer",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                globus_task_id=task_id,
                message=message,
                is_error=is_error,
            )

        _log_outcome(outcome, "bundle poll")
        outcomes.append(outcome)
    return outcomes


def synchronize_run_bundles(
    conn,
    config: CeresResultSyncConfig,
    *,
    limit: int,
    dry_run: bool = False,
    submit_func: SubmitFunc = submit_transfer,
    poll_func: PollFunc = poll_task,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic_func: Callable[[], float] = time.monotonic,
) -> tuple[list[SyncOutcome], bool]:
    """Submit requested bundles and wait for tracked tasks to settle."""
    submitted = submit_requested_bundle_transfers(
        conn,
        config,
        limit=limit,
        dry_run=dry_run,
        submit_func=submit_func,
    )
    if dry_run:
        return submitted, False

    latest = {outcome.run_id: outcome for outcome in submitted}
    started_at = monotonic_func()
    while True:
        for outcome in poll_transferring_bundle_transfers(conn, poll_func=poll_func):
            latest[outcome.run_id] = outcome

        if not get_result_syncs(conn, statuses=["transferring"], limit=1):
            return list(latest.values()), False

        elapsed = monotonic_func() - started_at
        if elapsed >= config.poll_timeout_seconds:
            return list(latest.values()), True
        sleep_func(
            min(config.poll_interval_seconds, config.poll_timeout_seconds - elapsed)
        )


# Transferred-bundle verification --------------------------------------------


def _validate_persisted_request_identity(
    sync: Mapping[str, Any],
    config: CeresResultSyncConfig,
) -> dict[str, Any]:
    """Confirm immutable request JSON still agrees with its database row."""
    try:
        raw_request = json.loads(sync["request_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResultSyncRequestError(
            f"stored request_json is invalid for run {sync['run_id']}: {exc}"
        ) from exc

    request = validate_result_sync_request(
        raw_request,
        allowed_source_endpoints={config.atlas_endpoint},
        allowed_destination_endpoints={config.ceres_endpoint},
    )
    validate_request_route(request, config)

    request_status = request["run"]["status"]
    if request_status == "partial":
        request_status = "partial_success"
    expected = {
        "run_id": request["run"]["run_id"],
        "batch_id": request["run"]["batch_id"],
        "stage": request["run"]["stage"],
        "run_status": request_status,
        "promotion_succeeded": int(request["promotion"]["succeeded"]),
        "src_endpoint": request["run_bundle"]["src_endpoint"],
        "dst_endpoint": request["run_bundle"]["dst_endpoint"],
        "src_path": request["run_bundle"]["src_path"],
        "dst_path": request["run_bundle"]["dst_path"],
        "recursive": int(request["run_bundle"]["recursive"]),
    }
    conflicts = [field for field, value in expected.items() if sync[field] != value]
    if conflicts:
        raise ResultSyncRequestError(
            f"stored request does not match result_syncs row: fields={conflicts}"
        )
    return request


def verify_transferred_run_bundles(
    conn,
    config: CeresResultSyncConfig,
    *,
    dry_run: bool = False,
) -> list[SyncOutcome]:
    """Validate transferred Ceres bundles and advance them to ``verified``."""
    outcomes: list[SyncOutcome] = []
    for sync in get_result_syncs(conn, statuses=["transferred"], limit=10000):
        run_id = str(sync["run_id"])
        previous_status = str(sync["status"])
        try:
            _validate_sync_row_route(sync, config)
            request = _validate_persisted_request_identity(sync, config)
            validate_transferred_run_bundle(
                Path(sync["dst_path"]),
                run_id=run_id,
                batch_id=str(sync["batch_id"]),
                stage=str(sync["stage"]),
                run_status=str(sync["run_status"]),
                ended_at=str(request["run"]["ended_at"]),
            )
            if dry_run:
                status = "would_verify"
                message = "bundle identity, artifacts, sizes, and checksums validated"
            else:
                status = mark_result_sync_status(
                    conn, run_id=run_id, status="verified"
                )["status"]
                message = "bundle identity, artifacts, sizes, and checksums verified"
            outcome = SyncOutcome(
                phase="verification",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                message=message,
            )
        except (OSError, ValueError) as exc:
            message = _error_summary(str(exc))
            if dry_run:
                status = "invalid"
            else:
                status = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="failed",
                    error_summary=message,
                )["status"]
            outcome = SyncOutcome(
                phase="verification",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                message=message,
                is_error=True,
            )

        _log_outcome(outcome, "bundle verification")
        outcomes.append(outcome)
    return outcomes


# Ceres promotion and canonical ingestion ------------------------------------


def _promotion_order(sync: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    request = json.loads(sync["request_json"])
    promoted_at = request["promotion"]["promoted_at"]
    return (
        datetime.fromisoformat(promoted_at.replace("Z", "+00:00"))
        if promoted_at
        else datetime.min.replace(tzinfo=timezone.utc),
        datetime.fromisoformat(request["request_created_at"].replace("Z", "+00:00")),
        str(sync["run_id"]),
    )


def _latest_promoted_sync(
    conn,
    *,
    batch_id: str,
    stage: str,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT * FROM result_syncs
        WHERE batch_id = ? AND stage = ?
          AND run_status = 'success' AND promotion_succeeded = 1
        """,
        (batch_id, stage),
    ).fetchall()
    if not rows:
        return None
    return max((dict(row) for row in rows), key=_promotion_order)


def _promotion_destination(
    config: CeresResultSyncConfig,
    *,
    batch_id: str,
    stage: str,
) -> Path:
    if PurePosixPath(batch_id).parts != (batch_id,) or batch_id in {".", ".."}:
        raise ResultSyncConfigError(
            f"batch_id must be one safe directory name for promotion: {batch_id!r}"
        )
    try:
        suffix = config.promotion_suffixes[stage]
    except KeyError as exc:
        raise ResultSyncConfigError(
            f"No Ceres promotion suffix configured for stage {stage!r}"
        ) from exc
    return config.promotion_root / batch_id / suffix


def _ingest_and_mark_result_sync(conn, sync: Mapping[str, Any]) -> None:
    report_path = Path(sync["dst_path"]) / "run_report.json"
    ingested = ingest_run_report(conn, report_path)
    if ingested["run_id"] != sync["run_id"]:
        conn.rollback()
        raise ValueError(
            f"ingested run_id {ingested['run_id']!r} does not match {sync['run_id']!r}"
        )
    conn.commit()
    mark_result_sync_status(conn, run_id=str(sync["run_id"]), status="ingested")


def finalize_verified_run_bundles(
    conn,
    config: CeresResultSyncConfig,
    *,
    dry_run: bool = False,
) -> list[SyncOutcome]:
    """Promote eligible Ceres results and ingest verified run reports."""
    outcomes: list[SyncOutcome] = []
    verified = get_result_syncs(conn, statuses=["verified"], limit=10000)

    def priority(sync: Mapping[str, Any]) -> tuple[int, tuple[datetime, datetime, str]]:
        latest = _latest_promoted_sync(
            conn,
            batch_id=str(sync["batch_id"]),
            stage=str(sync["stage"]),
        )
        is_latest = latest is not None and latest["run_id"] == sync["run_id"]
        return (0 if is_latest else 1, _promotion_order(sync))

    for sync in sorted(verified, key=priority):
        run_id = str(sync["run_id"])
        previous_status = str(sync["status"])
        try:
            run_status = str(sync["run_status"])
            if run_status == "success":
                if not bool(sync["promotion_succeeded"]):
                    raise ValueError(
                        "successful Atlas run was not promoted; refusing canonical ingestion"
                    )
                latest = _latest_promoted_sync(
                    conn,
                    batch_id=str(sync["batch_id"]),
                    stage=str(sync["stage"]),
                )
                if latest is None:
                    raise ValueError("no promoted Atlas run found for successful result")
                if latest["run_id"] == run_id:
                    destination = _promotion_destination(
                        config,
                        batch_id=str(sync["batch_id"]),
                        stage=str(sync["stage"]),
                    )
                    if dry_run:
                        message = f"would promote to {destination} and ingest run report"
                    else:
                        promoted = promote_run_bundle(
                            Path(sync["dst_path"]),
                            destination,
                            artifacts_root=Path(sync["dst_path"]) / "artifacts",
                        )
                        message = (
                            f"promoted {promoted.artifact_count} artifacts to "
                            f"{promoted.destination} and ingested run report"
                        )
                elif latest["status"] == "ingested":
                    message = (
                        f"superseded by promoted run {latest['run_id']}; "
                        "ingested for history only"
                    )
                else:
                    outcome = SyncOutcome(
                        phase="finalization",
                        run_id=run_id,
                        previous_status=previous_status,
                        status="deferred",
                        message=(
                            f"waiting for newer promoted run {latest['run_id']} "
                            "to be ingested"
                        ),
                        is_error=True,
                    )
                    _log_outcome(outcome, "bundle finalization")
                    outcomes.append(outcome)
                    continue
            else:
                message = "ingested non-successful run report without promotion"

            if dry_run:
                status = "would_ingest"
            else:
                _ingest_and_mark_result_sync(conn, sync)
                status = "ingested"
            outcome = SyncOutcome(
                phase="finalization",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                message=message,
            )
        except (OSError, ValueError) as exc:
            message = _error_summary(str(exc))
            if dry_run:
                status = "invalid"
            else:
                conn.rollback()
                status = mark_result_sync_status(
                    conn,
                    run_id=run_id,
                    status="failed",
                    error_summary=message,
                )["status"]
            outcome = SyncOutcome(
                phase="finalization",
                run_id=run_id,
                previous_status=previous_status,
                status=status,
                message=message,
                is_error=True,
            )

        _log_outcome(outcome, "bundle finalization")
        outcomes.append(outcome)
    return outcomes
