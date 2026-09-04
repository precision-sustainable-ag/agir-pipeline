"""Configuration loading for the validation-only stage scaffold."""

from __future__ import annotations

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
    return SegToCutConfig(
        image_extensions=image_extensions,
        mask_extension=mask_extension,
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

