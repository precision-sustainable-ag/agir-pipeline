"""Conservative connected-component cleanup for local cutout masks."""

from __future__ import annotations

import math

import cv2
import numpy as np
from numpy.typing import NDArray

from . import (
    SKIP_NO_EXPECTED_CLASS_PIXELS,
    SKIP_NO_FOREGROUND_AFTER_CLEANUP,
)
from .contracts import CleanupResult


def resolve_border_band_widths(
    shape: tuple[int, int], border_band_fraction: float
) -> tuple[int, int]:
    """Return top/bottom and left/right band widths for a crop shape."""

    height, width = shape
    return (
        max(1, math.ceil(height * border_band_fraction)),
        max(1, math.ceil(width * border_band_fraction)),
    )


def border_sweep_cleanup(
    mask: NDArray[np.uint8],
    *,
    expected_class_id: int,
    border_band_fraction: float,
) -> CleanupResult:
    """Remove expected-class components confined to the crop's border band.

    Components use 8-connectivity. A border component is retained when at least
    one of its pixels lies outside the border band in the crop interior.
    Pixels belonging to other classes are always treated as background.
    """

    if not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.dtype != np.uint8:
        raise ValueError("mask must be a two-dimensional uint8 array")
    if isinstance(expected_class_id, bool) or not isinstance(expected_class_id, int):
        raise ValueError("expected_class_id must be an integer in the range 1..255")
    if not 1 <= expected_class_id <= 255:
        raise ValueError("expected_class_id must be in the range 1..255")
    if (
        isinstance(border_band_fraction, bool)
        or not isinstance(border_band_fraction, (int, float))
        or not math.isfinite(border_band_fraction)
        or not 0 < border_band_fraction < 0.5
    ):
        raise ValueError("border_band_fraction must be a finite number between 0 and 0.5")

    candidate = mask == expected_class_id
    cleaned = np.zeros(mask.shape, dtype=np.uint8)
    if not np.any(candidate):
        return CleanupResult(
            mask=cleaned,
            removed_components=0,
            remaining_components=0,
            removed_pixels=0,
            skip_reason=SKIP_NO_EXPECTED_CLASS_PIXELS,
        )

    component_count, labels = cv2.connectedComponents(
        candidate.astype(np.uint8), connectivity=8
    )
    height, width = mask.shape
    vertical_band, horizontal_band = resolve_border_band_widths(
        mask.shape, border_band_fraction
    )
    interior = np.zeros(mask.shape, dtype=bool)
    if height > 2 * vertical_band and width > 2 * horizontal_band:
        interior[
            vertical_band : height - vertical_band,
            horizontal_band : width - horizontal_band,
        ] = True

    removed_components = 0
    remaining_components = 0
    removed_pixels = 0
    for component_label in range(1, component_count):
        component = labels == component_label
        if np.any(component & interior):
            cleaned[component] = expected_class_id
            remaining_components += 1
        else:
            removed_components += 1
            removed_pixels += int(np.count_nonzero(component))

    skip_reason = (
        SKIP_NO_FOREGROUND_AFTER_CLEANUP
        if remaining_components == 0
        else None
    )
    return CleanupResult(
        mask=cleaned,
        removed_components=removed_components,
        remaining_components=remaining_components,
        removed_pixels=removed_pixels,
        skip_reason=skip_reason,
    )
