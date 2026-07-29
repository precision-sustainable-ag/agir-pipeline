"""Validation and atomic I/O for Atlas-to-Ceres result-sync requests.

Atlas stage jobs write validated requests to an outbox.  The Ceres result-sync
process loads and validates the same requests before using any endpoint or path
to submit Globus transfers.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

REQUEST_VERSION = "1.0"
SOURCE_SITE = "ATLAS"
DESTINATION_SITE = "CERES"

VALID_RUN_STATUSES = frozenset(
    {
        "success",
        "partial",
        "failed",
        "canceled",
        "skipped",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "request_version",
        "request_created_at",
        "source_site",
        "destination_site",
        "run",
        "promotion",
        "run_bundle",
    }
)
_RUN_FIELDS = frozenset({"run_id", "batch_id", "stage", "status", "ended_at"})
_PROMOTION_FIELDS = frozenset({"succeeded", "promoted_at"})
_RUN_BUNDLE_FIELDS = frozenset(
    {
        "src_endpoint",
        "dst_endpoint",
        "src_path",
        "dst_path",
        "recursive",
    }
)


class ResultSyncRequestError(ValueError):
    """Raised when a result-sync request violates the request contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultSyncRequestError(f"{field} must be an object")
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: Collection[str],
    field: str,
) -> None:
    expected_set = set(expected)
    missing = sorted(expected_set - set(value))
    unexpected = sorted(set(value) - expected_set)
    if missing:
        raise ResultSyncRequestError(f"{field} missing required fields: {missing}")
    if unexpected:
        raise ResultSyncRequestError(f"{field} has unexpected fields: {unexpected}")


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultSyncRequestError(f"{field} must be a non-empty string")
    return value


def _validate_utc_datetime(value: Any, field: str) -> datetime:
    text = _require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResultSyncRequestError(f"{field} must be an ISO-8601 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ResultSyncRequestError(f"{field} must use UTC")
    return parsed


def _validate_uuid(value: Any, field: str) -> str:
    text = _require_nonempty_string(value, field)
    try:
        uuid.UUID(text)
    except ValueError as exc:
        raise ResultSyncRequestError(f"{field} must be a UUID") from exc
    return text


def _validate_transfer_path(value: Any, field: str) -> str:
    text = _require_nonempty_string(value, field)
    if "\x00" in text:
        raise ResultSyncRequestError(f"{field} must not contain a NUL byte")

    path = PurePosixPath(text)
    if not path.is_absolute() or len(path.parts) < 3 or path.parts[1] != "90daydata":
        raise ResultSyncRequestError(f"{field} must be an absolute path below /90daydata")
    if ".." in path.parts:
        raise ResultSyncRequestError(f"{field} must not contain '..'")
    return text


def _validate_endpoint(
    value: Any,
    field: str,
    allowed: Collection[str] | None,
) -> str:
    endpoint = _require_nonempty_string(value, field)
    if allowed is not None and endpoint not in allowed:
        raise ResultSyncRequestError(
            f"{field} endpoint {endpoint!r} is not in the configured allowlist"
        )
    return endpoint


def validate_result_sync_request(
    request: Mapping[str, Any],
    *,
    allowed_source_endpoints: Collection[str] | None = None,
    allowed_destination_endpoints: Collection[str] | None = None,
) -> dict[str, Any]:
    """Validate and return a detached copy of one result-sync request.

    Endpoint allowlists are optional so Atlas can validate request structure
    while writing.  Ceres should always provide its configured endpoint
    allowlists before trusting a request received from another system.
    """
    request = _require_object(request, "request")
    _require_exact_fields(request, _TOP_LEVEL_FIELDS, "request")

    if request["request_version"] != REQUEST_VERSION:
        raise ResultSyncRequestError(f"request_version must be {REQUEST_VERSION!r}")
    request_created_at = _validate_utc_datetime(
        request["request_created_at"],
        "request_created_at",
    )
    if request["source_site"] != SOURCE_SITE:
        raise ResultSyncRequestError(f"source_site must be {SOURCE_SITE!r}")
    if request["destination_site"] != DESTINATION_SITE:
        raise ResultSyncRequestError(f"destination_site must be {DESTINATION_SITE!r}")

    run = _require_object(request["run"], "run")
    _require_exact_fields(run, _RUN_FIELDS, "run")
    _validate_uuid(run["run_id"], "run.run_id")
    _require_nonempty_string(run["batch_id"], "run.batch_id")
    _require_nonempty_string(run["stage"], "run.stage")
    if run["status"] not in VALID_RUN_STATUSES:
        raise ResultSyncRequestError(f"run.status must be one of {sorted(VALID_RUN_STATUSES)}")
    run_ended_at = _validate_utc_datetime(run["ended_at"], "run.ended_at")
    if request_created_at < run_ended_at:
        raise ResultSyncRequestError(
            "request_created_at must not be earlier than run.ended_at"
        )

    promotion = _require_object(request["promotion"], "promotion")
    _require_exact_fields(promotion, _PROMOTION_FIELDS, "promotion")
    promotion_succeeded = promotion["succeeded"]
    if not isinstance(promotion_succeeded, bool):
        raise ResultSyncRequestError("promotion.succeeded must be a boolean")
    if promotion_succeeded and run["status"] != "success":
        raise ResultSyncRequestError("promotion cannot succeed unless run.status is 'success'")
    promoted_at = promotion["promoted_at"]
    if promotion_succeeded:
        parsed_promoted_at = _validate_utc_datetime(
            promoted_at,
            "promotion.promoted_at",
        )
        if parsed_promoted_at < run_ended_at:
            raise ResultSyncRequestError(
                "promotion.promoted_at must not be earlier than run.ended_at"
            )
        if request_created_at < parsed_promoted_at:
            raise ResultSyncRequestError(
                "request_created_at must not be earlier than promotion.promoted_at"
            )
    elif promoted_at is not None:
        raise ResultSyncRequestError(
            "promotion.promoted_at must be null when promotion did not succeed"
        )

    run_bundle = _require_object(request["run_bundle"], "run_bundle")
    _require_exact_fields(run_bundle, _RUN_BUNDLE_FIELDS, "run_bundle")
    src_endpoint = _validate_endpoint(
        run_bundle["src_endpoint"],
        "run_bundle.src_endpoint",
        allowed_source_endpoints,
    )
    dst_endpoint = _validate_endpoint(
        run_bundle["dst_endpoint"],
        "run_bundle.dst_endpoint",
        allowed_destination_endpoints,
    )
    if src_endpoint == dst_endpoint:
        raise ResultSyncRequestError("run_bundle source and destination endpoints must differ")
    _validate_transfer_path(run_bundle["src_path"], "run_bundle.src_path")
    _validate_transfer_path(run_bundle["dst_path"], "run_bundle.dst_path")
    if not isinstance(run_bundle["recursive"], bool):
        raise ResultSyncRequestError("run_bundle.recursive must be a boolean")

    # JSON round-tripping both detaches nested values from the caller and
    # guarantees that the validated request can be serialized to the outbox.
    try:
        return json.loads(json.dumps(request))
    except (TypeError, ValueError) as exc:
        raise ResultSyncRequestError("request must contain only JSON values") from exc


def build_result_sync_request(
    *,
    run_report: Mapping[str, Any],
    promotion_succeeded: bool,
    promoted_at: str | None,
    src_endpoint: str,
    dst_endpoint: str,
    src_path: str | Path,
    dst_path: str | Path,
    request_created_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate one request from a completed Atlas run report."""
    report = _require_object(run_report, "run_report")
    required_report_fields = ("run_id", "batch_id", "stage", "status", "ended_at")
    missing = [field for field in required_report_fields if field not in report]
    if missing:
        raise ResultSyncRequestError(
            f"run_report missing required result-sync fields: {missing}"
        )

    request = {
        "request_version": REQUEST_VERSION,
        "request_created_at": request_created_at or _utc_now(),
        "source_site": SOURCE_SITE,
        "destination_site": DESTINATION_SITE,
        "run": {
            "run_id": report["run_id"],
            "batch_id": report["batch_id"],
            "stage": report["stage"],
            "status": report["status"],
            "ended_at": report["ended_at"],
        },
        "promotion": {
            "succeeded": promotion_succeeded,
            "promoted_at": promoted_at,
        },
        "run_bundle": {
            "src_endpoint": src_endpoint,
            "dst_endpoint": dst_endpoint,
            "src_path": str(src_path),
            "dst_path": str(dst_path),
            "recursive": True,
        },
    }
    return validate_result_sync_request(request)


def load_result_sync_request(
    path: str | Path,
    *,
    allowed_source_endpoints: Collection[str] | None = None,
    allowed_destination_endpoints: Collection[str] | None = None,
) -> dict[str, Any]:
    """Load and validate one result-sync request from disk."""
    request_path = Path(path)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultSyncRequestError(
            f"Unable to read result-sync request {request_path}: {exc}"
        ) from exc

    return validate_result_sync_request(
        request,
        allowed_source_endpoints=allowed_source_endpoints,
        allowed_destination_endpoints=allowed_destination_endpoints,
    )


def write_result_sync_request(
    path: str | Path,
    request: Mapping[str, Any],
) -> Path:
    """Validate and atomically write one Atlas outbox request.

    The temporary file is written in the destination directory so
    :func:`os.replace` remains an atomic same-filesystem rename.
    """
    validated = validate_result_sync_request(request)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        existing = load_result_sync_request(destination)
        if existing == validated:
            return destination
        raise ResultSyncRequestError(
            f"Refusing to replace conflicting result-sync request {destination}"
        )

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(validated, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return destination
