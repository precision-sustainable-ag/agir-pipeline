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
)
from stages.jpg_to_det.processor import (
    load_config,
    validate_config,
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


# ================ Test Load Config  ================

def test_load_config_valid(config_file):
    cfg = load_config(config_file)
    assert cfg["base_imgsz"] == 4096
    assert isinstance(cfg["scales"], list)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_load_config_empty_file(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_config(p)


# ================ Test Validate Config  ================

def test_validate_config_complete():
    validate_config(VALID_CONFIG)


def test_validate_config_missing_required_key():
    bad = {k: v for k, v in VALID_CONFIG.items() if k != "conf"}
    with pytest.raises(ValueError, match="conf"):
        validate_config(bad)


def test_validate_config_empty_scales():
    bad = {**VALID_CONFIG, "scales": []}
    with pytest.raises(ValueError, match="scales"):
        validate_config(bad)


def test_validate_config_invalid_conf():
    bad = {**VALID_CONFIG, "conf": 1.5}
    with pytest.raises(ValueError, match="conf"):
        validate_config(bad)


def test_validate_config_none():
    with pytest.raises(ValueError, match="empty"):
        validate_config(None)


# ================ Test Process Image  ================

def _make_processor(config_file, tmp_path):
    """Create a Processor with a mocked YOLO model."""
    mock_model = MagicMock()
    mock_model.names = {0: "plant"}

    with patch("stages.jpg_to_det.processor.YOLO", return_value=mock_model):
        model_path = tmp_path / "model.pt"
        model_path.touch()
        proc = Processor(config_file, model_path, device="cpu")

    return proc


def test_process_image_success(config_file, fake_jpg, tmp_path):
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"

    det_tensor = torch.tensor([
        [10.0, 20.0, 30.0, 40.0, 0.9, 0.0],
        [50.0, 60.0, 70.0, 80.0, 0.8, 0.0],
    ])

    # run multiscale processing
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

        result = proc.process_image(fake_jpg, out_dir)

    # assert valid item status, texxt path, and number of detections
    assert result.status == ITEM_OK
    assert result.txt_path == txt_path
    assert result.n_detections == 2

    # verify detection_rows schema for CSV output
    assert len(result.detection_rows) == 2
    expected_keys = {"image_id", "bounding_box_id", "xmin", "ymin", "xmax", "ymax", "conf", "class", "classname"}
    assert set(result.detection_rows[0].keys()) == expected_keys


def test_process_image_missing_jpg(config_file, tmp_path):
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"
    missing = tmp_path / "nope.jpg"

    # running on empty image... assert failure
    result = proc.process_image(missing, out_dir)
    assert result.status == ITEM_FAILED
    assert result.error_code == ERROR_IMAGE_READ_FAILED


def test_process_image_inference_failure(config_file, fake_jpg, tmp_path):
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"

    # manually add inference runtime error
    with patch("stages.jpg_to_det.processor.run_multiscale", side_effect=RuntimeError("inference crashed")):
        result = proc.process_image(fake_jpg, out_dir)

    assert result.status == ITEM_FAILED
    assert result.error_code == ERROR_INFERENCE_FAILED


def test_process_image_export_failure(config_file, fake_jpg, tmp_path):
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"
    det_tensor = torch.tensor([[10.0, 20.0, 30.0, 40.0, 0.9, 0.0]])

    # manually add export failed error
    with patch("stages.jpg_to_det.processor.run_multiscale", return_value=det_tensor), \
         patch("stages.jpg_to_det.processor.export_predictions", side_effect=RuntimeError("export failed")):
        result = proc.process_image(fake_jpg, out_dir)

    assert result.status == ITEM_FAILED
    assert result.error_code == ERROR_EXPORT_FAILED


# ================ Test Process Batch  ================

def test_process_batch_all_success(config_file, fake_jpg, tmp_path):
    """ Test process batch where process image succeeds """
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"
    ok_result = DetectionResult(
        image_id="test_image", status=ITEM_OK,
        txt_path=out_dir / "test_image.txt",
        detection_rows=[{"image_id": "test_image", "bounding_box_id": 0}],
        n_detections=1,
    )

    with patch.object(proc, "process_image", return_value=ok_result):
        results = proc.process_batch([fake_jpg], out_dir, fail_stop=False)

    assert len(results) == 1
    assert results[0].status == ITEM_OK
    assert results[0].n_detections == 1


def test_process_batch_fail_stop_raises(config_file, fake_jpg, tmp_path):
    """ Test process batch where process image fails + fail stop is true """
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"

    import cv2
    img2 = tmp_path / "img2.jpg"
    cv2.imwrite(str(img2), np.zeros((4, 4, 3), dtype=np.uint8))

    fail_result = DetectionResult(
        image_id="test_image", status=ITEM_FAILED,
        error_code=ERROR_IMAGE_READ_FAILED,
        error_type="RuntimeError", error_message="image read error",
    )
    ok_result = DetectionResult(
        image_id="img2", status=ITEM_OK,
        txt_path=out_dir / "img2.txt",
        detection_rows=[{"image_id": "img2", "bounding_box_id": 0}],
        n_detections=1,
    )

    # should raise on the first failure, never reaching img2
    with patch.object(proc, "process_image", side_effect=[fail_result, ok_result]) as mock_pi:
        with pytest.raises(RuntimeError):
            proc.process_batch([fake_jpg, img2], out_dir, fail_stop=True)
        assert mock_pi.call_count == 1


def test_process_batch_no_fail_stop_continues(config_file, fake_jpg, tmp_path):
    """ Test process batch where process image fails + fail stop is false """
    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"

    import cv2
    img2 = tmp_path / "img2.jpg"
    cv2.imwrite(str(img2), np.zeros((4, 4, 3), dtype=np.uint8))

    fail_result = DetectionResult(
        image_id="test_image", status=ITEM_FAILED,
        error_code=ERROR_IMAGE_READ_FAILED,
        error_type="RuntimeError", error_message="image read error",
    )
    ok_result = DetectionResult(
        image_id="img2", status=ITEM_OK,
        txt_path=out_dir / "img2.txt",
        detection_rows=[{"image_id": "img2", "bounding_box_id": 0}],
        n_detections=1,
    )

    with patch.object(proc, "process_image", side_effect=[fail_result, ok_result]):
        results = proc.process_batch([fake_jpg, img2], out_dir, fail_stop=False)

    assert len(results) == 2
    assert results[0].status == ITEM_FAILED
    assert results[1].status == ITEM_OK


def test_process_batch_parallel_mode(config_file, fake_jpg, tmp_path):
    """ Test process batch with multiple workers """

    proc = _make_processor(config_file, tmp_path)
    out_dir = tmp_path / "out"
    ok_result = DetectionResult(
        image_id="test_image", status=ITEM_OK,
        txt_path=out_dir / "test_image.txt",
        detection_rows=[{"image_id": "test_image", "bounding_box_id": 0}],
        n_detections=1,
    )

    with patch.object(proc, "process_image", return_value=ok_result):
        results = proc.process_batch([fake_jpg], out_dir, fail_stop=False, max_workers=4)

    assert len(results) == 1
    assert results[0].status == ITEM_OK
