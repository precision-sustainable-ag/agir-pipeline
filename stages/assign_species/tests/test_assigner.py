import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from stages.assign_species.assigner import assign_monoculture, assign_spatial

ZONE_CRS = "EPSG:32617"


@pytest.fixture
def shapefile(tmp_path):
    """Two non-overlapping rectangular zone polygons with distinct species codes."""
    zones = gpd.GeoDataFrame(
        {"species": ["ABUTH", "BETVU"]},
        geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30)],
        crs=ZONE_CRS,
    )
    path = tmp_path / "zones.shp"
    zones.to_file(path)
    return path


@pytest.fixture
def geo_csv(tmp_path):
    """Three detections: two inside zones, one outside all zones (for nearest fallback)."""
    path = tmp_path / "NC_2024-06-01_georeferenced.csv"
    pd.DataFrame(
        {
            "image_id": ["IMG_0001", "IMG_0001", "IMG_0001"],
            "bounding_box_id": [0, 1, 2],
            "classname": ["weed", "weed", "weed"],
            "conf": [0.95, 0.90, 0.85],
            "xmin": [0.1, 0.4, 0.7],
            "ymin": [0.1, 0.4, 0.7],
            "xmax": [0.2, 0.5, 0.8],
            "ymax": [0.2, 0.5, 0.8],
            # inside zone 1, inside zone 2, outside both
            "world_centroid_x": [5.0, 25.0, 15.0],
            "world_centroid_y": [5.0, 25.0, 15.0],
            "world_tl_x": [4.0, 24.0, 14.0],
            "world_tl_y": [4.0, 24.0, 14.0],
            "world_tr_x": [6.0, 26.0, 16.0],
            "world_tr_y": [4.0, 24.0, 14.0],
            "world_bl_x": [4.0, 24.0, 14.0],
            "world_bl_y": [6.0, 26.0, 16.0],
            "world_br_x": [6.0, 26.0, 16.0],
            "world_br_y": [6.0, 26.0, 16.0],
            "crs": [ZONE_CRS, ZONE_CRS, ZONE_CRS],
        }
    ).to_csv(path, index=False)
    return path


@pytest.fixture
def det_csv(tmp_path):
    """Basic detection CSV as produced by jpg_to_det — no world coordinate columns."""
    path = tmp_path / "NC_2024-06-01.csv"
    pd.DataFrame(
        {
            "image_id": ["IMG_0001", "IMG_0001"],
            "bounding_box_id": [0, 1],
            "classname": ["weed", "weed"],
            "conf": [0.95, 0.90],
            "xmin": [0.1, 0.4],
            "ymin": [0.1, 0.4],
            "xmax": [0.2, 0.5],
            "ymax": [0.2, 0.5],
        }
    ).to_csv(path, index=False)
    return path


def test_assign_spatial_within(geo_csv, shapefile):
    """Centroids inside a zone polygon get that zone's species and spatial_join method."""
    result = assign_spatial(str(geo_csv), str(shapefile))

    # test bbox 0 and 1
    inside = result[result["bounding_box_id"].isin([0, 1])]

    # validate species ids
    assert set(inside["species_id"]) == {"ABUTH", "BETVU"}

    # validate spaital join
    assert (inside["assignment_method"] == "spatial_join").all()


def test_assign_spatial_nearest_fallback(geo_csv, shapefile):
    """Centroid outside all zone polygons falls back to the nearest polygon."""
    result = assign_spatial(str(geo_csv), str(shapefile))

    # test bbox 2
    outside = result[result["bounding_box_id"] == 2]
    assert outside["species_id"].notna().all()
    assert (outside["assignment_method"] == "nearest_polygon").all()


def test_assign_monoculture_sets_species_and_method(det_csv):
    """Monoculture -> detections receive given species code and monoculture_config method."""
    result = assign_monoculture(str(det_csv), "BETVU")

    assert (result["species_id"] == "BETVU").all()
    assert (result["assignment_method"] == "monoculture_config").all()


def test_assign_monoculture_no_world_columns(det_csv):
    """Monoculture output contains no world coordinate columns."""
    result = assign_monoculture(str(det_csv), "BETVU")

    # no "world_" columns
    world_cols = [c for c in result.columns if c.startswith("world_")]
    assert world_cols == [], f"unexpected world columns: {world_cols}"
