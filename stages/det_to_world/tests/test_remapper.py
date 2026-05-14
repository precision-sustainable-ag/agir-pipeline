from pathlib import Path

import numpy as np
import pytest

from stages import ITEM_FAILED, ITEM_OK
from stages.det_to_world import ERROR_GRID_NOT_FOUND
from stages.det_to_world.remapper import (
    GEO_COLUMNS,
    load_detection_rows,
    map_bbox,
    remap_rows,
    write_georeferenced_csv,
    GridCache,
)


@pytest.fixture
def detection_csv(tmp_path):
    # Two images: IMG_0001 has one valid detection and one out-of-bounds detection,
    # IMG_0002 has no matching grid file.
    csv_path = tmp_path / "detections.csv"
    csv_path.write_text(
        "\n".join(
            [
                "image_id,bounding_box_id,xmin,ymin,xmax,ymax,conf",
                "IMG_0001,0,0.1,0.2,0.3,0.4,0.95",
                "IMG_0001,1,0.6,0.6,1.2,0.8,0.90",
                "IMG_0002,0,0.1,0.2,0.3,0.4,0.80",
            ]
        )
    )
    return csv_path


@pytest.fixture
def grid_dir(tmp_path):
    # Simple linear grid covering the full sensor (0-100 px).
    # world_x == u and world_y == v, so normalized coord 0.1 -> world 10.0, etc.
    grid_dir = tmp_path / "grids"
    grid_dir.mkdir()

    u_pixels = np.array([0.0, 100.0])
    v_pixels = np.array([0.0, 100.0])
    world_x = np.array([[0.0, 100.0], [0.0, 100.0]])
    world_y = np.array([[0.0, 0.0], [100.0, 100.0]])

    np.savez(
        grid_dir / "IMG_0001.npz",
        u_pixels=u_pixels,
        v_pixels=v_pixels,
        world_x=world_x,
        world_y=world_y,
        world_z=np.zeros_like(world_x),
        sensor_width=np.array([100]),
        sensor_height=np.array([100]),
        crs=np.array(["EPSG:32617"]),
    )
    return grid_dir


@pytest.fixture
def nudge_grid_dir(tmp_path):
    # Grid that doesn't start at pixel 0 — valid pixel range is [2, 98].
    # Used to test that corners falling outside the grid boundary are nudged inward.
    grid_dir = tmp_path / "nudge_grids"
    grid_dir.mkdir()

    u_pixels = np.array([2.0, 50.0, 98.0])
    v_pixels = np.array([2.0, 50.0, 98.0])
    world_x = np.array(
        [
            [2.0, 50.0, 98.0],
            [2.0, 60.0, 118.0],
            [2.0, 70.0, 138.0],
        ]
    )
    world_y = np.array(
        [
            [2.0, 2.0, 2.0],
            [50.0, 55.0, 60.0],
            [98.0, 108.0, 118.0],
        ]
    )

    np.savez(
        grid_dir / "IMG_0003.npz",
        u_pixels=u_pixels,
        v_pixels=v_pixels,
        world_x=world_x,
        world_y=world_y,
        world_z=np.zeros_like(world_x),
        sensor_width=np.array([100]),
        sensor_height=np.array([100]),
        crs=np.array(["EPSG:32617"]),
    )
    return grid_dir


def test_load_detection_rows_validates_required_columns(tmp_path):
    # CSV is missing ymin, ymax, xmax 
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("image_id,bounding_box_id,xmin\nIMG_0001,0,0.1\n")


    # error message should indicate which required columns are missing
    with pytest.raises(ValueError, match="missing required columns"):
        load_detection_rows(csv_path)


def test_map_bbox_maps_all_corners(grid_dir):
    # successful mapping
    grid = GridCache(grid_dir).get("IMG_0001")

    # Bounding box from (0.1, 0.2) to (0.3, 0.4)
    row = {
        "image_id": "IMG_0001",
        "bounding_box_id": "0",
        "xmin": "0.1",
        "ymin": "0.2",
        "xmax": "0.3",
        "ymax": "0.4",
    }

    # mapping should produce correct world coordinates for all corners, centroid, and CRS without warnings
    mapped, warning = map_bbox(row, grid)

    assert warning is None
    assert mapped["world_tl_x"] == pytest.approx(10.0)
    assert mapped["world_tl_y"] == pytest.approx(20.0)
    assert mapped["world_br_x"] == pytest.approx(30.0)
    assert mapped["world_br_y"] == pytest.approx(40.0)
    assert mapped["world_centroid_x"] == pytest.approx(20.0)
    assert mapped["world_centroid_y"] == pytest.approx(30.0)
    assert mapped["crs"] == "EPSG:32617"


def test_remap_rows_handles_warnings_and_missing_grids(detection_csv, grid_dir):
    # IMG_0001: one detection maps successfully, one is out-of-bounds and becomes a warning.
    # IMG_0002: no grid file exists, so the whole image fails with E_GRID_NOT_FOUND.


    _, rows = load_detection_rows(detection_csv)

    mapped_rows, results = remap_rows(rows, grid_dir)

    assert len(mapped_rows) == 1
    assert results[0].image_id == "IMG_0001"
    assert results[0].status == ITEM_OK
    assert results[0].n_output_rows == 1
    assert len(results[0].warnings) == 1

    assert results[1].image_id == "IMG_0002"
    assert results[1].status == ITEM_FAILED
    assert results[1].error_code == ERROR_GRID_NOT_FOUND


def test_map_bbox_applies_inward_nudges(nudge_grid_dir):
    # Top-left corner at (0.0, 0.0) is outside the grid
    # Validate nudging makes it eventually successful


    grid = GridCache(nudge_grid_dir).get("IMG_0003")
    row = {
        "image_id": "IMG_0003",
        "bounding_box_id": "0",
        "xmin": "0.0",
        "ymin": "0.0",
        "xmax": "0.3",
        "ymax": "0.4",
    }

    mapped, warning = map_bbox(row, grid)

    assert warning is None
    assert mapped["world_tl_x"] == pytest.approx(2.0)
    assert mapped["world_tl_y"] == pytest.approx(2.0)


def test_map_bbox_uses_tl_br_midpoint_for_centroid(nudge_grid_dir):

    grid = GridCache(nudge_grid_dir).get("IMG_0003")

    # centroid should be midpoint of TL and BR corners, not average of all corners
    row = {
        "image_id": "IMG_0003",
        "bounding_box_id": "1",
        "xmin": "0.2",
        "ymin": "0.2",
        "xmax": "0.8",
        "ymax": "0.8",
    }

    mapped, warning = map_bbox(row, grid)

    assert warning is None
    expected_x = (mapped["world_tl_x"] + mapped["world_br_x"]) / 2
    expected_y = (mapped["world_tl_y"] + mapped["world_br_y"]) / 2
    average_of_corners_x = (
        mapped["world_tl_x"] + mapped["world_tr_x"] + mapped["world_bl_x"] + mapped["world_br_x"]
    ) / 4

    assert mapped["world_centroid_x"] == pytest.approx(expected_x)
    assert mapped["world_centroid_y"] == pytest.approx(expected_y)
    assert mapped["world_centroid_x"] != pytest.approx(average_of_corners_x)


def test_write_georeferenced_csv_supports_header_only(tmp_path):
    # test skip-remap - no mapped rows, but still a valid csv with headers
    
    input_fieldnames = ["image_id", "bounding_box_id", "xmin", "ymin", "xmax", "ymax"]
    fieldnames = input_fieldnames + GEO_COLUMNS
    csv_path = tmp_path / "header_only.csv"

    write_georeferenced_csv([], fieldnames, csv_path)

    lines = csv_path.read_text().splitlines()
    
    assert len(lines) == 1
    assert "world_centroid_x" in lines[0]
