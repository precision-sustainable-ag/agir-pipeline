"""Tests for det_to_seg's batched inference and bbox compositing."""

from unittest.mock import patch

import numpy as np
import pytest
import torch
import torch.nn as nn

from stages.det_to_seg.segmentor import (
    DEFAULT_INFERENCE_BATCH_SIZE,
    _should_tile,
    composite_bbox_masks,
    predict_masks_batch,
)


class _OnesLogitModel(nn.Module):
    """
    1x1 conv stand-in for a real segmentation model — preserves input H/W
    exactly (no internal downsample/upsample), and its fixed bias lets tests
    control the output mask deterministically (positive bias -> everything
    above threshold after sigmoid; negative bias -> everything below).
    """

    def __init__(self, bias: float) -> None:
        super().__init__()
        self.conv = nn.Conv2d(3, 1, kernel_size=1)
        with torch.no_grad():
            self.conv.weight.zero_()
            self.conv.bias.fill_(bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.conv(x)


def _rgb(h: int, w: int) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


class TestShouldTile:
    def test_large_side_triggers_tiling(self):
        assert _should_tile(2000, 100, use_tiling=True, tile_trigger_side=1024, tile_trigger_area=1024 * 1024)

    def test_large_area_triggers_tiling(self):
        assert _should_tile(1100, 1100, use_tiling=True, tile_trigger_side=1024, tile_trigger_area=1024 * 1024)

    def test_small_crop_does_not_tile(self):
        assert not _should_tile(200, 200, use_tiling=True, tile_trigger_side=1024, tile_trigger_area=1024 * 1024)

    def test_tiling_disabled_never_tiles(self):
        assert not _should_tile(5000, 5000, use_tiling=False, tile_trigger_side=1024, tile_trigger_area=1024 * 1024)


class TestPredictMasksBatch:
    def test_empty_input_returns_empty_list(self):
        assert predict_masks_batch(_OnesLogitModel(10.0), [], thr=0.5, divisor=32, device="cpu") == []

    def test_positive_bias_yields_all_foreground(self):
        model = _OnesLogitModel(bias=10.0)
        masks = predict_masks_batch(model, [_rgb(40, 50)], thr=0.5, divisor=32, device="cpu")
        assert len(masks) == 1
        assert masks[0].shape == (40, 50)
        assert (masks[0] == 1).all()

    def test_negative_bias_yields_all_background(self):
        model = _OnesLogitModel(bias=-10.0)
        masks = predict_masks_batch(model, [_rgb(40, 50)], thr=0.5, divisor=32, device="cpu")
        assert (masks[0] == 0).all()

    def test_differently_sized_crops_each_keep_their_own_shape(self):
        # Crops in the same batch get padded up to the batch's max size
        # internally — each returned mask must still match its own crop's
        # original (unpadded) shape, not the batch's padded shape.
        model = _OnesLogitModel(bias=10.0)
        crops = [_rgb(20, 30), _rgb(64, 64), _rgb(17, 100)]
        masks = predict_masks_batch(model, crops, thr=0.5, divisor=32, device="cpu")
        assert [m.shape for m in masks] == [(20, 30), (64, 64), (17, 100)]

    def test_no_divisor_still_works(self):
        model = _OnesLogitModel(bias=10.0)
        masks = predict_masks_batch(model, [_rgb(33, 47)], thr=0.5, divisor=None, device="cpu")
        assert masks[0].shape == (33, 47)


class TestCompositeBboxMasks:
    _CONFIG = {
        "threshold": 0.5,
        "pad_divisor": 32,
        "tile_size": 1024,
        "overlap": 128,
        "tiling": True,
    }

    def test_composites_batchable_boxes_into_full_mask(self):
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(100, 100)
        boxes = [(10, 10, 30, 30), (50, 50, 70, 80)]

        mask = composite_bbox_masks(model, image, boxes, self._CONFIG, device="cpu")

        assert mask.shape == (100, 100)
        assert (mask[10:30, 10:30] == 1).all()
        assert (mask[50:80, 50:70] == 1).all()
        # Outside any box, nothing should be set.
        assert mask[0, 0] == 0
        assert mask[99, 99] == 0

    def test_clips_boxes_to_image_bounds(self):
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(50, 50)
        # Box extends past the image on both edges.
        boxes = [(-10, -10, 20, 20)]

        mask = composite_bbox_masks(model, image, boxes, self._CONFIG, device="cpu")

        assert (mask[0:20, 0:20] == 1).all()

    def test_out_of_bounds_box_still_produces_a_minimal_valid_crop(self):
        # The clamp logic (x1 in [0, w-1], x2 in [x1+1, w]) always yields at
        # least a 1px crop, even for a box entirely outside the image — it
        # never actually collapses to size 0, so this should process
        # normally rather than being skipped or raising.
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(50, 50)
        boxes = [(200, 200, 220, 220)]

        mask = composite_bbox_masks(model, image, boxes, self._CONFIG, device="cpu")

        assert mask[49, 49] == 1
        assert mask[0, 0] == 0

    def test_large_box_routes_through_tiled_path_not_batched_path(self):
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(1200, 1200)
        boxes = [(0, 0, 1200, 1200)]  # exceeds the 1024 tile-trigger side

        with patch(
            "stages.det_to_seg.segmentor.predict_mask_tiled",
            return_value=np.ones((1200, 1200), dtype=np.uint8),
        ) as mock_tiled, patch(
            "stages.det_to_seg.segmentor.predict_masks_batch",
        ) as mock_batch:
            mask = composite_bbox_masks(model, image, boxes, self._CONFIG, device="cpu")

        mock_tiled.assert_called_once()
        mock_batch.assert_not_called()
        assert (mask == 1).all()

    def test_small_boxes_route_through_batched_path_not_tiled_path(self):
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(100, 100)
        boxes = [(10, 10, 30, 30), (50, 50, 70, 70)]

        with patch(
            "stages.det_to_seg.segmentor.predict_mask_tiled",
        ) as mock_tiled, patch(
            "stages.det_to_seg.segmentor.predict_masks_batch",
            return_value=[np.ones((20, 20), dtype=np.uint8), np.ones((20, 20), dtype=np.uint8)],
        ) as mock_batch:
            composite_bbox_masks(model, image, boxes, self._CONFIG, device="cpu")

        mock_tiled.assert_not_called()
        mock_batch.assert_called_once()

    def test_batchable_boxes_are_chunked_by_batch_size(self):
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(200, 200)
        # 5 boxes with batch_size=2 -> 3 chunks (2, 2, 1).
        boxes = [(x, x, x + 10, x + 10) for x in range(0, 100, 20)]
        config = {**self._CONFIG, "batch_size": 2}

        with patch(
            "stages.det_to_seg.segmentor.predict_masks_batch",
            side_effect=lambda model, crops, **kw: [np.ones(c.shape[:2], dtype=np.uint8) for c in crops],
        ) as mock_batch:
            composite_bbox_masks(model, image, boxes, config, device="cpu")

        assert mock_batch.call_count == 3
        chunk_sizes = sorted(len(call.args[1]) for call in mock_batch.call_args_list)
        assert chunk_sizes == [1, 2, 2]

    def test_default_batch_size_used_when_not_configured(self):
        assert "batch_size" not in self._CONFIG  # sanity: config below omits it
        model = _OnesLogitModel(bias=10.0)
        image = _rgb(500, 500)
        # More boxes than DEFAULT_INFERENCE_BATCH_SIZE, spaced out so none
        # overlap, to actually exercise the chunk-size boundary.
        n_boxes = DEFAULT_INFERENCE_BATCH_SIZE * 2 + 5
        boxes = [(x, x, x + 5, x + 5) for x in range(0, n_boxes * 10, 10)]

        with patch(
            "stages.det_to_seg.segmentor.predict_masks_batch",
            side_effect=lambda model, crops, **kw: [np.ones(c.shape[:2], dtype=np.uint8) for c in crops],
        ) as mock_batch:
            composite_bbox_masks(model, image, boxes, self._CONFIG, device="cpu")

        chunk_sizes = [len(call.args[1]) for call in mock_batch.call_args_list]
        assert max(chunk_sizes) == DEFAULT_INFERENCE_BATCH_SIZE
        assert sum(chunk_sizes) == n_boxes
