"""Validation and atomic publication for complete ``seg_to_cut`` batches."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from orchestrator.artifact_validation import ArtifactValidationError, sha256_file


ARTIFACT_KEYS = (
    "cropout_path",
    "cutout_path",
    "mask_path",
    "metadata_path",
)


@dataclass(frozen=True)
class ValidatedCutoutBatch:
    batch_id: str
    run_id: str
    artifacts: tuple[tuple[Path, str], ...]
    cutout_count: int


@dataclass(frozen=True)
class CutoutPublicationResult:
    destination: Path
    cutout_count: int
    artifact_count: int
    published_paths: tuple[Path, ...]


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactValidationError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"unable to read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{label} must contain a JSON object")
    return value


def _artifact_path(root: Path, relative_value: Any) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise ArtifactValidationError("artifact path must be a non-empty string")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ArtifactValidationError(
            f"artifact path must stay below the artifacts directory: {relative_value!r}"
        )
    path = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactValidationError(f"artifact path contains a symbolic link: {path}")
    if not path.is_file():
        raise ArtifactValidationError(f"artifact missing on disk: {path}")
    return path


def _read_image(path: Path, flags: int, label: str) -> np.ndarray:
    decoded = cv2.imread(str(path), flags)
    if decoded is None:
        raise ArtifactValidationError(f"unable to decode {label}: {path}")
    return decoded


def _resolved_class_id(metadata: dict[str, Any]) -> Any:
    category = metadata.get("category")
    if not isinstance(category, dict):
        return None
    cultivar_class_id = category.get("cultivar_class_id")
    return cultivar_class_id if cultivar_class_id is not None else category.get("class_id")


def _validate_decoded_set(
    *,
    cutout_id: str,
    batch_id: str,
    cropout_path: Path,
    cutout_path: Path,
    mask_path: Path,
    metadata_path: Path,
) -> None:
    cropout = _read_image(cropout_path, cv2.IMREAD_UNCHANGED, "cropout JPEG")
    cutout = _read_image(cutout_path, cv2.IMREAD_UNCHANGED, "cutout PNG")
    mask = _read_image(mask_path, cv2.IMREAD_UNCHANGED, "mask PNG")
    metadata = _load_object(metadata_path, "cutout metadata")

    if cropout.dtype != np.uint8 or cropout.ndim != 3 or cropout.shape[2] != 3:
        raise ArtifactValidationError(f"{cutout_id}: cropout must decode as uint8 RGB")
    height, width = cropout.shape[:2]
    if cutout.dtype != np.uint8 or cutout.shape != (height, width, 4):
        raise ArtifactValidationError(f"{cutout_id}: cutout must decode as matching RGBA")
    if mask.dtype != np.uint8 or mask.shape != (height, width):
        raise ArtifactValidationError(f"{cutout_id}: mask must be matching single-channel uint8")

    class_id = _resolved_class_id(metadata)
    if not isinstance(class_id, int) or isinstance(class_id, bool) or not 1 <= class_id <= 255:
        raise ArtifactValidationError(f"{cutout_id}: metadata has no valid resolved class ID")
    values = set(int(value) for value in np.unique(mask))
    if class_id not in values or not values <= {0, class_id}:
        raise ArtifactValidationError(
            f"{cutout_id}: mask values {sorted(values)} do not match class {class_id}"
        )

    target = mask == class_id
    alpha = cutout[..., 3]
    if not np.array_equal(alpha, np.where(target, 255, 0).astype(np.uint8)):
        raise ArtifactValidationError(f"{cutout_id}: alpha channel does not match the mask")
    if np.any(cutout[~target, :3] != 0):
        raise ArtifactValidationError(f"{cutout_id}: non-target cutout RGB must be black")

    expected_metadata = {
        "batch_id": batch_id,
        "cutout_id": cutout_id,
        "cutout_height": height,
        "cutout_width": width,
    }
    for field, expected in expected_metadata.items():
        if metadata.get(field) != expected:
            raise ArtifactValidationError(
                f"{cutout_id}: metadata {field}={metadata.get(field)!r}, expected {expected!r}"
            )
    image_id = metadata.get("image_id")
    cutout_num = metadata.get("cutout_num")
    if (
        not isinstance(image_id, str)
        or not image_id
        or not isinstance(cutout_num, int)
        or isinstance(cutout_num, bool)
        or f"{image_id}_{cutout_num}" != cutout_id
    ):
        raise ArtifactValidationError(
            f"{cutout_id}: metadata image_id and cutout_num do not match cutout_id"
        )


def validate_cutout_run_bundle(run_dir: str | Path) -> ValidatedCutoutBatch:
    """Validate a complete manifest-backed ``seg_to_cut`` run bundle."""
    root = Path(run_dir)
    report = _load_object(root / "run_report.json", "run_report.json")
    manifest = _load_object(root / "manifest.json", "manifest.json")
    if report.get("stage") != "seg_to_cut" or manifest.get("stage") != "seg_to_cut":
        raise ArtifactValidationError("run report and manifest must have stage='seg_to_cut'")
    if report.get("exit_code") != 0:
        raise ArtifactValidationError(
            f"seg_to_cut run exit_code={report.get('exit_code')}; publication requires 0",
            outcome="skip",
        )

    batch_id = report.get("batch_id")
    run_id = report.get("run_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise ArtifactValidationError("run report has no batch_id")
    if not isinstance(run_id, str) or not run_id:
        raise ArtifactValidationError("run report has no run_id")
    if manifest.get("batch_id") != batch_id or manifest.get("run_id") != run_id:
        raise ArtifactValidationError("manifest identity does not match the run report")

    artifacts_root = Path(manifest.get("artifacts_root", root / "artifacts"))
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ArtifactValidationError("manifest has no items")
    invalid_statuses = [
        item.get("status") if isinstance(item, dict) else type(item).__name__
        for item in items
        if not isinstance(item, dict) or item.get("status") not in {"ok", "skipped", "failed"}
    ]
    if invalid_statuses:
        raise ArtifactValidationError(
            f"manifest contains unsupported item statuses: {invalid_statuses}"
        )
    failed = [item for item in items if item.get("status") == "failed"]
    if failed:
        raise ArtifactValidationError("manifest contains failed cutouts", outcome="skip")
    successful = [item for item in items if item.get("status") == "ok"]
    if not successful:
        raise ArtifactValidationError("manifest has no successful cutouts", outcome="skip")

    outputs = report.get("outputs") or {}
    counts = outputs.get("counts") or {}
    expected_counts = {
        "n_units_succeeded": len(successful),
        "n_units_failed": len(failed),
        "n_units_skipped": sum(item.get("status") == "skipped" for item in items),
    }
    for field, expected in expected_counts.items():
        if counts.get(field) != expected:
            raise ArtifactValidationError(
                f"run report {field}={counts.get(field)!r}, expected {expected}"
            )

    collected: list[tuple[Path, str]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for item in successful:
        cutout_id = item.get("image_id")
        if not isinstance(cutout_id, str) or not cutout_id or cutout_id in seen_ids:
            raise ArtifactValidationError(f"invalid or duplicate cutout ID: {cutout_id!r}")
        seen_ids.add(cutout_id)
        artifacts = item.get("artifacts") or {}
        checksums = item.get("checksum") or {}
        sizes = item.get("size_bytes") or {}
        if set(artifacts) != set(ARTIFACT_KEYS):
            raise ArtifactValidationError(
                f"{cutout_id}: expected exactly four artifact keys {ARTIFACT_KEYS}"
            )
        if set(checksums) != set(ARTIFACT_KEYS) or set(sizes) != set(ARTIFACT_KEYS):
            raise ArtifactValidationError(
                f"{cutout_id}: every artifact requires a checksum and size"
            )

        expected_names = {
            "cropout_path": f"{cutout_id}.jpg",
            "cutout_path": f"{cutout_id}.png",
            "mask_path": f"{cutout_id}_mask.png",
            "metadata_path": f"{cutout_id}.json",
        }
        paths: dict[str, Path] = {}
        for key in ARTIFACT_KEYS:
            path = _artifact_path(artifacts_root, artifacts[key])
            if path.name != expected_names[key] or path.name in seen_names:
                raise ArtifactValidationError(
                    f"{cutout_id}: invalid or duplicate filename for {key}: {path.name!r}"
                )
            if sizes[key] != path.stat().st_size:
                raise ArtifactValidationError(f"{cutout_id}: size mismatch for {key}")
            if checksums[key] != sha256_file(path):
                raise ArtifactValidationError(f"{cutout_id}: checksum mismatch for {key}")
            seen_names.add(path.name)
            paths[key] = path
            collected.append((path, path.name))

        _validate_decoded_set(
            cutout_id=cutout_id,
            batch_id=batch_id,
            cropout_path=paths["cropout_path"],
            cutout_path=paths["cutout_path"],
            mask_path=paths["mask_path"],
            metadata_path=paths["metadata_path"],
        )

    return ValidatedCutoutBatch(
        batch_id=batch_id,
        run_id=str(run_id),
        artifacts=tuple(collected),
        cutout_count=len(successful),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_cutout_batch(
    run_dir: str | Path,
    destination: str | Path,
) -> CutoutPublicationResult:
    """Validate and atomically publish one batch without replacing existing data."""
    validated = validate_cutout_run_bundle(run_dir)
    dest = Path(destination)
    if dest.name != validated.batch_id:
        raise ArtifactValidationError(
            f"destination batch {dest.name!r} does not match {validated.batch_id!r}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        raise ArtifactValidationError(f"refusing to replace existing batch: {dest}")

    temporary = dest.parent / f".{dest.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    published = False
    try:
        for source, name in validated.artifacts:
            target = temporary / name
            shutil.copy2(source, target)
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(temporary)
        if dest.exists() or dest.is_symlink():
            raise ArtifactValidationError(f"refusing to replace existing batch: {dest}")
        temporary.rename(dest)
        published = True
        _fsync_directory(dest.parent)
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)

    paths = tuple(dest / name for _, name in validated.artifacts)
    return CutoutPublicationResult(
        destination=dest,
        cutout_count=validated.cutout_count,
        artifact_count=len(paths),
        published_paths=paths,
    )
