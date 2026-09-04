"""Discover and validate ``seg_to_cut`` inputs without producing cutouts."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from stages.common.class_ids import ClassIdResolutionError, build_class_id_index

from . import (
    ERROR_CSV_INVALID,
    ERROR_DIMENSION_MISMATCH,
    ERROR_DUPLICATE_DETECTION,
    ERROR_DUPLICATE_INPUT,
    ERROR_EMPTY_BOUNDING_BOX,
    ERROR_IMAGE_INVALID,
    ERROR_IMAGE_MISSING,
    ERROR_MASK_INVALID,
    ERROR_MASK_MISSING,
    ERROR_UNKNOWN_MASK_VALUE,
)
from .config import SegToCutConfig
from .contracts import (
    BatchValidationResult,
    DetectionInput,
    PixelBoundingBox,
    ValidatedImageInput,
)
from .errors import SegToCutInputError

REQUIRED_CSV_COLUMNS = (
    "image_id",
    "bounding_box_id",
    "xmin",
    "ymin",
    "xmax",
    "ymax",
    "species_id",
)


@dataclass(frozen=True)
class _DetectionRow:
    image_id: str
    normalized_image_id: str
    bounding_box_id: int
    normalized_bbox: tuple[float, float, float, float]
    species_id: str
    cultivar_id: str | None


def _input_error(code: str, message: str, **context: Any) -> SegToCutInputError:
    return SegToCutInputError(code, message, **context)


def _normalize_image_id(value: Any, *, context: str) -> tuple[str, str]:
    image_id = "" if value is None else str(value).strip()
    if not image_id:
        raise _input_error(ERROR_CSV_INVALID, f"{context}: image_id must not be empty")
    return image_id, image_id.casefold()


def _parse_nonnegative_int(value: Any, *, field: str, context: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context}: {field} must be an integer, got {value!r}",
            field=field,
        ) from None
    if parsed < 0:
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context}: {field} must be non-negative, got {parsed}",
            field=field,
        )
    return parsed


def _parse_coordinate(value: Any, *, field: str, context: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context}: {field} must be numeric, got {value!r}",
            field=field,
        ) from None
    if not math.isfinite(parsed):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context}: {field} must be finite, got {value!r}",
            field=field,
        )
    return parsed


def load_detection_rows(path: str | Path) -> tuple[_DetectionRow, ...]:
    """Parse and stably sort georeferenced detections, rejecting duplicate identities."""

    csv_path = Path(path)
    try:
        csv_file = csv_path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise _input_error(
            ERROR_CSV_INVALID,
            f"Failed to open georeferenced CSV {csv_path}: {exc}",
            path=str(csv_path),
        ) from exc

    rows: list[_DetectionRow] = []
    identities: set[tuple[str, int]] = set()
    with csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or ())
        missing = [field for field in REQUIRED_CSV_COLUMNS if field not in fieldnames]
        if missing:
            raise _input_error(
                ERROR_CSV_INVALID,
                f"Georeferenced CSV {csv_path} is missing required column(s): "
                f"{', '.join(missing)}",
                path=str(csv_path),
            )

        for row_number, row in enumerate(reader, start=2):
            context = f"{csv_path.name} row {row_number}"
            image_id, normalized_image_id = _normalize_image_id(
                row.get("image_id"), context=context
            )
            bounding_box_id = _parse_nonnegative_int(
                row.get("bounding_box_id"), field="bounding_box_id", context=context
            )
            identity = (normalized_image_id, bounding_box_id)
            if identity in identities:
                raise _input_error(
                    ERROR_DUPLICATE_DETECTION,
                    f"{context}: duplicate detection identity {identity!r}",
                    image_id=image_id,
                    bounding_box_id=bounding_box_id,
                )
            identities.add(identity)

            normalized_bbox = tuple(
                _parse_coordinate(row.get(field), field=field, context=context)
                for field in ("xmin", "ymin", "xmax", "ymax")
            )
            species_id = (row.get("species_id") or "").strip()
            cultivar_value = row.get("cultivar_id")
            cultivar_id = (
                str(cultivar_value).strip()
                if cultivar_value is not None and str(cultivar_value).strip()
                else None
            )
            rows.append(
                _DetectionRow(
                    image_id=image_id,
                    normalized_image_id=normalized_image_id,
                    bounding_box_id=bounding_box_id,
                    normalized_bbox=normalized_bbox,
                    species_id=species_id,
                    cultivar_id=cultivar_id,
                )
            )

    rows.sort(key=lambda row: (row.normalized_image_id, row.bounding_box_id))
    return tuple(rows)


def normalized_bbox_to_pixels(
    normalized_bbox: Sequence[float],
    *,
    width: int,
    height: int,
    image_id: str = "<unknown>",
    bounding_box_id: int = -1,
) -> PixelBoundingBox:
    """Convert normalized XYXY coordinates to a clipped half-open pixel box."""

    if width <= 0 or height <= 0:
        raise _input_error(
            ERROR_IMAGE_INVALID,
            f"Image {image_id!r} has invalid dimensions {width}x{height}",
            image_id=image_id,
        )
    if len(normalized_bbox) != 4:
        raise _input_error(
            ERROR_CSV_INVALID,
            f"Detection {image_id!r}:{bounding_box_id} must have four bounding-box coordinates",
            image_id=image_id,
            bounding_box_id=bounding_box_id,
        )

    xmin, ymin, xmax, ymax = (float(value) for value in normalized_bbox)
    if not all(math.isfinite(value) for value in (xmin, ymin, xmax, ymax)):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"Detection {image_id!r}:{bounding_box_id} has non-finite bounding-box coordinates",
            image_id=image_id,
            bounding_box_id=bounding_box_id,
        )
    pixel_bbox = PixelBoundingBox(
        xmin=max(0, min(width, math.floor(xmin * width))),
        ymin=max(0, min(height, math.floor(ymin * height))),
        xmax=max(0, min(width, math.ceil(xmax * width))),
        ymax=max(0, min(height, math.ceil(ymax * height))),
    )
    if pixel_bbox.width <= 0 or pixel_bbox.height <= 0:
        raise _input_error(
            ERROR_EMPTY_BOUNDING_BOX,
            f"Detection {image_id!r}:{bounding_box_id} has an empty bounding box after clipping",
            image_id=image_id,
            bounding_box_id=bounding_box_id,
        )
    return pixel_bbox


def _parse_mask_class_id(value: Any, *, context: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context} must be an integer class ID, got {value!r}",
        ) from None
    if not 1 <= parsed <= 255:
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context} must be in uint8 foreground range 1..255, got {parsed}",
        )
    return parsed


def load_catalog(path: str | Path) -> tuple[Mapping[str, Any], frozenset[int]]:
    """Load the generated catalog and return every valid foreground mask value."""

    catalog_path = Path(path)
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _input_error(
            ERROR_CSV_INVALID,
            f"Failed to load species catalog {catalog_path}: {exc}",
            path=str(catalog_path),
        ) from exc
    if not isinstance(catalog, Mapping):
        raise _input_error(ERROR_CSV_INVALID, "Species catalog must contain a JSON object")

    species = catalog.get("species")
    cultivars = catalog.get("cultivars", {})
    if not isinstance(species, Mapping) or not isinstance(cultivars, Mapping):
        raise _input_error(
            ERROR_CSV_INVALID,
            "Species catalog must contain 'species' and 'cultivars' objects",
        )

    known: set[int] = set()
    for species_id, entry in species.items():
        if not isinstance(entry, Mapping) or "class_id" not in entry:
            raise _input_error(
                ERROR_CSV_INVALID,
                f"Species catalog entry {species_id!r} must contain class_id",
            )
        known.add(
            _parse_mask_class_id(
                entry["class_id"], context=f"species {species_id!r} class_id"
            )
        )
    for cultivar_id, entry in cultivars.items():
        if not isinstance(entry, Mapping):
            raise _input_error(
                ERROR_CSV_INVALID,
                f"Cultivar catalog entry {cultivar_id!r} must be an object",
            )
        known.add(_parse_mask_class_id(cultivar_id, context=f"cultivar {cultivar_id!r}"))
    return catalog, frozenset(known)


def _build_file_index(
    directory: str | Path,
    *,
    extensions: Sequence[str],
    kind: str,
) -> dict[str, Path]:
    root = Path(directory)
    if not root.is_dir():
        code = ERROR_IMAGE_MISSING if kind == "image" else ERROR_MASK_MISSING
        raise _input_error(code, f"{kind.title()} directory does not exist: {root}")

    allowed = {extension.lower() for extension in extensions}
    index: dict[str, Path] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        key = path.stem.casefold()
        previous = index.get(key)
        if previous is not None:
            raise _input_error(
                ERROR_DUPLICATE_INPUT,
                f"Duplicate {kind} files resolve to image_id {key!r}: {previous} and {path}",
                image_id=key,
            )
        index[key] = path
    return index


def _validate_image_and_mask(
    *, image_id: str, image_path: Path, mask_path: Path, known_class_ids: frozenset[int]
) -> tuple[int, int]:
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise _input_error(
            ERROR_IMAGE_INVALID,
            f"Image {image_path} is not a readable RGB JPG",
            image_id=image_id,
        )
    mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
    if mask is None:
        raise _input_error(
            ERROR_MASK_INVALID,
            f"Mask {mask_path} is not readable",
            image_id=image_id,
        )
    if mask.ndim != 2 or mask.dtype != np.uint8:
        raise _input_error(
            ERROR_MASK_INVALID,
            f"Mask {mask_path} must be single-channel uint8, got shape={mask.shape} "
            f"dtype={mask.dtype}",
            image_id=image_id,
        )

    height, width = image.shape[:2]
    if mask.shape != (height, width):
        raise _input_error(
            ERROR_DIMENSION_MISMATCH,
            f"Image {image_path} is {width}x{height}, but mask {mask_path} has shape "
            f"{mask.shape}",
            image_id=image_id,
        )

    foreground = {int(value) for value in np.unique(mask) if int(value) != 0}
    unknown = sorted(foreground - known_class_ids)
    if unknown:
        raise _input_error(
            ERROR_UNKNOWN_MASK_VALUE,
            f"Mask {mask_path} contains unknown foreground class value(s): {unknown}",
            image_id=image_id,
            values=unknown,
        )
    return width, height


def discover_and_validate_inputs(
    *,
    images_dir: str | Path,
    masks_dir: str | Path,
    georeferenced_csv: str | Path,
    species_catalog: str | Path,
    config: SegToCutConfig,
) -> BatchValidationResult:
    """Discover, match, and fully validate all inputs needed before cutout generation."""

    rows = load_detection_rows(georeferenced_csv)
    catalog, known_class_ids = load_catalog(species_catalog)
    try:
        class_ids = build_class_id_index(georeferenced_csv, catalog)
    except ClassIdResolutionError as exc:
        raise _input_error(ERROR_CSV_INVALID, str(exc)) from exc

    image_index = _build_file_index(
        images_dir, extensions=config.image_extensions, kind="image"
    )
    mask_index = _build_file_index(
        masks_dir, extensions=(config.mask_extension,), kind="mask"
    )

    rows_by_image: dict[str, list[_DetectionRow]] = {}
    for row in rows:
        rows_by_image.setdefault(row.normalized_image_id, []).append(row)

    validated_images: list[ValidatedImageInput] = []
    for normalized_image_id in sorted(rows_by_image):
        image_rows = rows_by_image[normalized_image_id]
        image_id = image_rows[0].image_id
        image_path = image_index.get(normalized_image_id)
        if image_path is None:
            raise _input_error(
                ERROR_IMAGE_MISSING,
                f"No JPG image matches image_id {image_id!r}",
                image_id=image_id,
            )
        mask_path = mask_index.get(normalized_image_id)
        if mask_path is None:
            raise _input_error(
                ERROR_MASK_MISSING,
                f"No segmentation mask matches image_id {image_id!r}",
                image_id=image_id,
            )

        width, height = _validate_image_and_mask(
            image_id=image_id,
            image_path=image_path,
            mask_path=mask_path,
            known_class_ids=known_class_ids,
        )
        detections: list[DetectionInput] = []
        for row in image_rows:
            class_id = class_ids.get(row.image_id, row.bounding_box_id)
            if class_id is None:
                raise _input_error(
                    ERROR_CSV_INVALID,
                    f"No class assignment for detection {row.image_id!r}:{row.bounding_box_id}",
                )
            if class_id not in known_class_ids:
                raise _input_error(
                    ERROR_CSV_INVALID,
                    f"Detection {row.image_id!r}:{row.bounding_box_id} resolves to "
                    f"class {class_id}, which is absent from the species catalog",
                    image_id=row.image_id,
                    bounding_box_id=row.bounding_box_id,
                )
            detections.append(
                DetectionInput(
                    image_id=row.image_id,
                    bounding_box_id=row.bounding_box_id,
                    normalized_bbox=row.normalized_bbox,
                    pixel_bbox=normalized_bbox_to_pixels(
                        row.normalized_bbox,
                        width=width,
                        height=height,
                        image_id=row.image_id,
                        bounding_box_id=row.bounding_box_id,
                    ),
                    class_id=class_id,
                    species_id=row.species_id,
                    cultivar_id=row.cultivar_id,
                )
            )
        validated_images.append(
            ValidatedImageInput(
                image_id=image_id,
                image_path=image_path,
                mask_path=mask_path,
                width=width,
                height=height,
                detections=tuple(detections),
            )
        )

    return BatchValidationResult(
        images=tuple(validated_images),
        known_class_ids=known_class_ids,
    )
