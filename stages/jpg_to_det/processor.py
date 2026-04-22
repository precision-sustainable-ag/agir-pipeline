"""
JPG -> Detection processor.
"""

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List

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
# Multiprocessing worker globals
# ---------------------------------------------------------------------------

_WORKER_MODEL = None
_WORKER_CONFIG = None
_WORKER_DEVICE = None
_WORKER_NAMES = None


def _init_worker(config_path: str, model_path: str, device: str) -> None:
    """
    Initialize one YOLO model per worker process.
    """
    global _WORKER_MODEL, _WORKER_CONFIG, _WORKER_DEVICE, _WORKER_NAMES

    _WORKER_CONFIG = load_config(Path(config_path))
    _WORKER_DEVICE = device

    try:
        _WORKER_MODEL = YOLO(str(model_path))
    except Exception as e:
        raise RuntimeError(f"{ERROR_MODEL_LOAD_FAILED}: {e}") from e

    _WORKER_NAMES = _WORKER_MODEL.names


def _process_image_worker(jpg_path: str, output_dir: str) -> DetectionResult:
    """
    Process a single image inside a worker process.
    """
    global _WORKER_MODEL, _WORKER_CONFIG, _WORKER_DEVICE, _WORKER_NAMES

    jpg_path = Path(jpg_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    image_id = jpg_path.stem

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

    try:
        det_abs = run_multiscale(
            model=_WORKER_MODEL,
            im0_bgr=im0,
            config=_WORKER_CONFIG,
            device=_WORKER_DEVICE,
        )
    except Exception as e:
        return DetectionResult(
            image_id=image_id,
            status=ITEM_FAILED,
            error_code=ERROR_INFERENCE_FAILED,
            error_type=type(e).__name__,
            error_message=str(e),
        )

    try:
        txt_path, detection_rows = export_predictions(
            results_raw_xyxy_abs=det_abs,
            save_dir=output_dir,
            filename=str(jpg_path),
            names=_WORKER_NAMES,
            im0=im0,
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
# Processor
# ---------------------------------------------------------------------------

class Processor:
    """
    Detection processor — loads YOLO model once for sequential mode.
    Parallel mode uses one model per worker process.
    """

    def __init__(
        self,
        config_path: Path,
        model_path: Path,
        device: str = "cpu",
    ) -> None:
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
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
        Sequential single-image path.
        """
        jpg_path = Path(jpg_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        image_id = jpg_path.stem

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

        try:
            det_abs = run_multiscale(
                model=self.model,
                im0_bgr=im0,
                config=self.config,
                device=self.device,
            )
        except Exception as e:
            return DetectionResult(
                image_id=image_id,
                status=ITEM_FAILED,
                error_code=ERROR_INFERENCE_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        try:
            txt_path, detection_rows = export_predictions(
                results_raw_xyxy_abs=det_abs,
                save_dir=output_dir,
                filename=str(jpg_path),
                names=self.names,
                im0=im0,
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
        Process multiple JPG images, optionally in parallel via process pool.
        """
        jpg_list = [Path(p) for p in jpg_images]
        results: List[DetectionResult] = []
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if max_workers <= 1:
            logger.info(
                "Execution plan | device=%s | mode=sequential | workers=1 | model_copies=1",
                self.device,
            )
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
            return results

        workers = max(1, int(max_workers))
        logger.info(
            "Execution plan | device=%s | mode=process-pool | workers=%d | model_copies=%d",
            self.device,
            workers,
            workers,
        )

        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(str(self.config_path), str(self.model_path), self.device),
        ) as executor:
            future_to_path = {
                executor.submit(_process_image_worker, str(jpg_path), str(output_dir)): jpg_path
                for jpg_path in jpg_list
            }

            for future in as_completed(future_to_path):
                jpg_path = future_to_path[future]

                try:
                    result = future.result()
                except Exception as e:
                    result = DetectionResult(
                        image_id=jpg_path.stem,
                        status=ITEM_FAILED,
                        error_code=ERROR_INFERENCE_FAILED,
                        error_type=type(e).__name__,
                        error_message=str(e),
                    )

                results.append(result)

                if result.status == ITEM_OK:
                    logger.info("Processed %s -> %s", jpg_path.name, result.txt_path.name)
                else:
                    logger.error(
                        "Failed processing %s [%s]: %s",
                        jpg_path.name, result.error_code, result.error_message,
                    )
                    if fail_stop:
                        for pending in future_to_path:
                            pending.cancel()
                        return results

        # optional: keep output ordering stable
        result_map = {r.image_id: r for r in results}
        ordered_results = [result_map[p.stem] for p in jpg_list if p.stem in result_map]
        return ordered_results