"""Failure-safe encoding and validation of four-file cutout sets."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from . import ERROR_ARTIFACT_WRITE
from .contracts import CutoutArtifactSet
from .errors import SegToCutArtifactError

JPEG_QUALITY = 100
_SAFE_CUTOUT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def artifact_paths(output_dir: str | Path, cutout_id: str) -> CutoutArtifactSet:
    """Return deterministic final paths without writing anything."""
    _validate_cutout_id(cutout_id)
    root = Path(output_dir)
    return CutoutArtifactSet(
        cropout_path=root / f"{cutout_id}.jpg",
        cutout_path=root / f"{cutout_id}.png",
        mask_path=root / f"{cutout_id}_mask.png",
        metadata_path=root / f"{cutout_id}.json",
    )


def _artifact_error(message: str, **context: Any) -> SegToCutArtifactError:
    return SegToCutArtifactError(ERROR_ARTIFACT_WRITE, message, **context)


def _validate_cutout_id(cutout_id: str) -> None:
    if not isinstance(cutout_id, str) or not _SAFE_CUTOUT_ID.fullmatch(cutout_id):
        raise _artifact_error(
            "cutout_id must contain only letters, numbers, '.', '_' and '-'",
            cutout_id=cutout_id,
        )


def _resolved_metadata_class_id(metadata: Mapping[str, Any]) -> Any:
    category = metadata.get("category")
    if not isinstance(category, Mapping):
        return None
    cultivar_class_id = category.get("cultivar_class_id")
    return cultivar_class_id if cultivar_class_id is not None else category.get("class_id")


def _validate_inputs(
    rgb_crop: NDArray[np.uint8],
    cleaned_mask: NDArray[np.uint8],
    *,
    expected_class_id: int,
    cutout_id: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_cutout_id(cutout_id)
    if (
        not isinstance(expected_class_id, int)
        or isinstance(expected_class_id, bool)
        or not 1 <= expected_class_id <= 255
    ):
        raise _artifact_error("expected_class_id must be an integer in 1..255")
    if (
        not isinstance(rgb_crop, np.ndarray)
        or rgb_crop.dtype != np.uint8
        or rgb_crop.ndim != 3
        or rgb_crop.shape[2] != 3
        or not rgb_crop.size
    ):
        raise _artifact_error("rgb_crop must be a non-empty HxWx3 uint8 RGB array")
    if (
        not isinstance(cleaned_mask, np.ndarray)
        or cleaned_mask.dtype != np.uint8
        or cleaned_mask.ndim != 2
        or cleaned_mask.shape != rgb_crop.shape[:2]
    ):
        raise _artifact_error("cleaned_mask must be uint8 and match the RGB crop")
    values = set(int(value) for value in np.unique(cleaned_mask))
    if not values <= {0, expected_class_id} or expected_class_id not in values:
        raise _artifact_error(
            "cleaned_mask must contain target pixels and only the expected class value",
            mask_values=sorted(values),
            expected_class_id=expected_class_id,
        )
    if not isinstance(metadata, Mapping):
        raise _artifact_error("metadata must be a JSON object")
    try:
        normalized = json.loads(json.dumps(metadata, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise _artifact_error(f"metadata is not finite JSON: {exc}") from exc
    height, width = cleaned_mask.shape
    expected_fields = {
        "cutout_id": cutout_id,
        "cutout_height": height,
        "cutout_width": width,
    }
    for field, expected in expected_fields.items():
        if type(normalized.get(field)) is not type(expected) or normalized.get(field) != expected:
            raise _artifact_error(
                f"metadata {field} must equal {expected!r}",
                field=field,
            )
    if normalized.get("validated") is not False:
        raise _artifact_error("metadata validated must equal False", field="validated")
    metadata_class_id = _resolved_metadata_class_id(normalized)
    if (
        not isinstance(metadata_class_id, int)
        or isinstance(metadata_class_id, bool)
        or metadata_class_id != expected_class_id
    ):
        raise _artifact_error("metadata resolved class value does not match the mask")
    return normalized


def _encode_image(
    extension: str, image: NDArray[np.uint8], params: list[int] | None = None
) -> bytes:
    ok, encoded = cv2.imencode(extension, image, params or [])
    if not ok:
        raise _artifact_error(f"OpenCV failed to encode {extension}")
    return encoded.tobytes()


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _publish_temp_artifact(temporary_path: Path, final_path: Path) -> None:
    """Create a final name for one complete file without replacing anything."""
    os.link(temporary_path, final_path)


def _read_image(path: Path, *, description: str) -> NDArray[np.uint8]:
    encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise _artifact_error(f"failed to decode temporary {description}")
    return decoded


def _validate_temporary_artifacts(
    temporary: CutoutArtifactSet,
    *,
    rgb_crop: NDArray[np.uint8],
    cleaned_mask: NDArray[np.uint8],
    expected_rgba: NDArray[np.uint8],
    metadata: dict[str, Any],
) -> None:
    cropout = _read_image(temporary.cropout_path, description="cropout JPEG")
    if cropout.dtype != np.uint8 or cropout.shape != rgb_crop.shape:
        raise _artifact_error("decoded cropout JPEG has the wrong format or dimensions")

    cutout = _read_image(temporary.cutout_path, description="cutout PNG")
    expected_bgra = cv2.cvtColor(expected_rgba, cv2.COLOR_RGBA2BGRA)
    if (
        cutout.dtype != np.uint8
        or cutout.shape != expected_bgra.shape
        or not np.array_equal(cutout, expected_bgra)
    ):
        raise _artifact_error("decoded cutout PNG does not match the RGBA contract")

    mask = _read_image(temporary.mask_path, description="mask PNG")
    if mask.dtype != np.uint8 or mask.ndim != 2 or not np.array_equal(mask, cleaned_mask):
        raise _artifact_error("decoded mask PNG does not match the cleaned mask")

    try:
        decoded_metadata = json.loads(temporary.metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _artifact_error(f"failed to decode temporary metadata JSON: {exc}") from exc
    if decoded_metadata != metadata:
        raise _artifact_error("decoded metadata JSON does not match the supplied metadata")


def write_cutout_artifacts(
    output_dir: str | Path,
    *,
    cutout_id: str,
    rgb_crop: NDArray[np.uint8],
    cleaned_mask: NDArray[np.uint8],
    expected_class_id: int,
    metadata: Mapping[str, Any],
) -> CutoutArtifactSet:
    """Write, reopen, validate, and publish one complete four-file cutout set.

    Existing final artifacts are never overwritten. If writing, validation, or
    publication fails, every temporary file and every final file published by
    this call is removed.
    """
    normalized_metadata = _validate_inputs(
        rgb_crop,
        cleaned_mask,
        expected_class_id=expected_class_id,
        cutout_id=cutout_id,
        metadata=metadata,
    )
    final = artifact_paths(output_dir, cutout_id)
    existing = [str(path) for path in final.paths if path.exists()]
    if existing:
        raise _artifact_error("refusing to overwrite an existing cutout set", paths=existing)

    final.cropout_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary = CutoutArtifactSet(
        *(path.with_name(f".{path.name}.{token}.tmp") for path in final.paths)
    )
    target = cleaned_mask != 0
    black_masked_rgb = np.where(target[..., None], rgb_crop, 0).astype(np.uint8)
    rgba = np.dstack((black_masked_rgb, np.where(target, 255, 0).astype(np.uint8)))
    payloads = (
        _encode_image(
            ".jpg",
            cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
        ),
        _encode_image(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)),
        _encode_image(".png", cleaned_mask),
        (json.dumps(normalized_metadata, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
            "utf-8"
        ),
    )

    published: list[Path] = []
    try:
        for path, payload in zip(temporary.paths, payloads, strict=True):
            _write_bytes(path, payload)
        _validate_temporary_artifacts(
            temporary,
            rgb_crop=rgb_crop,
            cleaned_mask=cleaned_mask,
            expected_rgba=rgba,
            metadata=normalized_metadata,
        )
        for temporary_path, final_path in zip(temporary.paths, final.paths, strict=True):
            _publish_temp_artifact(temporary_path, final_path)
            published.append(final_path)
            temporary_path.unlink()
    except Exception as exc:
        for path in (*temporary.paths, *published):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, SegToCutArtifactError):
            raise
        raise _artifact_error(f"failed to write cutout {cutout_id!r}: {exc}") from exc
    return final
