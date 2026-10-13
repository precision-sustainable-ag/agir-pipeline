"""DET -> SEG processor."""

from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
import yaml
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from stages import ITEM_FAILED, ITEM_OK
from stages.common import resolve_path
from stages.common.class_ids import (
    ClassIdIndex,
    DetectionBox,
    resolve_detection_class_ids,
)

from . import (
    ERROR_DET_READ_FAILED,
    ERROR_EXPORT_FAILED,
    ERROR_IMAGE_READ_FAILED,
    ERROR_INFERENCE_FAILED,
    ERROR_MODEL_LOAD_FAILED,
)
from .segmentor import (
    build_seg_model,
    composite_bbox_masks_cross_image,
    get_forward_call_count,
    reset_forward_call_count,
    write_mask_png,
)

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
    decode_seconds: float = 0.0
    inference_seconds: float = 0.0
    write_seconds: float = 0.0
    n_forward_calls: int = 0


@dataclass
class _PreparedImage:
    """CPU-only prep output for one image: either a finished result (mask
    already exists, or decode/parse failed) or a decoded image ready for
    GPU inference. Produced by Processor._prepare, which never touches the
    model/GPU, so it's safe to run on a background thread."""

    image_id: str
    mask_path: Path
    early_result: SegmentationResult | None = None
    im_rgb: np.ndarray | None = None
    detections: list[DetectionBox] | None = None
    n_fallback_detections: int = 0
    decode_seconds: float = 0.0
    height: int = 0
    width: int = 0


@dataclass
class _InferenceOutput:
    """An ordered inference result awaiting either no work or a PNG write."""

    prepared: _PreparedImage
    result: SegmentationResult
    class_mask: np.ndarray | None = None


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
DEFAULT_PREPARE_WORKERS = 2
DEFAULT_PREFETCH_DEPTH = 4
DEFAULT_WRITE_WORKERS = 2
DEFAULT_WRITE_QUEUE_DEPTH = 8


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

    # prefetch_depth is retained as a backwards-compatible fallback. New
    # configs should tune preparation lookahead and inference grouping
    # independently.
    config.setdefault("prefetch_depth", DEFAULT_PREFETCH_DEPTH)
    config.setdefault("prepare_queue_depth", config["prefetch_depth"])
    config.setdefault("inference_window_size", config["prefetch_depth"])

    for key, default in (
        ("prepare_workers", DEFAULT_PREPARE_WORKERS),
        ("prefetch_depth", DEFAULT_PREFETCH_DEPTH),
        ("prepare_queue_depth", DEFAULT_PREFETCH_DEPTH),
        ("inference_window_size", DEFAULT_PREFETCH_DEPTH),
        ("write_workers", DEFAULT_WRITE_WORKERS),
        ("write_queue_depth", DEFAULT_WRITE_QUEUE_DEPTH),
    ):
        config.setdefault(key, default)
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"Config '{key}' must be a positive integer, got {value!r}")


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

    def _prepare(self, txt_path: Path, jpg_path: Path, output_dir: Path) -> _PreparedImage:
        """CPU-only: idempotent skip check, JPEG decode, detection parsing.
        Never touches the model or GPU -- safe to call from a background
        thread while the main thread runs GPU inference for another image."""
        image_id = txt_path.stem
        mask_path = output_dir / f"{image_id}.png"

        if mask_path.exists():
            return _PreparedImage(
                image_id=image_id,
                mask_path=mask_path,
                early_result=SegmentationResult(
                    image_id=image_id, status=ITEM_OK, mask_path=mask_path
                ),
            )

        decode_start = time.perf_counter()
        try:
            im_bgr = cv2.imread(str(jpg_path))
            if im_bgr is None:
                raise RuntimeError(f"cv2.imread returned None for {jpg_path}")
            im_rgb = cv2.cvtColor(im_bgr, cv2.COLOR_BGR2RGB)
            height, width = im_rgb.shape[:2]
        except Exception as e:
            return _PreparedImage(
                image_id=image_id,
                mask_path=mask_path,
                early_result=SegmentationResult(
                    image_id=image_id,
                    status=ITEM_FAILED,
                    error_code=ERROR_IMAGE_READ_FAILED,
                    error_type=type(e).__name__,
                    error_message=str(e),
                ),
            )
        decode_seconds = time.perf_counter() - decode_start

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
            return _PreparedImage(
                image_id=image_id,
                mask_path=mask_path,
                early_result=SegmentationResult(
                    image_id=image_id,
                    status=ITEM_FAILED,
                    error_code=ERROR_DET_READ_FAILED,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    decode_seconds=decode_seconds,
                ),
            )

        return _PreparedImage(
            image_id=image_id,
            mask_path=mask_path,
            im_rgb=im_rgb,
            detections=detections,
            n_fallback_detections=n_fallback_detections,
            decode_seconds=decode_seconds,
            height=height,
            width=width,
        )

    @staticmethod
    def _result_from_prepared(
        prepared: _PreparedImage,
        *,
        status: str,
        inference_seconds: float = 0.0,
        n_forward_calls: int = 0,
        **kwargs,
    ) -> SegmentationResult:
        return SegmentationResult(
            image_id=prepared.image_id,
            status=status,
            n_detections=len(prepared.detections or []),
            n_fallback_detections=prepared.n_fallback_detections,
            decode_seconds=prepared.decode_seconds,
            inference_seconds=inference_seconds,
            n_forward_calls=n_forward_calls,
            **kwargs,
        )

    def _infer_many(
        self,
        prepared_images: list[_PreparedImage],
    ) -> list[_InferenceOutput]:
        """Run shared GPU batches without blocking on PNG output."""
        outputs: list[_InferenceOutput | None] = [None] * len(prepared_images)
        active_indices = [
            index
            for index, prepared in enumerate(prepared_images)
            if prepared.early_result is None
        ]

        for index, prepared in enumerate(prepared_images):
            if prepared.early_result is not None:
                outputs[index] = _InferenceOutput(prepared, prepared.early_result)

        if not active_indices:
            return [output for output in outputs if output is not None]

        active = [prepared_images[index] for index in active_indices]
        reset_forward_call_count()
        inference_start = time.perf_counter()
        try:
            class_masks = composite_bbox_masks_cross_image(
                model=self.model,
                images=[
                    (prepared.im_rgb, prepared.detections or [])
                    for prepared in active
                ],
                config=self.config,
                device=self.device,
            )
            if len(class_masks) != len(active):
                raise RuntimeError(
                    f"Cross-image inference returned {len(class_masks)} masks "
                    f"for {len(active)} images"
                )
        except Exception as e:
            window_seconds = time.perf_counter() - inference_start
            inference_seconds = window_seconds / len(active)
            forward_calls = get_forward_call_count()
            calls_base, calls_extra = divmod(forward_calls, len(active))
            for active_position, result_index in enumerate(active_indices):
                prepared = prepared_images[result_index]
                result = self._result_from_prepared(
                    prepared,
                    status=ITEM_FAILED,
                    error_code=ERROR_INFERENCE_FAILED,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    inference_seconds=inference_seconds,
                    n_forward_calls=calls_base + (active_position < calls_extra),
                )
                outputs[result_index] = _InferenceOutput(prepared, result)
            return [output for output in outputs if output is not None]

        window_seconds = time.perf_counter() - inference_start
        inference_seconds = window_seconds / len(active)
        forward_calls = get_forward_call_count()
        calls_base, calls_extra = divmod(forward_calls, len(active))

        for active_position, (result_index, class_mask) in enumerate(
            zip(active_indices, class_masks)
        ):
            prepared = prepared_images[result_index]
            calls_for_image = calls_base + (active_position < calls_extra)
            result = self._result_from_prepared(
                prepared,
                status=ITEM_OK,
                mask_path=prepared.mask_path,
                inference_seconds=inference_seconds,
                n_forward_calls=calls_for_image,
            )
            outputs[result_index] = _InferenceOutput(prepared, result, class_mask)

        return [output for output in outputs if output is not None]

    @staticmethod
    def _write_inference_output(output: _InferenceOutput) -> SegmentationResult:
        """Write one mask on an output worker and convert errors to results."""
        if output.class_mask is None:
            return output.result

        write_start = time.perf_counter()
        try:
            write_mask_png(
                output.class_mask,
                output.prepared.mask_path,
                expected_shape=(output.prepared.height, output.prepared.width),
            )
        except Exception as e:
            result = output.result
            result.status = ITEM_FAILED
            result.mask_path = None
            result.error_code = ERROR_EXPORT_FAILED
            result.error_type = type(e).__name__
            result.error_message = str(e)
            result.write_seconds = time.perf_counter() - write_start
            return result

        output.result.write_seconds = time.perf_counter() - write_start
        return output.result

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

        prepare_workers = int(self.config["prepare_workers"])
        prepare_queue_depth = int(self.config["prepare_queue_depth"])
        inference_window_size = int(self.config["inference_window_size"])
        write_workers = int(self.config["write_workers"])
        write_queue_depth = int(self.config["write_queue_depth"])
        logger.info(
            "Execution plan | device=%s | mode=parallel-prepare+cross-image-batching "
            "| prepare_workers=%d | prepare_queue_depth=%d | inference_window_size=%d "
            "| write_workers=%d | write_queue_depth=%d | model_copies=1",
            self.device,
            prepare_workers,
            prepare_queue_depth,
            inference_window_size,
            write_workers,
            write_queue_depth,
        )
        if total == 0:
            return results

        # Prepare a few images in parallel so the GPU does not have to wait.
        # Keep the queue small and process the images in their original order.
        def _prepare_safely(txt_path: Path, jpg_path: Path) -> _PreparedImage:
            try:
                return self._prepare(txt_path, jpg_path, output_dir)
            except Exception as e:
                image_id = txt_path.stem
                return _PreparedImage(
                    image_id=image_id,
                    mask_path=output_dir / f"{image_id}.png",
                    early_result=SegmentationResult(
                        image_id=image_id,
                        status=ITEM_FAILED,
                        error_code=ERROR_IMAGE_READ_FAILED,
                        error_type=type(e).__name__,
                        error_message=f"Unexpected error during prepare: {e}",
                    ),
                )

        pair_iter = iter(pairs)
        pending: deque[Future[_PreparedImage]] = deque()
        prepare_executor = ThreadPoolExecutor(
            max_workers=prepare_workers,
            thread_name_prefix="det_to_seg-prepare",
        )
        write_executor = ThreadPoolExecutor(
            max_workers=write_workers,
            thread_name_prefix="det_to_seg-write",
        )
        pending_outputs: deque[
            tuple[_PreparedImage, SegmentationResult | Future[SegmentationResult]]
        ] = deque()
        outstanding_writes = 0

        def _submit_next() -> bool:
            try:
                txt_path, jpg_path = next(pair_iter)
            except StopIteration:
                return False
            pending.append(prepare_executor.submit(_prepare_safely, txt_path, jpg_path))
            return True

        for _ in range(min(prepare_queue_depth, total)):
            _submit_next()

        def _emit_one(progress, *, block: bool) -> bool:
            """Emit the oldest result. Return False when it is not ready."""
            nonlocal outstanding_writes
            if not pending_outputs:
                return False
            prepared, result_or_future = pending_outputs[0]
            if isinstance(result_or_future, Future):
                if not block and not result_or_future.done():
                    return False
                result = result_or_future.result()
                outstanding_writes -= 1
            else:
                result = result_or_future
            pending_outputs.popleft()

            idx = len(results) + 1
            progress.set_postfix_str(prepared.image_id)
            progress.update(1)
            results.append(result)
            if result.status == ITEM_OK:
                logger.info(
                    "[%d/%d] Image %s OK -> %s (detections=%d, fallback=%d, "
                    "decode=%.3fs, inference=%.3fs, forward_calls=%d, write=%.3fs)",
                    idx,
                    total,
                    prepared.image_id,
                    result.mask_path,
                    result.n_detections,
                    result.n_fallback_detections,
                    result.decode_seconds,
                    result.inference_seconds,
                    result.n_forward_calls,
                    result.write_seconds,
                )
            else:
                logger.error(
                    "[%d/%d] Image %s FAILED [%s]: %s",
                    idx,
                    total,
                    prepared.image_id,
                    result.error_code,
                    result.error_message,
                )
            return True

        try:
            with (
                logging_redirect_tqdm(),
                tqdm(total=total, desc="det_to_seg", unit="img") as progress,
            ):
                submitted_for_inference = 0
                stop_requested = False
                while submitted_for_inference < total and not stop_requested:
                    window_size = min(
                        inference_window_size, total - submitted_for_inference
                    )
                    prepared_window: list[_PreparedImage] = []
                    for _ in range(window_size):
                        prepared_window.append(pending.popleft().result())
                        _submit_next()

                    if fail_stop:
                        for index, prepared in enumerate(prepared_window):
                            early = prepared.early_result
                            if early is not None and early.status != ITEM_OK:
                                prepared_window = prepared_window[: index + 1]
                                stop_requested = True
                                break

                    submitted_for_inference += len(prepared_window)
                    window_outputs = self._infer_many(prepared_window)
                    for output in window_outputs:
                        if output.class_mask is None:
                            pending_outputs.append((output.prepared, output.result))
                            if fail_stop and output.result.status != ITEM_OK:
                                stop_requested = True
                        else:
                            # Preserve the configured bound, but allow a full
                            # queue to overlap with the next GPU window. Wait
                            # only when another mask is about to be submitted.
                            while outstanding_writes >= write_queue_depth:
                                _emit_one(progress, block=True)
                                if fail_stop and results[-1].status != ITEM_OK:
                                    stop_requested = True
                                    break
                            if stop_requested:
                                break
                            future = write_executor.submit(
                                self._write_inference_output, output
                            )
                            pending_outputs.append((output.prepared, future))
                            outstanding_writes += 1

                    while pending_outputs and _emit_one(progress, block=False):
                        if fail_stop and results[-1].status != ITEM_OK:
                            stop_requested = True
                            break

                while pending_outputs:
                    _emit_one(progress, block=True)
                    if fail_stop and results[-1].status != ITEM_OK:
                        break
        finally:
            for future in pending:
                future.cancel()
            prepare_executor.shutdown(wait=True, cancel_futures=True)
            write_executor.shutdown(wait=True, cancel_futures=True)
        return results
