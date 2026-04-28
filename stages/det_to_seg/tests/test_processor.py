"""Tests for det_to_seg processor with model inference mocked."""

from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest
import yaml

from stages import ITEM_FAILED, ITEM_OK
from stages.det_to_seg import (
    ERROR_DET_READ_FAILED,
    ERROR_EXPORT_FAILED,
    ERROR_IMAGE_READ_FAILED,
    ERROR_INFERENCE_FAILED,
    ERROR_MODEL_LOAD_FAILED,
)
from stages.det_to_seg.processor import (
    Processor,
    SegmentationResult,
    load_config,
    parse_yolo_detections,
    validate_config,
)


VALID_CONFIG = {
    "weights": "/tmp/fake.ckpt",
    "arch": "Unet",
    "encoder": "mit_b4",
    "threshold": 0.5,
    "pad_divisor": 32,
    "tile_size": 1024,
    "overlap": 128,
    "tiling": True,
}


@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "seg.yaml"
    p.write_text(yaml.dump(VALID_CONFIG))
    return p


@pytest.fixture
def fake_jpg(tmp_path):
    img = np.zeros((20, 30, 3), dtype=np.uint8)
    p = tmp_path / "img001.jpg"
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def fake_txt(tmp_path):
    p = tmp_path / "img001.txt"
    p.write_text("0 0.5 0.5 0.4 0.5 0.9\n")
    return p


def _make_processor(config_file):
    with patch("stages.det_to_seg.processor._load_model_from_config", return_value=object()):
        return Processor(config_file, device="cpu")


class TestConfig:
    def test_load_config_valid(self, config_file):
        cfg = load_config(config_file)
        assert cfg["arch"] == "Unet"

    def test_load_config_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "missing.yaml")

    def test_validate_missing_key(self):
        bad = {k: v for k, v in VALID_CONFIG.items() if k != "weights"}
        with pytest.raises(ValueError, match="weights"):
            validate_config(bad)


class TestDetParsing:
    def test_parse_yolo_detections(self, fake_txt):
        boxes = parse_yolo_detections(fake_txt, width=100, height=50)
        assert len(boxes) == 1
        x1, y1, x2, y2 = boxes[0]
        assert 0 <= x1 < x2 <= 100
        assert 0 <= y1 < y2 <= 50

    def test_parse_bad_row(self, tmp_path):
        txt = tmp_path / "bad.txt"
        txt.write_text("0 0.5\n")
        with pytest.raises(ValueError):
            parse_yolo_detections(txt, width=100, height=100)


class TestProcessor:
    def test_success(self, config_file, fake_jpg, fake_txt, tmp_path):
        proc = _make_processor(config_file)
        out_dir = tmp_path / "out"

        with patch("stages.det_to_seg.processor.composite_bbox_masks", return_value=np.zeros((20, 30), dtype=np.uint8)), \
             patch("stages.det_to_seg.processor.write_mask_png") as mock_write:
            result = proc.process_image(fake_txt, fake_jpg, out_dir)

        assert result.status == ITEM_OK
        assert result.mask_path.name == "img001_mask.png"
        assert result.n_detections == 1
        mock_write.assert_called_once()

    def test_zero_detection_still_ok(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file)
        txt = tmp_path / "img001.txt"
        txt.write_text("")
        out_dir = tmp_path / "out"

        with patch("stages.det_to_seg.processor.composite_bbox_masks", return_value=np.zeros((20, 30), dtype=np.uint8)), \
             patch("stages.det_to_seg.processor.write_mask_png"):
            result = proc.process_image(txt, fake_jpg, out_dir)

        assert result.status == ITEM_OK
        assert result.n_detections == 0

    def test_image_read_failure(self, config_file, fake_txt, tmp_path):
        proc = _make_processor(config_file)
        missing_jpg = tmp_path / "missing.jpg"
        result = proc.process_image(fake_txt, missing_jpg, tmp_path / "out")
        assert result.status == ITEM_FAILED
        assert result.error_code == ERROR_IMAGE_READ_FAILED

    def test_det_read_failure(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file)
        bad_txt = tmp_path / "img001.txt"
        bad_txt.write_text("0 1\n")

        result = proc.process_image(bad_txt, fake_jpg, tmp_path / "out")
        assert result.status == ITEM_FAILED
        assert result.error_code == ERROR_DET_READ_FAILED

    def test_inference_failure(self, config_file, fake_jpg, fake_txt, tmp_path):
        proc = _make_processor(config_file)
        with patch("stages.det_to_seg.processor.composite_bbox_masks", side_effect=RuntimeError("boom")):
            result = proc.process_image(fake_txt, fake_jpg, tmp_path / "out")

        assert result.status == ITEM_FAILED
        assert result.error_code == ERROR_INFERENCE_FAILED

    def test_export_failure(self, config_file, fake_jpg, fake_txt, tmp_path):
        proc = _make_processor(config_file)
        with patch("stages.det_to_seg.processor.composite_bbox_masks", return_value=np.zeros((20, 30), dtype=np.uint8)), \
             patch("stages.det_to_seg.processor.write_mask_png", side_effect=RuntimeError("write failed")):
            result = proc.process_image(fake_txt, fake_jpg, tmp_path / "out")

        assert result.status == ITEM_FAILED
        assert result.error_code == ERROR_EXPORT_FAILED

    def test_idempotent_existing_mask(self, config_file, fake_jpg, fake_txt, tmp_path):
        proc = _make_processor(config_file)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        existing = out_dir / "img001_mask.png"
        existing.write_bytes(b"already there")

        with patch("stages.det_to_seg.processor.composite_bbox_masks") as mock_compose:
            result = proc.process_image(fake_txt, fake_jpg, out_dir)

        assert result.status == ITEM_OK
        assert result.mask_path == existing
        mock_compose.assert_not_called()

    def test_model_load_failure_wrapped(self, config_file):
        with patch("stages.det_to_seg.processor._load_model_from_config", side_effect=RuntimeError("bad ckpt")):
            with pytest.raises(RuntimeError, match=ERROR_MODEL_LOAD_FAILED):
                Processor(config_file, device="cpu")


class TestBatch:
    def test_fail_stop_returns_early(self, config_file, tmp_path):
        proc = _make_processor(config_file)

        fail = SegmentationResult(
            image_id="a",
            status=ITEM_FAILED,
            error_code=ERROR_IMAGE_READ_FAILED,
            error_type="RuntimeError",
            error_message="no image",
        )
        ok = SegmentationResult(image_id="b", status=ITEM_OK, mask_path=tmp_path / "b_mask.png")

        with patch.object(proc, "process_image", side_effect=[fail, ok]):
            pairs = [(tmp_path / "a.txt", tmp_path / "a.jpg"), (tmp_path / "b.txt", tmp_path / "b.jpg")]
            results = proc.process_batch(pairs, tmp_path / "out", fail_stop=True)

        assert len(results) == 1
        assert results[0].status == ITEM_FAILED

    def test_no_fail_stop_continues(self, config_file, tmp_path):
        proc = _make_processor(config_file)

        fail = SegmentationResult(
            image_id="a",
            status=ITEM_FAILED,
            error_code=ERROR_IMAGE_READ_FAILED,
            error_type="RuntimeError",
            error_message="no image",
        )
        ok = SegmentationResult(image_id="b", status=ITEM_OK, mask_path=tmp_path / "b_mask.png")

        with patch.object(proc, "process_image", side_effect=[fail, ok]):
            pairs = [(tmp_path / "a.txt", tmp_path / "a.jpg"), (tmp_path / "b.txt", tmp_path / "b.jpg")]
            results = proc.process_batch(pairs, tmp_path / "out", fail_stop=False)

        assert len(results) == 2
        assert results[0].status == ITEM_FAILED
        assert results[1].status == ITEM_OK
