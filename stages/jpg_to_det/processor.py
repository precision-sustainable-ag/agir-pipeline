"""
JPG -> Detection processor.
"""

import logging
import multiprocessing as mp
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List

import cv2
import torch
import yaml
from ultralytics import YOLO

from . import (
    ERROR_MODEL_LOAD_FAILED,
    ERROR_IMAGE_READ_FAILED,
    ERROR_INFERENCE_FAILED,
    ERROR_EXPORT_FAILED,
)
from .detector import run_multiscale, export_predictions
from stages import ITEM_OK, ITEM_FAILED

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class DetectionResult:
    """Result of processing a single image."""
    image_id: str
    status: str
    txt_path: Path | None = None
    detection_rows: list[dict] = field(default_factory=list)
    n_detections: int = 0
    error_code: str = ""
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool = False


# ---------------------------------------------------------------------------
# Worker functions
# ---------------------------------------------------------------------------

_worker_model = None
_worker_config = None
_worker_device = None
_worker_names = None


def _worker_init(model_path: str, config: dict, device: str, torch_threads: int) -> None:
    """Initializer called once per worker process."""
    global _worker_model, _worker_config, _worker_device, _worker_names

    t = str(torch_threads)

    # limit Pytorch Threads to prevent overuse

    # OpenMP threads
    os.environ["OMP_NUM_THREADS"] = t
    # Intel Math Kernel Library Threads
    os.environ["MKL_NUM_THREADS"] = t
    # OpenBLAS threads set
    os.environ["OPENBLAS_NUM_THREADS"] = t

    torch.set_num_threads(torch_threads)

    _worker_model = YOLO(model_path)
    _worker_config = config
    _worker_device = device
    _worker_names = _worker_model.names


def _worker_process_image(jpg_path_str: str, output_dir_str: str) -> DetectionResult:
    """Run detection on a single image inside a worker process."""
    jpg_path = Path(jpg_path_str)
    output_dir = Path(output_dir_str)
    output_dir.mkdir(parents=True, exist_ok=True)
    image_id = jpg_path.stem

    # Read image with OpenCV
    try:
        im0 = cv2.imread(str(jpg_path))
        if im0 is None:
            raise RuntimeError(f"cv2.imread returned None for {jpg_path}")
    except Exception as e:
        return DetectionResult(
            image_id=image_id,
            status=ITEM_FAILED,
            error_code=ERROR_IMAGE_READ_FAILED,
            error_type=type(e).__name__,
            error_message=str(e),
        )

    # Perform YOLO inference
    try:
        det_abs = run_multiscale(
            model=_worker_model, im0_bgr=im0,
            config=_worker_config, device=_worker_device,
        )
    except Exception as e:
        return DetectionResult(
            image_id=image_id,
            status=ITEM_FAILED,
            error_code=ERROR_INFERENCE_FAILED,
            error_type=type(e).__name__,
            error_message=str(e),
        )

    # Export results
    try:
        txt_path, detection_rows = export_predictions(
            results_raw_xyxy_abs=det_abs, save_dir=output_dir,
            filename=str(jpg_path), names=_worker_names, im0=im0,
        )
    except Exception as e:
        return DetectionResult(
            image_id=image_id,
            status=ITEM_FAILED,
            error_code=ERROR_EXPORT_FAILED,
            error_type=type(e).__name__,
            error_message=str(e),
        )

    return DetectionResult(
        image_id=image_id,
        status=ITEM_OK,
        txt_path=txt_path,
        detection_rows=detection_rows,
        n_detections=len(detection_rows),
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def validate_config(config: dict) -> None:
    """
    Validate that config has all required keys with sane values.

    Raises:
        ValueError: If config is missing required keys or has invalid values.
    """
    if not config:
        raise ValueError("Config is empty or None")

    required_config_keys = [
        "base_imgsz", "scales", "per_scale_conf", "per_scale_iou",
        "per_scale_max_det", "conf", "iou", "final_max_det",
    ]

    for key in required_config_keys:
        if key not in config:
            raise ValueError(f"Config missing required key: {key}")

    scales = config["scales"]
    if not isinstance(scales, list) or len(scales) == 0:
        raise ValueError("Config 'scales' must be a non-empty list")

    conf = config["conf"]
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        raise ValueError(f"Config 'conf' must be a float in [0, 1], got {conf}")


def load_config(config_path: Path) -> dict:
    """
    Load detection config from YAML file.

    Raises:
        FileNotFoundError: If config file not found.
        ValueError: If config is empty or validation fails.
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        config = yaml.safe_load(f)

    if config is None:
        raise ValueError(f"Config file is empty or invalid: {config_path}")

    validate_config(config)
    return config


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class Processor:
    """
    Detection processor — loads YOLO model once, runs multiscale detection
    on each JPG image.  Parallel mode uses multiprocessing with spawn.
    """

    def __init__(
        self,
        config_path: Path,
        model_path: Path,
        device: str = "cpu",
    ) -> None:
        self.config = load_config(config_path)
        self.device = device
        self.model_path = Path(model_path)

        if not self.model_path.exists():
            raise FileNotFoundError(f"Model file not found: {self.model_path}")

        try:
            self.model = YOLO(str(self.model_path))
        except Exception as e:
            raise RuntimeError(f"{ERROR_MODEL_LOAD_FAILED}: {e}") from e
        
        self.names = self.model.names

    def process_image(self, jpg_path: Path, output_dir: Path) -> DetectionResult:
        """
        Run detection on a single JPG (sequential / in-process).

        Returns a DetectionResult (always), with status ITEM_OK or ITEM_FAILED.
        """
        jpg_path = Path(jpg_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        image_id = jpg_path.stem
        device = self.device

        # Read image with opencv
        try:
            im0 = cv2.imread(str(jpg_path))
            if im0 is None:
                raise RuntimeError(f"cv2.imread returned None for {jpg_path}")
        except Exception as e:
            return DetectionResult(
                image_id=image_id,
                status=ITEM_FAILED,
                error_code=ERROR_IMAGE_READ_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        # run inference with yolo
        try:
            det_abs = run_multiscale(
                model=self.model, im0_bgr=im0,
                config=self.config, device=device,
            )
        except Exception as e:
            return DetectionResult(
                image_id=image_id,
                status=ITEM_FAILED,
                error_code=ERROR_INFERENCE_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        # export file path
        try:
            txt_path, detection_rows = export_predictions(
                results_raw_xyxy_abs=det_abs, save_dir=output_dir,
                filename=str(jpg_path), names=self.names, im0=im0,
            )
        except Exception as e:
            return DetectionResult(
                image_id=image_id,
                status=ITEM_FAILED,
                error_code=ERROR_EXPORT_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        return DetectionResult(
            image_id=image_id,
            status=ITEM_OK,
            txt_path=txt_path,
            detection_rows=detection_rows,
            n_detections=len(detection_rows),
        )

    def process_batch(
        self,
        jpg_images: Iterable[Path],
        output_dir: Path,
        fail_stop: bool = True,
        max_workers: int = 0,
    ) -> List[DetectionResult]:
        """
        Process multiple JPG images, optionally in parallel via multiprocessing.
        """
        jpg_list = list(jpg_images)
        results: List[DetectionResult] = []
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if max_workers <= 1:
            # ---- sequential ----
            for jpg_path in jpg_list:
                result = self.process_image(jpg_path, output_dir)
                results.append(result)
                if result.status == ITEM_OK:
                    logger.info("Processed %s -> %s", jpg_path.name, result.txt_path.name)
                else:
                    logger.error(
                        "Failed processing %s [%s]: %s",
                        jpg_path.name, result.error_code, result.error_message,
                    )
                    if fail_stop:
                        return results
        else:
            # ---- parallel (multiprocessing with spawn) ----
            ctx = mp.get_context("spawn")

            # manage torch threads
            if "cuda" in self.device:
                torch_threads = 1
            else:
                # get cpu cores to make distributed torch threads
                cpu_count = os.cpu_count() or 1
                torch_threads = max(1, cpu_count // max_workers)
            output_dir_str = str(output_dir)

            pool = ctx.Pool(
                processes=max_workers,
                initializer=_worker_init,
                initargs=(str(self.model_path), self.config, self.device, torch_threads),
            )

            try:
                async_results = []
                for jpg_path in jpg_list:
                    ar = pool.apply_async(
                        _worker_process_image, (str(jpg_path), output_dir_str),
                    )
                    async_results.append((ar, jpg_path))

                failed = False
                for ar, jpg_path in async_results:
                    result = ar.get()
                    results.append(result)
                    if result.status == ITEM_OK:
                        logger.info("Processed %s -> %s", jpg_path.name, result.txt_path.name)
                    else:
                        logger.error(
                            "Failed processing %s [%s]: %s",
                            jpg_path.name, result.error_code, result.error_message,
                        )
                        if fail_stop:
                            failed = True
                            break

                if failed:
                    pool.terminate()
            finally:
                pool.close()
                pool.join()

        return results
