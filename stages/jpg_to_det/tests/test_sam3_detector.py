"""
Tests for jpg_to_det's SAM 3 backend.

SAM 3 itself is faked (processor with detect_batch); only the box assembly,
config validation and Processor wiring under our control run.
"""

from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest
import torch
import yaml

from stages import ITEM_FAILED, ITEM_OK
from stages.jpg_to_det import ERROR_IMAGE_READ_FAILED, ERROR_INFERENCE_FAILED
from stages.jpg_to_det.processor import Processor, validate_config
from stages.jpg_to_det.sam3_detector import (
    DELTA_FORMAT, class_names, read_delta, run_sam3, run_sam3_batch, to_rgb_tensor,
)

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
    """BoxOnlySam3Processor stand-in: fixed boxes (image px) and scores per prompt."""

    device = "cpu"

    def __init__(self, outputs: dict, fail_batches_over: int | None = None):
        self.outputs = outputs
        self.fail_batches_over = fail_batches_over
        self.prompts = []
        self.batch_sizes = []

    def detect_batch(self, images, prompts):
        self.batch_sizes.append(len(images))
        if self.fail_batches_over is not None and len(images) > self.fail_batches_over:
            raise RuntimeError("out of memory")
        self.prompts.extend(prompts)
        results = []
        for _ in images:
            per_prompt = []
            for prompt in prompts:
                boxes, scores = self.outputs.get(prompt, ([], []))
                per_prompt.append((torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
                                   torch.tensor(scores, dtype=torch.float32)))
            results.append(per_prompt)
        return results


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

    def test_leaves_image_unchanged(self):
        im = np.zeros((100, 120, 3), np.uint8)
        im[..., 0] = 7
        run_sam3(FakeProcessor({}), im, SAM3_CONFIG)
        assert (im[..., 0] == 7).all()

    def test_batch_one_result_per_image(self):
        proc = FakeProcessor({"plant": ([[10, 10, 150, 60]], [0.9])})
        images = [to_rgb_tensor(np.zeros((100, 120, 3), np.uint8)),
                  to_rgb_tensor(np.zeros((100, 200, 3), np.uint8))]
        dets = run_sam3_batch(proc, images, SAM3_CONFIG)
        assert proc.batch_sizes == [2]
        assert [d[0, 2].item() for d in dets] == [120, 150]  # clipped to each image's width

    def test_to_rgb_tensor(self):
        im = np.zeros((4, 6, 3), np.uint8)
        im[..., 0] = 255  # blue in BGR
        rgb = to_rgb_tensor(im)
        assert rgb.shape == (3, 4, 6)
        assert rgb[2].eq(255).all() and rgb[0].eq(0).all()

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

    def test_batch_size_defaults_to_one(self):
        config = dict(SAM3_CONFIG)
        validate_config(config)
        assert config["batch_size"] == 1

    @pytest.mark.parametrize("batch_size", [0, -1, 2.5, "4", True])
    def test_invalid_batch_size(self, batch_size):
        with pytest.raises(ValueError, match="batch_size"):
            validate_config({**SAM3_CONFIG, "batch_size": batch_size})

    def test_shipped_config_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "configs" / "sam3.yaml"
        validate_config(yaml.safe_load(path.read_text()))


# ================ Processor wiring ================

class TestSam3Processor:
    OUTPUTS = {"plant": ([[20, 10, 60, 50]], [0.9]),
               "color chart": ([[100, 50, 140, 90]], [0.8])}

    @pytest.fixture
    def make(self, tmp_path):
        def make(n_images=2, batch_size=1, **fake_kwargs):
            config_path = tmp_path / "sam3.yaml"
            config_path.write_text(yaml.safe_dump({**SAM3_CONFIG, "batch_size": batch_size}))
            model_path = tmp_path / "sam3_finetuned.pt"
            model_path.touch()
            jpgs = []
            for i in range(n_images):
                jpg = tmp_path / f"img{i}.jpg"
                cv2.imwrite(str(jpg), np.zeros((100, 200, 3), np.uint8))
                jpgs.append(jpg)
            fake = FakeProcessor(self.OUTPUTS, **fake_kwargs)
            with patch("stages.jpg_to_det.processor.load_sam3_detector", return_value=fake) as load:
                processor = Processor(config_path, model_path, device="cpu")
            load.assert_called_once()
            return processor, fake, jpgs, tmp_path / "out"
        return make

    def test_yolo_format_txt_and_rows(self, make):
        processor, _, jpgs, out = make()
        result = processor.process_image(jpgs[0], out)
        assert result.status == ITEM_OK
        lines = result.txt_path.read_text().splitlines()
        assert lines == ["0 0.200000 0.300000 0.200000 0.400000 0.900000",
                         "1 0.600000 0.700000 0.200000 0.400000 0.800000"]
        assert [r["classname"] for r in result.detection_rows] == ["plant", "color_checker"]

    def test_batch_uses_one_model_not_process_pool(self, make):
        processor, _, jpgs, out = make()
        with patch("stages.jpg_to_det.processor.ProcessPoolExecutor") as pool:
            results = processor.process_batch(jpgs, out, max_workers=4)
        pool.assert_not_called()
        assert [r.status for r in results] == [ITEM_OK, ITEM_OK]

    def test_batches_images_in_input_order(self, make):
        processor, fake, jpgs, out = make(n_images=5, batch_size=2)
        results = processor.process_batch(jpgs, out, max_workers=3)
        assert fake.batch_sizes == [2, 2, 1]
        assert [r.image_id for r in results] == [j.stem for j in jpgs]
        assert all(r.status == ITEM_OK and r.n_detections == 2 for r in results)
        assert processor.last_timings["images"] == 5

    def test_same_output_as_single_image(self, make):
        processor, _, jpgs, out = make(n_images=3, batch_size=2)
        single = processor.process_image(jpgs[0], out / "single").txt_path.read_text()
        batched = processor.process_batch(jpgs, out / "batched", max_workers=2)
        assert all(r.txt_path.read_text() == single for r in batched)

    def test_unreadable_image_fails_alone(self, make):
        processor, fake, jpgs, out = make(n_images=3, batch_size=3)
        jpgs[1].write_bytes(b"not a jpg")
        results = processor.process_batch(jpgs, out, fail_stop=False, max_workers=2)
        assert [r.status for r in results] == [ITEM_OK, ITEM_FAILED, ITEM_OK]
        assert results[1].error_code == ERROR_IMAGE_READ_FAILED
        assert fake.batch_sizes == [2]

    def test_fail_stop_on_read_failure(self, make):
        processor, fake, jpgs, out = make(n_images=4, batch_size=1)
        jpgs[1].write_bytes(b"not a jpg")
        results = processor.process_batch(jpgs, out, fail_stop=True, max_workers=2)
        assert [r.status for r in results] == [ITEM_OK, ITEM_FAILED]

    def test_failed_batch_retried_per_image(self, make):
        processor, fake, jpgs, out = make(n_images=3, batch_size=3, fail_batches_over=1)
        results = processor.process_batch(jpgs, out, fail_stop=True, max_workers=2)
        assert fake.batch_sizes == [3, 1, 1, 1]
        assert [r.status for r in results] == [ITEM_OK] * 3

    def test_inference_failure(self, make):
        processor, fake, jpgs, out = make(n_images=2, batch_size=2, fail_batches_over=0)
        results = processor.process_batch(jpgs, out, fail_stop=False, max_workers=2)
        assert [r.error_code for r in results] == [ERROR_INFERENCE_FAILED] * 2
