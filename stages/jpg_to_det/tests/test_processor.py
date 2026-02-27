"""
Tests for jpg_to_det processor — all YOLO/model calls are mocked.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest
import torch
import yaml

from stages.jpg_to_det import (
    ERROR_IMAGE_READ_FAILED,
    ERROR_INFERENCE_FAILED,
    ERROR_EXPORT_FAILED,
    ERROR_CFG_VALIDATION_FAILED,
    ERROR_MODEL_LOAD_FAILED,
    ERROR_UNKNOWN,
)
from stages.jpg_to_det.processor import (
    load_config,
    validate_config,
    _classify_error,
    DetectionResult,
    Processor,
)
from stages import ITEM_OK, ITEM_FAILED


# ================ Data Setup ================

VALID_CONFIG = {
    "base_imgsz": 4096,
    "scales": [0.5, 1.0],
    "per_scale_conf": 0.15,
    "per_scale_iou": 0.5,
    "per_scale_max_det": 1000,
    "conf": 0.70,
    "iou": 0.5,
    "final_max_det": 1000,
    "wbf_iou": 0.55,
    "wbf_score_thr": 0.001,
    "edge_aware": {"enabled": True, "edge_band_rel": 0.08, "min_factor": 0.60, "taper_rel": 0.20},
    "post_fusion_nms": {"enabled": False, "iou": 0.5},
}


@pytest.fixture
def config_file(tmp_path):
    """Write a valid config YAML and return its path."""
    p = tmp_path / "det.yaml"
    p.write_text(yaml.dump(VALID_CONFIG))
    return p


@pytest.fixture
def bad_config_missing_key(tmp_path):
    # exclude scales key
    cfg = {k: v for k, v in VALID_CONFIG.items() if k != "scales"}
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump(cfg))
    return p


@pytest.fixture
def fake_jpg(tmp_path):
    """Create a tiny 4x4 BGR image saved as JPG."""
    import cv2

    img = np.zeros((4, 4, 3), dtype=np.uint8)
    img[1, 1] = [255, 0, 0]
    p = tmp_path / "test_image.jpg"
    cv2.imwrite(str(p), img)
    return p


def _make_processor(config_file, tmp_path):
    """Create a Processor with a mocked YOLO model."""
    mock_model = MagicMock()
    mock_model.names = {0: "plant"}

    with patch("stages.jpg_to_det.processor.YOLO", return_value=mock_model):
        model_path = tmp_path / "model.pt"
        model_path.touch()
        proc = Processor(config_file, model_path, device="cpu")

    return proc


# ================ Test Load Config ================

class TestLoadConfig:
    def test_valid(self, config_file):
        cfg = load_config(config_file)
        assert cfg["base_imgsz"] == 4096
        assert isinstance(cfg["scales"], list)

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.yaml")

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_config(p)


# ================ Test Validate Config ================

class TestValidateConfig:
    def test_complete(self):
        validate_config(VALID_CONFIG)  # should not raise

    def test_missing_required_key(self):
        bad = {k: v for k, v in VALID_CONFIG.items() if k != "conf"}
        with pytest.raises(ValueError, match="conf"):
            validate_config(bad)

    def test_empty_scales(self):
        bad = {**VALID_CONFIG, "scales": []}
        with pytest.raises(ValueError, match="scales"):
            validate_config(bad)

    def test_invalid_conf(self):
        bad = {**VALID_CONFIG, "conf": 1.5}
        with pytest.raises(ValueError, match="conf"):
            validate_config(bad)

    def test_none_config(self):
        with pytest.raises(ValueError, match="empty"):
            validate_config(None)


# ================ Test Classify Error ================

class TestClassifyError:
    def test_file_not_found(self):
        assert _classify_error(FileNotFoundError("no file")) == ERROR_IMAGE_READ_FAILED

    def test_model_not_found(self):
        assert _classify_error(FileNotFoundError("model not found")) == ERROR_MODEL_LOAD_FAILED

    def test_value_error(self):
        assert _classify_error(ValueError("bad config")) == ERROR_CFG_VALIDATION_FAILED

    def test_inference_keyword(self):
        assert _classify_error(RuntimeError("inference crashed")) == ERROR_INFERENCE_FAILED

    def test_export_keyword(self):
        assert _classify_error(RuntimeError("export failed")) == ERROR_EXPORT_FAILED

    def test_image_read_keyword(self):
        assert _classify_error(RuntimeError("image read error")) == ERROR_IMAGE_READ_FAILED

    def test_unknown(self):
        assert _classify_error(RuntimeError("something weird")) == ERROR_UNKNOWN


# ================ Test Process Image ================

class TestProcessImage:
    def test_success(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"

        det_tensor = torch.tensor([
            [10.0, 20.0, 30.0, 40.0, 0.9, 0.0],
            [50.0, 60.0, 70.0, 80.0, 0.8, 0.0],
        ])

        with patch("stages.jpg_to_det.processor.run_multiscale", return_value=det_tensor), \
             patch("stages.jpg_to_det.processor.export_predictions") as mock_export:
            txt_path = out_dir / "test_image.txt"
            rows = [
                {"image_id": "test_image", "bounding_box_id": 0, "xmin": 0.1, "ymin": 0.2,
                 "xmax": 0.3, "ymax": 0.4, "conf": 0.9, "class": 0, "classname": "plant"},
                {"image_id": "test_image", "bounding_box_id": 1, "xmin": 0.5, "ymin": 0.6,
                 "xmax": 0.7, "ymax": 0.8, "conf": 0.8, "class": 0, "classname": "plant"},
            ]
            mock_export.return_value = (txt_path, rows)

            result_txt, result_rows = proc.process_image(fake_jpg, out_dir)

        assert result_txt == txt_path
        assert len(result_rows) == 2

    def test_missing_jpg(self, config_file, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"
        missing = tmp_path / "nope.jpg"

        with pytest.raises(RuntimeError):
            proc.process_image(missing, out_dir)

    def test_inference_failure(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"

        with patch("stages.jpg_to_det.processor.run_multiscale", side_effect=RuntimeError("inference crashed")):
            with pytest.raises(RuntimeError):
                proc.process_image(fake_jpg, out_dir)

    def test_export_failure(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"
        det_tensor = torch.tensor([[10.0, 20.0, 30.0, 40.0, 0.9, 0.0]])

        with patch("stages.jpg_to_det.processor.run_multiscale", return_value=det_tensor), \
             patch("stages.jpg_to_det.processor.export_predictions", side_effect=RuntimeError("export failed")):
            with pytest.raises(RuntimeError):
                proc.process_image(fake_jpg, out_dir)


# ================ Test Process Batch ================

class TestProcessBatch:
    def test_all_success(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"
        txt_path = out_dir / "test_image.txt"
        rows = [{"image_id": "test_image", "bounding_box_id": 0}]

        with patch.object(proc, "process_image", return_value=(txt_path, rows)):
            results = proc.process_batch([fake_jpg], out_dir, fail_stop=False)

        assert len(results) == 1
        assert results[0].status == ITEM_OK
        assert results[0].n_detections == 1

    def test_fail_stop_raises(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"

        with patch.object(proc, "process_image", side_effect=RuntimeError("image read error")):
            with pytest.raises(RuntimeError):
                proc.process_batch([fake_jpg], out_dir, fail_stop=True)

    def test_no_fail_stop_continues(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"

        import cv2
        img2 = tmp_path / "img2.jpg"
        cv2.imwrite(str(img2), np.zeros((4, 4, 3), dtype=np.uint8))

        txt_path = out_dir / "img2.txt"
        rows = [{"image_id": "img2", "bounding_box_id": 0}]

        with patch.object(proc, "process_image", side_effect=[
            RuntimeError("image read error"),
            (txt_path, rows),
        ]):
            results = proc.process_batch([fake_jpg, img2], out_dir, fail_stop=False)

        assert len(results) == 2
        assert results[0].status == ITEM_FAILED
        assert results[1].status == ITEM_OK

    def test_parallel_mode(self, config_file, fake_jpg, tmp_path):
        proc = _make_processor(config_file, tmp_path)
        out_dir = tmp_path / "out"
        txt_path = out_dir / "test_image.txt"
        rows = [{"image_id": "test_image", "bounding_box_id": 0}]

        with patch.object(proc, "process_image", return_value=(txt_path, rows)):
            results = proc.process_batch([fake_jpg], out_dir, fail_stop=False, max_workers=4)

        assert len(results) == 1
        assert results[0].status == ITEM_OK
