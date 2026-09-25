#!/usr/bin/env python3
"""Benchmark det_to_seg per-box inference against whole-image inference.

Modes (each produces the same class-coded mask format as det_to_seg):

  boxes  current production path: composite_bbox_masks, one crop per detection
  full   tile the whole image, then gather instances by clipping the stitched
         foreground to each detection box (TXT order, first box wins overlaps)
  grid   same whole-image tile grid, but only infer tiles that touch a box

Timings exclude JPG decode (identical across modes). forward_s is GPU forward
time (synchronized); overhead_s is everything else inside the mode (tensor
prep, Hann blending, compositing). Each detection gets its own label (1, 2, ...)
so agreement vs `boxes` is measured per instance, not just per species.

Example:
  python tests/gpu/det_to_seg/bench_full_image.py \
    --images /path/to/images --detections /path/to/detections \
    --out /path/to/bench_out --n 30 --device cuda:0 --save-diffs
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from stages.det_to_seg import segmentor  # noqa: E402
from stages.det_to_seg.processor import load_config, parse_yolo_detections  # noqa: E402

MODES = ("boxes", "full", "grid")


# ---------------------------------------------------------------------------
# Forward-call instrumentation
# ---------------------------------------------------------------------------

class _ForwardStats:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.tiles = 0
        self.pixels = 0
        self.seconds = 0.0


STATS = _ForwardStats()
_original_predict_probabilities = segmentor._predict_probabilities


def _sync(device: str) -> None:
    if str(device).startswith("cuda"):
        torch.cuda.synchronize()


def _timed_predict_probabilities(model, tensor, device):
    _sync(device)
    t0 = time.perf_counter()
    out = _original_predict_probabilities(model, tensor, device)
    _sync(device)
    STATS.seconds += time.perf_counter() - t0
    STATS.calls += 1
    STATS.tiles += int(tensor.shape[0])
    STATS.pixels += int(tensor.shape[0] * tensor.shape[2] * tensor.shape[3])
    return out


# Every inference path (single, batched, tiled) resolves this module global.
segmentor._predict_probabilities = _timed_predict_probabilities


# ---------------------------------------------------------------------------
# Whole-image inference + instance gathering
# ---------------------------------------------------------------------------

def tile_positions(h: int, w: int, tile_size: int, overlap: int) -> list[tuple[int, int, int, int]]:
    """Same grid predict_mask_tiled builds, but over the full image."""
    step = max(1, tile_size - overlap)
    return [
        (y, x, min(y + tile_size, h), min(x + tile_size, w))
        for y in range(0, h, step)
        for x in range(0, w, step)
    ]


def tiles_touching_boxes(positions, boxes_xyxy):
    return [
        (y, x, y2, x2)
        for (y, x, y2, x2) in positions
        if any(y < by2 and by1 < y2 and x < bx2 and bx1 < x2 for (bx1, by1, bx2, by2) in boxes_xyxy)
    ]


def infer_tiles(model, image_rgb, positions, config, device):
    """Hann-blended probability accumulation, mirroring predict_mask_tiled."""
    h, w = image_rgb.shape[:2]
    acc = np.zeros((h, w), dtype=np.float32)
    wsum = np.zeros((h, w), dtype=np.float32)
    divisor = int(config["pad_divisor"]) or None
    batch_size = int(config.get("batch_size", segmentor.DEFAULT_BATCH_SIZE))

    for start in range(0, len(positions), batch_size):
        chunk = positions[start : start + batch_size]
        tiles = [image_rgb[y:y2, x:x2] for (y, x, y2, x2) in chunk]
        probs = segmentor._predict_probabilities_padded(model, tiles, divisor, device)
        for (y, x, y2, x2), p in zip(chunk, probs):
            win = segmentor._hann2d(y2 - y, x2 - x)
            acc[y:y2, x:x2] += p * win
            wsum[y:y2, x:x2] += win
    return acc, wsum


def gather_instances(acc, wsum, prepared, thr, shape):
    """Threshold only inside boxes and composite in TXT order (first box wins),
    matching composite_bbox_masks' overlap rule."""
    out = np.zeros(shape, dtype=np.uint8)
    for crop in prepared:
        x1, y1, x2, y2 = crop.xyxy
        fg = acc[y1:y2, x1:x2] / np.clip(wsum[y1:y2, x1:x2], 1e-6, None) >= thr
        region = out[y1:y2, x1:x2]
        region[fg & (region == 0)] = crop.detection.class_id
    return out


def run_mode(mode, model, image_rgb, detections, config, device):
    if mode == "boxes":
        return segmentor.composite_bbox_masks(model, image_rgb, detections, config, device)

    prepared = segmentor._prepare_crops(image_rgb, detections)
    h, w = image_rgb.shape[:2]
    positions = tile_positions(h, w, int(config["tile_size"]), int(config["overlap"]))
    if mode == "grid":
        positions = tiles_touching_boxes(positions, [c.xyxy for c in prepared])
    acc, wsum = infer_tiles(model, image_rgb, positions, config, device)
    return gather_instances(acc, wsum, prepared, float(config["threshold"]), (h, w))


# ---------------------------------------------------------------------------
# Comparison + visualization
# ---------------------------------------------------------------------------

def compare(ref: np.ndarray, other: np.ndarray) -> dict:
    a, b = ref > 0, other > 0
    union = int(np.count_nonzero(a | b))
    if union == 0:
        return {"fg_iou": 1.0, "instance_agreement": 1.0}
    same = int(np.count_nonzero((ref == other) & (a | b)))
    return {
        "fg_iou": int(np.count_nonzero(a & b)) / union,
        "instance_agreement": same / union,
    }


def write_diff(path: Path, image_rgb, ref, other, max_width: int = 1800) -> None:
    """green = same instance, yellow = both fg but different instance,
    red = boxes-only fg, blue = this-mode-only fg."""
    scale = min(1.0, max_width / image_rgb.shape[1])
    size = (int(image_rgb.shape[1] * scale), int(image_rgb.shape[0] * scale))
    img = cv2.resize(cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR), size, interpolation=cv2.INTER_AREA)
    r = cv2.resize(ref, size, interpolation=cv2.INTER_NEAREST)
    o = cv2.resize(other, size, interpolation=cv2.INTER_NEAREST)
    color = np.zeros_like(img)
    color[(r > 0) & (o > 0) & (r == o)] = (0, 200, 0)
    color[(r > 0) & (o > 0) & (r != o)] = (0, 220, 220)
    color[(r > 0) & (o == 0)] = (0, 0, 255)
    color[(r == 0) & (o > 0)] = (255, 0, 0)
    painted = color.any(axis=2)
    img[painted] = (0.45 * img[painted] + 0.55 * color[painted]).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 85])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def find_pairs(images_dir: Path, detections_dir: Path) -> list[tuple[Path, Path]]:
    jpgs = {
        p.stem.lower(): p
        for p in images_dir.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg")
    }
    pairs = []
    for txt in sorted(detections_dir.iterdir()):
        if txt.suffix.lower() == ".txt" and txt.stem.lower() in jpgs:
            pairs.append((txt, jpgs[txt.stem.lower()]))
    return pairs


def _stats(values) -> dict:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {}
    return {
        "mean": float(arr.mean()),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def summarize(records: list[dict], modes: list[str]) -> dict:
    summary = {}
    base_total = sum(r["modes"]["boxes"]["total_s"] for r in records) if "boxes" in modes else None
    for mode in modes:
        rows = [r["modes"][mode] for r in records]
        total = sum(m["total_s"] for m in rows)
        entry = {
            "total_s": _stats([m["total_s"] for m in rows]),
            "forward_s": _stats([m["forward_s"] for m in rows]),
            "overhead_s": _stats([m["total_s"] - m["forward_s"] for m in rows]),
            "forward_calls": _stats([m["forward_calls"] for m in rows]),
            "tiles": _stats([m["tiles"] for m in rows]),
            "megapixels_inferred": _stats([m["pixels"] / 1e6 for m in rows]),
            "peak_gpu_mb": max((m.get("peak_gpu_mb") or 0) for m in rows),
        }
        if base_total:
            entry["speedup_vs_boxes"] = base_total / total if total else None
        if mode != "boxes" and "boxes" in modes:
            entry["fg_iou_vs_boxes"] = _stats([m["fg_iou"] for m in rows])
            entry["instance_agreement_vs_boxes"] = _stats([m["instance_agreement"] for m in rows])
        summary[mode] = entry
    return summary


def print_summary(summary: dict, records: list[dict]) -> None:
    cov = _stats([r["box_union_frac"] for r in records])
    print(f"\n{len(records)} images, box union coverage mean {cov['mean']:.2f} "
          f"p50 {cov['p50']:.2f} p90 {cov['p90']:.2f}")
    header = f"{'mode':<6} {'total s':>8} {'fwd s':>7} {'ovh s':>7} {'calls':>6} {'tiles':>6} {'MP inf':>7} {'speedup':>8} {'fgIoU':>6} {'inst':>6}"
    print(header)
    print("-" * len(header))
    for mode, s in summary.items():
        speed = s.get("speedup_vs_boxes")
        iou = s.get("fg_iou_vs_boxes", {}).get("mean")
        inst = s.get("instance_agreement_vs_boxes", {}).get("mean")
        print(
            f"{mode:<6} {s['total_s']['mean']:>8.2f} {s['forward_s']['mean']:>7.2f} "
            f"{s['overhead_s']['mean']:>7.2f} {s['forward_calls']['mean']:>6.1f} "
            f"{s['tiles']['mean']:>6.1f} {s['megapixels_inferred']['mean']:>7.1f} "
            f"{(f'{speed:.2f}x' if speed else '-'):>8} "
            f"{(f'{iou:.3f}' if iou is not None else '-'):>6} "
            f"{(f'{inst:.3f}' if inst is not None else '-'):>6}"
        )
    print("(means per image; speedup = sum(boxes total) / sum(mode total))")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", type=Path, required=True, help="Directory of source JPGs")
    ap.add_argument("--detections", type=Path, required=True, help="Directory of YOLO detection .txt files")
    ap.add_argument("--c", type=Path, default=Path(__file__).resolve().parents[3] / "stages/det_to_seg/configs/default.yaml",
                    help="det_to_seg model config (default: stages/det_to_seg/configs/default.yaml)")
    ap.add_argument("--out", type=Path, required=True, help="Output directory for bench_results.json (and diffs)")
    ap.add_argument("--n", type=int, default=30, help="Number of images to sample (0 = all)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--modes", default=",".join(MODES), help=f"Comma-separated subset of {MODES}")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--save-diffs", action="store_true", help="Write downscaled diff overlays vs boxes mode")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = set(modes) - set(MODES)
    if unknown:
        ap.error(f"unknown modes: {sorted(unknown)}")

    config = load_config(args.c)
    pairs = find_pairs(args.images, args.detections)
    if not pairs:
        print(f"No matching txt/jpg pairs in {args.detections} and {args.images}", file=sys.stderr)
        return 1
    random.Random(args.seed).shuffle(pairs)
    if args.n > 0:
        pairs = pairs[: args.n]

    print(f"config: {args.c}")
    print(f"tile_size={config['tile_size']} overlap={config['overlap']} batch_size={config.get('batch_size')} "
          f"device={args.device} modes={modes} images={len(pairs)}")

    model = segmentor.build_seg_model(config["arch"], config["encoder"], Path(config["weights"]), args.device)

    cuda = str(args.device).startswith("cuda")
    if cuda:
        # Warm up kernels/allocator on a full-size tile batch so the first
        # timed image doesn't carry one-time CUDA init cost.
        tile = int(config["tile_size"])
        warm = [np.zeros((tile, tile, 3), dtype=np.uint8)] * int(config.get("batch_size", 16))
        segmentor._predict_probabilities_padded(model, warm, int(config["pad_divisor"]) or None, args.device)
        _sync(args.device)
    records = []
    skipped_empty = 0
    for idx, (txt_path, jpg_path) in enumerate(pairs):
        t0 = time.perf_counter()
        im_bgr = cv2.imread(str(jpg_path))
        if im_bgr is None:
            print(f"skip {jpg_path.name}: unreadable", file=sys.stderr)
            continue
        image_rgb = cv2.cvtColor(im_bgr, cv2.COLOR_BGR2RGB)
        del im_bgr
        decode_s = time.perf_counter() - t0
        h, w = image_rgb.shape[:2]

        detections = parse_yolo_detections(txt_path, w, h)
        if not detections:
            skipped_empty += 1
            continue
        detections = [dataclasses.replace(d, class_id=(i % 255) + 1) for i, d in enumerate(detections)]

        union = np.zeros((h, w), dtype=bool)
        for c in segmentor._prepare_crops(image_rgb, detections):
            x1, y1, x2, y2 = c.xyxy
            union[y1:y2, x1:x2] = True
        box_union_frac = float(union.mean())
        del union

        # Rotate mode order per image so no mode always runs first/last.
        order = modes[idx % len(modes):] + modes[: idx % len(modes)]
        masks, results = {}, {}
        for mode in order:
            STATS.reset()
            if cuda:
                torch.cuda.reset_peak_memory_stats(args.device)
            _sync(args.device)
            t0 = time.perf_counter()
            masks[mode] = run_mode(mode, model, image_rgb, detections, config, args.device)
            _sync(args.device)
            results[mode] = {
                "total_s": time.perf_counter() - t0,
                "forward_s": STATS.seconds,
                "forward_calls": STATS.calls,
                "tiles": STATS.tiles,
                "pixels": STATS.pixels,
                "peak_gpu_mb": torch.cuda.max_memory_allocated(args.device) / 2**20 if cuda else None,
            }

        if "boxes" in masks:
            for mode in masks:
                if mode == "boxes":
                    continue
                results[mode].update(compare(masks["boxes"], masks[mode]))
                if args.save_diffs:
                    write_diff(args.out / "diffs" / f"{jpg_path.stem}_{mode}.jpg", image_rgb, masks["boxes"], masks[mode])

        records.append({
            "image_id": jpg_path.stem,
            "width": w,
            "height": h,
            "n_detections": len(detections),
            "box_union_frac": box_union_frac,
            "decode_s": decode_s,
            "modes": results,
        })
        line = "  ".join(
            f"{m}={results[m]['total_s']:.2f}s/{results[m]['tiles']}t"
            + (f"/iou{results[m]['fg_iou']:.3f}" if "fg_iou" in results[m] else "")
            for m in modes
        )
        print(f"[{len(records)}/{len(pairs)}] {jpg_path.stem} dets={len(detections)} "
              f"cov={box_union_frac:.2f}  {line}", flush=True)

    if not records:
        print("No images with detections were processed", file=sys.stderr)
        return 1

    summary = summarize(records, modes)
    print_summary(summary, records)
    if skipped_empty:
        print(f"skipped {skipped_empty} images with zero detections")

    args.out.mkdir(parents=True, exist_ok=True)
    out_json = args.out / "bench_results.json"
    out_json.write_text(json.dumps({
        "config_path": str(args.c),
        "config": {k: config[k] for k in ("tile_size", "overlap", "batch_size", "threshold", "pad_divisor", "tiling") if k in config},
        "device": args.device,
        "gpu": torch.cuda.get_device_name(args.device) if cuda else None,
        "modes": modes,
        "skipped_empty": skipped_empty,
        "summary": summary,
        "images": records,
    }, indent=2))
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
