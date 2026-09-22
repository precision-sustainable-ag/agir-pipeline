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

def predict_mask_single(
    model: torch.nn.Module,
    crop_rgb: np.ndarray,
    thr: float,
    divisor: Optional[int],
    device: str,
) -> np.ndarray:
    return predict_masks_batched(
        model=model,
        crops_rgb=[crop_rgb],
        thr=thr,
        divisor=divisor,
        device=device,
    )[0]


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


def predict_mask_tiled(
    model: torch.nn.Module,
    crop_rgb: np.ndarray,
    thr: float,
    divisor: Optional[int],
    device: str,
    tile_size: int = 1024,
    overlap: int = 128,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> np.ndarray:
    h, w = crop_rgb.shape[:2]
    acc = np.zeros((h, w), dtype=np.float32)
    wsum = np.zeros((h, w), dtype=np.float32)
    step = max(1, tile_size - overlap)

    positions: list[tuple[int, int, int, int]] = []
    y = 0
    while y < h:
        x = 0
        while x < w:
            positions.append((y, x, min(y + tile_size, h), min(x + tile_size, w)))
            x += step
        y += step

    # Batch tiles through the shared padded-probability primitive so a large
    # crop's tiles cost a handful of forward calls, not one per tile. Tiles
    # are accumulated as continuous probabilities and thresholded once at the
    # end (below) -- never via the thresholding predict_masks_batched, which
    # would threshold each tile before Hann-blending and corrupt the seams
    # the windowing exists to smooth.
    for start in range(0, len(positions), batch_size):
        chunk = positions[start : start + batch_size]
        tiles = [crop_rgb[y:y2, x:x2] for (y, x, y2, x2) in chunk]
        probabilities = _predict_probabilities_padded(model, tiles, divisor, device)
        for (y, x, y2, x2), p in zip(chunk, probabilities):
            win = _hann2d(y2 - y, x2 - x)
            acc[y:y2, x:x2] += p * win
            wsum[y:y2, x:x2] += win

    return (acc / np.clip(wsum, 1e-6, None) >= thr).astype(np.uint8)



def predict_mask(
    model: torch.nn.Module,
    crop_rgb: np.ndarray,
    *,
    thr: float,
    divisor: Optional[int],
    device: str,
    use_tiling: bool,
    tile_size: int,
    overlap: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    tile_trigger_side: int = TILE_TRIGGER_SIDE,
    tile_trigger_area: int = TILE_TRIGGER_AREA,
) -> np.ndarray:
    h, w = crop_rgb.shape[:2]
    should_tile = use_tiling and (max(h, w) > tile_trigger_side or (h * w) > tile_trigger_area)

    if should_tile:
        return predict_mask_tiled(
            model=model,
            crop_rgb=crop_rgb,
            thr=thr,
            divisor=divisor,
            device=device,
            tile_size=tile_size,
            overlap=overlap,
            batch_size=batch_size,
        )

    return predict_mask_single(
        model=model,
        crop_rgb=crop_rgb,
        thr=thr,
        divisor=divisor,
        device=device,
    )


# ---------------------------------------------------------------------------
# BBox compositing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PreparedCrop:
    order: int
    detection: DetectionBox
    xyxy: tuple[int, int, int, int]
    crop_rgb: np.ndarray


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


def _chunk_by_size(crops: Sequence[_PreparedCrop], batch_size: int) -> list[list[_PreparedCrop]]:
    """Sort crops by longest side so crops batched together are similarly
    sized (bounds padding waste when they're padded to a shared shape), then
    slice into fixed-size chunks."""
    ordered = sorted(crops, key=lambda crop: max(crop.crop_rgb.shape[:2]))
    return [ordered[i : i + batch_size] for i in range(0, len(ordered), batch_size)]


def _validate_crop_mask(mask: np.ndarray, crop: _PreparedCrop) -> None:
    expected_shape = crop.crop_rgb.shape[:2]
    if not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.shape != expected_shape:
        actual = getattr(mask, "shape", None)
        raise ValueError(
            f"Detection {crop.detection.bounding_box_id} produced mask shape {actual}; "
            f"expected {expected_shape}"
        )


def composite_bbox_masks(
    model: torch.nn.Module,
    image_rgb: np.ndarray,
    detections: Sequence[DetectionBox],
    config: dict,
    device: str,
) -> np.ndarray:
    height, width = image_rgb.shape[:2]
    full_mask = np.zeros((height, width), dtype=np.uint8)
    prepared = _prepare_crops(image_rgb, detections)
    if not prepared:
        return full_mask

    threshold = float(config["threshold"])
    divisor = int(config["pad_divisor"]) if int(config["pad_divisor"]) > 0 else None
    batch_size = config.get("batch_size", DEFAULT_BATCH_SIZE)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError(f"batch_size must be a positive integer, got {batch_size!r}")

    masks_by_order: dict[int, np.ndarray] = {}
    large_crops: list[_PreparedCrop] = []
    ordinary_crops: list[_PreparedCrop] = []
    for crop in prepared:
        (large_crops if _is_large_crop(crop.crop_rgb) else ordinary_crops).append(crop)

    for crop in large_crops:
        masks_by_order[crop.order] = predict_mask(
            model=model,
            crop_rgb=crop.crop_rgb,
            thr=threshold,
            divisor=divisor,
            device=device,
            use_tiling=bool(config["tiling"]),
            tile_size=int(config["tile_size"]),
            overlap=int(config["overlap"]),
            batch_size=batch_size,
        )

    # Sort-by-longest-side then fixed-size chunking replaces exact-shape
    # grouping: real YOLO boxes almost never share an exact pixel size, so
    # requiring an exact match left batch_size effectively unused. Crops in a
    # chunk are padded (not resized) to the chunk's own max shape inside
    # predict_masks_batched, so grouping strategy has no effect on any crop's
    # own pixel content -- only how many forward calls it costs to infer them.
    for chunk in _chunk_by_size(ordinary_crops, batch_size):
        masks = predict_masks_batched(
            model=model,
            crops_rgb=[crop.crop_rgb for crop in chunk],
            thr=threshold,
            divisor=divisor,
            device=device,
        )
        if len(masks) != len(chunk):
            raise RuntimeError(
                f"Batched inference returned {len(masks)} masks for {len(chunk)} crops"
            )
        for crop, mask in zip(chunk, masks):
            masks_by_order[crop.order] = mask

    # Composite in original TXT order even though inference was grouped into
    # size-sorted chunks. The first detection owns overlap pixels; later
    # detections may only fill pixels that are still background.
    for crop in prepared:
        mask_crop = masks_by_order[crop.order]
        _validate_crop_mask(mask_crop, crop)
        x1, y1, x2, y2 = crop.xyxy
        region = full_mask[y1:y2, x1:x2]
        writable = (mask_crop != 0) & (region == 0)
        region[writable] = _validated_class_id(crop.detection)

    return full_mask


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
