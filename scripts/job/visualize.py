#!/usr/bin/env python3
"""
scripts/job/visualize.py
=====================

Generate a downscaled random sample of stage outputs for QC review.

Works for any stage — behaviour is controlled by --mode:

  raw_to_jpg   Downscale a random sample of JPGs.
               Output: sample JPGs at --scale (default 0.15).

  jpg_to_det   Draw YOLO detection boxes on a random sample of JPGs.
               Requires --detections directory of .txt files.
               Output: overlay JPGs downscaled to --max-width.

Output always goes to --output and is promoted to:
  <final_dest_root>/<batch_id>/<stage>/sample/

Usage
-----
# raw_to_jpg sample:
python scripts/job/visualize.py \\
    --mode raw_to_jpg \\
    --images /path/to/batch/images \\
    --output /path/to/sample \\
    --sample-size 24 \\
    --scale 0.15

# jpg_to_det overlay sample:
python scripts/job/visualize.py \\
    --mode jpg_to_det \\
    --images /path/to/batch/images \\
    --detections /path/to/run/artifacts \\
    --output /path/to/sample \\
    --sample-size 24 \\
    --max-width 1800

Exit codes:
  0  Success
  1  Hard failure (bad args, unreadable directories)
"""

from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path
import shutil

import cv2

logger = logging.getLogger(__name__)

# Detection overlay style
_BOX_COLOR      = (0, 255, 0)
_BOX_THICKNESS  = 6
_FONT           = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE     = 1.2
_FONT_THICKNESS = 3
_FONT_COLOR     = (0, 255, 0)
_LABEL_Y_MIN    = 40


# ---------------------------------------------------------------------------
# Per-mode render functions
# ---------------------------------------------------------------------------

def _render_raw_to_jpg(image_path: Path, out_path: Path, scale: float) -> bool:
    """Downscale a single JPG by scale factor and write to out_path."""
    im = cv2.imread(str(image_path))
    if im is None:
        logger.warning("Could not read: %s", image_path)
        return False
    h, w = im.shape[:2]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if scale != 1.0:
        im = cv2.resize(im, (max(1, int(w * scale)), max(1, int(h * scale))))
        ok = cv2.imwrite(str(out_path), im)
    else:
        # If scale is 1.0, just copy the file instead of re-encoding
        shutil.copy(image_path, out_path)
        ok = True

    if not ok:
        logger.warning("cv2.imwrite failed for %s", out_path)
    return ok


def _render_jpg_to_det(
    image_path: Path,
    det_path: Path,
    out_path: Path,
    max_width: int,
) -> bool:
    """Draw YOLO detection boxes on one JPG, downscale to max_width, write overlay."""
    im = cv2.imread(str(image_path))
    if im is None:
        logger.warning("Could not read: %s", image_path)
        return False

    h, w = im.shape[:2]

    if det_path.exists():
        for line in det_path.read_text().strip().splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                cls_id, xc, yc, bw, bh, conf = parts[:6]
                xc, yc, bw, bh, conf = map(float, (xc, yc, bw, bh, conf))
            except ValueError:
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
                _FONT, _FONT_SCALE, _FONT_COLOR, _FONT_THICKNESS, cv2.LINE_AA,
            )

    if im.shape[1] > max_width:
        scale = max_width / im.shape[1]
        im = cv2.resize(im, (int(im.shape[1] * scale), int(im.shape[0] * scale)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(out_path), im)
    if not ok:
        logger.warning("cv2.imwrite failed for %s", out_path)
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a downscaled random sample of stage outputs for QC."
    )
    parser.add_argument(
        "--mode", required=True, choices=["raw_to_jpg", "jpg_to_det"],
        help="Stage to visualize.",
    )
    parser.add_argument(
        "--images", required=True, type=Path,
        help="Directory of source JPG images.",
    )
    parser.add_argument(
        "--detections", type=Path, default=None,
        help="Directory of .txt detection files (jpg_to_det mode only).",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Directory to write sample images.",
    )
    parser.add_argument(
        "--sample-size", type=int, default=24,
        help="Number of images to sample (default: 24).",
    )
    parser.add_argument(
        "--scale", type=float, default=0.15,
        help="Downscale factor for raw_to_jpg mode (default: 0.15).",
    )
    parser.add_argument(
        "--max-width", type=int, default=1800,
        help="Max output width in pixels for jpg_to_det mode (default: 1800).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducible sampling (default: 42).",
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

    if args.mode == "jpg_to_det" and (args.detections is None or not args.detections.is_dir()):
        logger.error("--detections must be a valid directory for jpg_to_det mode")
        return 1

    images = sorted(
        list(args.images.glob("*.jpg")) +
        list(args.images.glob("*.JPG")) +
        list(args.images.glob("*.jpeg"))
    )

    if not images:
        logger.warning("No JPGs found in %s — skipping visualization", args.images)
        return 0

    random.seed(args.seed)
    sample = random.sample(images, min(args.sample_size, len(images)))
    logger.info(
        "Rendering %d/%d images → %s", len(sample), len(images), args.output
    )

    written = failed = 0
    # generate random list of indices of full-sized images to include in the sample (max 5)
    fullsized_rdm_idx = random.sample(range(len(sample)), min(5, len(sample)))

    for idx, image_path in enumerate(sample):
        out_path = args.output / image_path.name

        if args.mode == "raw_to_jpg":
            if idx in fullsized_rdm_idx:
                out_path_full_size = args.output / f"{image_path.stem}_fullsize.jpg"
                ok = _render_raw_to_jpg(image_path, out_path_full_size, 1.0)  # full size        
            ok = _render_raw_to_jpg(image_path, out_path, args.scale)

        else:
            det_path = args.detections / f"{image_path.stem}.txt"
            ok = _render_jpg_to_det(image_path, det_path, out_path, args.max_width)

        if ok:
            written += 1
        else:
            failed += 1

    logger.info("Done — written=%d  failed=%d", written, failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())