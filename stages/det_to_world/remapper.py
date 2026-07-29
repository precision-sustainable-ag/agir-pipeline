"""
Core bbox remapping logic for det_to_world.
"""

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from stages import ITEM_FAILED, ITEM_OK

from . import ERROR_GRID_NOT_FOUND, ERROR_REMAP_FAILED

logger = logging.getLogger(__name__)

WARNING_SURFACE_MISS = "W_SURFACE_MISS"
NUDGE_PX = [0, 1, 2, 3, 5, 10]

REQUIRED_INPUT_COLUMNS = [
    "image_id",
    "bounding_box_id",
    "xmin",
    "ymin",
    "xmax",
    "ymax",
]

GEO_COLUMNS = [
    "world_tl_x",
    "world_tl_y",
    "world_tr_x",
    "world_tr_y",
    "world_bl_x",
    "world_bl_y",
    "world_br_x",
    "world_br_y",
    "world_centroid_x",
    "world_centroid_y",
    "crs",
]


@dataclass
class RemapWarning:
    unit_id: str
    code: str
    warning_type: str
    message: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageRemapResult:
    image_id: str
    status: str
    n_input_rows: int
    n_output_rows: int
    warnings: list[RemapWarning] = field(default_factory=list)
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None


# store npz data 
@dataclass
class GridData:
    image_id: str
    ix: Any
    iy: Any
    sensor_width: int
    sensor_height: int
    crs: str | None


class GridCache:
    """Load each image's NPZ grid at most once per run.

    ``grid_dir`` may be a flat directory of ``<image_id>.npz`` files, or a
    batch root containing NPZ files nested under sub-batch directories (e.g.
    ASFM's per-time-range ``<start>_<end>/pixel_world_grids/`` layout) — the
    NPZ index below is built with a recursive glob, so both layouts resolve
    the same way.
    """

    def __init__(self, grid_dir: Path):
        self.grid_dir = Path(grid_dir)
        # image id -> npz data dictionary
        self._cache: dict[str, GridData] = {}
        self._npz_index: dict[str, Path] | None = None

    def _index(self) -> dict[str, Path]:
        if self._npz_index is None:
            index: dict[str, Path] = {}
            for npz_path in sorted(self.grid_dir.rglob("*.npz")):
                if npz_path.stem in index:
                    logger.warning(
                        "Duplicate NPZ grid for image_id=%s: keeping %s, ignoring %s",
                        npz_path.stem, index[npz_path.stem], npz_path,
                    )
                    continue
                index[npz_path.stem] = npz_path
            self._npz_index = index
        return self._npz_index

    # load and cache grid data for an image id
    def get(self, image_id: str) -> GridData:
        if image_id in self._cache:
            return self._cache[image_id]

        npz_path = self._index().get(image_id)
        if npz_path is None:
            raise FileNotFoundError(
                f"Grid file not found for image_id={image_id} under {self.grid_dir}"
            )

        try:
            with np.load(npz_path, allow_pickle=True) as data:
                grid = self._load_grid(image_id, data)
        except Exception as exc:
            raise RuntimeError(f"Failed to load grid {npz_path}: {exc}") from exc

        self._cache[image_id] = grid
        return grid

    # load npz data as a grid object
    def _load_grid(self, image_id: str, data: Any) -> GridData:
        required_keys = [
            "u_pixels",
            "v_pixels",
            "world_x",
            "world_y",
            "sensor_width",
            "sensor_height",
            "crs",
        ]
        # validate missing keys
        missing = [key for key in required_keys if key not in data]
        if missing:
            raise ValueError(f"Missing keys in NPZ for {image_id}: {', '.join(missing)}")

        
        # image rows are indexed as (v, u)
        u_pixels = np.asarray(data["u_pixels"], dtype=float)
        v_pixels = np.asarray(data["v_pixels"], dtype=float)
        # real world corresponding (x, y) for (u, v)
        world_x = np.asarray(data["world_x"], dtype=float)
        world_y = np.asarray(data["world_y"], dtype=float)

        expected_shape = (len(v_pixels), len(u_pixels))
        if world_x.shape != expected_shape:
            raise ValueError(f"world_x shape {world_x.shape} does not match grid {expected_shape}")
        if world_y.shape != expected_shape:
            raise ValueError(f"world_y shape {world_y.shape} does not match grid {expected_shape}")

        ix = RegularGridInterpolator(
            (v_pixels, u_pixels),
            world_x,
            method="linear",
            bounds_error=False,
            fill_value=np.nan,
        )
        iy = RegularGridInterpolator(
            (v_pixels, u_pixels),
            world_y,
            method="linear",
            bounds_error=False,
            fill_value=np.nan,
        )

        crs_raw = np.asarray(data["crs"]).reshape(-1)
        crs = str(crs_raw[0]) if crs_raw.size else None

        return GridData(
            image_id=image_id,
            ix=ix,
            iy=iy,
            sensor_width=int(np.asarray(data["sensor_width"]).reshape(-1)[0]),
            sensor_height=int(np.asarray(data["sensor_height"]).reshape(-1)[0]),
            crs=crs,
        )


def _interpolate_point(grid: GridData, u: float, v: float) -> tuple[float, float]:
    """Interpolate the world coordinates for a single point (u, v) using the grid's interpolators."""
    point = np.array([[v, u]], dtype=float)
    world_x = float(np.asarray(grid.ix(point)).reshape(-1)[0])
    world_y = float(np.asarray(grid.iy(point)).reshape(-1)[0])
    return world_x, world_y


def _map_point(x: float, y: float, grid: GridData) -> tuple[float, float] | None:
    """Map a single normalized bbox corner (x, y) to world coordinates using the grid"""

    pixel_X = float(x * grid.sensor_width)
    pixel_Y = float(y * grid.sensor_height)

    center_X = grid.sensor_width / 2
    center_Y = grid.sensor_height / 2

    # direction from pixel to center
    dx = np.sign(center_X - pixel_X)
    dy = np.sign(center_Y - pixel_Y)

    u_min, u_max = grid.ix.grid[1][0], grid.ix.grid[1][-1]
    v_min, v_max = grid.ix.grid[0][0], grid.ix.grid[0][-1]

    # check for nearby valid points
    for nudge in NUDGE_PX:
        nudged_x = pixel_X + dx * nudge
        nudged_y = pixel_Y + dy * nudge
        nx = float(nudged_x)
        ny = float(nudged_y)

        if not (u_min <= nx <= u_max and v_min <= ny <= v_max):
            continue

        # maps normalized bbox corner to world coordinates
        world_x, world_y = _interpolate_point(grid, nx, ny)
        if not (np.isnan(world_x) or np.isnan(world_y)):
            return world_x, world_y

    return None


def _construct_global_coords(
    row: dict[str, str],
    coords: list[tuple[float, float]],
    crs: str | None,
) -> dict[str, Any]:
    top_left, top_right, bottom_left, bottom_right = coords
    centroid_x = float((top_left[0] + bottom_right[0]) / 2)
    centroid_y = float((top_left[1] + bottom_right[1]) / 2)

    mapped_row = dict(row)
    mapped_row.update(
        {
            "world_tl_x": top_left[0],
            "world_tl_y": top_left[1],
            "world_tr_x": top_right[0],
            "world_tr_y": top_right[1],
            "world_bl_x": bottom_left[0],
            "world_bl_y": bottom_left[1],
            "world_br_x": bottom_right[0],
            "world_br_y": bottom_right[1],
            "world_centroid_x": centroid_x,
            "world_centroid_y": centroid_y,
            "crs": crs,
        }
    )
    return mapped_row


def map_bbox(row: dict[str, str], grid: GridData) -> tuple[dict[str, Any] | None, RemapWarning | None]:
    """Map a single detection row to world coordinates, returning the mapped row or a warning if mapping fails."""
    coords = {
        "top_left": [float(row["xmin"]), float(row["ymin"])],
        "top_right": [float(row["xmax"]), float(row["ymin"])],
        "bottom_left": [float(row["xmin"]), float(row["ymax"])],
        "bottom_right": [float(row["xmax"]), float(row["ymax"])],
    }

    mapped = []
    for corner in ["top_left", "top_right", "bottom_left", "bottom_right"]:
        x, y = coords[corner]
        # convert normalized bbox coords to world coordinates using the grid
        point = _map_point(x, y, grid)
        if point is None:
            image_id = row["image_id"]
            bbox_id = row["bounding_box_id"]
            return None, RemapWarning(
                unit_id=f"{image_id}:{bbox_id}",
                code=WARNING_SURFACE_MISS,
                warning_type="SurfaceMissWarning",
                message=(
                    f"Skipping detection {bbox_id} for image {image_id}: "
                    "at least one bbox corner failed after inward nudges."
                ),
                meta={"image_id": image_id, "bounding_box_id": bbox_id},
            )
        mapped.append(point)

    return _construct_global_coords(row, mapped, grid.crs), None


def load_detection_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """ reads detection CSV and validates required columns, returns fieldnames and rows """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Detection CSV does not exist: {csv_path}")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        # find missing columns
        missing = [column for column in REQUIRED_INPUT_COLUMNS if column not in fieldnames]
        if missing:
            raise ValueError(f"Detection CSV missing required columns: {', '.join(missing)}")
        rows = list(reader)

    return fieldnames, rows


def write_georeferenced_csv(
    rows: list[dict[str, Any]],
    fieldnames: list[str],
    csv_path: Path,
) -> Path:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return csv_path


def remap_rows(
    rows: list[dict[str, str]],
    grid_dir: Path,
) -> tuple[list[dict[str, Any]], list[ImageRemapResult]]:
    """Remap detection rows to world coordinates, returning mapped rows and per-image results."""

    # cache image results to avoid redundant grid loads
    cache = GridCache(grid_dir)

    output_rows: list[dict[str, Any]] = []
    results: list[ImageRemapResult] = []

    rows_by_image: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_image.setdefault(row["image_id"], []).append(row)

    for image_id, image_rows in rows_by_image.items():
        try:
            # check cache
            grid = cache.get(image_id)
            

        except FileNotFoundError as exc:
            # cache miss
            results.append(
                ImageRemapResult(
                    image_id=image_id,
                    status=ITEM_FAILED,
                    n_input_rows=len(image_rows),
                    n_output_rows=0,
                    error_code=ERROR_GRID_NOT_FOUND,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            continue
        except Exception as exc:
            # general faiure
            results.append(
                ImageRemapResult(
                    image_id=image_id,
                    status=ITEM_FAILED,
                    n_input_rows=len(image_rows),
                    n_output_rows=0,
                    error_code=ERROR_REMAP_FAILED,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            continue

        warnings: list[RemapWarning] = []
        n_output_rows = 0

        for row in image_rows:
            try:
                mapped_row, warning = map_bbox(row, grid)
            except Exception as exc:
                results.append(
                    ImageRemapResult(
                        image_id=image_id,
                        status=ITEM_FAILED,
                        n_input_rows=len(image_rows),
                        n_output_rows=n_output_rows,
                        warnings=warnings,
                        error_code=ERROR_REMAP_FAILED,
                        error_type=type(exc).__name__,
                        error_message=(
                            f"Unexpected remap failure for image_id={image_id}, "
                            f"bounding_box_id={row.get('bounding_box_id')}: {exc}"
                        ),
                    )
                )
                break

            if warning is not None:
                logger.warning(warning.message)
                warnings.append(warning)
                continue

            output_rows.append(mapped_row)
            n_output_rows += 1
        else:
            results.append(
                ImageRemapResult(
                    image_id=image_id,
                    status=ITEM_OK,
                    n_input_rows=len(image_rows),
                    n_output_rows=n_output_rows,
                    warnings=warnings,
                )
            )

    return output_rows, results
