"""Tests for batched inference and class-ID mask compositing."""

from unittest.mock import patch

import cv2
import numpy as np
import pytest
import torch

from stages.common.class_ids import DetectionBox
from stages.det_to_seg.segmentor import (
    composite_bbox_masks,
    predict_masks_batched,
    validate_class_mask,
    write_mask_png,
)


CONFIG = {
    "threshold": 0.5,
    "pad_divisor": 32,
    "tile_size": 1024,
    "overlap": 128,
    "tiling": True,
    "batch_size": 2,
}


def _detection(bounding_box_id, xyxy, class_id):
    return DetectionBox(
        bounding_box_id=bounding_box_id,
        xyxy=xyxy,
        class_id=class_id,
    )


def _foreground_masks(*, crops_rgb, **kwargs):
    return [np.ones(crop.shape[:2], dtype=np.uint8) for crop in crops_rgb]


def test_predict_masks_batched_uses_one_model_call_for_the_chunk():
    class RecordingModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.batch_sizes = []

        def forward(self, inputs):
            self.batch_sizes.append(inputs.shape[0])
            return torch.full(
                (inputs.shape[0], 1, inputs.shape[2], inputs.shape[3]),
                10.0,
                device=inputs.device,
            )

    model = RecordingModel()
    crops = [np.zeros((5, 7, 3), dtype=np.uint8) for _ in range(3)]

    masks = predict_masks_batched(model, crops, thr=0.5, divisor=4, device="cpu")

    assert model.batch_sizes == [3]
    assert len(masks) == 3
    assert all(mask.shape == (5, 7) for mask in masks)
    assert all(np.all(mask == 1) for mask in masks)


def test_composites_class_ids_with_first_detection_winning_overlap():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    detections = [
        _detection(0, (0, 0, 4, 4), 11),
        _detection(1, (2, 2, 6, 6), 19),
    ]

    with patch(
        "stages.det_to_seg.segmentor.predict_masks_batched",
        side_effect=_foreground_masks,
    ):
        mask = composite_bbox_masks(object(), image, detections, CONFIG, "cpu")

    assert mask.dtype == np.uint8
    assert mask.shape == (8, 8)
    assert mask[1, 1] == 11
    assert mask[3, 3] == 11
    assert mask[5, 5] == 19
    assert mask[7, 7] == 0
    assert set(np.unique(mask)) == {0, 11, 19}


def test_groups_equal_sized_crops_and_chunks_by_batch_size():
    image = np.zeros((12, 12, 3), dtype=np.uint8)
    detections = [
        _detection(index, (index * 2, 0, index * 2 + 2, 2), index + 1)
        for index in range(5)
    ]

    with patch(
        "stages.det_to_seg.segmentor.predict_masks_batched",
        side_effect=_foreground_masks,
    ) as mock_batched:
        composite_bbox_masks(object(), image, detections, CONFIG, "cpu")

    assert [len(call.kwargs["crops_rgb"]) for call in mock_batched.call_args_list] == [2, 2, 1]


def test_large_crop_is_inferred_individually():
    image = np.zeros((1025, 2, 3), dtype=np.uint8)
    detections = [_detection(0, (0, 0, 2, 1025), 27)]

    with (
        patch(
            "stages.det_to_seg.segmentor.predict_mask",
            return_value=np.ones((1025, 2), dtype=np.uint8),
        ) as mock_individual,
        patch("stages.det_to_seg.segmentor.predict_masks_batched") as mock_batched,
    ):
        mask = composite_bbox_masks(object(), image, detections, CONFIG, "cpu")

    mock_individual.assert_called_once()
    assert mock_individual.call_args.kwargs["use_tiling"] is True
    mock_batched.assert_not_called()
    assert np.all(mask == 27)


def test_zero_detections_returns_background_without_inference():
    image = np.zeros((5, 7, 3), dtype=np.uint8)

    with (
        patch("stages.det_to_seg.segmentor.predict_mask") as mock_individual,
        patch("stages.det_to_seg.segmentor.predict_masks_batched") as mock_batched,
    ):
        mask = composite_bbox_masks(object(), image, [], CONFIG, "cpu")

    assert mask.shape == (5, 7)
    assert mask.dtype == np.uint8
    assert np.count_nonzero(mask) == 0
    mock_individual.assert_not_called()
    mock_batched.assert_not_called()


def test_class_zero_is_rejected_because_zero_is_background():
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    detections = [_detection(0, (0, 0, 4, 4), 0)]

    with pytest.raises(ValueError, match="range 1..255"):
        composite_bbox_masks(object(), image, detections, CONFIG, "cpu")


@pytest.mark.parametrize(
    "mask, message",
    [
        (np.zeros((4, 4), dtype=np.float32), "dtype uint8"),
        (np.zeros((4, 4, 1), dtype=np.uint8), "single-channel"),
        (np.zeros((3, 4), dtype=np.uint8), "does not match"),
    ],
)
def test_validate_class_mask_rejects_invalid_output(mask, message):
    with pytest.raises(ValueError, match=message):
        validate_class_mask(mask, expected_shape=(4, 4))


def test_write_mask_png_preserves_class_values(tmp_path):
    mask = np.array([[0, 11], [27, 107]], dtype=np.uint8)
    output = tmp_path / "image.png"

    write_mask_png(mask, output, expected_shape=(2, 2))

    written = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
    assert written.dtype == np.uint8
    assert written.shape == (2, 2)
    assert np.array_equal(written, mask)
