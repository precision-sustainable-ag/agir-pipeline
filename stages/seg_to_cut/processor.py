"""Validate inputs and produce deterministic cutout artifact sets."""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import math
import re
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from itertools import repeat
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from stages import ITEM_FAILED, ITEM_OK, ITEM_SKIPPED
from stages.common.class_ids import ClassIdIndex, ClassIdResolutionError, build_class_id_index

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
    ERROR_PROCESSING_FAILED,
    ERROR_UNKNOWN_MASK_VALUE,
)
from .cleanup import border_sweep_cleanup, resolve_border_band_widths
from .config import SegToCutConfig
from .contracts import (
    AreaMetricInput,
    BatchValidationResult,
    CleanupResult,
    CutoutProcessingResult,
    DetectionInput,
    PixelBoundingBox,
    ValidatedImageInput,
    WorldBoundingBox,
)
from .errors import SegToCutInputError
from .metadata import (
    calculate_area_properties,
    calculate_cutout_properties,
    finalize_species_bbox_metrics,
    null_metadata_reasons,
)
from .writer import write_cutout_artifacts

logger = logging.getLogger(__name__)
_EXIF_READ_LOCK = Lock()

_SAFE_ID_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

REQUIRED_CSV_COLUMNS = (
    "image_id",
    "xmin",
    "ymin",
    "xmax",
    "ymax",
    "species_id",
    "world_tl_x",
    "world_tl_y",
    "world_tr_x",
    "world_tr_y",
    "world_bl_x",
    "world_bl_y",
    "world_br_x",
    "world_br_y",
    "crs",
)

WORLD_COORDINATE_COLUMNS = (
    "world_tl_x",
    "world_tl_y",
    "world_tr_x",
    "world_tr_y",
    "world_bl_x",
    "world_bl_y",
    "world_br_x",
    "world_br_y",
)


@dataclass(frozen=True)
class _DetectionRow:
    image_id: str
    normalized_image_id: str
    bounding_box_id: int
    normalized_bbox: tuple[float, float, float, float]
    species_id: str
    cultivar_id: str | None
    world_bbox: WorldBoundingBox | None


@dataclass(frozen=True)
class _EligibilityPassResult:
    eligible: tuple[tuple[str, int], ...]
    area_inputs: tuple[AreaMetricInput, ...]
    early_results: tuple[CutoutProcessingResult, ...]


def _input_error(code: str, message: str, **context: Any) -> SegToCutInputError:
    return SegToCutInputError(code, message, **context)


def _bounded_worker_count(max_workers: int, item_count: int) -> int:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 0:
        raise ValueError("max_workers must be a non-negative integer")
    return min(max_workers, item_count)


def _normalize_image_id(value: Any, *, context: str) -> tuple[str, str]:
    image_id = "" if value is None else str(value).strip()
    if not image_id:
        raise _input_error(ERROR_CSV_INVALID, f"{context}: image_id must not be empty")
    if not _SAFE_ID_PART.fullmatch(image_id):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context}: image_id {image_id!r} cannot be used in a safe cutout filename",
            image_id=image_id,
        )
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


def _parse_optional_world_bbox(row: Mapping[str, Any]) -> WorldBoundingBox | None:
    """Parse a complete finite world box, otherwise mark its area unavailable.

    ``det_to_world`` emits these columns blank for non-georeferenced batches.
    Partial or malformed values likewise cannot support a physical area and are
    retained as ``None`` instead of being guessed.
    """

    raw_coordinates = [str(row.get(column) or "").strip() for column in WORLD_COORDINATE_COLUMNS]
    crs = str(row.get("crs") or "").strip()
    if not any(raw_coordinates) and not crs:
        return None
    if not all(raw_coordinates) or not crs:
        return None
    try:
        coordinates = tuple(float(value) for value in raw_coordinates)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in coordinates):
        return None
    return WorldBoundingBox(
        top_left=(coordinates[0], coordinates[1]),
        top_right=(coordinates[2], coordinates[3]),
        bottom_left=(coordinates[4], coordinates[5]),
        bottom_right=(coordinates[6], coordinates[7]),
        crs=crs,
    )


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

        generate_ids = "bounding_box_id" not in fieldnames
        if generate_ids:
            logger.warning(
                "Georeferenced CSV %s has no bounding_box_id column; generating IDs "
                "from 0 per image in CSV row order",
                csv_path,
            )
        next_ids: dict[str, int] = {}
        for row_number, row in enumerate(reader, start=2):
            context = f"{csv_path.name} row {row_number}"
            image_id, normalized_image_id = _normalize_image_id(
                row.get("image_id"), context=context
            )
            if generate_ids:
                bounding_box_id = next_ids.get(normalized_image_id, 0)
                next_ids[normalized_image_id] = bounding_box_id + 1
            else:
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
            if not species_id:
                raise _input_error(
                    ERROR_CSV_INVALID,
                    f"{context}: species_id must not be empty",
                    field="species_id",
                )
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
                    world_bbox=_parse_optional_world_bbox(row),
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


def _parse_mask_class_id(
    value: Any, *, context: str, allow_background: bool = False
) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"{context} must be an integer class ID, got {value!r}",
        ) from None
    if allow_background and parsed == 0:
        return parsed
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
        class_id = _parse_mask_class_id(
            entry["class_id"],
            context=f"species {species_id!r} class_id",
            allow_background=str(species_id).strip().upper() == "BACKGROUND",
        )
        if class_id != 0:
            known.add(class_id)
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


def _read_exif_metadata(image_path: Path) -> tuple[str | None, str | None]:
    """Return EXIF capture datetime and lens model without decoding image pixels."""

    try:
        # warnings.catch_warnings mutates process-wide state, so guard it when
        # input validation reads EXIF concurrently in a thread pool.
        with _EXIF_READ_LOCK, warnings.catch_warnings():
            # EXIF parsing does not decode pixels, so large source dimensions
            # do not create Pillow's decompression-bomb memory risk here.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(image_path) as image:
                exif = image.getexif()
                nested = exif.get_ifd(34665)
                datetime_value = exif.get(36867, nested.get(36867))
                lens_value = exif.get(42036, nested.get(42036))
    except (OSError, UnidentifiedImageError, ValueError, TypeError):
        logger.warning("Could not read EXIF metadata from %s", image_path)
        return None, None
    if not isinstance(datetime_value, str):
        logger.warning("Image %s has no EXIF DateTimeOriginal", image_path)
        capture_datetime = None
    else:
        datetime_value = datetime_value.strip()
        try:
            dt.datetime.strptime(datetime_value, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            logger.warning(
                "Image %s has invalid EXIF DateTimeOriginal %r", image_path, datetime_value
            )
            capture_datetime = None
        else:
            capture_datetime = datetime_value
    lens_model = lens_value.strip() if isinstance(lens_value, str) and lens_value.strip() else None
    if lens_model is None:
        logger.warning("Image %s has no EXIF LensModel", image_path)
    return capture_datetime, lens_model


def _validate_discovered_image(
    normalized_image_id: str,
    image_rows: Sequence[_DetectionRow],
    image_index: Mapping[str, Path],
    mask_index: Mapping[str, Path],
    class_ids: ClassIdIndex,
    known_class_ids: frozenset[int],
) -> ValidatedImageInput:
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
                world_bbox=row.world_bbox,
            )
        )
    capture_datetime, lens_model = _read_exif_metadata(image_path)
    return ValidatedImageInput(
        image_id=image_id,
        image_path=image_path,
        mask_path=mask_path,
        width=width,
        height=height,
        detections=tuple(detections),
        capture_datetime=capture_datetime,
        lens_model=lens_model,
    )


def discover_and_validate_inputs(
    *,
    images_dir: str | Path,
    masks_dir: str | Path,
    georeferenced_csv: str | Path,
    species_catalog: str | Path,
    config: SegToCutConfig,
    max_workers: int = 0,
) -> BatchValidationResult:
    """Discover, match, and fully validate all inputs needed before cutout generation."""

    rows = load_detection_rows(georeferenced_csv)
    catalog, known_class_ids = load_catalog(species_catalog)
    try:
        class_ids = build_class_id_index(georeferenced_csv, catalog)
    except ClassIdResolutionError as exc:
        raise _input_error(ERROR_CSV_INVALID, str(exc)) from exc

    image_index = _build_file_index(images_dir, extensions=config.image_extensions, kind="image")
    mask_index = _build_file_index(masks_dir, extensions=(config.mask_extension,), kind="mask")

    rows_by_image: dict[str, list[_DetectionRow]] = {}
    for row in rows:
        rows_by_image.setdefault(row.normalized_image_id, []).append(row)

    image_groups = tuple(
        (normalized_image_id, tuple(rows_by_image[normalized_image_id]))
        for normalized_image_id in sorted(rows_by_image)
    )

    def validate_group(group: tuple[str, tuple[_DetectionRow, ...]]) -> ValidatedImageInput:
        normalized_image_id, image_rows = group
        return _validate_discovered_image(
            normalized_image_id,
            image_rows,
            image_index,
            mask_index,
            class_ids,
            known_class_ids,
        )

    workers = _bounded_worker_count(max_workers, len(image_groups))
    if workers > 1:
        cv2.setNumThreads(1)
        logger.info("Validating images with %d threads", workers)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            validated_images = list(executor.map(validate_group, image_groups))
    else:
        logger.info("Validating images sequentially")
        validated_images = [validate_group(group) for group in image_groups]

    return BatchValidationResult(
        images=tuple(validated_images),
        known_class_ids=known_class_ids,
        catalog=catalog,
    )


def make_cutout_id(image_id: str, bounding_box_id: int) -> str:
    """Build the stable artifact identity for one upstream detection."""

    if not isinstance(image_id, str) or not _SAFE_ID_PART.fullmatch(image_id):
        raise _input_error(
            ERROR_CSV_INVALID,
            f"image_id {image_id!r} cannot be used in a safe cutout filename",
            image_id=image_id,
        )
    if isinstance(bounding_box_id, bool) or not isinstance(bounding_box_id, int):
        raise _input_error(
            ERROR_CSV_INVALID,
            "bounding_box_id must be an integer",
            image_id=image_id,
        )
    return f"{image_id}_{bounding_box_id}"


def _read_validated_rgb(image: ValidatedImageInput) -> np.ndarray:
    bgr = cv2.imread(str(image.image_path), cv2.IMREAD_COLOR)
    if bgr is None or bgr.shape != (image.height, image.width, 3):
        raise _input_error(
            ERROR_IMAGE_INVALID,
            f"Image {image.image_path} changed or became unreadable after validation",
            image_id=image.image_id,
        )
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _read_validated_mask(image: ValidatedImageInput) -> np.ndarray:
    mask = cv2.imread(str(image.mask_path), cv2.IMREAD_UNCHANGED)
    if mask is None or mask.dtype != np.uint8 or mask.shape != (image.height, image.width):
        raise _input_error(
            ERROR_MASK_INVALID,
            f"Mask {image.mask_path} changed or became unreadable after validation",
            image_id=image.image_id,
        )
    return mask


def _crop(array: np.ndarray, detection: DetectionInput) -> np.ndarray:
    box = detection.pixel_bbox
    return array[box.ymin : box.ymax, box.xmin : box.xmax]


def _category_metadata(detection: DetectionInput, catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten current catalog records into the documented category object."""

    species_catalog = catalog.get("species", {})
    cultivar_catalog = catalog.get("cultivars", {})
    species_entry = species_catalog.get(detection.species_id, {})
    category = dict(species_entry) if isinstance(species_entry, Mapping) else {}
    category["species_id"] = detection.species_id
    category["class_id"] = category.get("class_id")
    category.setdefault("USDA_symbol", detection.species_id)

    if all(channel in category for channel in ("r", "g", "b")):
        category["rgb"] = [category.pop(channel) for channel in ("r", "g", "b")]

    category["cultivar_id"] = detection.cultivar_id
    category["cultivar_class_id"] = detection.class_id if detection.cultivar_id else None
    category["cultivar_name"] = None
    if detection.cultivar_id:
        cultivar_entry = cultivar_catalog.get(str(detection.cultivar_id), {})
        if isinstance(cultivar_entry, Mapping):
            for key, value in cultivar_entry.items():
                output_name = key if key.startswith("cultivar_") else f"cultivar_{key}"
                category[output_name] = value
            category["cultivar_name"] = cultivar_entry.get(
                "cultivar_name", cultivar_entry.get("display_name")
            )
    return category


def _cutout_metadata(
    *,
    detection: DetectionInput,
    cutout_id: str,
    batch_id: str,
    rgb_crop: np.ndarray,
    cleanup: CleanupResult,
    properties: Mapping[str, Any],
    group_properties: Mapping[str, Any],
    catalog: Mapping[str, Any],
    config: SegToCutConfig,
    season: str | None,
    bbot_version: str | None,
    lens_model: str | None,
    capture_datetime: str | None,
    exif_lens_model: str | None,
) -> dict[str, Any]:
    vertical_band, horizontal_band = resolve_border_band_widths(
        rgb_crop.shape[:2], config.border_band_fraction
    )
    cutout_properties = {
        "is_primary": True,
        "intruder_cleanup": {
            "method": "border_sweep",
            "border_band_fraction": config.border_band_fraction,
            "border_width_px": {
                "top_bottom": vertical_band,
                "left_right": horizontal_band,
            },
            "removed_components": cleanup.removed_components,
            "remaining_components": cleanup.remaining_components,
            "removed_pixels": cleanup.removed_pixels,
        },
        **properties,
        **group_properties,
        "non_target_weed": None,
        "non_target_weed_pred_conf": None,
    }
    return {
        "season": season,
        "datetime": capture_datetime,
        "bbot_version": bbot_version,
        "batch_id": batch_id,
        "image_id": detection.image_id,
        "cutout_id": cutout_id,
        "cutout_num": detection.bounding_box_id,
        "cutout_height": int(rgb_crop.shape[0]),
        "cutout_width": int(rgb_crop.shape[1]),
        "lens_model": exif_lens_model or lens_model,
        "validated": False,
        "cutout_version": config.cutout_version,
        "cutout_props": cutout_properties,
        "category": _category_metadata(detection, catalog),
    }


def _skipped_result(detection: DetectionInput, reason: str) -> CutoutProcessingResult:
    return CutoutProcessingResult(
        image_id=detection.image_id,
        bounding_box_id=detection.bounding_box_id,
        cutout_id=make_cutout_id(detection.image_id, detection.bounding_box_id),
        status=ITEM_SKIPPED,
        skip_reason=reason,
    )


def _failed_result(detection: DetectionInput, exc: Exception) -> CutoutProcessingResult:
    return CutoutProcessingResult(
        image_id=detection.image_id,
        bounding_box_id=detection.bounding_box_id,
        cutout_id=make_cutout_id(detection.image_id, detection.bounding_box_id),
        status=ITEM_FAILED,
        error_code=getattr(exc, "code", ERROR_PROCESSING_FAILED),
        error_type=type(exc).__name__,
        error_message=str(exc),
    )


def _initialize_processing_worker() -> None:
    """Keep one OpenCV thread per image worker to avoid nested oversubscription."""

    cv2.setNumThreads(1)


def _evaluate_image_eligibility(
    image: ValidatedImageInput,
    config: SegToCutConfig,
) -> _EligibilityPassResult:
    eligible: list[tuple[str, int]] = []
    area_inputs: list[AreaMetricInput] = []
    early_results: list[CutoutProcessingResult] = []
    try:
        mask = _read_validated_mask(image)
    except Exception as exc:
        return _EligibilityPassResult(
            eligible=(),
            area_inputs=(),
            early_results=tuple(_failed_result(detection, exc) for detection in image.detections),
        )

    for detection in image.detections:
        cleanup = border_sweep_cleanup(
            _crop(mask, detection),
            expected_class_id=detection.class_id,
            border_band_fraction=config.border_band_fraction,
        )
        if cleanup.skipped:
            early_results.append(_skipped_result(detection, cleanup.skip_reason or "EMPTY_TARGET"))
            continue
        eligible.append(detection.identity)
        area = calculate_area_properties(detection.world_bbox, config=config)
        area_inputs.append(
            AreaMetricInput(
                image_id=detection.image_id,
                bounding_box_id=detection.bounding_box_id,
                species_id=detection.species_id,
                cultivar_id=detection.cultivar_id,
                bbox_area_cm2=area["bbox_area_cm2"],
            )
        )

    return _EligibilityPassResult(
        eligible=tuple(eligible),
        area_inputs=tuple(area_inputs),
        early_results=tuple(early_results),
    )


def _process_image_outputs(
    image: ValidatedImageInput,
    eligible_identities: tuple[tuple[str, int], ...],
    image_early_results: tuple[CutoutProcessingResult, ...],
    image_group_metrics: Mapping[tuple[str, int], Mapping[str, Any]],
    output_dir: str | Path,
    batch_id: str,
    catalog: Mapping[str, Any],
    config: SegToCutConfig,
    fail_stop: bool,
    season: str | None,
    bbot_version: str | None,
    lens_model: str | None,
) -> tuple[CutoutProcessingResult, ...]:
    eligible = set(eligible_identities)
    early_results = {result.identity: result for result in image_early_results}
    arrays: tuple[np.ndarray, np.ndarray] | None = None
    if eligible:
        try:
            arrays = (_read_validated_rgb(image), _read_validated_mask(image))
        except Exception as exc:
            for detection in image.detections:
                if detection.identity in eligible:
                    early_results[detection.identity] = _failed_result(detection, exc)
                    eligible.remove(detection.identity)

    results: list[CutoutProcessingResult] = []
    halted = False
    for detection in image.detections:
        if halted:
            results.append(_skipped_result(detection, "FAIL_STOP"))
            continue
        prior = early_results.get(detection.identity)
        if prior is not None:
            results.append(prior)
            if fail_stop and prior.status == ITEM_FAILED:
                halted = True
            continue
        try:
            assert arrays is not None
            rgb, mask = arrays
            rgb_crop = _crop(rgb, detection)
            cleanup = border_sweep_cleanup(
                _crop(mask, detection),
                expected_class_id=detection.class_id,
                border_band_fraction=config.border_band_fraction,
            )
            if cleanup.skipped:
                results.append(_skipped_result(detection, cleanup.skip_reason or "EMPTY_TARGET"))
                continue
            properties = calculate_cutout_properties(
                rgb_crop,
                cleanup.mask,
                expected_class_id=detection.class_id,
                pixel_bbox=detection.pixel_bbox,
                image_width=image.width,
                image_height=image.height,
                config=config,
                world_bbox=detection.world_bbox,
            )
            cutout_id = make_cutout_id(detection.image_id, detection.bounding_box_id)
            metadata = _cutout_metadata(
                detection=detection,
                cutout_id=cutout_id,
                batch_id=batch_id,
                rgb_crop=rgb_crop,
                cleanup=cleanup,
                properties=properties,
                group_properties=image_group_metrics[detection.identity],
                catalog=catalog,
                config=config,
                season=season,
                bbot_version=bbot_version,
                lens_model=lens_model,
                capture_datetime=image.capture_datetime,
                exif_lens_model=image.lens_model,
            )
            artifacts = write_cutout_artifacts(
                output_dir,
                cutout_id=cutout_id,
                rgb_crop=rgb_crop,
                cleaned_mask=cleanup.mask,
                expected_class_id=detection.class_id,
                metadata=metadata,
            )
            results.append(
                CutoutProcessingResult(
                    image_id=detection.image_id,
                    bounding_box_id=detection.bounding_box_id,
                    cutout_id=cutout_id,
                    status=ITEM_OK,
                    artifacts=artifacts,
                    null_metadata_reasons=null_metadata_reasons(
                        metadata, world_bbox=detection.world_bbox, config=config
                    ),
                )
            )
        except Exception as exc:
            results.append(_failed_result(detection, exc))
            if fail_stop:
                halted = True
    return tuple(results)


def process_validated_batch(
    validation: BatchValidationResult,
    *,
    output_dir: str | Path,
    batch_id: str,
    config: SegToCutConfig,
    fail_stop: bool = False,
    max_workers: int = 0,
    season: str | None = None,
    bbot_version: str | None = None,
    lens_model: str | None = None,
) -> tuple[CutoutProcessingResult, ...]:
    """Create cutouts with a bounded-memory, deterministic two-pass flow."""

    workers = _bounded_worker_count(max_workers, len(validation.images))
    if fail_stop and max_workers > 1:
        raise ValueError("fail_stop cannot be combined with parallel image processing")

    eligible: set[tuple[str, int]] = set()
    area_inputs: list[AreaMetricInput] = []
    early_results: dict[tuple[str, int], CutoutProcessingResult] = {}

    parallel = workers > 1
    logger.info(
        "Processing images in %s mode with %d worker%s",
        "parallel" if parallel else "sequential",
        workers if parallel else 1,
        "s" if parallel else "",
    )
    executor = (
        ProcessPoolExecutor(
            max_workers=workers,
            initializer=_initialize_processing_worker,
        )
        if parallel
        else None
    )
    try:
        # First pass decides which detections survive cleanup, allowing category
        # statistics to exclude empty targets without retaining image-sized arrays.
        if executor is None:
            eligibility_results = tuple(
                _evaluate_image_eligibility(image, config) for image in validation.images
            )
        else:
            eligibility_results = tuple(
                executor.map(
                    _evaluate_image_eligibility,
                    validation.images,
                    repeat(config),
                )
            )

        for eligibility in eligibility_results:
            eligible.update(eligibility.eligible)
            area_inputs.extend(eligibility.area_inputs)
            early_results.update((result.identity, result) for result in eligibility.early_results)

        group_metrics = finalize_species_bbox_metrics(area_inputs, config=config)
        output_tasks = []
        for image in validation.images:
            image_eligible = tuple(
                detection.identity
                for detection in image.detections
                if detection.identity in eligible
            )
            image_early = tuple(
                early_results[detection.identity]
                for detection in image.detections
                if detection.identity in early_results
            )
            image_metrics = {identity: group_metrics[identity] for identity in image_eligible}
            output_tasks.append((image, image_eligible, image_early, image_metrics))

        if executor is not None:
            result_groups = executor.map(
                _process_image_outputs,
                (task[0] for task in output_tasks),
                (task[1] for task in output_tasks),
                (task[2] for task in output_tasks),
                (task[3] for task in output_tasks),
                repeat(output_dir),
                repeat(batch_id),
                repeat(validation.catalog),
                repeat(config),
                repeat(False),
                repeat(season),
                repeat(bbot_version),
                repeat(lens_model),
            )
            return tuple(result for group in result_groups for result in group)

        results: list[CutoutProcessingResult] = []
        halted = False
        for image, image_eligible, image_early, image_metrics in output_tasks:
            if halted:
                results.extend(
                    _skipped_result(detection, "FAIL_STOP") for detection in image.detections
                )
                continue
            image_results = _process_image_outputs(
                image,
                image_eligible,
                image_early,
                image_metrics,
                output_dir,
                batch_id,
                validation.catalog,
                config,
                fail_stop,
                season,
                bbot_version,
                lens_model,
            )
            results.extend(image_results)
            if fail_stop and any(result.status == ITEM_FAILED for result in image_results):
                halted = True
        return tuple(results)
    finally:
        if executor is not None:
            executor.shutdown()
