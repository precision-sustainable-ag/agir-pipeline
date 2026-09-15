from __future__ import annotations

import pytest

from stages.seg_to_cut import ERROR_CONFIG_INVALID
from stages.seg_to_cut.config import SegToCutConfig, load_config, parse_config
from stages.seg_to_cut.errors import SegToCutConfigError


def test_packaged_default_config_loads() -> None:
    assert load_config() == SegToCutConfig(
        image_extensions=(".jpg", ".jpeg"),
        mask_extension=".png",
        border_width_px=100,
    )


def test_config_normalizes_extensions() -> None:
    config = parse_config({"image_extensions": [".JPG", ".JPEG"], "mask_extension": ".PNG"})

    assert config.image_extensions == (".jpg", ".jpeg")
    assert config.mask_extension == ".png"
    assert config.border_width_px == 100


def test_config_accepts_border_width() -> None:
    assert parse_config({"border_width_px": 5}).border_width_px == 5


@pytest.mark.parametrize(
    "config",
    [
        {"image_extensions": ".jpg"},
        {"image_extensions": []},
        {"image_extensions": ["jpg"]},
        {"image_extensions": [".jpg", ".JPG"]},
        {"mask_extension": "png"},
        {"border_width_px": 0},
        {"border_width_px": -1},
        {"border_width_px": 1.5},
        {"border_width_px": True},
    ],
)
def test_invalid_config_has_stable_error_code(config) -> None:
    with pytest.raises(SegToCutConfigError) as caught:
        parse_config(config)

    assert caught.value.code == ERROR_CONFIG_INVALID


def test_measurement_configuration():
    config = parse_config(
        {
            "border_width_px": 5,
            "edge_threshold": 0.25,
            "cutout_version": "2.1",
            "bbox_area": {"source": "camera"},
            "species_bbox_min_sample_size": 8,
            "abnormal_bbox_size_threshold": 0.25,
        }
    )
    assert config.border_width_px == 5
    assert config.edge_threshold == 0.25
    assert config.cutout_version == "2.1"
    assert config.bbox_area_source == "camera"
    assert config.species_bbox_min_sample_size == 8
    assert config.abnormal_bbox_size_threshold == 0.25


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True, "0.05"])
def test_invalid_edge_threshold(value):
    with pytest.raises(SegToCutConfigError):
        parse_config({"edge_threshold": value})


@pytest.mark.parametrize("value", [None, "", "  ", 2.0])
def test_invalid_cutout_version(value):
    with pytest.raises(SegToCutConfigError):
        parse_config({"cutout_version": value})


@pytest.mark.parametrize(
    "value",
    ["camera", [], {"source": "world"}, {"source": None}, {"source": 1}],
)
def test_invalid_bbox_area_configuration(value):
    with pytest.raises(SegToCutConfigError):
        parse_config({"bbox_area": value})


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_species_bbox_min_sample_size(value):
    with pytest.raises(SegToCutConfigError):
        parse_config({"species_bbox_min_sample_size": value})


@pytest.mark.parametrize("value", [-0.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_abnormal_bbox_size_threshold(value):
    with pytest.raises(SegToCutConfigError):
        parse_config({"abnormal_bbox_size_threshold": value})


def test_abnormal_bbox_size_threshold_can_exceed_one() -> None:
    assert parse_config({"abnormal_bbox_size_threshold": 1.5}).abnormal_bbox_size_threshold == 1.5
