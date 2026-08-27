#!/usr/bin/env python3
from pathlib import Path
import argparse
import json
import random

import cv2
import numpy as np

FALLBACK_COLOR_BGR = (0, 0, 255)  # red, for class IDs absent from the catalog


def load_species_colors(
    species_catalog_path: Path,
) -> tuple[dict[int, tuple[int, int, int]], dict[int, str]]:
    """Return per-class BGR colors and display names keyed by class_id."""
    catalog = json.loads(species_catalog_path.read_text(encoding="utf-8"))
    if "species" in catalog.keys():
        species = catalog.get("species", {})
    else:
        species = catalog

    colors: dict[int, tuple[int, int, int]] = {}
    names: dict[int, str] = {}
    for entry in species.values():
        class_id = entry.get("class_id")
        if "rgb" in entry:
            rgb = entry.get("rgb")
            
        elif "r" in entry and "b" in entry and "g" in entry:
            r = entry.get("r")
            g = entry.get("g")
            b = entry.get("b")
            rgb = [r,g,b]
        if class_id is None or rgb is None:
            continue
        r, g, b = rgb
        colors[int(class_id)] = (int(b), int(g), int(r))
        names[int(class_id)] = entry.get("common_name") or entry.get("USDA_symbol") or str(class_id)

    return colors, names


def build_color_lut(colors: dict[int, tuple[int, int, int]]) -> np.ndarray:
    """Build a 256-entry BGR lookup table for vectorized mask colorization."""
    lut = np.full((256, 3), FALLBACK_COLOR_BGR, dtype=np.uint8)
    for class_id, bgr in colors.items():
        if 0 <= class_id <= 255:
            lut[class_id] = bgr
    lut[0] = (0, 0, 0)  # background stays untinted
    return lut


def write_legend(
    path: Path,
    class_ids: list[int],
    colors: dict[int, tuple[int, int, int]],
    names: dict[int, str],
    row_height: int = 36,
    width: int = 420,
) -> None:
    """Render a swatch + label per class ID actually seen in the sampled masks."""
    img = np.full((row_height * len(class_ids) + 10, width, 3), 255, dtype=np.uint8)
    for i, class_id in enumerate(class_ids):
        y0 = 10 + i * row_height
        swatch_size = row_height - 10
        color = colors.get(class_id, FALLBACK_COLOR_BGR)
        cv2.rectangle(img, (10, y0), (10 + swatch_size, y0 + swatch_size), color, -1)
        cv2.putText(
            img,
            f"{class_id}: {names.get(class_id, 'unknown')}",
            (20 + swatch_size, y0 + swatch_size - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def draw_overlay(
    image_path: Path,
    mask_path: Path,
    det_path: Path | None,
    out_path: Path,
    max_width: int,
    alpha: float,
    color_lut: np.ndarray | None = None,
) -> tuple[bool, set[int]]:
    im = cv2.imread(str(image_path))
    if im is None:
        return False, set()

    h, w = im.shape[:2]

    present_classes: set[int] = set()
    if mask_path.exists():
        mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if mask is not None:
            if mask.shape[:2] != (h, w):
                mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            present_classes = {int(c) for c in np.unique(mask) if c != 0}
            if color_lut is not None:
                color_layer = color_lut[mask]
            else:
                color_layer = np.zeros_like(im)
                color_layer[mask > 0] = FALLBACK_COLOR_BGR
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
    return cv2.imwrite(str(out_path), im), present_classes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Overlay segmentation masks (and optionally detections) on JPGs."
    )
    parser.add_argument("--images", required=True, type=Path, help="Directory of source JPGs.")
    parser.add_argument(
        "--masks",
        required=True,
        type=Path,
        help="Directory of class-mask PNGs from det_to_seg.",
    )
    parser.add_argument(
        "--detections",
        type=Path,
        default=None,
        help="Directory of .txt detection outputs (optional).",
    )
    parser.add_argument("--output", required=True, type=Path, help="Directory for overlay JPGs.")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=12,
        help="Number of random images to visualize.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for repeatable sampling.")
    parser.add_argument(
        "--max-width",
        type=int,
        default=1800,
        help="Downscale overlays to this max width.",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.4,
        help="Mask overlay opacity (0=transparent, 1=opaque).",
    )
    parser.add_argument(
        "--species-catalog",
        type=Path,
        default=None,
        help=(
            "Species catalog JSON with per-class 'rgb' fields (e.g. species_info.json or "
            "species_catalog.generated.json). When given, masks are colorized per class "
            "and a legend.png is written; otherwise all classes render as solid red."
        ),
    )
    args = parser.parse_args()

    images = sorted(list(args.images.glob("*.jpg")) + list(args.images.glob("*.JPG")))
    if not images:
        raise SystemExit(f"No JPGs found in {args.images}")

    random.seed(args.seed)
    sample = random.sample(images, min(args.sample_size, len(images)))

    colors: dict[int, tuple[int, int, int]] = {}
    names: dict[int, str] = {}
    color_lut = None
    if args.species_catalog is not None:
        colors, names = load_species_colors(args.species_catalog)
        color_lut = build_color_lut(colors)

    written = 0
    all_present_classes: set[int] = set()
    for image_path in sample:
        mask_path = args.masks / f"{image_path.stem}.png"
        det_path = args.detections / f"{image_path.stem}.txt" if args.detections else None
        out_path = args.output / image_path.name
        ok, present_classes = draw_overlay(
            image_path, mask_path, det_path, out_path, args.max_width, args.alpha, color_lut
        )
        if ok:
            written += 1
            all_present_classes |= present_classes

    print(f"Wrote {written} overlays to {args.output}")

    if color_lut is not None and all_present_classes:
        legend_path = args.output / "legend.png"
        write_legend(legend_path, sorted(all_present_classes), colors, names)
        print(f"Wrote legend for {len(all_present_classes)} class(es) to {legend_path}")


if __name__ == "__main__":
    main()
