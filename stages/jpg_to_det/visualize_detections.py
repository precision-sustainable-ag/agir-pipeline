#!/usr/bin/env python3
"""
scripts/visualize_detections.py
=================================

Overlay YOLO-format detections on a random sample of JPG images and write
the rendered overlays to an output directory.

Used as a QC step in the jpg_to_det pipeline to produce a visual sample of
detection results for operator review without having to open every image.

Input format
------------
Detection files are YOLO-format .txt files, one per image, with the same
stem as the JPG:

    <class_id> <x_center> <y_center> <width> <height> <confidence>

All coordinates are normalized [0, 1] relative to image dimensions.
Images with no detections have an empty or absent .txt file — these are
rendered as-is with no boxes drawn (valid, not an error).

Output
------
Rendered overlay JPGs written to --output, one per sampled image.
Overlays are downscaled to --max-width if wider than that threshold.

Usage
-----
python scripts/visualize_detections.py \\
    --images     /path/to/batch/images \\
    --detections /path/to/run/artifacts \\
    --output     /path/to/visualizations \\
    --sample-size 24 \\
    --max-width   1800

Exit codes
----------
0  Success (even if some individual images failed to render)
1  Hard failure (output dir not writable, etc.)
"""

from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

# Box rendering style
_BOX_COLOR     = (0, 255, 0)   # green
_BOX_THICKNESS = 6
_FONT          = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE    = 1.2
_FONT_THICKNESS = 3
_FONT_COLOR    = (0, 255, 0)
_LABEL_Y_MIN   = 40            # minimum y so label isn't clipped at top


def draw_boxes(
    image_path: Path,
    det_path: Path,
    out_path: Path,
    max_width: int,
) -> bool:
    """
    Read one JPG, draw detection boxes from the corresponding .txt file,
    downscale if needed, and write the overlay to out_path.

    Returns True on success, False if the image could not be read.
    Images with no detections (empty or absent .txt) are written as-is.
    """
    im = cv2.imread(str(image_path))
    if im is None:
        logger.warning("Could not read image: %s", image_path)
        return False

    h, w = im.shape[:2]
    n_boxes = 0

    if det_path.exists():
        text = det_path.read_text().strip()
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 6:
                logger.debug("Skipping malformed detection line in %s: %r", det_path.name, line)
                continue

            try:
                cls_id, xc, yc, bw, bh, conf = parts[:6]
                xc, yc, bw, bh, conf = map(float, (xc, yc, bw, bh, conf))
            except ValueError:
                logger.debug("Could not parse detection line in %s: %r", det_path.name, line)
                continue

            x1 = int((xc - bw / 2) * w)
            y1 = int((yc - bh / 2) * h)
            x2 = int((xc + bw / 2) * w)
            y2 = int((yc + bh / 2) * h)

            cv2.rectangle(im, (x1, y1), (x2, y2), _BOX_COLOR, _BOX_THICKNESS)
            cv2.putText(
                im,
                f"{int(float(cls_id))} {conf:.2f}",
                (x1, max(_LABEL_Y_MIN, y1 - 10)),
                _FONT,
                _FONT_SCALE,
                _FONT_COLOR,
                _FONT_THICKNESS,
                cv2.LINE_AA,
            )
            n_boxes += 1
    else:
        logger.debug("No detection file for %s — rendering image with no boxes", image_path.stem)

    # Downscale if wider than max_width
    if im.shape[1] > max_width:
        scale = max_width / im.shape[1]
        im = cv2.resize(im, (int(im.shape[1] * scale), int(im.shape[0] * scale)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(out_path), im)
    if ok:
        logger.debug("Wrote overlay: %s (%d boxes)", out_path.name, n_boxes)
    else:
        logger.warning("cv2.imwrite failed for %s", out_path)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Overlay YOLO-format detections on a sample of JPGs for QC review."
    )
    parser.add_argument(
        "--images", required=True, type=Path,
        help="Directory of source JPG images.",
    )
    parser.add_argument(
        "--detections", required=True, type=Path,
        help="Directory of .txt detection files (one per image, same stem).",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Directory to write overlay JPGs.",
    )
    parser.add_argument(
        "--sample-size", type=int, default=24,
        help="Number of images to randomly sample (default: 24).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducible sampling (default: 42).",
    )
    parser.add_argument(
        "--max-width", type=int, default=1800,
        help="Downscale overlays wider than this (default: 1800px).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    if not args.images.is_dir():
        logger.error("Images directory not found: %s", args.images)
        return 1
    if not args.detections.is_dir():
        logger.error("Detections directory not found: %s", args.detections)
        return 1

    images = sorted(
        list(args.images.glob("*.jpg")) + list(args.images.glob("*.JPG"))
    )

    if not images:
        logger.warning("No JPGs found in %s — skipping visualization", args.images)
        return 0

    random.seed(args.seed)
    sample = random.sample(images, min(args.sample_size, len(images)))
    logger.info(
        "Rendering %d/%d images from %s → %s",
        len(sample), len(images), args.images, args.output,
    )

    written = failed = 0
    for image_path in sample:
        det_path = args.detections / f"{image_path.stem}.txt"
        out_path = args.output / image_path.name
        if draw_boxes(image_path, det_path, out_path, args.max_width):
            written += 1
        else:
            failed += 1

    logger.info("Visualization complete — written=%d  failed=%d", written, failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())