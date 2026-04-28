"""Segmentation model inference for mask generation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import torch
import segmentation_models_pytorch as smp

log = logging.getLogger(__name__)


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
    obj = torch.load(str(path), map_location="cpu", weights_only=False)
    sd = obj.get("state_dict", obj) if isinstance(obj, dict) else obj
    if not isinstance(sd, dict):
        raise RuntimeError(f"Unsupported checkpoint format: {path}")
    sd = _strip_prefix(sd)
    missing, unexpected = model.load_state_dict(sd, strict=strict)
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
    x = _to_tensor01(crop_rgb)
    xpad, pads = _pad_to_divisor(x, divisor)
    with torch.inference_mode():
        if str(device).startswith("cuda"):
            with torch.amp.autocast(device_type="cuda"):
                prob = torch.sigmoid(model(xpad.to(device)))
        else:
            prob = torch.sigmoid(model(xpad.to(device)))

    mask = (prob > thr).float().squeeze(0).squeeze(0).cpu().numpy().astype(np.uint8)
    if divisor:
        mask = _unpad(mask, pads)

    ch, cw = crop_rgb.shape[:2]
    if mask.shape != (ch, cw):
        mask = cv2.resize(mask, (cw, ch), interpolation=cv2.INTER_NEAREST)
    return mask



def _hann2d(h: int, w: int) -> np.ndarray:
    w2d = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    return w2d / (w2d.max() + 1e-8)



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
            with torch.inference_mode():
                if str(device).startswith("cuda"):
                    with torch.amp.autocast(device_type="cuda"):
                        prob = torch.sigmoid(model(tpad.to(device)))
                else:
                    prob = torch.sigmoid(model(tpad.to(device)))

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
    tile_trigger_side: int = 1024,
    tile_trigger_area: int = 1024 * 1024,
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

def composite_bbox_masks(
    model: torch.nn.Module,
    image_rgb: np.ndarray,
    boxes_xyxy: list[tuple[int, int, int, int]],
    config: dict,
    device: str,
) -> np.ndarray:
    h, w = image_rgb.shape[:2]
    full_mask = np.zeros((h, w), dtype=np.uint8)

    for x1, y1, x2, y2 in boxes_xyxy:
        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))

        crop_rgb = image_rgb[y1:y2, x1:x2]
        if crop_rgb.size == 0:
            continue

        mask_crop = predict_mask(
            model,
            crop_rgb,
            thr=float(config["threshold"]),
            divisor=int(config["pad_divisor"]) if int(config["pad_divisor"]) > 0 else None,
            device=device,
            use_tiling=bool(config["tiling"]),
            tile_size=int(config["tile_size"]),
            overlap=int(config["overlap"]),
        )
        full_mask[y1:y2, x1:x2] |= mask_crop

    return full_mask



def write_mask_png(mask01: np.ndarray, out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(out_path), (mask01.astype(np.uint8) * 255))
    if not ok:
        raise RuntimeError(f"cv2.imwrite failed for {out_path}")
