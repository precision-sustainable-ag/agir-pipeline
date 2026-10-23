#!/usr/bin/env python3
"""
Build ASFM-shaped pixel-to-world grids for legacy batches from their bbot
metadata JSON, so det_to_world can georeference new jpg_to_det detections.

Legacy batches (bbot 3.x, before the current ASFM wrote pixel_world_grids)
carry their old autosfm georeferencing in
semifield-developed-images/<batch_id>/metadata/<image_id>.json:
camera_info holds the same camera pose/calibration and whole-frame FOV
corners as the old reference/camera_reference.csv and fov.csv, plus each
legacy annotation's Metashape global_coordinates. det_to_world only reads
current ASFM output -- per-image NPZ grids plus paired camera_reference.csv/
fov.csv under semifield-asfm/<batch_id>/ -- so this writes that shape:

    <asfm_root>/<batch_id>/legacy_reference/
        provenance.json
        autosfm/reference/camera_reference.csv
        autosfm/reference/fov.csv
        autosfm/reference/pixel_world_grids/<image_id>.npz

Each image's grid is the projective (homography) map from its four pixel
corners to its four FOV corners, sampled every --grid-step pixels the way
ASFM samples its grids. The FOV corners are exact Metashape projections;
interior points ignore lens distortion and ground relief. Before anything is
written, every legacy annotation is mapped through the new grids with
det_to_world's own GridCache/map_bbox and compared with its Metashape
global_coordinates; a batch whose error exceeds --max-median-cm/--max-p95-cm
is not written.

Coordinates are written in the CRS season_date_ranges gives the batch
(legacy JSON is EPSG:4326 lon/lat; UTM seasons are reprojected). Images
without an aligned camera or complete FOV get no grid or reference row, so
det_to_world keeps their detections un-georeferenced (W_GRID_NOT_FOUND).

After writing, the grids still have to be indexed (--refresh-inventory, or
the nightly scan) before det_to_world's rerun readiness sees them.

Usage
-----
    python scripts/admin/legacy_reference_to_grids.py \\
        --batch-file configs/batch_list.example.txt --dry-run
    python scripts/admin/legacy_reference_to_grids.py \\
        --batch-id NC_2025-02-05 --refresh-inventory
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pyproj import CRS, Geod, Transformer

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.sqlite_db import open_db, resolve_season_for_batch
from stages.det_to_world.primary_inputs import combine_reference_pairs
from stages.det_to_world.remapper import GridCache, map_bbox

logger = logging.getLogger(__name__)

DEFAULT_DB = "/project/dash_agir/globus_index/globus_file_index.sqlite3"
DEFAULT_SOURCE_ROOT = Path("/90daydata/dash_agir/semifield-developed-images")
DEFAULT_ASFM_ROOT = Path("/90daydata/dash_agir/semifield-asfm")
DEFAULT_ENDPOINT_CONFIG = REPO_ROOT / "configs" / "globus_endpoint_config.example.yaml"
SUB_BATCH = "legacy_reference"
PROVENANCE = "provenance.json"

CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
GEOD = Geod(ellps="WGS84")
WGS84 = CRS.from_epsg(4326)

# camera_info key -> camera_reference.csv column (the old autosfm/ASFM names).
CAMERA_COEFFICIENTS = ("f", "cx", "cy", "k1", "k2", "k3", "k4", "p1", "p2", "b1", "b2")
CAMERA_FIELDS = [
    "label", "Estimated_X", "Estimated_Y", "Estimated_Z",
    "Estimated_Yaw", "Estimated_Pitch", "Estimated_Roll",
    *CAMERA_COEFFICIENTS, "pixel_width", "pixel_height", "Alignment",
]
FOV_FIELDS = [
    "label",
    *[f"{corner}_{axis}" for corner in CORNERS for axis in ("x", "y")],
    "width", "height",
]


class BatchError(Exception):
    """A batch can't be converted; reported, and the next batch continues."""


@dataclass
class LegacyImage:
    image_id: str
    width: int
    height: int
    camera: Dict[str, Any]
    fov: Dict[str, Tuple[float, float]]  # corner -> (lon, lat)
    annotations: List[Dict[str, Any]]


@dataclass
class BatchResult:
    batch_id: str
    status: str
    crs: str = ""
    images_written: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    detection_images: int = 0
    detection_images_with_grid: int = 0
    validation: Dict[str, Any] = field(default_factory=dict)
    message: str = ""


def load_batch_file(path: Path) -> List[str]:
    """Read batch_ids one per line; blank lines and '#' comments are ignored."""
    lines = path.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def resolve_batch_crs(conn, batch_id: str) -> Tuple[CRS, Dict[str, Any]]:
    """Return the CRS season_date_ranges assigns this batch's site and date."""
    site, _, batch_date = batch_id.partition("_")
    season = resolve_season_for_batch(conn, site=site, batch_date=batch_date)
    if season is None:
        raise BatchError(f"no season_date_ranges window covers {batch_id}")
    raw = str(season["crs"]).strip()
    if not raw.isdigit():
        raise BatchError(f"season {season['season_name']!r} has crs {raw!r}; need an EPSG code")
    return CRS.from_epsg(int(raw)), season


def _corner(fov: Dict[str, Any], corner: str) -> Optional[Tuple[float, float]]:
    value = fov.get(f"{corner}_xy")
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        lon, lat = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and abs(lon) <= 180 and abs(lat) <= 90):
        return None
    return lon, lat


def read_legacy_images(metadata_dir: Path, skipped: Dict[str, int]) -> List[LegacyImage]:
    """Load every image with an aligned camera and complete lon/lat FOV corners."""
    images: List[LegacyImage] = []
    for path in sorted(metadata_dir.glob("*.json")):
        data = json.loads(path.read_text())
        camera = data.get("camera_info") or {}
        fov = {corner: _corner(camera.get("fov") or {}, corner) for corner in CORNERS}
        if not camera.get("aligned") or any(value is None for value in fov.values()):
            skipped["no_aligned_fov"] = skipped.get("no_aligned_fov", 0) + 1
            continue
        try:
            width, height = int(data["fullres_width"]), int(data["fullres_height"])
        except (KeyError, TypeError, ValueError):
            skipped["no_image_size"] = skipped.get("no_image_size", 0) + 1
            continue
        images.append(
            LegacyImage(
                image_id=path.stem,
                width=width,
                height=height,
                camera=camera,
                fov=fov,
                annotations=data.get("annotations") or [],
            )
        )
    return images


def fit_homography(pixels: np.ndarray, world: np.ndarray) -> np.ndarray:
    """Exact 3x3 projective map taking four (x, y) points to four (X, Y) points."""
    a, b = [], []
    for (x, y), (u, v) in zip(pixels, world):
        a.append([x, y, 1, 0, 0, 0, -x * u, -y * u])
        a.append([0, 0, 0, x, y, 1, -x * v, -y * v])
        b.extend([u, v])
    h = np.linalg.solve(np.asarray(a, dtype=float), np.asarray(b, dtype=float))
    return np.append(h, 1.0).reshape(3, 3)


def sample_pixels(size: int, step: int) -> np.ndarray:
    """ASFM's sample positions: every ``step`` pixels plus the last pixel."""
    return np.unique(np.append(np.arange(0, size, step), size - 1)).astype(np.float32)


def build_grid(
    width: int, height: int, corners_xy: Dict[str, Tuple[float, float]], step: int
) -> Dict[str, np.ndarray]:
    """Sample the pixel->world homography for one image on ASFM's grid layout.

    Pixels are normalized by image size and world coordinates are offsets
    from the frame center scaled to unit size, so the 8x8 solve stays well
    conditioned for both degree and metre coordinates.
    """
    world = np.array([corners_xy[corner] for corner in CORNERS], dtype=float)
    origin = world.mean(axis=0)
    scale = float(np.abs(world - origin).max())
    if not scale > 0:
        raise ValueError("FOV corners coincide")
    unit = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=float)
    h = fit_homography(unit, (world - origin) / scale)

    u_pixels = sample_pixels(width, step)
    v_pixels = sample_pixels(height, step)
    uu, vv = np.meshgrid(u_pixels.astype(float) / width, v_pixels.astype(float) / height)
    num_x = h[0, 0] * uu + h[0, 1] * vv + h[0, 2]
    num_y = h[1, 0] * uu + h[1, 1] * vv + h[1, 2]
    den = h[2, 0] * uu + h[2, 1] * vv + h[2, 2]
    if not (np.all(np.isfinite(den)) and (np.all(den > 0) or np.all(den < 0))):
        raise ValueError("FOV corners don't form a valid quadrilateral")
    return {
        "u_pixels": u_pixels,
        "v_pixels": v_pixels,
        "world_x": origin[0] + scale * num_x / den,
        "world_y": origin[1] + scale * num_y / den,
        "sensor_width": np.array([width], dtype=np.int32),
        "sensor_height": np.array([height], dtype=np.int32),
    }


def _edge(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def fov_row(image_id: str, corners_xy: Dict[str, Tuple[float, float]]) -> Dict[str, Any]:
    """fov.csv row; width/height are mean opposite-edge lengths in CRS units, as autosfm wrote."""
    c = corners_xy
    row: Dict[str, Any] = {"label": image_id}
    for corner in CORNERS:
        row[f"{corner}_x"], row[f"{corner}_y"] = c[corner]
    row["width"] = (_edge(c["top_left"], c["top_right"]) + _edge(c["bottom_left"], c["bottom_right"])) / 2
    row["height"] = (_edge(c["top_left"], c["bottom_left"]) + _edge(c["top_right"], c["bottom_right"])) / 2
    return row


def camera_row(image: LegacyImage, to_crs: Transformer) -> Dict[str, Any]:
    camera = image.camera
    lon, lat, z = (list(camera.get("estimated_xyz") or []) + [None, None, None])[:3]
    if lon is None or lat is None:
        raise ValueError("camera_info.estimated_xyz is missing")
    x, y = to_crs.transform(float(lon), float(lat))
    coefficients = camera.get("camera_coefficients") or {}
    row: Dict[str, Any] = {
        "label": image.image_id,
        "Estimated_X": x,
        "Estimated_Y": y,
        "Estimated_Z": z,
        "Estimated_Yaw": camera.get("estimated_yaw"),
        "Estimated_Pitch": camera.get("estimated_pitch"),
        "Estimated_Roll": camera.get("estimated_roll"),
        "pixel_width": camera.get("pixel_width"),
        "pixel_height": camera.get("pixel_height"),
        "Alignment": True,
    }
    for key in CAMERA_COEFFICIENTS:
        row[key] = coefficients.get(key)
    return row


def write_reference(
    reference_dir: Path,
    images: List[LegacyImage],
    target_crs: CRS,
    step: int,
    skipped: Dict[str, int],
) -> List[LegacyImage]:
    """Write NPZ grids and paired camera/FOV CSVs; return the images written."""
    to_crs = Transformer.from_crs(WGS84, target_crs, always_xy=True)
    crs_label = f"EPSG:{target_crs.to_epsg()}"
    grid_dir = reference_dir / "pixel_world_grids"
    grid_dir.mkdir(parents=True)
    written: List[LegacyImage] = []
    camera_rows: List[Dict[str, Any]] = []
    fov_rows: List[Dict[str, Any]] = []
    for image in images:
        corners_xy = {corner: to_crs.transform(*image.fov[corner]) for corner in CORNERS}
        try:
            grid = build_grid(image.width, image.height, corners_xy, step)
            camera = camera_row(image, to_crs)
        except (ValueError, np.linalg.LinAlgError) as exc:
            logger.warning("Skipping %s: %s", image.image_id, exc)
            skipped["invalid_reference"] = skipped.get("invalid_reference", 0) + 1
            continue
        np.savez(grid_dir / f"{image.image_id}.npz", crs=np.array([crs_label]), **grid)
        camera_rows.append(camera)
        fov_rows.append(fov_row(image.image_id, corners_xy))
        written.append(image)
    for name, fields, rows in (
        ("camera_reference.csv", CAMERA_FIELDS, camera_rows),
        ("fov.csv", FOV_FIELDS, fov_rows),
    ):
        with (reference_dir / name).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return written


def validate(
    sub_batch_dir: Path,
    images: List[LegacyImage],
    target_crs: CRS,
    outlier_cm: float,
) -> Dict[str, Any]:
    """Map legacy annotations through the written grids and compare to Metashape.

    Uses det_to_world's own GridCache/map_bbox and combine_reference_pairs, so
    this also proves det_to_world can read what was written. Errors above
    ``outlier_cm`` are counted separately: some legacy global_coordinates are
    corrupt (thousands of km off), and those say nothing about the grid.
    """
    combine_reference_pairs(sub_batch_dir)
    cache = GridCache(sub_batch_dir)
    to_wgs84 = Transformer.from_crs(target_crs, WGS84, always_xy=True)
    errors: List[float] = []
    outliers = unmapped = 0
    for image in images:
        grid = cache.get(image.image_id)
        for annotation in image.annotations:
            truth = annotation.get("global_coordinates") or {}
            local = annotation.get("local_coordinates") or {}
            if not local.get("is_normalized") or any(_corner(_xy(truth), c) is None for c in CORNERS):
                continue
            xs = [float(local[c][0]) for c in CORNERS]
            ys = [float(local[c][1]) for c in CORNERS]
            row = {
                "image_id": image.image_id,
                "bounding_box_id": annotation.get("cutout_id", ""),
                "xmin": min(xs), "ymin": min(ys), "xmax": max(xs), "ymax": max(ys),
            }
            mapped, warning = map_bbox(row, grid)
            if mapped is None or warning is not None:
                unmapped += 1
                continue
            for corner, key in zip(CORNERS, ("tl", "tr", "bl", "br")):
                lon, lat = to_wgs84.transform(mapped[f"world_{key}_x"], mapped[f"world_{key}_y"])
                _, _, metres = GEOD.inv(truth[corner][0], truth[corner][1], lon, lat)
                if metres * 100 > outlier_cm:
                    outliers += 1
                else:
                    errors.append(metres * 100)
    stats: Dict[str, Any] = {"corners": len(errors), "outlier_corners": outliers, "unmapped_boxes": unmapped}
    if errors:
        values = np.asarray(errors)
        stats.update(
            median_cm=round(float(np.median(values)), 3),
            p95_cm=round(float(np.percentile(values, 95)), 3),
            max_cm=round(float(values.max()), 3),
        )
    return stats


def _xy(global_coordinates: Dict[str, Any]) -> Dict[str, Any]:
    """Expose global_coordinates corners under _corner()'s ``<corner>_xy`` keys."""
    return {f"{corner}_xy": global_coordinates.get(corner) for corner in CORNERS}


def detection_image_ids(detections_dir: Path) -> set:
    return {path.stem for path in detections_dir.glob("*.txt")} if detections_dir.is_dir() else set()


def _foreign_asfm_output(batch_dir: Path) -> List[Path]:
    """Real ASFM grids/references in the batch dir, outside this script's sub-batch."""
    if not batch_dir.is_dir():
        return []
    found = []
    for pattern in ("*.npz", "camera_reference.csv", "fov.csv"):
        for path in batch_dir.rglob(pattern):
            relative = path.relative_to(batch_dir).parts
            if relative[0] != SUB_BATCH and not relative[0].startswith(f".{SUB_BATCH}"):
                found.append(path)
    return found


def convert_batch(conn, batch_id: str, args: argparse.Namespace) -> BatchResult:
    result = BatchResult(batch_id=batch_id, status="failed")
    try:
        target_crs, season = resolve_batch_crs(conn, batch_id)
        result.crs = f"EPSG:{target_crs.to_epsg()}"
        metadata_dir = args.source_root / batch_id / "metadata"
        if not metadata_dir.is_dir():
            raise BatchError(f"no metadata JSON directory at {metadata_dir}")
        images = read_legacy_images(metadata_dir, result.skipped)
        if not images:
            raise BatchError(f"no metadata JSON with an aligned camera and FOV under {metadata_dir}")

        batch_dir = args.asfm_root / batch_id
        final_dir = batch_dir / SUB_BATCH
        foreign = _foreign_asfm_output(batch_dir)
        if foreign:
            raise BatchError(
                f"{batch_dir} already has ASFM output outside {SUB_BATCH}/ "
                f"(e.g. {foreign[0]}); not mixing legacy grids into it"
            )
        if final_dir.exists():
            if not args.force:
                result.status = "exists"
                result.message = f"{final_dir} exists; pass --force to rebuild it"
                return result
            if not (final_dir / PROVENANCE).is_file():
                raise BatchError(f"{final_dir} has no {PROVENANCE}; not replacing a directory this script didn't write")

        if args.dry_run:
            work_parent = Path(tempfile.mkdtemp(prefix=f"{batch_id}_"))
            work_dir = work_parent / SUB_BATCH
        else:
            batch_dir.mkdir(parents=True, exist_ok=True)
            work_dir = batch_dir / f".{SUB_BATCH}.tmp-{os.getpid()}"
            work_parent = None
        try:
            written = write_reference(
                work_dir / "autosfm" / "reference", images, target_crs, args.grid_step, result.skipped
            )
            result.images_written = len(written)
            detections = detection_image_ids(args.source_root / batch_id / "detections")
            result.detection_images = len(detections)
            result.detection_images_with_grid = len(detections & {image.image_id for image in written})
            result.validation = validate(work_dir, written, target_crs, args.outlier_cm)

            failure = _validation_failure(result.validation, args)
            if failure:
                raise BatchError(f"validation failed: {failure}")
            if args.dry_run:
                result.status = "would_write"
                return result

            provenance = {
                "batch_id": batch_id,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "script": "scripts/admin/legacy_reference_to_grids.py",
                "source": str(metadata_dir),
                "method": "per-image homography from camera_info.fov corners",
                "crs": result.crs,
                "season_name": season["season_name"],
                "grid_step_px": args.grid_step,
                "images_written": result.images_written,
                "images_skipped": result.skipped,
                "validation": result.validation,
            }
            (work_dir / PROVENANCE).write_text(json.dumps(provenance, indent=2) + "\n")
            if final_dir.exists():
                retired = batch_dir / f".{SUB_BATCH}.old-{os.getpid()}"
                final_dir.rename(retired)
                try:
                    work_dir.rename(final_dir)
                except OSError:
                    retired.rename(final_dir)
                    raise
                shutil.rmtree(retired)
            else:
                work_dir.rename(final_dir)
            result.status = "written"
            result.message = str(final_dir)
            return result
        finally:
            shutil.rmtree(work_parent or work_dir, ignore_errors=True)
    except (BatchError, OSError, RuntimeError, ValueError) as exc:
        result.message = str(exc)
        return result


def _validation_failure(stats: Dict[str, Any], args: argparse.Namespace) -> str:
    checked = stats["corners"] + stats["outlier_corners"]
    if stats["corners"] == 0:
        return "no legacy annotation corners to check against"
    if stats["median_cm"] > args.max_median_cm:
        return f"median {stats['median_cm']} cm > {args.max_median_cm} cm"
    if stats["p95_cm"] > args.max_p95_cm:
        return f"p95 {stats['p95_cm']} cm > {args.max_p95_cm} cm"
    if stats["outlier_corners"] > args.max_outlier_fraction * checked:
        return f"{stats['outlier_corners']}/{checked} corners off by more than {args.outlier_cm} cm"
    return ""


def refresh_asfm_inventory(args: argparse.Namespace) -> None:
    """Re-index the Ceres semifield-asfm scope so readiness sees the new grids."""
    from scripts.admin.globus_index import read_endpoint_configs, refresh_inventory_scope

    configs = [
        cfg for cfg in read_endpoint_configs(str(args.endpoint_config), logger)
        if cfg.site == "CERES" and Path(cfg.storage_root) / cfg.data_state == args.asfm_root
    ]
    if len(configs) != 1:
        raise RuntimeError(f"expected one enabled CERES scope for {args.asfm_root} in {args.endpoint_config}, found {len(configs)}")
    run_id, status = refresh_inventory_scope(db_path=args.db, config=configs[0], logger=logger)
    if status != "success":
        raise RuntimeError(f"inventory run {run_id} finished with status {status}")
    logger.info("Inventory run %s indexed %s", run_id, args.asfm_root)


def print_results(results: List[BatchResult]) -> None:
    print(f"\n{'batch_id':<16}{'status':<13}{'crs':<12}{'grids':>6}{'det imgs':>10}{'median':>9}{'p95':>8}{'outl':>6}  note")
    for r in results:
        v = r.validation
        median = f"{v['median_cm']:.2f}" if "median_cm" in v else "-"
        p95 = f"{v['p95_cm']:.2f}" if "p95_cm" in v else "-"
        detections = f"{r.detection_images_with_grid}/{r.detection_images}" if r.detection_images else "-"
        note = r.message or (", ".join(f"{k}={n}" for k, n in r.skipped.items()) if r.skipped else "")
        print(
            f"{r.batch_id:<16}{r.status:<13}{r.crs:<12}{r.images_written:>6}{detections:>10}"
            f"{median:>9}{p95:>8}{v.get('outlier_corners', '-'):>6}  {note}"
        )
    print("(median/p95 in cm vs legacy Metashape box corners; outl = corners off by more than --outlier-cm)\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build ASFM-shaped pixel-to-world grids for legacy batches from bbot metadata JSON."
    )
    parser.add_argument("--batch-id", action="append", default=[], help="Batch to convert; repeatable.")
    parser.add_argument("--batch-file", type=Path, help="File of batch_ids, one per line ('#' comments ignored).")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB with season_date_ranges (and the index, for --refresh-inventory).")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT, help="Root holding <batch_id>/metadata/*.json.")
    parser.add_argument("--asfm-root", type=Path, default=DEFAULT_ASFM_ROOT, help="Root to write <batch_id>/legacy_reference/ under.")
    parser.add_argument("--grid-step", type=int, default=400, help="Grid sample spacing in full-res pixels (400 gives ASFM's 35x25 on 13368x9520).")
    parser.add_argument("--max-median-cm", type=float, default=5.0)
    parser.add_argument("--max-p95-cm", type=float, default=25.0)
    parser.add_argument("--outlier-cm", type=float, default=100.0, help="Corner errors above this are counted as bad legacy coordinates.")
    parser.add_argument("--max-outlier-fraction", type=float, default=0.02)
    parser.add_argument("--force", action="store_true", help="Rebuild an existing legacy_reference/ written by this script.")
    parser.add_argument("--dry-run", action="store_true", help="Build and validate in a temp dir; write nothing under --asfm-root.")
    parser.add_argument("--refresh-inventory", action="store_true", help="Re-index the Ceres semifield-asfm scope after writing.")
    parser.add_argument("--endpoint-config", type=Path, default=DEFAULT_ENDPOINT_CONFIG)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    if args.dry_run and args.refresh_inventory:
        parser.error("--refresh-inventory can't be combined with --dry-run")
    if args.grid_step <= 0:
        parser.error("--grid-step must be positive")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    os.umask(0o002)

    batch_ids = list(args.batch_id) or (load_batch_file(args.batch_file) if args.batch_file else [])
    if not batch_ids:
        parser.error("pass --batch-id or --batch-file")

    conn = open_db(args.db, readonly=True)
    try:
        results = []
        for batch_id in dict.fromkeys(batch_ids):
            logger.info("Converting %s", batch_id)
            results.append(convert_batch(conn, batch_id, args))
    finally:
        conn.close()
    print_results(results)

    if args.refresh_inventory and any(r.status in ("written", "exists") for r in results):
        try:
            refresh_asfm_inventory(args)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Inventory refresh failed: %s", exc)
            return 1
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
