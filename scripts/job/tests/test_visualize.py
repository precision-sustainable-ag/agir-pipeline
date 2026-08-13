from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import geopandas as gpd
import numpy as np
import cv2

_MODULE_PATH = Path(__file__).resolve().parents[1] / "visualize.py"
_spec = importlib.util.spec_from_file_location("visualize", _MODULE_PATH)
visualize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(visualize)


def _write_image(path: Path, *, width: int = 100, height: int = 80) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = np.zeros((height, width, 3), dtype="uint8")
    cv2.imwrite(str(path), im)


def _write_georeferenced_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "image_id", "bounding_box_id", "xmin", "ymin", "xmax", "ymax",
        "world_centroid_x", "world_centroid_y",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_load_georeferenced_rows_by_image_groups_by_image_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "MD_2026-03-19_georeferenced.csv"
    _write_georeferenced_csv(csv_path, [
        {"image_id": "img_001", "bounding_box_id": "0", "xmin": "0.1", "ymin": "0.1",
         "xmax": "0.2", "ymax": "0.2", "world_centroid_x": "10.5", "world_centroid_y": "20.5"},
        {"image_id": "img_001", "bounding_box_id": "1", "xmin": "0.3", "ymin": "0.3",
         "xmax": "0.4", "ymax": "0.4", "world_centroid_x": "11.5", "world_centroid_y": "21.5"},
        {"image_id": "img_002", "bounding_box_id": "0", "xmin": "0.5", "ymin": "0.5",
         "xmax": "0.6", "ymax": "0.6", "world_centroid_x": "12.5", "world_centroid_y": "22.5"},
    ])

    rows_by_image = visualize.load_georeferenced_rows_by_image(csv_path)

    assert set(rows_by_image) == {"img_001", "img_002"}
    assert len(rows_by_image["img_001"]) == 2
    assert len(rows_by_image["img_002"]) == 1


def test_render_det_to_world_draws_box_and_writes_output(tmp_path: Path) -> None:
    image_path = tmp_path / "img_001.jpg"
    _write_image(image_path, width=100, height=80)
    out_path = tmp_path / "out" / "img_001.jpg"

    rows = [{
        "xmin": "0.1", "ymin": "0.1", "xmax": "0.5", "ymax": "0.5",
        "world_centroid_x": "10.123", "world_centroid_y": "20.456",
    }]

    ok = visualize._render_det_to_world(image_path, rows, out_path, max_width=1800)

    assert ok is True
    assert out_path.exists()
    im = cv2.imread(str(out_path))
    assert im is not None
    # The rectangle's green border should be present somewhere in the image.
    assert (im[:, :, 1] == 255).any()


def test_render_det_to_world_handles_no_rows(tmp_path: Path) -> None:
    image_path = tmp_path / "img_002.jpg"
    _write_image(image_path)
    out_path = tmp_path / "out" / "img_002.jpg"

    ok = visualize._render_det_to_world(image_path, [], out_path, max_width=1800)

    assert ok is True
    assert out_path.exists()


def test_render_det_to_world_downscales_to_max_width(tmp_path: Path) -> None:
    image_path = tmp_path / "img_003.jpg"
    _write_image(image_path, width=2000, height=1000)
    out_path = tmp_path / "out" / "img_003.jpg"

    ok = visualize._render_det_to_world(image_path, [], out_path, max_width=500)

    assert ok is True
    im = cv2.imread(str(out_path))
    assert im.shape[1] == 500


def test_polygon_area_unit_square() -> None:
    # A 2x3 axis-aligned rectangle has area 6, regardless of corner order,
    # as long as the order traces the polygon boundary (not diagonally).
    points = [(0.0, 0.0), (2.0, 0.0), (2.0, 3.0), (0.0, 3.0)]
    assert visualize._polygon_area(points) == 6.0


def test_bbox_area_cm2_computes_shoelace_area_from_world_corners() -> None:
    # 0.5m x 0.25m rectangle in world space -> 0.125 m^2 -> 1250 cm^2.
    row = {
        "crs": "32618",
        "world_tl_x": "0.0", "world_tl_y": "0.0",
        "world_tr_x": "0.5", "world_tr_y": "0.0",
        "world_br_x": "0.5", "world_br_y": "0.25",
        "world_bl_x": "0.0", "world_bl_y": "0.25",
    }
    assert visualize._bbox_area_cm2(row) == 1250.0


def test_bbox_area_cm2_returns_none_for_geographic_crs() -> None:
    row = {
        "crs": "4326",
        "world_tl_x": "0.0", "world_tl_y": "0.0",
        "world_tr_x": "0.5", "world_tr_y": "0.0",
        "world_br_x": "0.5", "world_br_y": "0.25",
        "world_bl_x": "0.0", "world_bl_y": "0.25",
    }
    assert visualize._bbox_area_cm2(row) is None


def test_bbox_area_cm2_returns_none_when_corners_missing() -> None:
    assert visualize._bbox_area_cm2({"crs": "LOCAL"}) is None


def test_render_det_to_world_labels_area_only(tmp_path: Path) -> None:
    image_path = tmp_path / "img_004.jpg"
    _write_image(image_path, width=200, height=200)
    out_path = tmp_path / "out" / "img_004.jpg"

    rows = [{
        "xmin": "0.1", "ymin": "0.1", "xmax": "0.5", "ymax": "0.5",
        "crs": "32618",
        "world_tl_x": "0.0", "world_tl_y": "0.0",
        "world_tr_x": "0.5", "world_tr_y": "0.0",
        "world_br_x": "0.5", "world_br_y": "0.25",
        "world_bl_x": "0.0", "world_bl_y": "0.25",
    }]

    ok = visualize._render_det_to_world(image_path, rows, out_path, max_width=1800)

    assert ok is True
    assert out_path.exists()


def test_detection_name_label_prefers_cultivar_over_species_name() -> None:
    # Mutually exclusive in practice (see species.assign_spatial), but if
    # both were somehow present, cultivar should win.
    row = {"cultivar_name": "Peanut - NC 20", "species_name": "Ragweed"}
    assert visualize._detection_name_label(row) == "Peanut - NC 20"


def test_detection_name_label_falls_back_to_species_name() -> None:
    row = {"species_name": "Ragweed"}
    assert visualize._detection_name_label(row) == "Ragweed"


def test_detection_name_label_none_when_absent() -> None:
    assert visualize._detection_name_label({}) is None
    assert visualize._detection_name_label({"cultivar_name": "", "species_name": ""}) is None


def _bbox_row(**overrides) -> dict[str, str]:
    row = {
        "image_id": "img_001", "bounding_box_id": "0",
        "crs": "EPSG:32617",
        "world_tl_x": "0.0", "world_tl_y": "0.0",
        "world_tr_x": "1.0", "world_tr_y": "0.0",
        "world_br_x": "1.0", "world_br_y": "1.0",
        "world_bl_x": "0.0", "world_bl_y": "1.0",
        "species_id": "ARHY",
        "assignment_method": "spatial_join",
    }
    row.update(overrides)
    return row


def test_write_bbox_shapefile_writes_geometry_and_renamed_fields(tmp_path: Path) -> None:
    rows = [_bbox_row(cultivar_id="107", cultivar_name="Peanut - EXP-OLEIC-001")]

    out_path = visualize.write_bbox_shapefile(rows, "NC_2026-07-17", tmp_path)

    assert out_path == tmp_path / "NC_2026-07-17_bboxes.shp"
    assert out_path.exists()
    gdf = gpd.read_file(out_path)
    assert len(gdf) == 1
    # DBF field names match the zone shapefile's own short names, not
    # det_to_world's CSV headers.
    assert gdf.iloc[0]["species"] == "ARHY"
    assert gdf.iloc[0]["cultc_id"] == "107"
    assert gdf.iloc[0]["disp_name"] == "Peanut - EXP-OLEIC-001"
    assert "cultivar_id" not in gdf.columns
    assert gdf.iloc[0].geometry.area == 1.0


def test_write_bbox_shapefile_skips_rows_without_world_corners(tmp_path: Path) -> None:
    # e.g. a monoculture batch's rows, which never had remapping applied.
    rows = [{"image_id": "img_001", "bounding_box_id": "0", "species_id": "ARHY"}]

    out_path = visualize.write_bbox_shapefile(rows, "TX_2025-07-01", tmp_path)

    assert out_path is None


def test_write_bbox_shapefile_returns_none_for_no_rows(tmp_path: Path) -> None:
    assert visualize.write_bbox_shapefile([], "NC_2026-07-17", tmp_path) is None


def test_render_det_to_world_labels_name_and_area(tmp_path: Path) -> None:
    image_path = tmp_path / "img_006.jpg"
    _write_image(image_path, width=200, height=200)
    out_path = tmp_path / "out" / "img_006.jpg"

    rows = [{
        "xmin": "0.1", "ymin": "0.1", "xmax": "0.5", "ymax": "0.5",
        "crs": "32618",
        "world_tl_x": "0.0", "world_tl_y": "0.0",
        "world_tr_x": "0.5", "world_tr_y": "0.0",
        "world_br_x": "0.5", "world_br_y": "0.25",
        "world_bl_x": "0.0", "world_bl_y": "0.25",
        "cultivar_name": "Peanut - NC 20",
    }]

    ok = visualize._render_det_to_world(image_path, rows, out_path, max_width=1800)

    assert ok is True
    im = cv2.imread(str(out_path))
    assert (im[:, :, 1] == 255).any()


def test_render_det_to_world_draws_box_without_label_when_area_unavailable(
    tmp_path: Path,
) -> None:
    # No world_* corner columns at all -> _bbox_area_cm2 returns None -> no
    # label should be drawn, but the box itself still should be (and the
    # function must not crash).
    image_path = tmp_path / "img_005.jpg"
    _write_image(image_path, width=200, height=200)
    out_path = tmp_path / "out" / "img_005.jpg"

    rows = [{"xmin": "0.1", "ymin": "0.1", "xmax": "0.5", "ymax": "0.5"}]

    ok = visualize._render_det_to_world(image_path, rows, out_path, max_width=1800)

    assert ok is True
    im = cv2.imread(str(out_path))
    assert (im[:, :, 1] == 255).any()


def _write_mask(path: Path, *, width: int, height: int, box=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mask = np.zeros((height, width), dtype="uint8")
    if box is not None:
        x1, y1, x2, y2 = box
        mask[y1:y2, x1:x2] = 255
    cv2.imwrite(str(path), mask)


def test_render_det_to_seg_overlays_mask_pixels(tmp_path: Path) -> None:
    image_path = tmp_path / "img_001.jpg"
    _write_image(image_path, width=100, height=80)
    mask_path = tmp_path / "img_001_mask.png"
    _write_mask(mask_path, width=100, height=80, box=(10, 10, 50, 50))
    out_path = tmp_path / "out" / "img_001.jpg"

    ok = visualize._render_det_to_seg(
        image_path, mask_path, None, out_path, max_width=1800, alpha=0.4
    )

    assert ok is True
    im = cv2.imread(str(out_path))
    # Masked region should have picked up red (channel 2) from the overlay;
    # the source image was pure black, so any red at all confirms blending.
    assert im[20, 20, 2] > 0
    # Outside the mask, the pixel should remain unaffected (still black).
    assert tuple(im[70, 90]) == (0, 0, 0)


def test_render_det_to_seg_handles_missing_mask(tmp_path: Path) -> None:
    image_path = tmp_path / "img_002.jpg"
    _write_image(image_path, width=100, height=80)
    out_path = tmp_path / "out" / "img_002.jpg"

    ok = visualize._render_det_to_seg(
        image_path, tmp_path / "does_not_exist_mask.png", None, out_path,
        max_width=1800, alpha=0.4,
    )

    assert ok is True
    assert out_path.exists()


def test_render_det_to_seg_draws_optional_detection_box(tmp_path: Path) -> None:
    image_path = tmp_path / "img_003.jpg"
    _write_image(image_path, width=200, height=200)
    mask_path = tmp_path / "img_003_mask.png"
    _write_mask(mask_path, width=200, height=200)
    det_path = tmp_path / "img_003.txt"
    det_path.write_text("0 0.5 0.5 0.4 0.4 0.9\n")
    out_path = tmp_path / "out" / "img_003.jpg"

    ok = visualize._render_det_to_seg(
        image_path, mask_path, det_path, out_path, max_width=1800, alpha=0.4
    )

    assert ok is True
    im = cv2.imread(str(out_path))
    # The rectangle's green border should be present somewhere in the image.
    assert (im[:, :, 1] == 255).any()


def test_render_det_to_seg_downscales_to_max_width(tmp_path: Path) -> None:
    image_path = tmp_path / "img_004.jpg"
    _write_image(image_path, width=2000, height=1000)
    mask_path = tmp_path / "img_004_mask.png"
    _write_mask(mask_path, width=2000, height=1000)
    out_path = tmp_path / "out" / "img_004.jpg"

    ok = visualize._render_det_to_seg(
        image_path, mask_path, None, out_path, max_width=500, alpha=0.4
    )

    assert ok is True
    im = cv2.imread(str(out_path))
    assert im.shape[1] == 500
