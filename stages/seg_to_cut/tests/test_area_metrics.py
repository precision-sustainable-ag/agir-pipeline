from __future__ import annotations

import json

import numpy as np
import pytest

from stages.seg_to_cut.config import SegToCutConfig
from stages.seg_to_cut.contracts import AreaMetricInput, PixelBoundingBox, WorldBoundingBox
from stages.seg_to_cut.metadata import (
    calculate_area_properties,
    calculate_bbox_area_cm2,
    calculate_cutout_properties,
    finalize_species_bbox_metrics,
    null_metadata_reasons,
)


def world_box(
    *,
    top_left=(0.0, 3.0),
    top_right=(2.0, 3.0),
    bottom_left=(0.0, 0.0),
    bottom_right=(2.0, 0.0),
    crs="EPSG:32617",
) -> WorldBoundingBox:
    return WorldBoundingBox(
        top_left=top_left,
        top_right=top_right,
        bottom_left=bottom_left,
        bottom_right=bottom_right,
        crs=crs,
    )


def area_record(
    bounding_box_id: int,
    estimate: float | None,
    *,
    species_id: str = "ABUTH",
    cultivar_id: str | None = None,
) -> AreaMetricInput:
    return AreaMetricInput(
        image_id="image_1",
        bounding_box_id=bounding_box_id,
        species_id=species_id,
        cultivar_id=cultivar_id,
        bbox_area_cm2=estimate,
    )


def test_world_area_uses_ordered_quadrilateral_and_converts_metres_to_cm2() -> None:
    assert calculate_bbox_area_cm2(world_box()) == pytest.approx(60_000.0)


def test_world_area_is_stable_for_large_utm_coordinates() -> None:
    bbox = world_box(
        top_left=(500_000.0, 3_900_003.0),
        top_right=(500_002.0, 3_900_003.0),
        bottom_left=(500_000.0, 3_900_000.0),
        bottom_right=(500_002.0, 3_900_000.0),
    )
    assert calculate_bbox_area_cm2(bbox) == pytest.approx(60_000.0)


def test_world_area_converts_projected_non_metre_units() -> None:
    result = calculate_bbox_area_cm2(world_box(crs="EPSG:2264"))
    us_survey_foot_metres = 0.30480060960121924
    assert result == pytest.approx(6 * us_survey_foot_metres**2 * 10_000)


@pytest.mark.parametrize(
    "bbox",
    [
        None,
        world_box(crs="EPSG:4326"),
        world_box(crs="not-a-crs"),
        world_box(bottom_right=(0.0, 0.0)),
        world_box(top_right=(2.0, 0.0), bottom_right=(2.0, 3.0)),
        world_box(top_right=(float("nan"), 3.0)),
        world_box(top_right=("invalid", 3.0)),
    ],
)
def test_unsupported_or_invalid_world_area_is_null(bbox) -> None:
    assert calculate_bbox_area_cm2(bbox) is None


def test_area_properties_use_georeferenced_csv_source() -> None:
    result = calculate_area_properties(world_box(), config=SegToCutConfig())
    assert result == {"bbox_area_cm2": pytest.approx(60_000.0)}
    json.dumps(result, allow_nan=False)


def test_camera_source_is_null_until_xyz_input_is_defined() -> None:
    config = SegToCutConfig(bbox_area_source="camera")
    assert calculate_area_properties(world_box(), config=config) == {"bbox_area_cm2": None}


@pytest.mark.parametrize(
    "bbox,source,reason",
    [(None, "georeferenced_csv", "missing"),
     (world_box(crs="EPSG:4326"), "georeferenced_csv", "not projected"),
     (world_box(), "camera", "XYZ")],
)
def test_null_reasons_identify_area_source_and_dependencies(bbox, source, reason):
    config = SegToCutConfig(bbox_area_source=source)
    metadata = {
        "datetime": None, "lens_model": None, "season": None,
        "cutout_props": {"bbox_area_cm2": None, "species_mean_bbox_area_cm2": None,
                         "species_bbox_sample_size": 0, "species_bbox_area_ratio": None,
                         "abnormal_bbox_size": None},
    }
    reasons = null_metadata_reasons(metadata, world_bbox=bbox, config=config)
    assert len(reasons) == 7
    assert reason in reasons["bbox_area_cm2"]
    assert "0 valid area samples" in reasons["species_mean_bbox_area_cm2"]
    assert "bbox_area_cm2, species_mean_bbox_area_cm2" in reasons["species_bbox_area_ratio"]


def test_available_fields_have_no_null_reasons():
    metadata = {"datetime": "2026-07-17", "lens_model": "lens", "season": "weeds",
                "cutout_props": {"bbox_area_cm2": 100.0, "species_mean_bbox_area_cm2": 100.0,
                                 "species_bbox_area_ratio": 1.0, "abnormal_bbox_size": False}}
    assert null_metadata_reasons(metadata, world_bbox=world_box(), config=SegToCutConfig()) == {}


def test_cutout_properties_include_available_area_fields() -> None:
    rgb_crop = np.full((3, 3, 3), 127, dtype=np.uint8)
    cleaned_mask = np.ones((3, 3), dtype=np.uint8)

    result = calculate_cutout_properties(
        rgb_crop,
        cleaned_mask,
        expected_class_id=1,
        pixel_bbox=PixelBoundingBox(xmin=0, ymin=0, xmax=3, ymax=3),
        image_width=3,
        image_height=3,
        config=SegToCutConfig(),
        world_bbox=world_box(),
    )

    assert result["bbox_area_cm2"] == pytest.approx(60_000.0)


def test_exact_abnormality_threshold_is_not_flagged() -> None:
    records = [area_record(index, value) for index, value in enumerate((50, 100, 100, 100, 150))]
    result = finalize_species_bbox_metrics(records, config=SegToCutConfig())

    assert result[("image_1", 0)] == {
        "species_mean_bbox_area_cm2": pytest.approx(100),
        "species_bbox_sample_size": 5,
        "species_bbox_area_ratio": pytest.approx(0.5),
        "abnormal_bbox_size": False,
    }
    assert result[("image_1", 4)]["species_bbox_area_ratio"] == pytest.approx(1.5)
    assert result[("image_1", 4)]["abnormal_bbox_size"] is False


def test_values_beyond_abnormality_threshold_are_flagged() -> None:
    records = [area_record(index, value) for index, value in enumerate((40, 100, 100, 100, 160))]
    result = finalize_species_bbox_metrics(records, config=SegToCutConfig())

    assert result[("image_1", 0)]["abnormal_bbox_size"] is True
    assert result[("image_1", 4)]["abnormal_bbox_size"] is True


def test_resolved_category_groups_cultivars_separately() -> None:
    records = (
        area_record(0, 10, cultivar_id="107"),
        area_record(1, 20, cultivar_id="107"),
        area_record(2, 100),
        area_record(3, 200),
    )
    config = SegToCutConfig(species_bbox_min_sample_size=2)
    result = finalize_species_bbox_metrics(records, config=config)

    assert result[("image_1", 0)]["species_mean_bbox_area_cm2"] == pytest.approx(15)
    assert result[("image_1", 0)]["species_bbox_sample_size"] == 2
    assert result[("image_1", 2)]["species_mean_bbox_area_cm2"] == pytest.approx(150)
    assert result[("image_1", 2)]["species_bbox_sample_size"] == 2


def test_aggregation_is_independent_of_record_order() -> None:
    records = tuple(area_record(index, value) for index, value in enumerate((25, 50, 75, 100)))
    config = SegToCutConfig(species_bbox_min_sample_size=2)

    forward = finalize_species_bbox_metrics(records, config=config)
    reverse = finalize_species_bbox_metrics(tuple(reversed(records)), config=config)

    assert forward == reverse


def test_invalid_estimates_are_excluded_and_minimum_sample_is_enforced() -> None:
    records = tuple(
        area_record(index, value)
        for index, value in enumerate((10, None, float("nan"), float("inf"), 0, -1))
    )
    result = finalize_species_bbox_metrics(
        records, config=SegToCutConfig(species_bbox_min_sample_size=2)
    )

    for item in result.values():
        assert item == {
            "species_mean_bbox_area_cm2": None,
            "species_bbox_sample_size": 1,
            "species_bbox_area_ratio": None,
            "abnormal_bbox_size": None,
        }
        json.dumps(item, allow_nan=False)


def test_missing_area_leaves_all_derived_values_null() -> None:
    records = tuple(area_record(index, None) for index in range(6))
    result = finalize_species_bbox_metrics(records, config=SegToCutConfig())

    assert all(item["species_bbox_sample_size"] == 0 for item in result.values())
    assert all(item["species_mean_bbox_area_cm2"] is None for item in result.values())
    assert all(item["species_bbox_area_ratio"] is None for item in result.values())
    assert all(item["abnormal_bbox_size"] is None for item in result.values())


def test_duplicate_area_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate area-metric identity"):
        finalize_species_bbox_metrics(
            (area_record(0, 10), area_record(0, 20)), config=SegToCutConfig()
        )


def test_empty_species_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="species_id must be non-empty"):
        finalize_species_bbox_metrics((area_record(0, 10, species_id=""),), config=SegToCutConfig())
