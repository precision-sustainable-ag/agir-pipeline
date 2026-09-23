from __future__ import annotations

import numpy as np
import pytest

from stages.seg_to_cut import (
    SKIP_NO_EXPECTED_CLASS_PIXELS,
    SKIP_NO_FOREGROUND_AFTER_CLEANUP,
)
from stages.seg_to_cut.cleanup import border_sweep_cleanup


def test_removes_border_fragment_and_keeps_interior_component() -> None:
    mask = np.zeros((9, 9), dtype=np.uint8)
    mask[0:2, 0:2] = 11
    mask[3:6, 3:6] = 11

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=2)

    assert result.removed_components == 1
    assert result.remaining_components == 1
    assert result.removed_pixels == 4
    assert result.skip_reason is None
    assert np.count_nonzero(result.mask == 11) == 9


def test_keeps_border_component_that_extends_into_interior() -> None:
    mask = np.zeros((9, 9), dtype=np.uint8)
    mask[0:5, 4] = 11

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=2)

    assert result.removed_components == 0
    assert result.remaining_components == 1
    assert result.removed_pixels == 0
    np.testing.assert_array_equal(result.mask, mask)


def test_eight_connectivity_keeps_corner_fragment_connected_to_interior() -> None:
    mask = np.zeros((6, 6), dtype=np.uint8)
    mask[0, 0] = 11
    mask[1, 1] = 11

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=1)

    assert result.removed_components == 0
    assert result.remaining_components == 1
    np.testing.assert_array_equal(result.mask, mask)


def test_other_class_values_are_excluded() -> None:
    mask = np.zeros((7, 7), dtype=np.uint8)
    mask[2:5, 2:5] = 11
    mask[3, 3] = 19

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=1)

    assert set(np.unique(result.mask)) == {0, 11}
    assert result.mask[3, 3] == 0
    assert result.remaining_components == 1


def test_missing_expected_class_returns_stable_skip_reason() -> None:
    mask = np.full((5, 5), 19, dtype=np.uint8)

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=1)

    assert result.skipped
    assert result.skip_reason == SKIP_NO_EXPECTED_CLASS_PIXELS
    assert not np.any(result.mask)
    assert result.removed_components == 0
    assert result.remaining_components == 0


def test_all_border_components_removed_returns_stable_skip_reason() -> None:
    mask = np.zeros((7, 7), dtype=np.uint8)
    mask[0, 2:4] = 11
    mask[6, 4:6] = 11

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=1)

    assert result.skip_reason == SKIP_NO_FOREGROUND_AFTER_CLEANUP
    assert result.removed_components == 2
    assert result.remaining_components == 0
    assert result.removed_pixels == 4
    assert not np.any(result.mask)


def test_small_crop_with_no_interior_removes_foreground() -> None:
    mask = np.full((3, 4), 11, dtype=np.uint8)

    result = border_sweep_cleanup(mask, expected_class_id=11, border_width_px=2)

    assert result.skip_reason == SKIP_NO_FOREGROUND_AFTER_CLEANUP
    assert result.removed_components == 1
    assert result.removed_pixels == 12


@pytest.mark.parametrize(
    ("mask", "class_id", "border_width"),
    [
        (np.zeros((3, 3, 1), dtype=np.uint8), 11, 1),
        (np.zeros((3, 3), dtype=np.uint16), 11, 1),
        (np.zeros((3, 3), dtype=np.uint8), 0, 1),
        (np.zeros((3, 3), dtype=np.uint8), 256, 1),
        (np.zeros((3, 3), dtype=np.uint8), 11, 0),
    ],
)
def test_rejects_invalid_arguments(mask, class_id, border_width) -> None:
    with pytest.raises(ValueError):
        border_sweep_cleanup(
            mask,
            expected_class_id=class_id,
            border_width_px=border_width,
        )
