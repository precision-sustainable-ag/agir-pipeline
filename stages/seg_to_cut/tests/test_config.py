from __future__ import annotations

import pytest

from stages.seg_to_cut import ERROR_CONFIG_INVALID
from stages.seg_to_cut.config import SegToCutConfig, load_config, parse_config
from stages.seg_to_cut.errors import SegToCutConfigError


def test_packaged_default_config_loads() -> None:
    assert load_config() == SegToCutConfig(
        image_extensions=(".jpg", ".jpeg"),
        mask_extension=".png",
    )


def test_config_normalizes_extensions() -> None:
    config = parse_config(
        {"image_extensions": [".JPG", ".JPEG"], "mask_extension": ".PNG"}
    )

    assert config.image_extensions == (".jpg", ".jpeg")
    assert config.mask_extension == ".png"


@pytest.mark.parametrize(
    "config",
    [
        {"image_extensions": ".jpg"},
        {"image_extensions": []},
        {"image_extensions": ["jpg"]},
        {"image_extensions": [".jpg", ".JPG"]},
        {"mask_extension": "png"},
    ],
)
def test_invalid_config_has_stable_error_code(config) -> None:
    with pytest.raises(SegToCutConfigError) as caught:
        parse_config(config)

    assert caught.value.code == ERROR_CONFIG_INVALID

