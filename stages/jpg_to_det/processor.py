"""
JPG -> Detection processor.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
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


class Processor:
    """
    Detection processor — loads YOLO model once, runs multiscale detection
    on each JPG image.
    """

    def __init__(
        self,
        config_path: Path,
        model_path: Path,
        device: str = "cpu",
    ) -> None:
        self.config = load_config(config_path)
        self.device = device

        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")

        self.model = YOLO(str(model_path))
        self.names = (
            self.model.names
            if isinstance(self.model.names, dict)
            else {i: n for i, n in enumerate(self.model.names)}
        )

    def process_image(self, jpg_path: Path, output_dir: Path) -> DetectionResult:
        """
        Run detection on a single JPG.

        Returns a DetectionResult (always), with status ITEM_OK or ITEM_FAILED.
        """
        jpg_path = Path(jpg_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        image_id = jpg_path.stem

        # 1. Read image
        try:
            im0 = cv2.imread(str(jpg_path))
            if im0 is None:
                raise RuntimeError(f"cv2.imread returned None for {jpg_path}")
        except Exception as e:
            return DetectionResult(
                image_id=image_id, status=ITEM_FAILED,
                error_code=ERROR_IMAGE_READ_FAILED,
                error_type=type(e).__name__, error_message=str(e),
            )

        # 2. Inference
        try:
            det_abs = run_multiscale(
                model=self.model, im0_bgr=im0,
                config=self.config, device=self.device,
            )
        except Exception as e:
            return DetectionResult(
                image_id=image_id, status=ITEM_FAILED,
                error_code=ERROR_INFERENCE_FAILED,
                error_type=type(e).__name__, error_message=str(e),
            )

        # 3. Export
        try:
            txt_path, detection_rows = export_predictions(
                results_raw_xyxy_abs=det_abs, save_dir=output_dir,
                filename=str(jpg_path), names=self.names, im0=im0,
            )
        except Exception as e:
            return DetectionResult(
                image_id=image_id, status=ITEM_FAILED,
                error_code=ERROR_EXPORT_FAILED,
                error_type=type(e).__name__, error_message=str(e),
            )

        return DetectionResult(
            image_id=image_id, status=ITEM_OK,
            txt_path=txt_path, detection_rows=detection_rows,
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
        Process multiple JPG images, optionally in parallel.
        """
        results: List[DetectionResult] = []
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if max_workers <= 1:
            for jpg_path in jpg_images:
                result = self.process_image(jpg_path, output_dir)
                results.append(result)
                if result.status == ITEM_OK:
                    logger.info("Processed %s -> %s", jpg_path.name, result.txt_path.name)
                else:
                    logger.error("Failed processing %s [%s]: %s", jpg_path.name, result.error_code, result.error_message)
                    if fail_stop:
                        raise RuntimeError(f"{result.error_code}: {result.error_message}")
        else:
            futures = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for jpg_path in jpg_images:
                    future = executor.submit(self.process_image, jpg_path, output_dir)
                    futures[future] = jpg_path

                for future in as_completed(futures):
                    jpg_path = futures[future]
                    result = future.result()
                    results.append(result)
                    if result.status == ITEM_OK:
                        logger.info("Processed %s -> %s", jpg_path.name, result.txt_path.name)
                    else:
                        logger.error("Failed processing %s [%s]: %s", jpg_path.name, result.error_code, result.error_message)
                        if fail_stop:
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise RuntimeError(f"{result.error_code}: {result.error_message}")

        return results
