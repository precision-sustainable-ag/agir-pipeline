from __future__ import annotations

import itertools
import json

import cv2
import numpy as np
import pytest
from skimage.measure import blur_effect

from stages.seg_to_cut.cleanup import border_sweep_cleanup
from stages.seg_to_cut.config import SegToCutConfig
from stages.seg_to_cut.contracts import PixelBoundingBox
from stages.seg_to_cut.metadata import (
    SIDES,
    calculate_crop_properties,
    calculate_cutout_properties,
    calculate_mask_properties,
    measurement_provenance,
)


def measure(mask, *, bbox=None, image_width=30, image_height=30, band=1, threshold=0.05):
    height, width = mask.shape
    return calculate_mask_properties(
        mask,
        expected_class_id=11,
        pixel_bbox=bbox or PixelBoundingBox(5, 5, 5 + width, 5 + height),
        image_width=image_width,
        image_height=image_height,
        config=SegToCutConfig(border_width_px=band, edge_threshold=threshold),
    )


def side_mask(sides):
    mask = np.zeros((9, 9), dtype=np.uint8)
    mask[4, 4] = 11
    for side in sides:
        if side == "top":
            mask[:5, 4] = 11
        elif side == "bottom":
            mask[4:, 4] = 11
        elif side == "left":
            mask[4, :5] = 11
        else:
            mask[4, 4:] = 11
    return mask


ALL_SIDE_SETS = [s for n in range(5) for s in itertools.combinations(SIDES, n)]


@pytest.mark.parametrize("sides", ALL_SIDE_SETS)
def test_all_edge_combinations_and_placement(sides):
    result = measure(side_mask(sides))
    edge = result["edge_cut"]
    assert edge["flagged_sides"] == list(sides)
    assert edge["flagged"] == bool(sides)
    assert result["extends_border"] == bool(sides)
    assert edge["source_image_sides"] == []
    assert edge["detection_box_truncation_sides"] == list(sides)
    if not sides:
        expected = "unrestricted"
    elif len(sides) == 1:
        expected = f"{sides[0]}_edge_only"
    elif len(sides) == 2 and set(sides) not in ({"top", "bottom"}, {"left", "right"}):
        expected = "_".join(sides) + "_corner_only"
    else:
        expected = "unsuitable"
    assert edge["synthetic_placement"] == expected
    assert edge["plant_fraction"] == {
        side: pytest.approx(1 / 9 if side in sides else 0) for side in SIDES
    }


@pytest.mark.parametrize("sides", ALL_SIDE_SETS)
def test_source_image_edges(sides):
    edge = measure(
        side_mask(sides),
        bbox=PixelBoundingBox(0, 0, 9, 9),
        image_width=9,
        image_height=9,
    )["edge_cut"]
    assert edge["source_image_sides"] == list(sides)
    assert edge["detection_box_truncation_sides"] == []


def test_mixed_source_and_detection_edges():
    edge = measure(side_mask(("top", "right")), bbox=PixelBoundingBox(5, 0, 14, 9))["edge_cut"]
    assert edge["source_image_sides"] == ["top"]
    assert edge["detection_box_truncation_sides"] == ["right"]


def test_band_occupancy_uses_area_and_strict_threshold():
    mask = np.zeros((6, 10), dtype=np.uint8)
    mask[0, 4:6] = 11
    result = measure(mask, band=2, threshold=0.1)
    assert result["edge_cut"]["plant_fraction"]["top"] == pytest.approx(2 / 20)
    assert result["edge_cut"]["flagged_sides"] == []
    assert result["extends_border"]  # Contact is independent of the threshold.


def test_band_can_flag_without_exact_edge_contact():
    mask = np.zeros((9, 9), dtype=np.uint8)
    mask[1, 4] = 11
    result = measure(mask, band=2)
    assert result["edge_cut"]["flagged_sides"] == ["top"]
    assert not result["extends_border"]


def test_oversized_bands_clip_to_actual_crop():
    mask = np.array([[11, 0, 0], [0, 0, 11]], dtype=np.uint8)
    result = measure(mask, band=20)
    assert result["edge_cut"]["plant_fraction"] == dict.fromkeys(SIDES, 1 / 3)
    assert result["edge_cut"]["band_width_px"] == 20
    assert result["edge_cut"]["synthetic_placement"] == "unsuitable"


@pytest.mark.parametrize(
    "mask",
    [
        np.array([[11]], dtype=np.uint8),
        np.full((1, 5), 11, dtype=np.uint8),
        np.full((5, 1), 11, dtype=np.uint8),
        np.eye(5, dtype=np.uint8) * 11,
        np.full((4, 6), 11, dtype=np.uint8),
    ],
)
def test_solid_and_degenerate_shapes(mask):
    result = measure(mask)
    assert result["solidity"] == pytest.approx(1)
    assert result["num_components"] == 1


def test_hull_spans_disconnected_components():
    mask = np.zeros((5, 5), dtype=np.uint8)
    mask[1, 1] = mask[1, 3] = mask[3, 1] = mask[3, 3] = 11
    result = measure(mask)
    assert result["num_components"] == 4
    assert result["solidity"] == pytest.approx(4 / 9)


def test_hole_reduces_solidity():
    mask = np.full((3, 3), 11, dtype=np.uint8)
    mask[1, 1] = 0
    assert measure(mask)["solidity"] == pytest.approx(8 / 9)


def test_cleanup_precedes_measurements():
    candidate = np.zeros((9, 9), dtype=np.uint8)
    candidate[0, 0:2] = 11
    candidate[3:6, 3:6] = 11
    candidate[8, 4] = 19
    config = SegToCutConfig(border_width_px=2)
    cleaned = border_sweep_cleanup(
        candidate, expected_class_id=11, border_width_px=config.border_width_px
    )
    result = measure(cleaned.mask, band=config.border_width_px)
    assert result["edge_cut"]["band_width_px"] == config.border_width_px
    assert measurement_provenance(config)["edge_cut"]["band_width_px"] == config.border_width_px
    assert result["num_components"] == cleaned.remaining_components == 1
    assert not result["extends_border"]
    assert result["edge_cut"]["synthetic_placement"] == "unrestricted"
    assert result["solidity"] == 1


def test_rgb_statistics_use_all_pixels_in_rgb_order():
    crop = np.array([[[255, 0, 0], [0, 255, 0]], [[0, 0, 255], [255, 255, 0]]], dtype=np.uint8)
    mask = np.full(crop.shape[:2], 11, dtype=np.uint8)
    result = calculate_crop_properties(crop, mask)
    assert result["cropout_rgb_mean"] == pytest.approx([0.5, 0.5, 0.25])
    assert result["cropout_rgb_std"] == pytest.approx([0.5, 0.5, np.sqrt(3) / 4])
    assert result["blur_effect"] is None
    json.dumps(result, allow_nan=False)


def test_blur_matches_historical_black_masked_algorithm():
    rng = np.random.default_rng(42)
    crop = rng.integers(0, 256, (80, 80, 3), dtype=np.uint8)
    mask = np.zeros((80, 80), dtype=np.uint8)
    mask[15:65, 20:60] = 11
    black_masked = np.where(mask[..., None] != 0, crop, 0).astype(np.uint8)

    result = calculate_crop_properties(crop, mask)["blur_effect"]

    assert result == pytest.approx(float(blur_effect(black_masked, channel_axis=2)))


def test_blur_ignores_unmasked_background_but_rgb_statistics_do_not():
    rng = np.random.default_rng(7)
    first = rng.integers(0, 256, (80, 80, 3), dtype=np.uint8)
    second = first.copy()
    mask = np.zeros((80, 80), dtype=np.uint8)
    mask[20:60, 20:60] = 11
    second[mask == 0] = 255 - second[mask == 0]

    first_result = calculate_crop_properties(first, mask)
    second_result = calculate_crop_properties(second, mask)

    assert first_result["blur_effect"] == pytest.approx(second_result["blur_effect"])
    assert first_result["cropout_rgb_mean"] != pytest.approx(second_result["cropout_rgb_mean"])


@pytest.mark.parametrize("shape", [(1, 1, 3), (2, 6, 3), (3, 3, 3)])
def test_undefined_small_crop_blur_is_null(shape):
    crop = np.zeros(shape, dtype=np.uint8)
    mask = np.ones(shape[:2], dtype=np.uint8)
    assert calculate_crop_properties(crop, mask)["blur_effect"] is None


@pytest.mark.parametrize("score", [np.nan, np.inf, -np.inf])
def test_nonfinite_blur_is_null(monkeypatch, score):
    monkeypatch.setattr("stages.seg_to_cut.metadata.blur_effect", lambda *a, **kw: score)
    result = calculate_crop_properties(
        np.zeros((9, 9, 3), dtype=np.uint8), np.ones((9, 9), dtype=np.uint8)
    )
    assert result["blur_effect"] is None
    json.dumps(result, allow_nan=False)


def test_combined_properties_do_not_mask_rgb_or_mutate_inputs():
    crop = np.full((9, 9, 3), [255, 0, 127], dtype=np.uint8)
    mask = side_mask(())
    original_crop, original_mask = crop.copy(), mask.copy()
    result = calculate_cutout_properties(
        crop,
        mask,
        expected_class_id=11,
        pixel_bbox=PixelBoundingBox(5, 5, 14, 14),
        image_width=30,
        image_height=30,
        config=SegToCutConfig(),
    )
    assert result["cropout_rgb_mean"] == pytest.approx([1, 0, 127 / 255])
    assert result["cropout_rgb_std"] == pytest.approx([0, 0, 0], abs=1e-14)
    assert result["num_components"] == 1
    np.testing.assert_array_equal(crop, original_crop)
    np.testing.assert_array_equal(mask, original_mask)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "mask",
    [
        np.zeros((5, 5), dtype=np.uint8),
        np.full((5, 5), 19, dtype=np.uint8),
        np.full((5, 5), 11, dtype=np.uint16),
    ],
)
def test_invalid_cleaned_masks_rejected(mask):
    with pytest.raises(ValueError):
        measure(mask)


def test_invalid_bounding_box_rejected():
    with pytest.raises(ValueError):
        measure(side_mask(()), bbox=PixelBoundingBox(0, 0, 8, 9))
    with pytest.raises(ValueError):
        measure(side_mask(()), bbox=PixelBoundingBox(-1, 0, 8, 9))


@pytest.mark.parametrize(
    "crop",
    [
        np.zeros((4, 4), dtype=np.uint8),
        np.zeros((4, 4, 4), dtype=np.uint8),
        np.zeros((4, 4, 3), dtype=float),
        np.zeros((0, 4, 3), dtype=np.uint8),
    ],
)
def test_invalid_rgb_input_rejected(crop):
    with pytest.raises(ValueError):
        calculate_crop_properties(crop, np.ones(crop.shape[:2], dtype=np.uint8))


@pytest.mark.parametrize(
    "mask",
    [
        np.zeros((4, 4), dtype=np.uint16),
        np.zeros((4, 4, 1), dtype=np.uint8),
        np.zeros((3, 4), dtype=np.uint8),
    ],
)
def test_invalid_crop_property_mask_rejected(mask):
    with pytest.raises(ValueError):
        calculate_crop_properties(np.zeros((4, 4, 3), dtype=np.uint8), mask)


def test_measurement_provenance_records_contract():
    provenance = measurement_provenance(SegToCutConfig())
    assert provenance["cutout_version"] == "2.0"
    assert provenance["rgb_statistics"]["pixel_selection"] == "whole_unmasked_crop"
    assert provenance["rgb_statistics"]["std_ddof"] == 0
    assert provenance["blur_effect"]["h_size"] == 11
    assert provenance["blur_effect"]["reduce_func"] == "max"
    assert provenance["blur_effect"]["pixel_selection"] == "whole_black_masked_cutout"
    assert provenance["blur_effect"]["background"] == "black"
    assert provenance["area_metrics"] == {
        "bbox_area_source": "georeferenced_csv",
        "world_area_algorithm": "planar_shoelace",
        "world_area_output_unit": "cm2",
        "camera_area_status": "unavailable_pending_xyz_camera_source",
        "species_bbox_grouping": "resolved_category",
        "species_bbox_min_sample_size": 5,
        "abnormal_bbox_size_threshold": 0.5,
    }
    assert provenance["libraries"]["scikit_image"]
    json.dumps(provenance, allow_nan=False)
