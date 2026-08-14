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

  det_to_world Draw georeferenced detection boxes (pixel-space corners,
               labeled with their computed world centroid) on every JPG
               found under --images. Requires --detections pointing at the
               *_georeferenced.csv file (not a directory) — det_to_world
               writes one CSV for the whole batch, unlike jpg_to_det's
               per-image .txt files. --sample-size is ignored: --images is
               expected to already be a pre-sampled subset (staged per
               transfer.routes.det_to_world.image_sample_size), so this mode
               renders all of it rather than sampling a second time.
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

# det_to_world overlay sample (renders every image under --images; no
# --sample-size — see note above):
python scripts/job/visualize.py \\
    --mode det_to_world \\
    --images /path/to/batch/images \\
    --detections /path/to/run/artifacts/BATCH_ID_georeferenced.csv \\
    --output /path/to/sample \\
    --max-width 1800

Exit codes:
  0  Success
  1  Hard failure (bad args, unreadable directories)
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
from pathlib import Path
import shutil
from tqdm import tqdm

import cv2
import geopandas as gpd
from shapely.geometry import Polygon

logger = logging.getLogger(__name__)

# Detection overlay style
_BOX_COLOR      = (0, 255, 0)
_BOX_THICKNESS  = 6
_FONT           = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE     = 1.2
_FONT_THICKNESS = 3
_FONT_COLOR     = (0, 255, 0)
_LABEL_Y_MIN    = 40

# det_to_world's labels are drawn *after* downscaling (see
# _render_det_to_world), unlike jpg_to_det's above — and a batch's boxes are
# often packed close together (many small plants per pot), so its labels use
# their own smaller/tighter sizing to stay legible without overlapping each
# other as much.
_DET_TO_WORLD_FONT_SCALE     = 0.7
_DET_TO_WORLD_FONT_THICKNESS = 2
_DET_TO_WORLD_LABEL_Y_MIN    = 20

# world_tl_x/y etc. are only meaningful as meters for a projected CRS (UTM
# EPSG codes, or this pipeline's 'LOCAL' project frame) — a geographic CRS
# like EPSG:4326 stores coordinates in degrees, where a shoelace area in raw
# units is not cm^2 without a geodetic reprojection. Skip the area label
# rather than show a wildly wrong number.
_DEGREE_CRS_CODES = {"4326"}


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


def load_georeferenced_rows_by_image(csv_path: Path) -> dict[str, list[dict[str, str]]]:
    """Group det_to_world's single-CSV output by image_id, so each sampled
    image only draws the rows that belong to it."""
    rows_by_image: dict[str, list[dict[str, str]]] = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_by_image.setdefault(row.get("image_id", ""), []).append(row)
    return rows_by_image


def _polygon_area(points: list[tuple[float, float]]) -> float:
    """Shoelace-formula area of a simple polygon given corners in order."""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _bbox_area_cm2(row: dict[str, str]) -> float | None:
    """
    Bbox area in cm^2 from its four remapped world corners (shoelace, not
    just width*height, since a bbox's world quad can be a rotated/skewed
    trapezoid rather than an axis-aligned rectangle).

    Returns None when the corners are missing/unparseable, or when the row's
    CRS is geographic (degrees) rather than a projected meter-based CRS —
    see _DEGREE_CRS_CODES.
    """
    crs = str(row.get("crs") or "").strip()
    if crs in _DEGREE_CRS_CODES:
        return None
    try:
        corners = [
            (float(row["world_tl_x"]), float(row["world_tl_y"])),
            (float(row["world_tr_x"]), float(row["world_tr_y"])),
            (float(row["world_br_x"]), float(row["world_br_y"])),
            (float(row["world_bl_x"]), float(row["world_bl_y"])),
        ]
    except (KeyError, TypeError, ValueError):
        return None
    area_m2 = _polygon_area(corners)
    return area_m2 * 10_000.0  # m^2 -> cm^2


# CSV column -> shapefile DBF field name. Shapefile DBF fields are capped at
# 10 characters, so these deliberately reuse the *zone* shapefile's own
# short names (species/comm_name/cultc_id/disp_name/class_id — see
# stages.det_to_world.species._OPTIONAL_ZONE_ATTRS) rather than
# det_to_world's more readable CSV headers (species_id, cultivar_name, ...)
# — both to fit the limit and so this output overlays cleanly in GIS
# software against the zone shapefile each box was assigned from.
_BBOX_SHP_FIELDS = {
    "image_id": "image_id",
    "bounding_box_id": "bbox_id",
    "species_id": "species",
    "species_name": "comm_name",
    "cultivar_id": "cultc_id",
    "cultivar_name": "disp_name",
    "class_id": "class_id",
}


def write_bbox_shapefile(rows: list[dict[str, str]], batch_id: str, output_dir: Path) -> Path | None:
    """
    Write one shapefile covering every detection in a det_to_world batch, as
    real-world box polygons (four remapped corners, not just a centroid
    point) with whatever species/cultivar/class attributes the batch's zone
    shapefile provided — see _BBOX_SHP_FIELDS for the CSV -> DBF field
    mapping. Meant to be opened in GIS software alongside the zone shapefile
    the batch was assigned against.

    Rows with no usable world corners (monoculture batches, which skip
    remapping entirely) are silently excluded — there's no real-world
    geometry for them. Returns the written .shp path, or None if no row had
    usable geometry at all.
    """
    if not rows:
        logger.warning("No rows to write for bbox shapefile (batch %s)", batch_id)
        return None

    available_columns = rows[0].keys()
    selected_fields = [
        (csv_col, dbf_field) for csv_col, dbf_field in _BBOX_SHP_FIELDS.items()
        if csv_col in available_columns
    ]

    geometries = []
    attributes: list[dict[str, str]] = []
    crs = None
    for row in rows:
        try:
            # Same corner order as _bbox_area_cm2's shoelace calc: tl, tr,
            # br, bl — traces the box's perimeter without crossing itself.
            corners = [
                (float(row["world_tl_x"]), float(row["world_tl_y"])),
                (float(row["world_tr_x"]), float(row["world_tr_y"])),
                (float(row["world_br_x"]), float(row["world_br_y"])),
                (float(row["world_bl_x"]), float(row["world_bl_y"])),
            ]
        except (KeyError, TypeError, ValueError):
            continue

        geometries.append(Polygon(corners))
        attributes.append({dbf_field: row.get(csv_col, "") for csv_col, dbf_field in selected_fields})
        if crs is None:
            crs = row.get("crs") or None

    if not geometries:
        logger.warning(
            "No rows had usable world corners — skipping bbox shapefile for %s", batch_id
        )
        return None

    gdf = gpd.GeoDataFrame(attributes, geometry=geometries, crs=crs)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{batch_id}_bboxes.shp"
    gdf.to_file(out_path)
    logger.info("Wrote %d box(es) to %s", len(gdf), out_path)
    return out_path


def _detection_name_label(row: dict[str, str]) -> str | None:
    """
    Human-readable species/cultivar name for this detection, if the batch's
    shapefile had one — cultivar_name (cultivar seasons, e.g. "Peanut - NC
    20") and species_name (comm_name, e.g. "Ragweed") are mutually exclusive
    per shapefile (see species.assign_spatial), so at most one is ever
    present. Monoculture batches and shapefiles with neither attribute have
    neither column — row.get() returns None rather than raising.
    """
    return row.get("cultivar_name") or row.get("species_name") or None


def _render_det_to_world(
    image_path: Path,
    rows: list[dict[str, str]],
    out_path: Path,
    max_width: int,
) -> bool:
    """
    Draw det_to_world's georeferenced boxes on one JPG.

    xmin/ymin/xmax/ymax are the same normalized pixel-space corners
    jpg_to_det produced (world_* columns are derived from them, not a
    separate pixel location), so overlaying them directly confirms boxes
    still line up with the image. Each box is labeled at its top-left corner
    with its world-space area (cm^2, via the four remapped corners) and,
    when the shapefile provided one, its species/cultivar name (see
    _detection_name_label) — no label at all if neither is available
    (missing corners, a geographic CRS, or no name attribute in the
    shapefile), so a georeferencing failure is visible at a glance.

    Boxes/labels are drawn *after* downscaling to max_width, not before —
    source images here are huge (e.g. 13368x9520), so text/box strokes sized
    for that resolution shrink to illegible slivers once the image is
    downscaled for the sample. xmin/ymin/xmax/ymax are normalized [0, 1], so
    they map directly onto the resized image's pixel dimensions either way.
    """
    im = cv2.imread(str(image_path))
    if im is None:
        logger.warning("Could not read: %s", image_path)
        return False

    orig_w = im.shape[1]
    if orig_w > max_width:
        scale = max_width / orig_w
        im = cv2.resize(im, (int(orig_w * scale), int(im.shape[0] * scale)))

    h, w = im.shape[:2]

    for row in rows:
        try:
            xmin, ymin = float(row["xmin"]), float(row["ymin"])
            xmax, ymax = float(row["xmax"]), float(row["ymax"])
        except (KeyError, ValueError):
            continue
        x1, y1 = int(xmin * w), int(ymin * h)
        x2, y2 = int(xmax * w), int(ymax * h)
        cv2.rectangle(im, (x1, y1), (x2, y2), _BOX_COLOR, _BOX_THICKNESS)

        area_cm2 = _bbox_area_cm2(row)
        name_label = _detection_name_label(row)
        label_parts = [name_label] if name_label else []
        if area_cm2 is not None:
            label_parts.append(f"{area_cm2:.1f} cm^2")
        if label_parts:
            y_area = max(_DET_TO_WORLD_LABEL_Y_MIN, y1 - 10)
            cv2.putText(
                im, " | ".join(label_parts), (x1, y_area),
                _FONT, _DET_TO_WORLD_FONT_SCALE, _FONT_COLOR, _DET_TO_WORLD_FONT_THICKNESS, cv2.LINE_AA,
            )

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
        "--mode", required=True, choices=["raw_to_jpg", "jpg_to_det", "det_to_world"],
        help="Stage to visualize.",
    )
    parser.add_argument(
        "--images", required=True, type=Path,
        help="Directory of source JPG images.",
    )
    parser.add_argument(
        "--detections", type=Path, default=None,
        help=(
            "Directory of .txt detection files (jpg_to_det mode), or the "
            "single *_georeferenced.csv file (det_to_world mode)."
        ),
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
        "--bbox-shp-output", type=Path, default=None,
        help=(
            "det_to_world mode only: also write a shapefile of every box in "
            "the batch (not just the sampled images) to this directory, "
            "with species/cultivar/class attributes. Omit to skip."
        ),
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

    if args.mode == "det_to_world" and (args.detections is None or not args.detections.is_file()):
        logger.error("--detections must be a valid *_georeferenced.csv file for det_to_world mode")
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
    if args.mode == "det_to_world":
        # --images already points at the pre-sampled subset staged for
        # visualization (transfer.routes.det_to_world.image_sample_size —
        # see orchestrator/input_staging_planner.py and slurm_job.sh.j2's
        # Step 2 copy cap), so re-sampling down to --sample-size here would
        # just be a second, independent cap that can silently drift out of
        # sync with the staging one. Render everything that's already there.
        sample = images
    else:
        sample = random.sample(images, min(args.sample_size, len(images)))
    logger.info(
        "Rendering %d/%d images → %s", len(sample), len(images), args.output
    )

    rows_by_image: dict[str, list[dict[str, str]]] = {}
    if args.mode == "det_to_world":
        rows_by_image = load_georeferenced_rows_by_image(args.detections)

    written = failed = 0
    # generate random list of indices of full-sized images to include in the sample (max 5)
    fullsized_rdm_idx = random.sample(range(len(sample)), min(5, len(sample)))

    for idx, image_path in enumerate(tqdm(sample)):
        out_path = args.output / image_path.name

        if args.mode == "raw_to_jpg":
            if idx in fullsized_rdm_idx:
                out_path_full_size = args.output / f"{image_path.stem}_fullsize.jpg"
                ok = _render_raw_to_jpg(image_path, out_path_full_size, 1.0)  # full size
            ok = _render_raw_to_jpg(image_path, out_path, args.scale)

        elif args.mode == "det_to_world":
            image_rows = rows_by_image.get(image_path.stem, [])
            ok = _render_det_to_world(image_path, image_rows, out_path, args.max_width)

        else:
            det_path = args.detections / f"{image_path.stem}.txt"
            ok = _render_jpg_to_det(image_path, det_path, out_path, args.max_width)

        if ok:
            written += 1
        else:
            failed += 1

    logger.info("Done — written=%d  failed=%d", written, failed)

    if args.mode == "det_to_world" and args.bbox_shp_output is not None:
        # Every box in the batch, not just the sampled images' — the
        # georeferenced CSV already has all of them, independent of which
        # images happened to get staged for the JPG overlay sample above.
        all_rows = [row for image_rows in rows_by_image.values() for row in image_rows]
        # "NC_2026-07-17_georeferenced.csv" -> "NC_2026-07-17"
        batch_id = args.detections.stem.removesuffix("_georeferenced")
        write_bbox_shapefile(all_rows, batch_id, args.bbox_shp_output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())