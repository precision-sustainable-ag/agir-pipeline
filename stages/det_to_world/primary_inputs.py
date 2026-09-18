"""Match primary-selection references to the grids used for remapping."""

from pathlib import Path
from typing import Any

from .primary_selection import load_reference_rows, select_primary_rows
from .remapper import GridCache


def select_with_references(
    rows: list[dict[str, Any]], cache: GridCache, reference_root: Path | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Use paired reference CSVs, preferring the reconstruction of each grid.

    An explicit root may contain reference/, autosfm/reference/, or nested
    time-range directories. Otherwise search inside the grid batch tree.
    Duplicate labels across reconstructions require an unambiguous match to
    the grid path; never choose an arbitrary reference or mix CSV pairs.
    All geometry and camera positions must already share a coordinate frame.
    """
    if not rows:
        return [], []
    if len({row.get("crs") for row in rows}) != 1 or not rows[0].get("crs"):
        raise ValueError("Legacy primary selection requires one common, populated CRS")
    root = reference_root if reference_root is not None else cache.grid_dir
    if not root.is_dir():
        raise ValueError(f"Primary reference directory does not exist: {root}")
    pairs = []
    for camera_path in sorted(root.rglob("camera_reference.csv")):
        fov_path = camera_path.with_name("fov.csv")
        if fov_path.is_file():
            pairs.append(
                (camera_path, load_reference_rows(camera_path), load_reference_rows(fov_path))
            )
    if not pairs:
        raise ValueError(f"No paired camera_reference.csv and fov.csv found under {root}")

    cameras, fovs, dimensions = {}, {}, {}
    used_paths = set()
    for image_id in dict.fromkeys(row["image_id"] for row in rows):
        grid_path = cache.path_for(image_id)
        candidates = [pair for pair in pairs if image_id in pair[1] and image_id in pair[2]]
        # Prefer a nearby reference directory when grids/references share a tree.
        nearby = []
        for ancestor in grid_path.parents:
            if ancestor == cache.grid_dir.parent:
                break
            nearby = [
                pair
                for pair in candidates
                if pair[0].parent
                in (ancestor / "reference", ancestor / "autosfm" / "reference", ancestor)
            ]
            if nearby:
                break
        if nearby:
            candidates = nearby
        elif len(candidates) > 1:
            # Separately staged references retain their batch-relative paths.
            grid_parts = set(grid_path.relative_to(cache.grid_dir).parts[:-1]) - {
                "autosfm",
                "outputs",
                "pixel_world_grids",
                "reference",
            }
            matching = [
                pair
                for pair in candidates
                if grid_parts.intersection(pair[0].relative_to(root).parts[:-1])
            ]
            if matching:
                candidates = matching
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one matching camera/FOV reference pair for {image_id}; "
                f"found {len(candidates)} for grid {grid_path}"
            )
        camera_path, camera_rows, fov_rows = candidates[0]
        cameras[image_id], fovs[image_id] = camera_rows[image_id], fov_rows[image_id]
        grid = cache.get(image_id)
        dimensions[image_id] = (grid.sensor_width, grid.sensor_height)
        used_paths.update((str(camera_path), str(camera_path.with_name("fov.csv"))))
    return select_primary_rows(rows, cameras, fovs, dimensions), sorted(used_paths)
