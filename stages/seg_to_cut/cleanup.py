"""Conservative connected-component cleanup for local cutout masks."""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray

from . import (
    SKIP_NO_EXPECTED_CLASS_PIXELS,
    SKIP_NO_FOREGROUND_AFTER_CLEANUP,
)
from .contracts import CleanupResult


def border_sweep_cleanup(
    mask: NDArray[np.uint8],
    *,
    expected_class_id: int,
    border_width_px: int,
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
    if isinstance(border_width_px, bool) or not isinstance(border_width_px, int):
        raise ValueError("border_width_px must be a positive integer")
    if border_width_px < 1:
        raise ValueError("border_width_px must be a positive integer")

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
    interior = np.zeros(mask.shape, dtype=bool)
    if height > 2 * border_width_px and width > 2 * border_width_px:
        interior[
            border_width_px : height - border_width_px,
            border_width_px : width - border_width_px,
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
