"""Reusable validation for manifest-backed pipeline run artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ArtifactValidationError(ValueError):
    """Raised when a run bundle is not safe to promote or synchronize."""

    def __init__(self, message: str, *, outcome: str = "fail") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class ValidatedRunBundle:
    """Loaded run metadata and the verified artifact location.
    ``items`` holds successfully-processed manifest items; failed items are
    reported in ``skipped_items`` when ``allow_partial=True``.
    """

    run_report: dict[str, Any]
    manifest: dict[str, Any]
    items: list[dict[str, Any]]
    artifacts_root: Path
    skipped_items: tuple[dict[str, Any], ...] = ()


_RUN_STATUS_MAP = {
    "success": "success",
    "partial": "partial_success",
    "partial_success": "partial_success",
    "failed": "failed",
    "canceled": "canceled",
    "skipped": "skipped",
}


def sha256_file(path: Path) -> str:
    """Return a pipeline-formatted SHA-256 checksum for ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ArtifactValidationError(f"{label} must not be a symbolic link: {path}")
    if not path.is_file():
        raise ArtifactValidationError(f"{label} not found in {path.parent}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"unable to read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{label} must contain a JSON object: {path}")
    return value


def _require_identity(
    value: Mapping[str, Any],
    *,
    label: str,
    run_id: str,
    batch_id: str,
    stage: str,
) -> None:
    expected = {
        "run_id": run_id,
        "batch_id": batch_id,
        "stage": stage,
    }
    mismatches = [
        f"{field}={value.get(field)!r} (expected {expected_value!r})"
        for field, expected_value in expected.items()
        if value.get(field) != expected_value
    ]
    if mismatches:
        raise ArtifactValidationError(
            f"{label} identity mismatch: " + ", ".join(mismatches)
        )


def _safe_artifact_path(artifacts_root: Path, relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ArtifactValidationError("artifact path must be a non-empty string")

    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ArtifactValidationError(
            f"artifact path must stay below the bundle artifacts directory: {relative_path!r}"
        )

    artifact_path = artifacts_root / relative
    current = artifacts_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactValidationError(
                f"artifact path must not contain symbolic links: {artifact_path}"
            )
    if not artifact_path.is_file():
        raise ArtifactValidationError(f"artifact missing on disk: {artifact_path}")
    return artifact_path


def validate_promotable_run_bundle(
    run_dir: Path,
    *,
    artifacts_root: Path | None = None,
    allow_partial: bool = False,
) -> ValidatedRunBundle:
    """
    Load and validate a successful, manifest-backed pipeline run.

    ``artifacts_root`` overrides the location recorded in the manifest. This
    allows the same validation to be applied after artifacts are copied to a
    different storage endpoint.

    ``allow_partial`` accepts runs with ``exit_code == 1`` and failed items,
    promoting only successful ones. Per-item artifacts require all items to
    succeed; batch-level artifacts can promote with some items missing.
    """
    run_report_path = run_dir / "run_report.json"
    if not run_report_path.exists():
        raise ArtifactValidationError(f"run_report.json not found in {run_dir}")

    run_report = json.loads(run_report_path.read_text())
    exit_code = run_report.get("exit_code")
    ok_exit_codes = {0, 1} if allow_partial else {0}
    if exit_code not in ok_exit_codes:
        raise ArtifactValidationError(
            f"run_report exit_code={exit_code} (need {sorted(ok_exit_codes)})",
            outcome="skip",
        )

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise ArtifactValidationError(f"manifest.json not found in {run_dir}")

    manifest = json.loads(manifest_path.read_text())
    items = manifest.get("items", [])
    if not items:
        raise ArtifactValidationError("manifest has no items")

    failed_items = [item for item in items if item.get("status") != "ok"]
    ok_items = [item for item in items if item.get("status") == "ok"]

    if failed_items and not allow_partial:
        details = [
            f"{len(failed_items)}/{len(items)} items failed — 100% success required"
        ]
        for item in failed_items:
            details.append(
                f"  FAILED: {item.get('image_id')} — "
                f"{item.get('error', {}).get('message', '')}"
            )
        raise ArtifactValidationError("\n".join(details), outcome="skip")

    if not ok_items:
        raise ArtifactValidationError("no successful items to promote", outcome="skip")

    resolved_artifacts_root = (
        Path(artifacts_root)
        if artifacts_root is not None
        else Path(manifest.get("artifacts_root", run_dir / "artifacts"))
    )

    for item in ok_items:
        image_id = item.get("image_id", "<unknown>")
        artifacts = item.get("artifacts") or {}
        checksums = item.get("checksum") or {}

        if not artifacts:
            raise ArtifactValidationError(
                f"item {image_id} has no artifacts in manifest"
            )

        for artifact_key, artifact_rel in artifacts.items():
            if not artifact_rel:
                raise ArtifactValidationError(
                    f"item {image_id} key '{artifact_key}' has no path"
                )

            artifact_path = resolved_artifacts_root / artifact_rel
            if not artifact_path.exists():
                raise ArtifactValidationError(
                    f"artifact missing on disk: {artifact_path}"
                )

            expected_checksum = checksums.get(artifact_key)
            if expected_checksum:
                actual_checksum = sha256_file(artifact_path)
                if actual_checksum != expected_checksum:
                    raise ArtifactValidationError(
                        f"checksum mismatch for {image_id} [{artifact_key}]\n"
                        f"  expected: {expected_checksum}\n"
                        f"  actual:   {actual_checksum}"
                    )

    return ValidatedRunBundle(
        run_report=run_report,
        manifest=manifest,
        items=ok_items,
        artifacts_root=resolved_artifacts_root,
        skipped_items=tuple(failed_items),
    )


def validate_transferred_run_bundle(
    run_dir: Path,
    *,
    run_id: str,
    batch_id: str,
    stage: str,
    run_status: str,
    ended_at: str,
) -> ValidatedRunBundle:
    """Validate one Ceres run bundle against its registered sync identity.

    Unlike promotion validation, this accepts unsuccessful runs so their
    diagnostic bundles can be synchronized. Every artifact declared by the
    manifest is still required and its checksum and size are checked when the
    manifest provides them. Atlas paths embedded in metadata are never trusted;
    artifacts are resolved only below ``run_dir/artifacts``.
    """
    if run_dir.name != run_id:
        raise ArtifactValidationError(
            f"run bundle directory {run_dir.name!r} does not match run_id {run_id!r}"
        )
    if run_dir.is_symlink():
        raise ArtifactValidationError(
            f"run bundle directory must not be a symbolic link: {run_dir}"
        )
    if not run_dir.is_dir():
        raise ArtifactValidationError(f"run bundle directory not found: {run_dir}")

    run_report = _load_json_object(run_dir / "run_report.json", "run_report.json")
    manifest = _load_json_object(run_dir / "manifest.json", "manifest.json")
    _require_identity(
        run_report,
        label="run_report.json",
        run_id=run_id,
        batch_id=batch_id,
        stage=stage,
    )
    _require_identity(
        manifest,
        label="manifest.json",
        run_id=run_id,
        batch_id=batch_id,
        stage=stage,
    )

    report_status = _RUN_STATUS_MAP.get(run_report.get("status"))
    expected_status = _RUN_STATUS_MAP.get(run_status)
    if report_status is None:
        raise ArtifactValidationError(
            f"run_report.json has unsupported status {run_report.get('status')!r}"
        )
    if expected_status is None or report_status != expected_status:
        raise ArtifactValidationError(
            "run_report.json status mismatch: "
            f"{run_report.get('status')!r} (expected {run_status!r})"
        )
    if report_status == "success" and run_report.get("exit_code") != 0:
        raise ArtifactValidationError(
            "successful run_report.json must have exit_code 0"
        )
    if run_report.get("ended_at") != ended_at:
        raise ArtifactValidationError(
            "run_report.json ended_at mismatch: "
            f"{run_report.get('ended_at')!r} (expected {ended_at!r})"
        )

    items = manifest.get("items")
    if not isinstance(items, list):
        raise ArtifactValidationError("manifest items must be an array")
    if report_status == "success" and not items:
        raise ArtifactValidationError("successful run manifest has no items")

    artifacts_root = run_dir / "artifacts"
    if artifacts_root.is_symlink():
        raise ArtifactValidationError(
            f"artifacts directory must not be a symbolic link: {artifacts_root}"
        )
    if not artifacts_root.is_dir():
        raise ArtifactValidationError(f"artifacts directory not found: {artifacts_root}")

    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ArtifactValidationError(f"manifest item {index} must be an object")
        image_id = item.get("image_id", f"item-{index}")
        item_status = item.get("status")
        if report_status == "success" and item_status != "ok":
            raise ArtifactValidationError(
                f"successful run has non-ok manifest item {image_id!r}: {item_status!r}"
            )

        artifacts = item.get("artifacts") or {}
        checksums = item.get("checksum") or {}
        sizes = item.get("size_bytes") or {}
        if not isinstance(artifacts, Mapping):
            raise ArtifactValidationError(
                f"item {image_id!r} artifacts must be an object"
            )
        if not isinstance(checksums, Mapping):
            raise ArtifactValidationError(
                f"item {image_id!r} checksum must be an object"
            )
        if not isinstance(sizes, Mapping):
            raise ArtifactValidationError(
                f"item {image_id!r} size_bytes must be an object"
            )
        if report_status == "success" and not artifacts:
            raise ArtifactValidationError(
                f"item {image_id!r} has no artifacts in manifest"
            )

        for artifact_key, artifact_rel in artifacts.items():
            artifact_path = _safe_artifact_path(artifacts_root, artifact_rel)
            expected_size = sizes.get(artifact_key)
            if expected_size is not None:
                if isinstance(expected_size, bool) or not isinstance(expected_size, int):
                    raise ArtifactValidationError(
                        f"invalid size for {image_id} [{artifact_key}]: {expected_size!r}"
                    )
                actual_size = artifact_path.stat().st_size
                if actual_size != expected_size:
                    raise ArtifactValidationError(
                        f"size mismatch for {image_id} [{artifact_key}]: "
                        f"expected {expected_size}, actual {actual_size}"
                    )

            expected_checksum = checksums.get(artifact_key)
            if expected_checksum:
                actual_checksum = sha256_file(artifact_path)
                if actual_checksum != expected_checksum:
                    raise ArtifactValidationError(
                        f"checksum mismatch for {image_id} [{artifact_key}]\n"
                        f"  expected: {expected_checksum}\n"
                        f"  actual:   {actual_checksum}"
                    )

    return ValidatedRunBundle(
        run_report=run_report,
        manifest=manifest,
        items=items,
        artifacts_root=artifacts_root,
    )
