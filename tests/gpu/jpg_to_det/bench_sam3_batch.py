#!/usr/bin/env python3
"""
Benchmark jpg_to_det's SAM 3 pipeline across batch sizes.

Runs the pre-batching path (one image at a time: decode, set_image, one
set_text_prompt per prompt, export) as the baseline, then
Processor.process_batch at each --batch-sizes value, and reports throughput,
where the time goes, peak GPU memory, and how far each run's detections are
from the baseline's.

    python tests/gpu/jpg_to_det/bench_sam3_batch.py \\
        --m /path/sam3_finetuned_best.pt --i /path/batch/images \\
        --n 48 --batch-sizes 1 2 4 8 --readers 8 --mem-cap-gb 80

--mem-cap-gb caps the GPU memory this process may use, to check that a batch
size fits a smaller card (e.g. 80 or 40 for an A100) from a bigger one.
"""

import argparse
import contextlib
import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

from stages.jpg_to_det.detector import export_predictions
from stages.jpg_to_det.processor import Processor

REPO = Path(__file__).resolve().parents[3]


def legacy_run_sam3(processor, im0_bgr, config):
    """run_sam3 before batching (set_image + set_text_prompt per prompt)."""
    h, w = im0_bgr.shape[:2]
    image = Image.fromarray(cv2.cvtColor(im0_bgr, cv2.COLOR_BGR2RGB))
    per_prompt = []
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        state = processor.set_image(image)
        for prompt in config["prompts"]:
            out = processor.set_text_prompt(prompt=str(prompt["prompt"]), state=state)
            boxes = torch.as_tensor(out["boxes"]).float().reshape(-1, 4).cpu()
            scores = torch.as_tensor(out["scores"]).float().reshape(-1, 1).cpu()
            per_prompt.append(torch.cat([boxes, scores, torch.full_like(scores, float(prompt["class"]))], 1))
        del state
    det = torch.cat(per_prompt)
    det[:, 0:4:2] = det[:, 0:4:2].clamp(0, w)
    det[:, 1:4:2] = det[:, 1:4:2].clamp(0, h)
    det = det[(det[:, 2] > det[:, 0]) & (det[:, 3] > det[:, 1])]
    det = det[det[:, 4].argsort(descending=True)]
    return det[: int(config.get("final_max_det", 1000))]


def run_legacy(processor, jpgs, out_dir):
    timings = {"read": 0.0, "gpu": 0.0, "export": 0.0}
    start = time.perf_counter()
    for jpg in jpgs:
        t0 = time.perf_counter()
        im0 = cv2.imread(str(jpg))
        t1 = time.perf_counter()
        det = legacy_run_sam3(processor.model, im0, processor.config)
        t2 = time.perf_counter()
        export_predictions(det, out_dir, str(jpg), processor.names, im0)
        t3 = time.perf_counter()
        timings["read"] += t1 - t0
        timings["gpu"] += t2 - t1
        timings["export"] += t3 - t2
    return {**timings, "total": time.perf_counter() - start, "images": len(jpgs)}


def read_txt(path):
    rows = np.loadtxt(path, ndmin=2) if path.stat().st_size else np.zeros((0, 6))
    return rows[np.lexsort(rows.T[::-1])]  # order-independent


def compare(ref_dir, run_dir, stems):
    """Images whose detection count differs, and the largest coordinate /
    confidence difference (normalized) where the counts match."""
    count_diff, max_box, max_conf = 0, 0.0, 0.0
    for stem in stems:
        a, b = read_txt(ref_dir / f"{stem}.txt"), read_txt(run_dir / f"{stem}.txt")
        if a.shape != b.shape:
            count_diff += 1
            continue
        if len(a):
            max_box = max(max_box, float(np.abs(a[:, 1:5] - b[:, 1:5]).max()))
            max_conf = max(max_conf, float(np.abs(a[:, 5] - b[:, 5]).max()))
    return {"images_count_differs": count_diff, "max_box_diff": max_box, "max_conf_diff": max_conf}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--c", type=Path, default=REPO / "stages/jpg_to_det/configs/sam3.yaml")
    ap.add_argument("--m", type=Path, required=True, help="SAM 3 checkpoint")
    ap.add_argument("--i", type=Path, required=True, help="JPG directory")
    ap.add_argument("--o", type=Path, default=Path("bench_sam3_batch_out"))
    ap.add_argument("--n", type=int, default=48, help="images per run")
    ap.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--readers", type=int, default=8, help="reader threads (jpg_to_det --t)")
    ap.add_argument("--mem-cap-gb", type=float, default=None, help="cap GPU memory to this many GB")
    ap.add_argument("--skip-legacy", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("stages.jpg_to_det.processor").setLevel(logging.WARNING)

    jpgs = sorted(args.i.glob("*.jpg"))[: args.n]
    stems = [j.stem for j in jpgs]
    print(f"{len(jpgs)} images from {args.i}; warming the page cache...", flush=True)
    for j in jpgs:
        j.read_bytes()

    total_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
    if args.mem_cap_gb:
        torch.cuda.set_per_process_memory_fraction(min(1.0, args.mem_cap_gb / total_gb))
    print(f"GPU {torch.cuda.get_device_name(0)} ({total_gb:.0f} GB), cap {args.mem_cap_gb or 'none'} GB", flush=True)

    processor = Processor(args.c, args.m, device="cuda")
    # warm-up: kernels, allocator, cached text features
    processor.config["batch_size"] = 1
    processor.process_batch(jpgs[:1], args.o / "warmup", fail_stop=False, max_workers=1)

    report = {}
    if not args.skip_legacy:
        torch.cuda.reset_peak_memory_stats()
        t = run_legacy(processor, jpgs, args.o / "legacy")
        t["peak_gb"] = torch.cuda.max_memory_allocated() / 1e9
        report["legacy"] = t
        print(f"legacy        {t['images'] / t['total']:.2f} img/s  read={t['read']:.1f}s gpu={t['gpu']:.1f}s "
              f"export={t['export']:.1f}s  peak={t['peak_gb']:.1f} GB", flush=True)

    ref = args.o / ("legacy" if not args.skip_legacy else f"bs{args.batch_sizes[0]}")
    for bs in args.batch_sizes:
        processor.config["batch_size"] = bs
        run_dir = args.o / f"bs{bs}"
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        results = []
        with contextlib.suppress(Exception):
            results = processor.process_batch(jpgs, run_dir, fail_stop=False, max_workers=args.readers)
        t = dict(processor.last_timings)
        t["peak_gb"] = torch.cuda.max_memory_allocated() / 1e9
        t["failed"] = sum(r.status != "ok" for r in results)
        if run_dir != ref:
            t.update(compare(ref, run_dir, stems))
        report[f"bs{bs}"] = t
        print(f"batch_size={bs:<3} {t['images'] / t['total']:.2f} img/s  waiting-on-reads={t['read_wait']:.1f}s "
              f"gpu={t['gpu']:.1f}s ({t['gpu'] / t['images']:.3f}s/img) export={t['export']:.1f}s  "
              f"peak={t['peak_gb']:.1f} GB  failed={t['failed']}"
              + (f"  vs {ref.name}: count-diff={t['images_count_differs']} "
                 f"max-box-diff={t['max_box_diff']:.2e} max-conf-diff={t['max_conf_diff']:.2e}"
                 if "max_box_diff" in t else ""), flush=True)

    args.o.mkdir(parents=True, exist_ok=True)
    (args.o / "report.json").write_text(json.dumps(report, indent=2))
    print(f"wrote {args.o / 'report.json'}")


if __name__ == "__main__":
    main()
