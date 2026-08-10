"""Create reconciled Ceres database snapshots and optionally send them to Atlas."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from orchestrator.globus_transfer import (
    TransferPollResult,
    TransferRequest,
    TransferSubmitResult,
    poll_task,
    submit_transfer,
)
from orchestrator.result_sync import (
    CeresResultSyncConfig,
    ResultSyncConfigError,
    load_ceres_result_sync_config,
    require_result_sync_schema,
)
from orchestrator.result_sync_reconciliation import (
    ReconciliationReport,
    reconcile_result_syncs,
)
from orchestrator.sqlite_db import create_sqlite_snapshot, open_db

SNAPSHOT_TRANSFER_LABEL = "agir:database_snapshot:ceres_to_atlas"


class SnapshotPublicationError(RuntimeError):
    """Raised when a reconciled snapshot cannot be created or transferred."""


@dataclass(frozen=True)
class SnapshotPublicationConfig:
    result_sync: CeresResultSyncConfig
    ceres_publish_path: Path
    atlas_destination_path: str | None


@dataclass(frozen=True)
class SnapshotPublicationResult:
    snapshot_path: Path | None
    reconciliation: ReconciliationReport
    globus_task_id: str | None = None


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultSyncConfigError(f"{field} must be an object")
    return value


def _optional_atlas_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ResultSyncConfigError(
            "snapshot.atlas_destination_path must be a non-empty string"
        )
    path = PurePosixPath(value)
    if not path.is_absolute() or len(path.parts) < 3 or ".." in path.parts:
        raise ResultSyncConfigError(
            "snapshot.atlas_destination_path must be a safe absolute path"
        )
    return value


def load_snapshot_publication_config(path: Path) -> SnapshotPublicationConfig:
    """Load snapshot settings alongside the existing Ceres sync settings."""
    result_sync = load_ceres_result_sync_config(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ResultSyncConfigError(f"Unable to load snapshot config {path}: {exc}") from exc
    config = _require_object(raw, "config")
    snapshot = _require_object(config.get("snapshot"), "snapshot")
    publish_value = snapshot.get("ceres_publish_path")
    if not isinstance(publish_value, str) or not publish_value.strip():
        raise ResultSyncConfigError(
            "snapshot.ceres_publish_path must be a non-empty string"
        )
    publish_path = Path(publish_value)
    if not publish_path.is_absolute() or len(publish_path.parts) < 3:
        raise ResultSyncConfigError(
            "snapshot.ceres_publish_path must be a safe absolute path"
        )
    if publish_path.resolve() == result_sync.db_path.resolve():
        raise ResultSyncConfigError(
            "snapshot.ceres_publish_path must differ from the canonical database"
        )
    return SnapshotPublicationConfig(
        result_sync=result_sync,
        ceres_publish_path=publish_path,
        atlas_destination_path=_optional_atlas_path(
            snapshot.get("atlas_destination_path")
        ),
    )


def _transfer_snapshot(
    config: SnapshotPublicationConfig,
    *,
    dry_run: bool,
    submit_func: Callable[..., TransferSubmitResult],
    poll_func: Callable[..., TransferPollResult],
    sleep_func: Callable[[float], None],
    monotonic_func: Callable[[], float],
) -> str:
    atlas_path = config.atlas_destination_path
    if atlas_path is None:
        raise SnapshotPublicationError(
            "--transfer requires snapshot.atlas_destination_path"
        )
    sync = config.result_sync
    submitted = submit_func(
        TransferRequest(
            src_endpoint=sync.ceres_endpoint,
            dst_endpoint=sync.atlas_endpoint,
            src_path=str(config.ceres_publish_path),
            dst_path=atlas_path,
            label=SNAPSHOT_TRANSFER_LABEL,
        ),
        dry_run=dry_run,
        recursive=False,
        sync_level="checksum",
    )
    if submitted.status != "submitted" or not submitted.globus_task_id:
        raise SnapshotPublicationError(
            f"Unable to submit database snapshot transfer: {submitted.details}"
        )

    task_id = submitted.globus_task_id
    started_at = monotonic_func()
    while True:
        polled = poll_func(task_id, dry_run=dry_run)
        if polled.status == "completed":
            return task_id
        if polled.status in {"failed", "canceled"}:
            raise SnapshotPublicationError(
                f"Database snapshot transfer {task_id} ended as "
                f"{polled.status}: {polled.details}"
            )
        elapsed = monotonic_func() - started_at
        if elapsed >= sync.poll_timeout_seconds:
            raise SnapshotPublicationError(
                f"Timed out waiting for database snapshot transfer {task_id}"
            )
        sleep_func(min(sync.poll_interval_seconds, sync.poll_timeout_seconds - elapsed))


def publish_reconciled_snapshot(
    config: SnapshotPublicationConfig,
    *,
    transfer: bool = False,
    dry_run: bool = False,
    submit_func: Callable[..., TransferSubmitResult] = submit_transfer,
    poll_func: Callable[..., TransferPollResult] = poll_task,
    sleep_func: Callable[[float], None] = time.sleep,
    monotonic_func: Callable[[], float] = time.monotonic,
) -> SnapshotPublicationResult:
    """Snapshot first, reconcile that exact image, then atomically publish it."""
    publish_path = config.ceres_publish_path
    publish_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{publish_path.name}.candidate.",
        suffix=".sqlite3",
        dir=publish_path.parent,
        delete=False,
    ) as temporary:
        candidate_path = Path(temporary.name)
    candidate_path.unlink()

    try:
        create_sqlite_snapshot(config.result_sync.db_path, candidate_path)
        conn = open_db(candidate_path, readonly=True)
        try:
            require_result_sync_schema(conn)
            report = reconcile_result_syncs(conn, config.result_sync)
        finally:
            conn.close()

        if not report.ready or dry_run:
            return SnapshotPublicationResult(
                snapshot_path=None,
                reconciliation=report,
            )

        candidate_path.replace(publish_path)
        task_id = None
        if transfer:
            task_id = _transfer_snapshot(
                config,
                dry_run=False,
                submit_func=submit_func,
                poll_func=poll_func,
                sleep_func=sleep_func,
                monotonic_func=monotonic_func,
            )
        return SnapshotPublicationResult(
            snapshot_path=publish_path,
            reconciliation=report,
            globus_task_id=task_id,
        )
    finally:
        candidate_path.unlink(missing_ok=True)
