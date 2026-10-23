from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

from stages.det_to_world.remapper import GridCache, map_bbox

_MODULE_PATH = Path(__file__).resolve().parents[1] / "legacy_reference_to_grids.py"
_spec = importlib.util.spec_from_file_location("legacy_reference_to_grids", _MODULE_PATH)
legacy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = legacy  # dataclasses look their module up here
_spec.loader.exec_module(legacy)

WIDTH, HEIGHT = 1000, 800
# A rotated parallelogram footprint in lon/lat (bottom_right = tr + bl - tl),
# so the pixel->world map is affine and the expected world point is exact.
TL = np.array([-78.672570, 35.774160])
TR = TL + np.array([0.000004, 0.000011])
BL = TL + np.array([0.000010, -0.000003])
BR = TR + BL - TL


def _world(x: float, y: float) -> list[float]:
    return list(TL + x * (TR - TL) + y * (BL - TL))


def _annotation(xmin, ymin, xmax, ymax) -> dict:
    local = {"top_left": [xmin, ymin], "top_right": [xmax, ymin],
             "bottom_left": [xmin, ymax], "bottom_right": [xmax, ymax]}
    return {
        "cutout_id": "box",
        "local_coordinates": {**local, "is_normalized": True},
        "global_coordinates": {corner: _world(*xy) for corner, xy in local.items()},
    }


def _write_image(metadata_dir: Path, image_id: str, *, aligned: bool = True) -> None:
    metadata_dir.mkdir(parents=True, exist_ok=True)
    fov = {"top_left_xy": list(TL), "top_right_xy": list(TR),
           "bottom_left_xy": list(BL), "bottom_right_xy": list(BR)}
    data = {
        "image_id": image_id,
        "fullres_width": WIDTH,
        "fullres_height": HEIGHT,
        "camera_info": {
            "aligned": aligned,
            "fov": fov,
            "estimated_xyz": [*_world(0.5, 0.5), 94.0],
            "estimated_yaw": 286.0, "estimated_pitch": -0.3, "estimated_roll": -0.4,
            "camera_coefficients": {"f": 11047.0, "cx": -34.0, "cy": -67.8},
            "pixel_width": 0.0046, "pixel_height": 0.0046,
        },
        "annotations": [_annotation(0.1, 0.2, 0.3, 0.5), _annotation(0.6, 0.1, 0.95, 0.4)],
    }
    (metadata_dir / f"{image_id}.json").write_text(json.dumps(data))


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE season_date_ranges (site TEXT, season_name TEXT, start_date TEXT, "
        "end_date TEXT, bbot_version TEXT, crs TEXT, pipeline_season TEXT, gh_reviewer TEXT)"
    )
    conn.executemany(
        "INSERT INTO season_date_ranges VALUES (?, ?, ?, ?, '3.1', ?, NULL, NULL)",
        [("NC", "covers", "2025-01-01", "2025-04-15", "4326"),
         ("NC", "dicamba", "2025-06-01", "2025-08-08", "32617"),
         ("NC", "old", "2022-01-01", "2022-12-31", "LOCAL")],
    )
    conn.commit()
    conn.close()
    source, asfm = tmp_path / "developed", tmp_path / "asfm"
    for batch in ("NC_2025-02-05", "NC_2025-07-01", "NC_2022-05-01"):
        _write_image(source / batch / "metadata", "NC_1")
        _write_image(source / batch / "metadata", "NC_2", aligned=False)
        (source / batch / "detections").mkdir()
        for image_id in ("NC_1", "NC_2"):
            (source / batch / "detections" / f"{image_id}.txt").write_text("0 0.5 0.5 0.1 0.1 0.9\n")
    args = argparse.Namespace(
        db=str(db), source_root=source, asfm_root=asfm, grid_step=100,
        max_median_cm=5.0, max_p95_cm=25.0, outlier_cm=100.0, max_outlier_fraction=0.02,
        force=False, dry_run=False,
    )
    return args


def _convert(args, batch_id):
    from orchestrator.sqlite_db import open_db

    conn = open_db(args.db, readonly=True)
    try:
        return legacy.convert_batch(conn, batch_id, args)
    finally:
        conn.close()


def test_fit_homography_maps_corners_exactly():
    pixels = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=float)
    world = np.array([[2, 3], [5, 3.5], [1.5, 7], [6, 8]], dtype=float)
    h = legacy.fit_homography(pixels, world)
    for (x, y), expected in zip(pixels, world):
        p = h @ [x, y, 1]
        assert p[:2] / p[2] == pytest.approx(expected)


def test_writes_det_to_world_readable_grids_in_season_crs(env):
    result = _convert(env, "NC_2025-02-05")

    assert result.status == "written", result.message
    assert (result.crs, result.images_written) == ("EPSG:4326", 1)
    assert result.skipped == {"no_aligned_fov": 1}
    assert (result.detection_images_with_grid, result.detection_images) == (1, 2)
    assert result.validation["median_cm"] < 0.1

    sub_batch = env.asfm_root / "NC_2025-02-05" / "legacy_reference"
    reference = sub_batch / "autosfm" / "reference"
    with np.load(reference / "pixel_world_grids" / "NC_1.npz") as npz:
        assert npz["u_pixels"][[0, -1]].tolist() == [0, WIDTH - 1]
        assert npz["world_x"].shape == (len(npz["v_pixels"]), len(npz["u_pixels"]))
    assert json.loads((sub_batch / "provenance.json").read_text())["crs"] == "EPSG:4326"
    labels = [row["label"] for row in csv.DictReader((reference / "fov.csv").open())]
    assert labels == ["NC_1"]

    grid = GridCache(sub_batch).get("NC_1")
    mapped, warning = map_bbox(
        {"image_id": "NC_1", "bounding_box_id": 0, "xmin": 0.25, "ymin": 0.25, "xmax": 0.5, "ymax": 0.75},
        grid,
    )
    assert warning is None and grid.crs == "EPSG:4326"
    assert [mapped["world_tl_x"], mapped["world_tl_y"]] == pytest.approx(_world(0.25, 0.25), abs=1e-10)


def test_reprojects_to_utm_season_crs(env):
    result = _convert(env, "NC_2025-07-01")

    assert result.status == "written", result.message
    assert result.crs == "EPSG:32617"
    assert result.validation["median_cm"] < 0.1
    reference = env.asfm_root / "NC_2025-07-01" / "legacy_reference" / "autosfm" / "reference"
    fov = next(csv.DictReader((reference / "fov.csv").open()))
    camera = next(csv.DictReader((reference / "camera_reference.csv").open()))
    to_utm = Transformer.from_crs(4326, 32617, always_xy=True)
    assert [float(fov["top_left_x"]), float(fov["top_left_y"])] == pytest.approx(to_utm.transform(*TL))
    assert float(camera["Estimated_X"]) == pytest.approx(to_utm.transform(*_world(0.5, 0.5))[0])
    assert 0.5 < float(fov["width"]) < 2  # metres, not degrees


def test_refuses_local_crs_and_foreign_asfm_output(env):
    assert "need an EPSG code" in _convert(env, "NC_2022-05-01").message

    foreign = env.asfm_root / "NC_2025-02-05" / "155843_165901" / "pixel_world_grids"
    foreign.mkdir(parents=True)
    (foreign / "NC_1.npz").write_bytes(b"")
    result = _convert(env, "NC_2025-02-05")
    assert result.status == "failed" and "already has ASFM output" in result.message


def test_existing_output_needs_force_and_dry_run_writes_nothing(env):
    env.dry_run = True
    assert _convert(env, "NC_2025-02-05").status == "would_write"
    assert not (env.asfm_root / "NC_2025-02-05").exists()

    env.dry_run = False
    assert _convert(env, "NC_2025-02-05").status == "written"
    assert _convert(env, "NC_2025-02-05").status == "exists"
    env.force = True
    assert _convert(env, "NC_2025-02-05").status == "written"
    assert [p.name for p in (env.asfm_root / "NC_2025-02-05").iterdir()] == ["legacy_reference"]
