from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from stages.seg_to_cut import (
    ERROR_CSV_INVALID,
    ERROR_DIMENSION_MISMATCH,
    ERROR_DUPLICATE_DETECTION,
    ERROR_DUPLICATE_INPUT,
    ERROR_EMPTY_BOUNDING_BOX,
    ERROR_IMAGE_MISSING,
    ERROR_MASK_INVALID,
    ERROR_MASK_MISSING,
    ERROR_UNKNOWN_MASK_VALUE,
)
from stages.seg_to_cut.config import SegToCutConfig
from stages.seg_to_cut.contracts import PixelBoundingBox
from stages.seg_to_cut.errors import SegToCutInputError
from stages.seg_to_cut.processor import (
    discover_and_validate_inputs,
    load_detection_rows,
    normalized_bbox_to_pixels,
)

from .helpers import detection_row, make_input_paths, write_csv, write_image_and_mask


@pytest.fixture
def input_paths(tmp_path: Path) -> dict[str, Path]:
    return make_input_paths(tmp_path)


def validate(paths: dict[str, Path]):
    return discover_and_validate_inputs(
        images_dir=paths["images"],
        masks_dir=paths["masks"],
        georeferenced_csv=paths["csv"],
        species_catalog=paths["catalog"],
        config=SegToCutConfig(),
    )


def assert_error_code(code: str, callable_):
    with pytest.raises(SegToCutInputError) as caught:
        callable_()
    assert caught.value.code == code
    return caught.value


def test_discovers_matches_and_sorts_inputs(input_paths) -> None:
    write_image_and_mask(input_paths, "Image_B", mask_value=19)
    write_image_and_mask(input_paths, "image_a", mask_value=11)
    write_csv(
        input_paths,
        [
            detection_row("IMAGE_B", 2, species_id="BETVU"),
            detection_row("image_a", 3),
            detection_row("image_a", 1),
        ],
    )

    result = validate(input_paths)

    assert result.image_count == 2
    assert result.detection_count == 3
    assert [image.image_id for image in result.images] == ["image_a", "IMAGE_B"]
    assert [d.bounding_box_id for d in result.images[0].detections] == [1, 3]
    assert result.images[1].detections[0].class_id == 19
    assert result.known_class_ids == frozenset({11, 19, 107})


def test_cultivar_id_is_the_expected_mask_value(input_paths) -> None:
    write_image_and_mask(input_paths, "image_1", mask_value=107)
    write_csv(
        input_paths,
        [detection_row("image_1", 0, species_id="ABUTH", cultivar_id="107")],
    )

    result = validate(input_paths)

    assert result.images[0].detections[0].class_id == 107
    assert result.images[0].detections[0].cultivar_id == "107"


def test_normalized_bbox_is_clipped_with_half_open_edges() -> None:
    bbox = normalized_bbox_to_pixels(
        (-0.1, 0.21, 1.1, 0.89),
        width=20,
        height=10,
        image_id="image_1",
        bounding_box_id=4,
    )

    assert bbox == PixelBoundingBox(xmin=0, ymin=2, xmax=20, ymax=9)
    assert bbox.width == 20
    assert bbox.height == 7


@pytest.mark.parametrize(
    "bbox",
    [
        (0.5, 0.1, 0.5, 0.9),
        (1.1, 0.1, 1.2, 0.9),
        (0.8, 0.2, 0.2, 0.7),
    ],
)
def test_empty_bbox_after_clipping_is_rejected(bbox) -> None:
    error = assert_error_code(
        ERROR_EMPTY_BOUNDING_BOX,
        lambda: normalized_bbox_to_pixels(
            bbox, width=20, height=10, image_id="image_1", bounding_box_id=7
        ),
    )

    assert error.context["bounding_box_id"] == 7


def test_duplicate_detection_identity_is_case_insensitive(input_paths) -> None:
    write_csv(
        input_paths,
        [detection_row("Image_1", 0), detection_row("image_1", 0)],
    )

    assert_error_code(
        ERROR_DUPLICATE_DETECTION,
        lambda: load_detection_rows(input_paths["csv"]),
    )


def test_missing_csv_column_is_rejected(input_paths) -> None:
    input_paths["csv"].write_text(
        "image_id,bounding_box_id,xmin,ymin,xmax,ymax\nimage_1,0,0,0,1,1\n",
        encoding="utf-8",
    )

    assert_error_code(ERROR_CSV_INVALID, lambda: load_detection_rows(input_paths["csv"]))


@pytest.mark.parametrize("value", ["not-a-number", "nan", "inf"])
def test_invalid_normalized_coordinate_is_rejected(input_paths, value) -> None:
    row = detection_row("image_1", 0)
    row["xmin"] = value
    write_csv(input_paths, [row])

    assert_error_code(ERROR_CSV_INVALID, lambda: load_detection_rows(input_paths["csv"]))


@pytest.mark.parametrize("value", ["", "1.5", "-1"])
def test_invalid_bounding_box_id_is_rejected(input_paths, value) -> None:
    row = detection_row("image_1", 0)
    row["bounding_box_id"] = value
    write_csv(input_paths, [row])

    assert_error_code(ERROR_CSV_INVALID, lambda: load_detection_rows(input_paths["csv"]))


def test_missing_image_is_rejected(input_paths) -> None:
    write_csv(input_paths, [detection_row("image_1", 0)])

    assert_error_code(ERROR_IMAGE_MISSING, lambda: validate(input_paths))


def test_missing_mask_is_rejected(input_paths) -> None:
    image = np.zeros((10, 20, 3), dtype=np.uint8)
    assert cv2.imwrite(str(input_paths["images"] / "image_1.jpg"), image)
    write_csv(input_paths, [detection_row("image_1", 0)])

    assert_error_code(ERROR_MASK_MISSING, lambda: validate(input_paths))


def test_duplicate_image_stems_are_rejected(input_paths) -> None:
    write_image_and_mask(input_paths, "image_1")
    duplicate = np.zeros((10, 20, 3), dtype=np.uint8)
    assert cv2.imwrite(str(input_paths["images"] / "IMAGE_1.jpeg"), duplicate)
    write_csv(input_paths, [detection_row("image_1", 0)])

    assert_error_code(ERROR_DUPLICATE_INPUT, lambda: validate(input_paths))


def test_image_and_mask_dimensions_must_match(input_paths) -> None:
    write_image_and_mask(input_paths, "image_1", mask_shape=(9, 20))
    write_csv(input_paths, [detection_row("image_1", 0)])

    assert_error_code(ERROR_DIMENSION_MISMATCH, lambda: validate(input_paths))


@pytest.mark.parametrize(
    ("mask_dtype", "color_mask"),
    [(np.uint16, False), (np.uint8, True)],
)
def test_mask_must_be_single_channel_uint8(input_paths, mask_dtype, color_mask) -> None:
    write_image_and_mask(
        input_paths,
        "image_1",
        mask_dtype=mask_dtype,
        color_mask=color_mask,
    )
    write_csv(input_paths, [detection_row("image_1", 0)])

    assert_error_code(ERROR_MASK_INVALID, lambda: validate(input_paths))


def test_unknown_mask_foreground_value_is_rejected(input_paths) -> None:
    write_image_and_mask(input_paths, "image_1", mask_value=250)
    write_csv(input_paths, [detection_row("image_1", 0)])

    error = assert_error_code(ERROR_UNKNOWN_MASK_VALUE, lambda: validate(input_paths))

    assert error.context["values"] == [250]


def test_detection_class_must_exist_in_catalog(input_paths) -> None:
    write_image_and_mask(input_paths, "image_1", mask_value=11)
    write_csv(
        input_paths,
        [detection_row("image_1", 0, species_id="ABUTH", cultivar_id="108")],
    )

    assert_error_code(ERROR_CSV_INVALID, lambda: validate(input_paths))


def test_validation_does_not_write_cutouts(input_paths) -> None:
    write_image_and_mask(input_paths, "image_1")
    write_csv(input_paths, [detection_row("image_1", 0)])
    batch_root = input_paths["images"].parent
    before = sorted(path.relative_to(batch_root) for path in batch_root.rglob("*"))

    validate(input_paths)

    after = sorted(path.relative_to(batch_root) for path in batch_root.rglob("*"))
    assert after == before
