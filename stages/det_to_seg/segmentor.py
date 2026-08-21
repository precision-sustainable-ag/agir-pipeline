"""Segmentation model inference for mask generation."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np
import torch
import segmentation_models_pytorch as smp

from .class_ids import DetectionBox, UINT8_MAX

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 16
TILE_TRIGGER_SIDE = 1024
TILE_TRIGGER_AREA = 1024 * 1024


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
    with torch.inference_mode():
        if str(device).startswith("cuda"):
            with torch.amp.autocast(device_type="cuda"):
                return torch.sigmoid(model(tensor.to(device)))
        return torch.sigmoid(model(tensor.to(device)))


def _pad_to_divisor(x: torch.Tensor, div: Optional[int]):
    if not div:
        return x, (0, 0, 0, 0)
    _, _, h, w = x.shape
    ph = (div - h % div) % div
    pw = (div - w % div) % div
    mode = "reflect" if ph < h and pw < w else "constant"
    padded = torch.nn.functional.pad(x, (0, pw, 0, ph), mode=mode)
    return padded, (0, ph, 0, pw)


def _unpad(arr: np.ndarray, pads) -> np.ndarray:
    _, pb, _, pr = pads
    h, w = arr.shape[:2]
    return arr[: h - pb if pb else h, : w - pr if pr else w]


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


def predict_masks_batched(
    model: torch.nn.Module,
    crops_rgb: Sequence[np.ndarray],
    thr: float,
    divisor: Optional[int],
    device: str,
) -> list[np.ndarray]:
    """Infer same-sized crops in one model call and return binary crop masks."""

    if not crops_rgb:
        return []

    crop_shapes = [crop.shape[:2] for crop in crops_rgb]
    if len(set(crop_shapes)) != 1:
        raise ValueError("Batched segmentation crops must all have the same height and width")

    batch = torch.cat([_to_tensor01(crop) for crop in crops_rgb], dim=0)
    padded, pads = _pad_to_divisor(batch, divisor)
    probabilities = _predict_probabilities(model, padded, device)
    masks = (probabilities > thr).float().squeeze(1).cpu().numpy().astype(np.uint8)

    results: list[np.ndarray] = []
    for mask, (crop_height, crop_width) in zip(masks, crop_shapes):
        if divisor:
            mask = _unpad(mask, pads)
        if mask.shape != (crop_height, crop_width):
            mask = cv2.resize(
                mask,
                (crop_width, crop_height),
                interpolation=cv2.INTER_NEAREST,
            )
        results.append(mask)
    return results


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
) -> np.ndarray:
    h, w = crop_rgb.shape[:2]
    acc = np.zeros((h, w), dtype=np.float32)
    wsum = np.zeros((h, w), dtype=np.float32)
    step = max(1, tile_size - overlap)

    y = 0
    while y < h:
        x = 0
        while x < w:
            y2 = min(y + tile_size, h)
            x2 = min(x + tile_size, w)
            tile = crop_rgb[y:y2, x:x2]
            th, tw = tile.shape[:2]
            win = _hann2d(th, tw)

            t = _to_tensor01(tile)
            tpad, pads = _pad_to_divisor(t, divisor)
            prob = _predict_probabilities(model, tpad, device)

            p = prob.squeeze(0).squeeze(0).cpu().numpy()
            if divisor:
                p = _unpad(p, pads)
            if p.shape != (th, tw):
                p = cv2.resize(p, (tw, th), interpolation=cv2.INTER_LINEAR)

            acc[y:y2, x:x2] += p * win
            wsum[y:y2, x:x2] += win
            x += step
        y += step

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
    ordinary_by_shape: dict[tuple[int, int], list[_PreparedCrop]] = defaultdict(list)

    for crop in prepared:
        if _is_large_crop(crop.crop_rgb):
            masks_by_order[crop.order] = predict_mask(
                model=model,
                crop_rgb=crop.crop_rgb,
                thr=threshold,
                divisor=divisor,
                device=device,
                use_tiling=bool(config["tiling"]),
                tile_size=int(config["tile_size"]),
                overlap=int(config["overlap"]),
            )
        else:
            ordinary_by_shape[crop.crop_rgb.shape[:2]].append(crop)

    for crops in ordinary_by_shape.values():
        for start in range(0, len(crops), batch_size):
            chunk = crops[start : start + batch_size]
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

    # Composite in original TXT order even though inference was grouped by
    # shape. The first detection owns overlap pixels; later detections may only
    # fill pixels that are still background.
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
