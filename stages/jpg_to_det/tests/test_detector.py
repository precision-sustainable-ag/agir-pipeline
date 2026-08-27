"""
Tests for jpg_to_det detector — mirror-pad inference helpers.

All YOLO/model calls are mocked; only the padding/unpadding logic under our
control is exercised.
"""

from unittest.mock import MagicMock

import numpy as np
import pytest
import torch

from stages.jpg_to_det.detector import (
    mirror_pad_image,
    run_multiscale,
    unpad_and_clip_detections,
)


# ================ mirror_pad_image ================

class TestMirrorPadImage:
    def test_noop_when_pad_px_zero(self):
        im = np.zeros((10, 10, 3), dtype=np.uint8)
        assert mirror_pad_image(im, 0) is im

    def test_noop_when_pad_px_negative(self):
        im = np.zeros((10, 10, 3), dtype=np.uint8)
        assert mirror_pad_image(im, -5) is im

    def test_padded_shape(self):
        im = np.zeros((10, 20, 3), dtype=np.uint8)
        padded = mirror_pad_image(im, 5)
        assert padded.shape == (20, 30, 3)

    def test_original_content_preserved_in_center(self):
        im = np.random.randint(0, 255, (10, 20, 3), dtype=np.uint8)
        pad_px = 6
        padded = mirror_pad_image(im, pad_px)
        center = padded[pad_px : pad_px + 10, pad_px : pad_px + 20]
        np.testing.assert_array_equal(center, im)


# ================ unpad_and_clip_detections ================

class TestUnpadAndClipDetections:
    def test_noop_when_pad_px_zero(self):
        dets = torch.tensor([[10.0, 10.0, 30.0, 30.0, 0.9, 0.0]])
        out = unpad_and_clip_detections(dets, 0, (100, 100))
        assert torch.equal(out, dets)

    def test_noop_when_empty(self):
        dets = torch.zeros((0, 6), dtype=torch.float32)
        out = unpad_and_clip_detections(dets, 20, (100, 100))
        assert out.shape == (0, 6)

    def test_shifts_interior_box(self):
        # Padded canvas is 140x140 (pad_px=20 around a 100x100 original).
        # Box fully inside the original region after shifting back.
        dets = torch.tensor([[30.0, 40.0, 50.0, 60.0, 0.9, 1.0]])
        out = unpad_and_clip_detections(dets, 20, (100, 100))
        assert out.shape == (1, 6)
        x1, y1, x2, y2, conf, cls = out[0].tolist()
        assert (x1, y1, x2, y2) == (10.0, 20.0, 30.0, 40.0)
        assert conf == pytest.approx(0.9)
        assert cls == 1.0

    def test_clips_box_straddling_original_boundary(self):
        # In padded coords this box spans from inside the padding to inside
        # the original frame; after shifting back it must clamp to 0.
        dets = torch.tensor([[10.0, 10.0, 30.0, 30.0, 0.9, 0.0]])
        out = unpad_and_clip_detections(dets, 20, (100, 100))
        assert out.shape == (1, 6)
        x1, y1, x2, y2, conf, cls = out[0].tolist()
        assert (x1, y1, x2, y2) == (0.0, 0.0, 10.0, 10.0)

    def test_drops_box_entirely_in_padding(self):
        # This box never touches real image content — must be dropped, not
        # clamped to a degenerate zero-area sliver.
        dets = torch.tensor([[0.0, 0.0, 15.0, 15.0, 0.9, 0.0]])
        out = unpad_and_clip_detections(dets, 20, (100, 100))
        assert out.shape == (0, 6)

    def test_mixed_batch_keeps_only_valid_boxes(self):
        dets = torch.tensor([
            [30.0, 40.0, 50.0, 60.0, 0.9, 1.0],  # interior -> kept
            [0.0, 0.0, 15.0, 15.0, 0.9, 0.0],  # padding-only -> dropped
        ])
        out = unpad_and_clip_detections(dets, 20, (100, 100))
        assert out.shape == (1, 6)
        assert out[0, 5].item() == 1.0


# ================ run_multiscale integration ================

class _FakeBoxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy = torch.tensor(xyxy, dtype=torch.float32)
        self.conf = torch.tensor(conf, dtype=torch.float32)
        self.cls = torch.tensor(cls, dtype=torch.float32)

    def __len__(self):
        return self.xyxy.shape[0]


class _FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes


_BASE_CONFIG = {
    "base_imgsz": 128,
    "scales": [1.0],
    "per_scale_conf": 0.1,
    "per_scale_iou": 0.5,
    "per_scale_max_det": 100,
    "conf": 0.1,
    "iou": 0.5,
    "final_max_det": 100,
    "wbf_iou": 0.55,
    "wbf_score_thr": 0.001,
}


class TestRunMultiscaleMirrorPad:
    def test_pads_canvas_and_maps_detections_back(self):
        orig_h, orig_w = 100, 100
        pad_px = 20
        im0_bgr = np.zeros((orig_h, orig_w, 3), dtype=np.uint8)

        config = {**_BASE_CONFIG, "mirror_pad": {"enabled": True, "pad_px": pad_px}}

        captured_shapes = []

        def fake_predict(source, **kwargs):
            captured_shapes.append(source.shape[:2])
            boxes = _FakeBoxes(
                xyxy=[[10, 10, 30, 30], [0, 0, 15, 15]],
                conf=[0.9, 0.9],
                cls=[0, 0],
            )
            return [_FakeResult(boxes)]

        model = MagicMock()
        model.predict.side_effect = fake_predict

        result = run_multiscale(model, im0_bgr, config, device="cpu")

        # The model saw the padded canvas, not the original frame.
        assert captured_shapes == [(orig_h + 2 * pad_px, orig_w + 2 * pad_px)]

        # Box B (fully in the mirrored padding) is dropped; box A is
        # shifted back and clamped into the original frame.
        assert result.shape == (1, 6)
        x1, y1, x2, y2, conf, cls = result[0].tolist()
        assert (x1, y1, x2, y2) == (0.0, 0.0, 10.0, 10.0)
        assert 0 <= x1 <= orig_w and 0 <= x2 <= orig_w
        assert 0 <= y1 <= orig_h and 0 <= y2 <= orig_h

    def test_disabled_leaves_canvas_and_coordinates_unchanged(self):
        orig_h, orig_w = 100, 100
        im0_bgr = np.zeros((orig_h, orig_w, 3), dtype=np.uint8)

        config = {**_BASE_CONFIG, "mirror_pad": {"enabled": False, "pad_px": 20}}

        captured_shapes = []

        def fake_predict(source, **kwargs):
            captured_shapes.append(source.shape[:2])
            boxes = _FakeBoxes(xyxy=[[10, 10, 30, 30]], conf=[0.9], cls=[0])
            return [_FakeResult(boxes)]

        model = MagicMock()
        model.predict.side_effect = fake_predict

        result = run_multiscale(model, im0_bgr, config, device="cpu")

        assert captured_shapes == [(orig_h, orig_w)]
        assert result.shape == (1, 6)
        x1, y1, x2, y2, conf, cls = result[0].tolist()
        assert (x1, y1, x2, y2) == pytest.approx((10.0, 10.0, 30.0, 30.0))

    def test_absent_mirror_pad_key_defaults_to_disabled(self):
        orig_h, orig_w = 100, 100
        im0_bgr = np.zeros((orig_h, orig_w, 3), dtype=np.uint8)

        config = {**_BASE_CONFIG}  # no "mirror_pad" key at all
        assert "mirror_pad" not in config

        captured_shapes = []

        def fake_predict(source, **kwargs):
            captured_shapes.append(source.shape[:2])
            return [_FakeResult(_FakeBoxes(xyxy=[], conf=[], cls=[]))]

        model = MagicMock()
        model.predict.side_effect = fake_predict

        run_multiscale(model, im0_bgr, config, device="cpu")

        assert captured_shapes == [(orig_h, orig_w)]
