"""
Tests for jpg_to_det's SAM 3 backend.

SAM 3 itself is faked (processor with set_image / set_text_prompt); only the
box assembly, config validation and Processor wiring under our control run.
"""

from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest
import torch
import yaml

from stages import ITEM_OK
from stages.jpg_to_det.processor import Processor, validate_config
from stages.jpg_to_det.sam3_detector import DELTA_FORMAT, class_names, read_delta, run_sam3

SAM3_CONFIG = {
    "backend": "sam3",
    "resolution": 1008,
    "conf": 0.5,
    "final_max_det": 1000,
    "prompts": [
        {"prompt": "plant", "class": 0, "name": "plant"},
        {"prompt": "color chart", "class": 1, "name": "color_checker"},
    ],
}


class FakeProcessor:
    """Sam3Processor stand-in: fixed boxes (image px) and scores per prompt."""

    device = "cpu"

    def __init__(self, outputs: dict):
        self.outputs = outputs
        self.prompts = []

    def set_image(self, image):
        return {"size": image.size}

    def set_text_prompt(self, prompt, state):
        self.prompts.append(prompt)
        boxes, scores = self.outputs.get(prompt, ([], []))
        return {"boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
                "scores": torch.tensor(scores, dtype=torch.float32)}


# ================ run_sam3 ================

class TestRunSam3:
    def test_boxes_per_prompt_with_class_ids(self):
        proc = FakeProcessor({
            "plant": ([[10, 10, 50, 60], [70, 5, 90, 30]], [0.7, 0.9]),
            "color chart": ([[0, 80, 20, 100]], [0.8]),
        })
        det = run_sam3(proc, np.zeros((100, 120, 3), np.uint8), SAM3_CONFIG)
        assert proc.prompts == ["plant", "color chart"]
        assert det.shape == (3, 6)
        assert det[:, 4].tolist() == pytest.approx([0.9, 0.8, 0.7])  # by descending conf
        assert det[:, 5].tolist() == [0, 1, 0]
        assert det[0, :4].tolist() == [70, 5, 90, 30]

    def test_clips_to_image_and_drops_empty(self):
        proc = FakeProcessor({"plant": ([[-5, -5, 50, 200], [130, 10, 140, 20]], [0.9, 0.8])})
        det = run_sam3(proc, np.zeros((100, 120, 3), np.uint8), SAM3_CONFIG)
        assert det.tolist() == [[0, 0, 50, 100, pytest.approx(0.9), 0]]

    def test_no_detections(self):
        det = run_sam3(FakeProcessor({}), np.zeros((100, 120, 3), np.uint8), SAM3_CONFIG)
        assert det.shape == (0, 6)

    def test_final_max_det(self):
        proc = FakeProcessor({"plant": ([[0, 0, 10, 10]] * 5, [0.9, 0.8, 0.7, 0.6, 0.55])})
        det = run_sam3(proc, np.zeros((100, 120, 3), np.uint8), {**SAM3_CONFIG, "final_max_det": 2})
        assert det[:, 4].tolist() == pytest.approx([0.9, 0.8])

    def test_class_names(self):
        assert class_names(SAM3_CONFIG) == {0: "plant", 1: "color_checker"}


# ================ checkpoints ================

class TestReadDelta:
    def test_export_is_delta(self, tmp_path):
        path = tmp_path / "sam3_finetuned.pt"
        torch.save({"format": DELTA_FORMAT, "base": "sam3", "resolution": 2016,
                    "detector": {"w": torch.zeros(2)}}, path)
        assert read_delta(path)["resolution"] == 2016

    def test_full_checkpoint_is_not(self, tmp_path):
        path = tmp_path / "sam3.pt"
        torch.save({"model": {"detector.w": torch.zeros(2)}}, path)
        assert read_delta(path) is None


# ================ config ================

class TestValidateSam3Config:
    def test_complete(self):
        validate_config(dict(SAM3_CONFIG))

    def test_yolo_keys_not_required(self):
        assert "base_imgsz" not in SAM3_CONFIG
        validate_config(dict(SAM3_CONFIG))

    def test_backend_defaults_to_yolo(self):
        with pytest.raises(ValueError, match="base_imgsz"):
            validate_config({k: v for k, v in SAM3_CONFIG.items() if k != "backend"})

    def test_unknown_backend(self):
        with pytest.raises(ValueError, match="backend"):
            validate_config({**SAM3_CONFIG, "backend": "detr"})

    def test_missing_prompts(self):
        with pytest.raises(ValueError, match="prompts"):
            validate_config({k: v for k, v in SAM3_CONFIG.items() if k != "prompts"})

    def test_prompt_missing_class(self):
        with pytest.raises(ValueError, match="prompt, class and name"):
            validate_config({**SAM3_CONFIG, "prompts": [{"prompt": "plant", "name": "plant"}]})

    def test_duplicate_class_ids(self):
        prompts = [{"prompt": "plant", "class": 0, "name": "plant"},
                   {"prompt": "weed", "class": 0, "name": "weed"}]
        with pytest.raises(ValueError, match="unique"):
            validate_config({**SAM3_CONFIG, "prompts": prompts})

    def test_shipped_config_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "configs" / "sam3.yaml"
        validate_config(yaml.safe_load(path.read_text()))


# ================ Processor wiring ================

class TestSam3Processor:
    @pytest.fixture
    def setup(self, tmp_path):
        config_path = tmp_path / "sam3.yaml"
        config_path.write_text(yaml.safe_dump(SAM3_CONFIG))
        model_path = tmp_path / "sam3_finetuned.pt"
        model_path.touch()
        jpgs = []
        for name in ("a", "b"):
            jpg = tmp_path / f"{name}.jpg"
            cv2.imwrite(str(jpg), np.zeros((100, 200, 3), np.uint8))
            jpgs.append(jpg)
        fake = FakeProcessor({"plant": ([[20, 10, 60, 50]], [0.9]),
                              "color chart": ([[100, 50, 140, 90]], [0.8])})
        with patch("stages.jpg_to_det.processor.load_sam3_detector", return_value=fake) as load:
            processor = Processor(config_path, model_path, device="cpu")
        load.assert_called_once()
        return processor, jpgs, tmp_path / "out"

    def test_yolo_format_txt_and_rows(self, setup):
        processor, jpgs, out = setup
        result = processor.process_image(jpgs[0], out)
        assert result.status == ITEM_OK
        lines = result.txt_path.read_text().splitlines()
        assert lines == ["0 0.200000 0.300000 0.200000 0.400000 0.900000",
                         "1 0.600000 0.700000 0.200000 0.400000 0.800000"]
        assert [r["classname"] for r in result.detection_rows] == ["plant", "color_checker"]

    def test_batch_runs_sequentially(self, setup):
        processor, jpgs, out = setup
        with patch("stages.jpg_to_det.processor.ProcessPoolExecutor") as pool:
            results = processor.process_batch(jpgs, out, max_workers=4)
        pool.assert_not_called()
        assert [r.status for r in results] == [ITEM_OK, ITEM_OK]
