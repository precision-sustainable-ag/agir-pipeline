"""Configuration loading for input validation and cutout measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import ERROR_CONFIG_INVALID
from .errors import SegToCutConfigError

DEFAULT_CONFIG_PATH = Path(__file__).parent / "configs" / "default.yaml"


@dataclass(frozen=True)
class SegToCutConfig:
    image_extensions: tuple[str, ...] = (".jpg", ".jpeg")
    mask_extension: str = ".png"
    border_width_px: int = 100
    edge_threshold: float = 0.05
    cutout_version: str = "2.0"
    bbox_area_source: str = "georeferenced_csv"
    species_bbox_min_sample_size: int = 5
    abnormal_bbox_size_threshold: float = 0.50


def _extension(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"{field} must be a non-empty file extension",
            field=field,
        )
    extension = value.strip().lower()
    if not extension.startswith(".") or "/" in extension or "\\" in extension:
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"{field} must be an extension such as '.png', got {value!r}",
            field=field,
        )
    return extension


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"{field} must be a positive integer, got {value!r}",
            field=field,
        )
    return value


def _fraction(value: Any, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"{field} must be a finite number in [0, 1]",
            field=field,
        )
    return float(value)


def _nonnegative_float(value: Any, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"{field} must be a finite non-negative number",
            field=field,
        )
    return float(value)


def parse_config(data: Mapping[str, Any]) -> SegToCutConfig:
    """Validate an in-memory configuration mapping."""

    raw_image_extensions = data.get("image_extensions", [".jpg", ".jpeg"])
    if isinstance(raw_image_extensions, (str, bytes)) or not isinstance(
        raw_image_extensions, (list, tuple)
    ):
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            "image_extensions must be a list of file extensions",
            field="image_extensions",
        )

    image_extensions = tuple(
        _extension(value, field="image_extensions") for value in raw_image_extensions
    )
    if not image_extensions:
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            "image_extensions must contain at least one extension",
            field="image_extensions",
        )
    if len(set(image_extensions)) != len(image_extensions):
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            "image_extensions must not contain duplicates",
            field="image_extensions",
        )

    mask_extension = _extension(data.get("mask_extension", ".png"), field="mask_extension")
    border_width_px = _positive_int(data.get("border_width_px", 100), field="border_width_px")
    edge_threshold = _fraction(data.get("edge_threshold", 0.05), field="edge_threshold")
    cutout_version = data.get("cutout_version", "2.0")
    if not isinstance(cutout_version, str) or not cutout_version.strip():
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            "cutout_version must be a non-empty string",
            field="cutout_version",
        )
    raw_bbox_area = data.get("bbox_area")
    if raw_bbox_area is None:
        bbox_area_source = data.get("bbox_area_source", "georeferenced_csv")
    elif isinstance(raw_bbox_area, Mapping):
        bbox_area_source = raw_bbox_area.get("source", "georeferenced_csv")
    else:
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            "bbox_area must be a YAML object",
            field="bbox_area",
        )
    if bbox_area_source not in {"georeferenced_csv", "camera"}:
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            "bbox_area.source must be 'georeferenced_csv' or 'camera'",
            field="bbox_area.source",
        )
    species_bbox_min_sample_size = _positive_int(
        data.get("species_bbox_min_sample_size", 5),
        field="species_bbox_min_sample_size",
    )
    abnormal_bbox_size_threshold = _nonnegative_float(
        data.get("abnormal_bbox_size_threshold", 0.50),
        field="abnormal_bbox_size_threshold",
    )
    return SegToCutConfig(
        image_extensions=image_extensions,
        mask_extension=mask_extension,
        border_width_px=border_width_px,
        edge_threshold=edge_threshold,
        cutout_version=cutout_version.strip(),
        bbox_area_source=bbox_area_source,
        species_bbox_min_sample_size=species_bbox_min_sample_size,
        abnormal_bbox_size_threshold=abnormal_bbox_size_threshold,
    )


def load_config(path: str | Path | None = None) -> SegToCutConfig:
    """Load YAML configuration, defaulting to the packaged stage config."""

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"Failed to load seg_to_cut config {config_path}: {exc}",
            path=str(config_path),
        ) from exc

    if not isinstance(data, Mapping):
        raise SegToCutConfigError(
            ERROR_CONFIG_INVALID,
            f"seg_to_cut config {config_path} must contain a YAML object",
            path=str(config_path),
        )
    return parse_config(data)
