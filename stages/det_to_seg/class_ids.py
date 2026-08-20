"""Resolve detections to the class IDs written into segmentation masks."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping


FALLBACK_CLASS_ID = 27
UINT8_MAX = 255


class ClassIdResolutionError(ValueError):
    """Base class for invalid class-assignment inputs."""


class InvalidClassIdError(ClassIdResolutionError):
    """Raised when a class ID cannot be represented by a uint8 mask."""


class UnknownSpeciesError(ClassIdResolutionError):
    """Raised when a CSV species ID is absent from the species catalog."""


class DuplicateDetectionKeyError(ClassIdResolutionError):
    """Raised when duplicate CSV rows assign different classes to one detection."""


@dataclass(frozen=True)
class DetectionBox:
    """A YOLO box plus the stable row identity used by det_to_world."""

    bounding_box_id: int
    xyxy: tuple[int, int, int, int]
    class_id: int = FALLBACK_CLASS_ID


@dataclass(frozen=True)
class ClassIdIndex:
    """Class IDs keyed by normalized image ID and bounding-box row ID."""

    by_detection: Mapping[tuple[str, int], int]

    def get(self, image_id: str, bounding_box_id: int) -> int | None:
        return self.by_detection.get((_normalize_image_id(image_id), bounding_box_id))


@dataclass(frozen=True)
class DetectionClassResolution:
    """Resolved detections and the number that used the generic fallback."""

    detections: tuple[DetectionBox, ...]
    fallback_count: int


def _normalize_image_id(value: Any) -> str:
    if value is None:
        raise ClassIdResolutionError("image_id must not be empty")
    image_id = str(value).strip()
    if not image_id:
        raise ClassIdResolutionError("image_id must not be empty")
    return image_id.casefold()


def _parse_integer(value: Any, *, field: str, context: str) -> int:
    if isinstance(value, bool):
        raise ClassIdResolutionError(f"{context}: {field} must be an integer, got {value!r}")

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(stripped)
            except ValueError:
                pass

    raise ClassIdResolutionError(f"{context}: {field} must be an integer, got {value!r}")


def _parse_bounding_box_id(value: Any, *, context: str) -> int:
    bounding_box_id = _parse_integer(value, field="bounding_box_id", context=context)
    if bounding_box_id < 0:
        raise ClassIdResolutionError(
            f"{context}: bounding_box_id must be non-negative, got {bounding_box_id}"
        )
    return bounding_box_id


def _parse_class_id(value: Any, *, field: str, context: str) -> int:
    try:
        class_id = _parse_integer(value, field=field, context=context)
    except ClassIdResolutionError as exc:
        raise InvalidClassIdError(str(exc)) from exc

    if not 0 <= class_id <= UINT8_MAX:
        raise InvalidClassIdError(
            f"{context}: {field} must be in uint8 range 0..{UINT8_MAX}, got {class_id}"
        )
    return class_id


def load_species_catalog(path: str | Path) -> dict[str, Any]:
    """Load and validate the species lookup portion of the generated catalog."""

    path = Path(path)
    try:
        with path.open(encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ClassIdResolutionError(f"Failed to load species catalog {path}: {exc}") from exc

    if not isinstance(catalog, dict):
        raise ClassIdResolutionError(f"Species catalog {path} must contain a JSON object")

    species = catalog.get("species")
    if not isinstance(species, dict):
        raise ClassIdResolutionError(
            f"Species catalog {path} must contain a 'species' object"
        )

    for species_id, entry in species.items():
        if not isinstance(species_id, str) or not species_id.strip():
            raise ClassIdResolutionError(
                f"Species catalog {path} contains an invalid species key {species_id!r}"
            )
        if not isinstance(entry, dict) or "class_id" not in entry:
            raise ClassIdResolutionError(
                f"Species catalog entry {species_id!r} must be an object with class_id"
            )

    return catalog


def build_class_id_index(
    georeferenced_csv_path: str | Path,
    catalog: Mapping[str, Any],
) -> ClassIdIndex:
    """Build the detection-to-class lookup from a det_to_world CSV."""

    csv_path = Path(georeferenced_csv_path)
    species_catalog = catalog.get("species")
    if not isinstance(species_catalog, Mapping):
        raise ClassIdResolutionError("Species catalog must contain a 'species' object")

    by_detection: dict[tuple[str, int], int] = {}
    try:
        csv_file = csv_path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ClassIdResolutionError(
            f"Failed to open georeferenced CSV {csv_path}: {exc}"
        ) from exc

    with csv_file:
        reader = csv.DictReader(csv_file)
        required_columns = {"image_id", "bounding_box_id", "species_id"}
        fieldnames = set(reader.fieldnames or ())
        missing_columns = sorted(required_columns - fieldnames)
        if missing_columns:
            raise ClassIdResolutionError(
                f"Georeferenced CSV {csv_path} is missing required column(s): "
                f"{', '.join(missing_columns)}"
            )

        for row_number, row in enumerate(reader, start=2):
            context = f"{csv_path.name} row {row_number}"
            image_id = _normalize_image_id(row.get("image_id"))
            bounding_box_id = _parse_bounding_box_id(
                row.get("bounding_box_id"), context=context
            )

            cultivar_id = row.get("cultivar_id")
            if cultivar_id is not None and cultivar_id.strip():
                class_id = _parse_class_id(
                    cultivar_id, field="cultivar_id", context=context
                )
            else:
                species_id = (row.get("species_id") or "").strip()
                entry = species_catalog.get(species_id)
                if entry is None:
                    raise UnknownSpeciesError(
                        f"{context}: species catalog has no entry for species_id {species_id!r}"
                    )
                if not isinstance(entry, Mapping) or "class_id" not in entry:
                    raise ClassIdResolutionError(
                        f"{context}: species catalog entry {species_id!r} "
                        "must be an object with class_id"
                    )
                class_id = _parse_class_id(
                    entry["class_id"], field=f"species {species_id!r} class_id", context=context
                )

            key = (image_id, bounding_box_id)
            previous = by_detection.get(key)
            if previous is not None and previous != class_id:
                raise DuplicateDetectionKeyError(
                    f"{context}: duplicate detection key {key!r} resolves to both "
                    f"class {previous} and class {class_id}"
                )
            by_detection[key] = class_id

    return ClassIdIndex(by_detection=by_detection)


def load_class_id_index(
    georeferenced_csv_path: str | Path,
    species_catalog_path: str | Path,
) -> ClassIdIndex:
    """Load both auxiliary inputs and return a ready-to-use lookup."""

    catalog = load_species_catalog(species_catalog_path)
    return build_class_id_index(georeferenced_csv_path, catalog)


def resolve_detection_class_ids(
    image_id: str,
    detections: Iterable[DetectionBox],
    class_ids: ClassIdIndex,
    *,
    fallback_class_id: int = FALLBACK_CLASS_ID,
) -> DetectionClassResolution:
    """Assign class IDs to boxes, using the PLANT sentinel for absent joins."""

    fallback_class_id = _parse_class_id(
        fallback_class_id, field="fallback_class_id", context="class resolution"
    )
    resolved: list[DetectionBox] = []
    fallback_count = 0

    for detection in detections:
        class_id = class_ids.get(image_id, detection.bounding_box_id)
        if class_id is None:
            class_id = fallback_class_id
            fallback_count += 1
        else:
            class_id = _parse_class_id(
                class_id,
                field="class_id",
                context=(
                    f"detection {image_id!r} bounding_box_id="
                    f"{detection.bounding_box_id}"
                ),
            )
        resolved.append(replace(detection, class_id=class_id))

    return DetectionClassResolution(
        detections=tuple(resolved),
        fallback_count=fallback_count,
    )
