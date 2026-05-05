#!/usr/bin/env python3
from pathlib import Path
import argparse
import random

import cv2
import numpy as np


def draw_overlay(
    image_path: Path,
    mask_path: Path,
    det_path: Path | None,
    out_path: Path,
    max_width: int,
    alpha: float,
) -> bool:
    im = cv2.imread(str(image_path))
    if im is None:
        return False

    h, w = im.shape[:2]

    if mask_path.exists():
        mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if mask is not None:
            if mask.shape[:2] != (h, w):
                mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            color_layer = np.zeros_like(im)
            color_layer[mask > 127] = (0, 0, 255)  # red for masked pixels
            im = cv2.addWeighted(color_layer, alpha, im, 1.0, 0)

    if det_path is not None and det_path.exists():
        text = det_path.read_text().strip()
        if text:
            for line in text.splitlines():
                parts = line.split()
                if len(parts) < 6:
                    continue

                cls_id, xc, yc, bw, bh, conf = parts[:6]
                xc, yc, bw, bh, conf = map(float, (xc, yc, bw, bh, conf))

                x1 = int((xc - bw / 2) * w)
                y1 = int((yc - bh / 2) * h)
                x2 = int((xc + bw / 2) * w)
                y2 = int((yc + bh / 2) * h)

                cv2.rectangle(im, (x1, y1), (x2, y2), (0, 255, 0), 6)
                cv2.putText(
                    im,
                    f"{int(float(cls_id))} {conf:.2f}",
                    (x1, max(40, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.2,
                    (0, 255, 0),
                    3,
                    cv2.LINE_AA,
                )

    if im.shape[1] > max_width:
        scale = max_width / im.shape[1]
        im = cv2.resize(im, (int(im.shape[1] * scale), int(im.shape[0] * scale)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    return cv2.imwrite(str(out_path), im)


def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay segmentation masks (and optionally detections) on JPGs.")
    parser.add_argument("--images", required=True, type=Path, help="Directory of source JPGs.")
    parser.add_argument("--masks", required=True, type=Path, help="Directory of *_mask.png outputs from det_to_seg.")
    parser.add_argument("--detections", type=Path, default=None, help="Directory of .txt detection outputs (optional).")
    parser.add_argument("--output", required=True, type=Path, help="Directory for overlay JPGs.")
    parser.add_argument("--sample-size", type=int, default=12, help="Number of random images to visualize.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for repeatable sampling.")
    parser.add_argument("--max-width", type=int, default=1800, help="Downscale overlays to this max width.")
    parser.add_argument("--alpha", type=float, default=0.4, help="Mask overlay opacity (0=transparent, 1=opaque).")
    args = parser.parse_args()

    images = sorted(list(args.images.glob("*.jpg")) + list(args.images.glob("*.JPG")))
    if not images:
        raise SystemExit(f"No JPGs found in {args.images}")

    random.seed(args.seed)
    sample = random.sample(images, min(args.sample_size, len(images)))

    written = 0
    for image_path in sample:
        mask_path = args.masks / f"{image_path.stem}_mask.png"
        det_path = args.detections / f"{image_path.stem}.txt" if args.detections else None
        out_path = args.output / image_path.name
        if draw_overlay(image_path, mask_path, det_path, out_path, args.max_width, args.alpha):
            written += 1

    print(f"Wrote {written} overlays to {args.output}")


if __name__ == "__main__":
    main()