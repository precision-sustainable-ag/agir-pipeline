#!/usr/bin/env python3
from pathlib import Path
import argparse
import random

import cv2


def draw_boxes(image_path: Path, det_path: Path, out_path: Path, max_width: int) -> bool:
    im = cv2.imread(str(image_path))
    if im is None:
        return False

    h, w = im.shape[:2]

    # If a image detection file exists and contains rows, draw each predicted box on the image
    if det_path.exists():
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

                # Detection files use normalized YOLO coordinates, so convert back to pixels.
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
        # Downscale wide outputs so they are easier to open and inspect.
        scale = max_width / im.shape[1]
        im = cv2.resize(im, (int(im.shape[1] * scale), int(im.shape[0] * scale)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    return cv2.imwrite(str(out_path), im)


def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay YOLO-format detections on JPGs.")
    parser.add_argument("--images", required=True, type=Path, help="Directory of source JPGs.")
    parser.add_argument("--detections", required=True, type=Path, help="Directory of .txt detection outputs.")
    parser.add_argument("--output", required=True, type=Path, help="Directory for overlay JPGs.")
    parser.add_argument("--sample-size", type=int, default=12, help="Number of random images to visualize.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for repeatable sampling.")
    parser.add_argument("--max-width", type=int, default=1800, help="Downscale overlays to this max width.")
    args = parser.parse_args()

    images = sorted(list(args.images.glob("*.jpg")) + list(args.images.glob("*.JPG")))
    if not images:
        raise SystemExit(f"No JPGs found in {args.images}")

    random.seed(args.seed)
    sample = random.sample(images, min(args.sample_size, len(images)))

    written = 0
    for image_path in sample:
        det_path = args.detections / f"{image_path.stem}.txt"
        out_path = args.output / image_path.name
        if draw_boxes(image_path, det_path, out_path, args.max_width):
            written += 1

    print(f"Wrote {written} overlays to {args.output}")
