"""DET -> SEG processor."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import yaml
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from stages import ITEM_FAILED, ITEM_OK
from stages.common import resolve_path

from . import (
    ERROR_DET_READ_FAILED,
    ERROR_EXPORT_FAILED,
    ERROR_IMAGE_READ_FAILED,
    ERROR_INFERENCE_FAILED,
    ERROR_MODEL_LOAD_FAILED,
)
from .class_ids import (
    ClassIdIndex,
    DetectionBox,
    resolve_detection_class_ids,
)
from .segmentor import build_seg_model, composite_bbox_masks, write_mask_png

logger = logging.getLogger(__name__)


@dataclass
class SegmentationResult:
    image_id: str
    status: str
    mask_path: Path | None = None
    n_detections: int = 0
    n_fallback_detections: int = 0
    error_code: str = ""
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool = False


_REQUIRED_CONFIG_KEYS = [
    "weights",
    "arch",
    "encoder",
    "threshold",
    "pad_divisor",
    "tile_size",
    "overlap",
    "tiling",
]

DEFAULT_BATCH_SIZE = 16


def validate_config(config: dict) -> None:
    if not config:
        raise ValueError("Config is empty or None")

    for key in _REQUIRED_CONFIG_KEYS:
        if key not in config:
            raise ValueError(f"Config missing required key: {key}")

    threshold = config["threshold"]
    if not isinstance(threshold, (int, float)) or threshold < 0.0 or threshold > 1.0:
        raise ValueError(f"Config 'threshold' must be in [0, 1], got {threshold}")

    for key in ("pad_divisor", "tile_size"):
        if int(config[key]) <= 0:
            raise ValueError(f"Config '{key}' must be > 0")

    if int(config["overlap"]) < 0:
        raise ValueError("Config 'overlap' must be >= 0")

    config.setdefault("batch_size", DEFAULT_BATCH_SIZE)
    batch_size = config["batch_size"]
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError(f"Config 'batch_size' must be a positive integer, got {batch_size!r}")


def load_config(config_path: Path) -> dict:
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        config = yaml.safe_load(f)

    if config is None:
        raise ValueError(f"Config file is empty or invalid: {config_path}")

    config["weights"] = str(resolve_path(str(config["weights"]), config_path.parent))
    validate_config(config)
    return config


def parse_yolo_detections(txt_path: Path, width: int, height: int) -> list[DetectionBox]:
    """
    Parse YOLO txt lines as: cls xc yc w h conf (normalized).

    Returns boxes with clamped absolute xyxy coordinates and the original
    zero-based TXT row ID used by det_to_world's georeferenced CSV.
    """
    txt_path = Path(txt_path)
    if not txt_path.exists():
        raise FileNotFoundError(f"Detection txt not found: {txt_path}")

    boxes: list[DetectionBox] = []
    try:
        with open(txt_path) as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as e:
        raise RuntimeError(f"Failed reading detection txt: {txt_path}") from e

    for bounding_box_id, line in enumerate(lines):
        row_number = bounding_box_id + 1
        parts = line.split()
        if len(parts) < 5:
            raise ValueError(
                f"Malformed detection row {row_number} in {txt_path.name}: '{line}'"
            )

        try:
            # cls and conf are accepted but not used.
            _cls = float(parts[0])
            xc = float(parts[1])
            yc = float(parts[2])
            bw = float(parts[3])
            bh = float(parts[4])
            if len(parts) >= 6:
                _conf = float(parts[5])
        except Exception as e:
            raise ValueError(
                f"Non-numeric detection row {row_number} in {txt_path.name}: '{line}'"
            ) from e

        x1 = (xc - bw / 2.0) * width
        y1 = (yc - bh / 2.0) * height
        x2 = (xc + bw / 2.0) * width
        y2 = (yc + bh / 2.0) * height

        x1_i = int(max(0, min(width, round(x1))))
        y1_i = int(max(0, min(height, round(y1))))
        x2_i = int(max(0, min(width, round(x2))))
        y2_i = int(max(0, min(height, round(y2))))

        if x2_i <= x1_i or y2_i <= y1_i:
            continue

        boxes.append(
            DetectionBox(
                bounding_box_id=bounding_box_id,
                xyxy=(x1_i, y1_i, x2_i, y2_i),
            )
        )

    return boxes


def _load_model_from_config(config: dict, device: str):
    weights_path = Path(config["weights"])
    if not weights_path.exists():
        raise FileNotFoundError(f"Model weights not found: {weights_path}")
    return build_seg_model(
        arch=config["arch"],
        encoder=config["encoder"],
        weights_path=weights_path,
        device=device,
    )


class Processor:
    """Segmentation processor for det_to_seg."""

    def __init__(
        self,
        config_path: Path,
        device: str = "cpu",
        class_id_index: ClassIdIndex | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.device = device
        self.class_id_index = class_id_index

        try:
            self.model = _load_model_from_config(self.config, device=device)
        except Exception as e:
            raise RuntimeError(f"{ERROR_MODEL_LOAD_FAILED}: {e}") from e

    def process_image(self, txt_path: Path, jpg_path: Path, output_dir: Path) -> SegmentationResult:
        txt_path = Path(txt_path)
        jpg_path = Path(jpg_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        image_id = txt_path.stem
        mask_path = output_dir / f"{image_id}.png"

        if mask_path.exists():
            return SegmentationResult(image_id=image_id, status=ITEM_OK, mask_path=mask_path)

        try:
            im_bgr = cv2.imread(str(jpg_path))
            if im_bgr is None:
                raise RuntimeError(f"cv2.imread returned None for {jpg_path}")
            im_rgb = cv2.cvtColor(im_bgr, cv2.COLOR_BGR2RGB)
            height, width = im_rgb.shape[:2]
        except Exception as e:
            return SegmentationResult(
                image_id=image_id,
                status=ITEM_FAILED,
                error_code=ERROR_IMAGE_READ_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        try:
            detections = parse_yolo_detections(txt_path, width=width, height=height)
            if self.class_id_index is not None:
                resolution = resolve_detection_class_ids(
                    image_id,
                    detections,
                    self.class_id_index,
                )
                detections = list(resolution.detections)
                n_fallback_detections = resolution.fallback_count
            else:
                n_fallback_detections = len(detections)
        except Exception as e:
            return SegmentationResult(
                image_id=image_id,
                status=ITEM_FAILED,
                error_code=ERROR_DET_READ_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        try:
            class_mask = composite_bbox_masks(
                model=self.model,
                image_rgb=im_rgb,
                detections=detections,
                config=self.config,
                device=self.device,
            )
        except Exception as e:
            return SegmentationResult(
                image_id=image_id,
                status=ITEM_FAILED,
                n_detections=len(detections),
                n_fallback_detections=n_fallback_detections,
                error_code=ERROR_INFERENCE_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        try:
            write_mask_png(class_mask, mask_path, expected_shape=(height, width))
        except Exception as e:
            return SegmentationResult(
                image_id=image_id,
                status=ITEM_FAILED,
                n_detections=len(detections),
                n_fallback_detections=n_fallback_detections,
                error_code=ERROR_EXPORT_FAILED,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        return SegmentationResult(
            image_id=image_id,
            status=ITEM_OK,
            mask_path=mask_path,
            n_detections=len(detections),
            n_fallback_detections=n_fallback_detections,
        )

    def process_batch(
        self,
        image_pairs: Iterable[tuple[Path, Path]],
        output_dir: Path,
        fail_stop: bool = True,
    ) -> list[SegmentationResult]:
        pairs = [(Path(txt), Path(jpg)) for txt, jpg in image_pairs]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[SegmentationResult] = []
        total = len(pairs)

        logger.info(
            "Execution plan | device=%s | mode=sequential | workers=1 | model_copies=1",
            self.device,
        )
        with logging_redirect_tqdm():
            progress = tqdm(pairs, total=total, desc="det_to_seg", unit="img")
            for idx, (txt_path, jpg_path) in enumerate(progress, start=1):
                image_id = txt_path.stem
                progress.set_postfix_str(image_id)
                logger.info("[%d/%d] Processing image %s", idx, total, image_id)

                result = self.process_image(txt_path, jpg_path, output_dir)
                results.append(result)

                if result.status == ITEM_OK:
                    logger.info(
                        "[%d/%d] Image %s OK -> %s (detections=%d, fallback=%d)",
                        idx,
                        total,
                        image_id,
                        result.mask_path,
                        result.n_detections,
                        result.n_fallback_detections,
                    )
                else:
                    logger.error(
                        "[%d/%d] Image %s FAILED [%s]: %s",
                        idx,
                        total,
                        image_id,
                        result.error_code,
                        result.error_message,
                    )
                    if fail_stop:
                        return results
        return results
