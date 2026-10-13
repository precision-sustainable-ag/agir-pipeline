"""Segmentation model inference for mask generation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np
import torch
import segmentation_models_pytorch as smp

from stages.common.class_ids import DetectionBox, UINT8_MAX

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 16
TILE_TRIGGER_SIDE = 1024
TILE_TRIGGER_AREA = 1024 * 1024

# Counts calls to _predict_probabilities, the single chokepoint every
# inference path (single-crop, batched-crop, tiled) funnels through. Used to
# measure real GPU forward-call counts for perf baselining; not read by any
# inference logic itself.
_forward_call_count = 0


def reset_forward_call_count() -> None:
    global _forward_call_count
    _forward_call_count = 0


def get_forward_call_count() -> int:
    return _forward_call_count


# ---------------------------------------------------------------------------
# Weight loading (mirrors infer_bbox.py)
# ---------------------------------------------------------------------------


def _strip_prefix(sd: dict, prefixes=("model.", "module.")) -> dict:
    out = {}
    for k, v in sd.items():
        nk = k
        for p in prefixes:
            if nk.startswith(p):
                nk = nk[len(p):]
        out[nk] = v
    return out


def load_weights_flex(model: torch.nn.Module, path: Path, strict: bool = False) -> None:
    # load model weights
    obj = torch.load(str(path), map_location="cpu", weights_only=False)

    # extract model state dictionary from torch.load output
    sd = obj.get("state_dict", obj) if isinstance(obj, dict) else obj

    if not isinstance(sd, dict):
        raise RuntimeError(f"Unsupported checkpoint format: {path}")
    sd = _strip_prefix(sd)

    missing, unexpected = model.load_state_dict(sd, strict=strict)
    # check issues from model state dict loading
    if missing:
        log.warning("missing keys: %s", missing)
    if unexpected:
        log.warning("unexpected keys: %s", unexpected)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def build_seg_model(arch: str, encoder: str, weights_path: Path, device: str) -> torch.nn.Module:
    model = smp.create_model(
        arch=arch,
        encoder_name=encoder,
        encoder_weights=None,
        in_channels=3,
        classes=1,
    )
    model = model.to(device).eval()
    load_weights_flex(model, Path(weights_path))
    return model


# ---------------------------------------------------------------------------
# Tensor helpers
# ---------------------------------------------------------------------------

def _to_tensor01(rgb: np.ndarray) -> torch.Tensor:
    t = torch.from_numpy(rgb.astype(np.float32) / 255.0)
    return t.permute(2, 0, 1).unsqueeze(0)


def _predict_probabilities(
    model: torch.nn.Module,
    tensor: torch.Tensor,
    device: str,
) -> torch.Tensor:
    global _forward_call_count
    _forward_call_count += 1
    with torch.inference_mode():
        if str(device).startswith("cuda"):
            with torch.amp.autocast(device_type="cuda"):
                return torch.sigmoid(model(tensor.to(device)))
        return torch.sigmoid(model(tensor.to(device)))


# ---------------------------------------------------------------------------
# Segmentation inference
# ---------------------------------------------------------------------------

def _predict_probabilities_padded(
    model: torch.nn.Module,
    crops_rgb: Sequence[np.ndarray],
    divisor: Optional[int],
    device: str,
) -> list[np.ndarray]:
    """Infer crops of any size in one model call and return raw (unthresholded)
    probability maps, each sliced back to its own crop's native (h, w).

    Crops are padded up to the batch's common max (h, w) -- reflect-padded
    when legal (mirrors _pad_to_divisor's rule, applied per crop against its
    own native size), constant-padded otherwise -- so torch.cat works across
    differently-sized crops without distorting any crop's own pixel content.

    Returns unthresholded probabilities so tiled inference can Hann-blend
    across overlapping tiles before thresholding once; predict_masks_batched
    is a thin thresholding wrapper around this for callers that just want
    binary masks.
    """
    if not crops_rgb:
        return []

    shapes = [crop.shape[:2] for crop in crops_rgb]
    max_h = max(h for h, _ in shapes)
    max_w = max(w for _, w in shapes)
    if divisor:
        max_h += (divisor - max_h % divisor) % divisor
        max_w += (divisor - max_w % divisor) % divisor

    padded_tensors = []
    for crop in crops_rgb:
        h, w = crop.shape[:2]
        ph, pw = max_h - h, max_w - w
        mode = "reflect" if ph < h and pw < w else "constant"
        padded_tensors.append(
            torch.nn.functional.pad(_to_tensor01(crop), (0, pw, 0, ph), mode=mode)
        )

    batch = torch.cat(padded_tensors, dim=0)
    probabilities = _predict_probabilities(model, batch, device)
    probabilities = probabilities.squeeze(1).cpu().numpy()

    return [prob[:h, :w] for prob, (h, w) in zip(probabilities, shapes)]


def predict_masks_batched(
    model: torch.nn.Module,
    crops_rgb: Sequence[np.ndarray],
    thr: float,
    divisor: Optional[int],
    device: str,
) -> list[np.ndarray]:
    """Infer crops of any size in one model call and return thresholded binary
    crop masks, each sliced back to its own crop's native (h, w)."""
    probabilities = _predict_probabilities_padded(model, crops_rgb, divisor, device)
    return [(p > thr).astype(np.uint8) for p in probabilities]


def _hann2d(h: int, w: int) -> np.ndarray:
    w2d = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    normalized = w2d / (w2d.max() + 1e-8)
    # A pure Hann window is zero on its outer edge. Keep a small positive
    # floor so pixels on the full crop boundary receive tiled predictions.
    return np.maximum(normalized, 1e-3)


def _tile_positions(
    height: int,
    width: int,
    tile_size: int,
    overlap: int,
) -> list[tuple[int, int, int, int]]:
    step = max(1, tile_size - overlap)
    positions: list[tuple[int, int, int, int]] = []
    for y in range(0, height, step):
        for x in range(0, width, step):
            positions.append((y, x, min(y + tile_size, height), min(x + tile_size, width)))
    return positions


# ---------------------------------------------------------------------------
# BBox compositing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PreparedCrop:
    order: int
    detection: DetectionBox
    xyxy: tuple[int, int, int, int]
    crop_rgb: np.ndarray


@dataclass(frozen=True)
class _CrossImageCrop:
    image_index: int
    crop: _PreparedCrop


@dataclass
class _TiledCropState:
    item: _CrossImageCrop
    acc: np.ndarray
    wsum: np.ndarray


@dataclass(frozen=True)
class _TileTask:
    state_index: int
    y: int
    x: int
    y2: int
    x2: int
    tile_rgb: np.ndarray


def _validated_class_id(detection: DetectionBox) -> int:
    class_id = detection.class_id
    if isinstance(class_id, bool) or not isinstance(class_id, Integral):
        raise ValueError(
            f"Detection {detection.bounding_box_id} class_id must be an integer, "
            f"got {class_id!r}"
        )
    class_id = int(class_id)
    if not 1 <= class_id <= UINT8_MAX:
        raise ValueError(
            f"Detection {detection.bounding_box_id} class_id must be in mask range "
            f"1..{UINT8_MAX}, got {class_id}"
        )
    return class_id


def _prepare_crops(
    image_rgb: np.ndarray,
    detections: Sequence[DetectionBox],
) -> list[_PreparedCrop]:
    height, width = image_rgb.shape[:2]
    prepared: list[_PreparedCrop] = []

    for order, detection in enumerate(detections):
        _validated_class_id(detection)
        x1, y1, x2, y2 = detection.xyxy
        x1 = max(0, min(x1, width - 1))
        y1 = max(0, min(y1, height - 1))
        x2 = max(x1 + 1, min(x2, width))
        y2 = max(y1 + 1, min(y2, height))
        crop_rgb = image_rgb[y1:y2, x1:x2]
        if crop_rgb.size == 0:
            continue
        prepared.append(
            _PreparedCrop(
                order=order,
                detection=detection,
                xyxy=(x1, y1, x2, y2),
                crop_rgb=crop_rgb,
            )
        )

    return prepared


def _is_large_crop(crop: np.ndarray) -> bool:
    height, width = crop.shape[:2]
    return max(height, width) > TILE_TRIGGER_SIDE or height * width > TILE_TRIGGER_AREA


def _shape_sort_key(array: np.ndarray) -> tuple[int, int, int]:
    height, width = array.shape[:2]
    return max(height, width), min(height, width), height * width


def _chunk_cross_image_crops(
    crops: Sequence[_CrossImageCrop],
    batch_size: int,
) -> list[list[_CrossImageCrop]]:
    ordered = sorted(crops, key=lambda item: _shape_sort_key(item.crop.crop_rgb))
    return [ordered[i : i + batch_size] for i in range(0, len(ordered), batch_size)]


def _chunk_tile_tasks(
    tasks: Sequence[_TileTask],
    batch_size: int,
) -> list[list[_TileTask]]:
    # Most tiles have the configured fixed size. Keep each crop's tile order
    # stable so Hann accumulation remains deterministic, while allowing the
    # last partial batch for one crop to be filled by tiles from the next.
    return [list(tasks[i : i + batch_size]) for i in range(0, len(tasks), batch_size)]


def _validate_crop_mask(mask: np.ndarray, crop: _PreparedCrop) -> None:
    expected_shape = crop.crop_rgb.shape[:2]
    if not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.shape != expected_shape:
        actual = getattr(mask, "shape", None)
        raise ValueError(
            f"Detection {crop.detection.bounding_box_id} produced mask shape {actual}; "
            f"expected {expected_shape}"
        )


def composite_bbox_masks_cross_image(
    model: torch.nn.Module,
    images: Sequence[tuple[np.ndarray, Sequence[DetectionBox]]],
    config: dict,
    device: str,
) -> list[np.ndarray]:
    """Create masks for several images using shared crop and tile batches."""
    if not images:
        return []

    threshold = float(config["threshold"])
    divisor = int(config["pad_divisor"]) if int(config["pad_divisor"]) > 0 else None
    batch_size = config.get("batch_size", DEFAULT_BATCH_SIZE)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError(f"batch_size must be a positive integer, got {batch_size!r}")

    tile_size = int(config["tile_size"])
    overlap = int(config["overlap"])
    use_tiling = bool(config["tiling"])
    prepared_by_image: list[list[_PreparedCrop]] = []
    masks_by_image: list[dict[int, np.ndarray]] = []
    full_masks: list[np.ndarray] = []
    ordinary: list[_CrossImageCrop] = []
    tiled: list[_CrossImageCrop] = []

    for image_index, (image_rgb, detections) in enumerate(images):
        height, width = image_rgb.shape[:2]
        prepared = _prepare_crops(image_rgb, detections)
        prepared_by_image.append(prepared)
        masks_by_image.append({})
        full_masks.append(np.zeros((height, width), dtype=np.uint8))

        for crop in prepared:
            item = _CrossImageCrop(image_index=image_index, crop=crop)
            if use_tiling and _is_large_crop(crop.crop_rgb):
                tiled.append(item)
            else:
                ordinary.append(item)

    ordinary_batches = _chunk_cross_image_crops(ordinary, batch_size)
    for chunk in ordinary_batches:
        masks = predict_masks_batched(
            model=model,
            crops_rgb=[item.crop.crop_rgb for item in chunk],
            thr=threshold,
            divisor=divisor,
            device=device,
        )
        if len(masks) != len(chunk):
            raise RuntimeError(
                f"Batched inference returned {len(masks)} masks for {len(chunk)} crops"
            )
        for item, mask in zip(chunk, masks):
            masks_by_image[item.image_index][item.crop.order] = mask

    tiled_states: list[_TiledCropState] = []
    tile_tasks: list[_TileTask] = []
    for item in tiled:
        height, width = item.crop.crop_rgb.shape[:2]
        state_index = len(tiled_states)
        tiled_states.append(
            _TiledCropState(
                item=item,
                acc=np.zeros((height, width), dtype=np.float32),
                wsum=np.zeros((height, width), dtype=np.float32),
            )
        )
        for y, x, y2, x2 in _tile_positions(height, width, tile_size, overlap):
            tile_tasks.append(
                _TileTask(
                    state_index=state_index,
                    y=y,
                    x=x,
                    y2=y2,
                    x2=x2,
                    tile_rgb=item.crop.crop_rgb[y:y2, x:x2],
                )
            )

    tile_batches = _chunk_tile_tasks(tile_tasks, batch_size)
    for chunk in tile_batches:
        probabilities = _predict_probabilities_padded(
            model,
            [task.tile_rgb for task in chunk],
            divisor,
            device,
        )
        if len(probabilities) != len(chunk):
            raise RuntimeError(
                f"Batched inference returned {len(probabilities)} tile predictions "
                f"for {len(chunk)} tiles"
            )
        for task, probability in zip(chunk, probabilities):
            state = tiled_states[task.state_index]
            window = _hann2d(task.y2 - task.y, task.x2 - task.x)
            state.acc[task.y : task.y2, task.x : task.x2] += probability * window
            state.wsum[task.y : task.y2, task.x : task.x2] += window

    for state in tiled_states:
        mask = (
            state.acc / np.clip(state.wsum, 1e-6, None) >= threshold
        ).astype(np.uint8)
        masks_by_image[state.item.image_index][state.item.crop.order] = mask

    log.info(
        "Cross-image batching | images=%d | ordinary_crops=%d batches=%d "
        "| tiled_crops=%d tiles=%d batches=%d",
        len(images),
        len(ordinary),
        len(ordinary_batches),
        len(tiled),
        len(tile_tasks),
        len(tile_batches),
    )

    for image_index, prepared in enumerate(prepared_by_image):
        full_mask = full_masks[image_index]
        for crop in prepared:
            mask_crop = masks_by_image[image_index][crop.order]
            _validate_crop_mask(mask_crop, crop)
            x1, y1, x2, y2 = crop.xyxy
            region = full_mask[y1:y2, x1:x2]
            writable = (mask_crop != 0) & (region == 0)
            region[writable] = _validated_class_id(crop.detection)

    return full_masks


def validate_class_mask(
    mask: np.ndarray,
    *,
    expected_shape: tuple[int, int] | None = None,
) -> None:
    if not isinstance(mask, np.ndarray):
        raise ValueError(f"Segmentation mask must be a numpy array, got {type(mask).__name__}")
    if mask.ndim != 2:
        raise ValueError(f"Segmentation mask must be single-channel, got shape {mask.shape}")
    if mask.dtype != np.uint8:
        raise ValueError(f"Segmentation mask must have dtype uint8, got {mask.dtype}")
    if expected_shape is not None and mask.shape != expected_shape:
        raise ValueError(
            f"Segmentation mask shape {mask.shape} does not match source image {expected_shape}"
        )


def write_mask_png(
    class_mask: np.ndarray,
    out_path: Path,
    *,
    expected_shape: tuple[int, int] | None = None,
) -> None:
    validate_class_mask(class_mask, expected_shape=expected_shape)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(out_path), class_mask)
    if not ok:
        raise RuntimeError(f"cv2.imwrite failed for {out_path}")
