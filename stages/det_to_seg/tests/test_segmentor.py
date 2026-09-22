"""Tests for batched inference and class-ID mask compositing."""

from unittest.mock import patch

import cv2
import numpy as np
import pytest
import torch

from stages.common.class_ids import DetectionBox
from stages.det_to_seg.segmentor import (
    _hann2d,
    composite_bbox_masks,
    predict_mask_tiled,
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


def test_predict_masks_batched_handles_varied_shapes_in_one_call():
    # Before padding-based batching, this would raise "must all have the
    # same height and width" -- real YOLO boxes essentially never share an
    # exact pixel size, so this is the case that matters in production.
    class RecordingModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.call_shapes = []

        def forward(self, inputs):
            self.call_shapes.append(tuple(inputs.shape))
            return torch.full(
                (inputs.shape[0], 1, inputs.shape[2], inputs.shape[3]),
                10.0,
                device=inputs.device,
            )

    model = RecordingModel()
    crops = [
        np.zeros((5, 7, 3), dtype=np.uint8),
        np.zeros((20, 9, 3), dtype=np.uint8),
        np.zeros((11, 30, 3), dtype=np.uint8),
    ]

    masks = predict_masks_batched(model, crops, thr=0.5, divisor=4, device="cpu")

    assert (
        len(model.call_shapes) == 1
    ), "all 3 differently-shaped crops should cost one forward call"
    assert model.call_shapes[0][0] == 3
    assert [mask.shape for mask in masks] == [(5, 7), (20, 9), (11, 30)]
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


def test_chunks_varied_sized_crops_together_sorted_by_longest_side():
    image = np.zeros((20, 20, 3), dtype=np.uint8)
    detections = [
        _detection(0, (0, 0, 3, 3), 1),  # 3x3 crop, longest side 3
        _detection(1, (5, 5, 9, 10), 2),  # (h=5,w=4) crop, longest side 5
        _detection(2, (10, 10, 14, 12), 3),  # (h=2,w=4) crop, longest side 4
    ]
    config = {**CONFIG, "batch_size": 3}

    with patch(
        "stages.det_to_seg.segmentor.predict_masks_batched",
        side_effect=_foreground_masks,
    ) as mock_batched:
        composite_bbox_masks(object(), image, detections, config, "cpu")

    mock_batched.assert_called_once()  # all 3, differently shaped, fit in one chunk
    called_shapes = [crop.shape[:2] for crop in mock_batched.call_args.kwargs["crops_rgb"]]
    # sorted ascending by longest side: detection 0 (3), detection 2 (4), detection 1 (5)
    assert called_shapes == [(3, 3), (2, 4), (5, 4)]


def test_padded_batch_does_not_leak_beyond_each_crops_own_bbox():
    # A tiny crop batched with a much larger one forces heavy padding on the
    # tiny crop's inference canvas. If the padded region were resized back
    # down instead of sliced, or sliced incorrectly, the tiny crop's mask
    # would come back the wrong content and could corrupt the composite
    # beyond its own bbox.
    image = np.zeros((40, 40, 3), dtype=np.uint8)
    detections = [
        _detection(0, (0, 0, 4, 4), 5),  # small: 4x4
        _detection(1, (10, 10, 34, 34), 9),  # ordinary: 24x24
    ]
    config = {**CONFIG, "batch_size": 2, "pad_divisor": 8}

    class AllForegroundModel(torch.nn.Module):
        def forward(self, inputs):
            return torch.full(
                (inputs.shape[0], 1, inputs.shape[2], inputs.shape[3]),
                10.0,
                device=inputs.device,
            )

    mask = composite_bbox_masks(AllForegroundModel().eval(), image, detections, config, "cpu")

    assert mask.shape == (40, 40)
    assert np.all(mask[0:4, 0:4] == 5)
    assert np.all(mask[0:4, 4:10] == 0)  # nothing leaked right of detection 0
    assert np.all(mask[4:10, 0:4] == 0)  # nothing leaked below detection 0
    assert np.all(mask[10:34, 10:34] == 9)
    assert np.count_nonzero(mask) == 4 * 4 + 24 * 24


def test_batch_size_one_infers_each_ordinary_crop_individually():
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    detections = [
        _detection(0, (0, 0, 3, 3), 1),
        _detection(1, (4, 4, 8, 9), 2),  # different shape than detection 0
    ]
    config = {**CONFIG, "batch_size": 1}

    with patch(
        "stages.det_to_seg.segmentor.predict_masks_batched",
        side_effect=_foreground_masks,
    ) as mock_batched:
        composite_bbox_masks(object(), image, detections, config, "cpu")

    assert [len(call.kwargs["crops_rgb"]) for call in mock_batched.call_args_list] == [1, 1]


def test_batching_tiny_crop_with_near_max_ordinary_crop_does_not_crash():
    # Reflect padding is only legal when the pad amount is smaller than the
    # crop's OWN native size. A tiny crop chunked with a crop near the 1024px
    # "large crop" ceiling forces heavy padding on the tiny crop -- if
    # legality were decided against the chunk's shape (or the other crop's
    # shape) instead of each crop's own, this would hit PyTorch's reflect-pad
    # legality error.
    image = np.zeros((1024, 1600, 3), dtype=np.uint8)
    detections = [
        _detection(0, (0, 0, 3, 3), 5),  # tiny: 3x3
        _detection(1, (500, 0, 1520, 1020), 9),  # near-1024px ordinary crop, non-overlapping bbox
    ]
    config = {**CONFIG, "batch_size": 2}

    class ConstModel(torch.nn.Module):
        def forward(self, inputs):
            return torch.full(
                (inputs.shape[0], 1, inputs.shape[2], inputs.shape[3]),
                10.0,
                device=inputs.device,
            )

    mask = composite_bbox_masks(ConstModel().eval(), image, detections, config, "cpu")

    assert mask.shape == (1024, 1600)
    assert np.all(mask[0:3, 0:3] == 5)
    assert np.all(mask[0:1020, 500:1520] == 9)
    assert np.count_nonzero(mask) == 3 * 3 + 1020 * 1020


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


def test_tiled_inference_batches_tiles_into_one_call():
    class RecordingModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.call_sizes = []

        def forward(self, inputs):
            self.call_sizes.append(inputs.shape[0])
            return torch.full(
                (inputs.shape[0], 1, inputs.shape[2], inputs.shape[3]),
                10.0,
                device=inputs.device,
            )

    model = RecordingModel()
    # 300x300 crop, tile_size=200, overlap=0 -> step=200 -> 4 tiles
    # (0,0),(0,200),(200,0),(200,200), well within batch_size=16.
    crop = np.zeros((300, 300, 3), dtype=np.uint8)

    mask = predict_mask_tiled(
        model,
        crop,
        thr=0.5,
        divisor=8,
        device="cpu",
        tile_size=200,
        overlap=0,
        batch_size=16,
    )

    assert model.call_sizes == [4], "all 4 tiles should cost exactly one forward call"
    assert mask.shape == (300, 300)
    assert np.all(mask == 1)


def test_tiled_inference_blends_continuous_probabilities_before_thresholding():
    # Two tiles (T0: cols 0-5, T1: cols 4-7) overlap at cols 4-5. Each tile
    # gets a distinct, controlled probability from a fake model. The
    # implementation must accumulate CONTINUOUS probabilities weighted by the
    # Hann window and threshold once at the end -- not threshold each tile's
    # own prediction first and then blend 0/1 values, which is the seam-
    # corrupting bug batching tiles makes easy to introduce by accident (e.g.
    # by routing tile-batching through the thresholding predict_masks_batched
    # instead of the raw-probability primitive both paths share).
    p0, p1 = 0.6, 0.2  # p0 alone would threshold to 1; p1 alone to 0
    logit0 = float(np.log(p0 / (1 - p0)))
    logit1 = float(np.log(p1 / (1 - p1)))

    class TwoTileModel(torch.nn.Module):
        def forward(self, x):
            assert x.shape[0] == 2, "both tiles should be batched into one call"
            out = torch.empty((2, 1, x.shape[2], x.shape[3]))
            out[0] = logit0
            out[1] = logit1
            return out

    crop = np.zeros((4, 8, 3), dtype=np.uint8)  # content unused by this fake model
    thr = 0.5

    mask = predict_mask_tiled(
        TwoTileModel(),
        crop,
        thr=thr,
        divisor=None,
        device="cpu",
        tile_size=6,
        overlap=2,
        batch_size=16,
    )
    assert mask.shape == (4, 8)

    # Independently derive the true Hann-weighted blend at the seam (row 0,
    # col 4) using the same _hann2d the implementation calls, combined here
    # explicitly -- not by calling predict_mask_tiled itself.
    win0 = _hann2d(4, 6)  # T0 spans (y0:4, x0:6)
    win1 = _hann2d(4, 4)  # T1 spans (y0:4, x4:8)
    w0, w1 = win0[0, 4], win1[0, 0]  # col 4 is local col 4 in T0, local col 0 in T1
    blended = (p0 * w0 + p1 * w1) / (w0 + w1)
    expected_seam = 1 if blended >= thr else 0

    # This pixel is deliberately chosen so the correct continuous blend
    # disagrees with a threshold-then-blend implementation (which would
    # average T0's own thresholded 1 and T1's own thresholded 0 and land on
    # the other side of thr here) -- otherwise this test couldn't catch that
    # bug.
    threshold_then_blend = (1 * w0 + 0 * w1) / (w0 + w1)
    assert (threshold_then_blend >= thr) != (expected_seam == 1)

    assert mask[0, 4] == expected_seam
    assert mask[0, 0] == (1 if p0 >= thr else 0)  # T0-only region
    assert mask[0, 7] == (1 if p1 >= thr else 0)  # T1-only region


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
