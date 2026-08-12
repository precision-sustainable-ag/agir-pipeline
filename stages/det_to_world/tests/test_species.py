import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from stages.det_to_world.species import (
    UnknownSpeciesCodeError,
    ZoneTooFarError,
    assign_monoculture,
    assign_spatial,
    enrich_with_catalog,
    load_catalog,
)

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
def common_name_shapefile(tmp_path):
    """Two zone polygons with a common-name attribute (e.g. cover_crops_2025_2026)."""
    zones = gpd.GeoDataFrame(
        {
            "species": ["ABUTH", "BETVU"],
            "comm_name": ["Velvetleaf", "Sugarbeet"],
        },
        geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30)],
        crs=ZONE_CRS,
    )
    path = tmp_path / "common_name_zones.shp"
    zones.to_file(path)
    return path


@pytest.fixture
def class_id_shapefile(tmp_path):
    """Zone polygons that also carry a class_id attribute, alongside comm_name."""
    zones = gpd.GeoDataFrame(
        {
            "species": ["ABUTH", "BETVU"],
            "comm_name": ["Velvetleaf", "Sugarbeet"],
            "class_id": ["75", "76"],
        },
        geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30)],
        crs=ZONE_CRS,
    )
    path = tmp_path / "class_id_zones.shp"
    zones.to_file(path)
    return path


@pytest.fixture
def cultivar_shapefile(tmp_path):
    """Two zone polygons, same species, distinct cultivars (e.g. peanuts_2026)."""
    zones = gpd.GeoDataFrame(
        {
            "species": ["ARHY", "ARHY"],
            "cultc_id": ["107", "102"],
            "disp_name": ["Peanut - EXP-OLEIC-001", "Peanut - TifNV-HG"],
        },
        geometry=[box(0, 0, 10, 10), box(20, 20, 30, 30)],
        crs=ZONE_CRS,
    )
    path = tmp_path / "cultivar_zones.shp"
    zones.to_file(path)
    return path


@pytest.fixture
def geo_df():
    """Three detections: two inside zones, one just outside both (~2.8m from the
    nearest zone 1 corner, well within the default nearest-fallback threshold)."""
    return pd.DataFrame(
        {
            "image_id": ["IMG_0001", "IMG_0001", "IMG_0001"],
            "bounding_box_id": [0, 1, 2],
            "classname": ["weed", "weed", "weed"],
            "conf": [0.95, 0.90, 0.85],
            "xmin": [0.1, 0.4, 0.7],
            "ymin": [0.1, 0.4, 0.7],
            "xmax": [0.2, 0.5, 0.8],
            "ymax": [0.2, 0.5, 0.8],
            # inside zone 1, inside zone 2, just outside both (nearest: zone 1)
            "world_centroid_x": [5.0, 25.0, 12.0],
            "world_centroid_y": [5.0, 25.0, 12.0],
            "world_tl_x": [4.0, 24.0, 11.0],
            "world_tl_y": [4.0, 24.0, 11.0],
            "world_tr_x": [6.0, 26.0, 13.0],
            "world_tr_y": [4.0, 24.0, 11.0],
            "world_bl_x": [4.0, 24.0, 11.0],
            "world_bl_y": [6.0, 26.0, 13.0],
            "world_br_x": [6.0, 26.0, 13.0],
            "world_br_y": [6.0, 26.0, 13.0],
            "crs": [ZONE_CRS, ZONE_CRS, ZONE_CRS],
        }
    )


@pytest.fixture
def far_geo_df():
    """Single detection ~99m from the nearest zone polygon — beyond any
    reasonable nearest-fallback threshold."""
    return pd.DataFrame(
        {
            "image_id": ["IMG_0001"],
            "bounding_box_id": [0],
            "classname": ["weed"],
            "conf": [0.95],
            "xmin": [0.1],
            "ymin": [0.1],
            "xmax": [0.2],
            "ymax": [0.2],
            "world_centroid_x": [100.0],
            "world_centroid_y": [100.0],
            "world_tl_x": [99.0],
            "world_tl_y": [99.0],
            "world_tr_x": [101.0],
            "world_tr_y": [99.0],
            "world_bl_x": [99.0],
            "world_bl_y": [101.0],
            "world_br_x": [101.0],
            "world_br_y": [101.0],
            "crs": [ZONE_CRS],
        }
    )


@pytest.fixture
def det_df():
    """Basic detection rows as produced by jpg_to_det — no world coordinate columns."""
    return pd.DataFrame(
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
    )


def test_assign_spatial_within(geo_df, shapefile):
    """Centroids inside a zone polygon get that zone's species and spatial_join method."""
    result = assign_spatial(geo_df, str(shapefile))

    # test bbox 0 and 1
    inside = result[result["bounding_box_id"].isin([0, 1])]

    # validate species ids
    assert set(inside["species_id"]) == {"ABUTH", "BETVU"}

    # validate spaital join
    assert (inside["assignment_method"] == "spatial_join").all()


def test_assign_spatial_nearest_fallback(geo_df, shapefile):
    """Centroid outside all zone polygons falls back to the nearest polygon."""
    result = assign_spatial(geo_df, str(shapefile))

    # test bbox 2
    outside = result[result["bounding_box_id"] == 2]
    assert outside["species_id"].notna().all()
    assert (outside["assignment_method"] == "nearest_polygon").all()


def test_assign_spatial_raises_when_nearest_zone_beyond_default_threshold(far_geo_df, shapefile):
    """A detection ~99m from every zone polygon exceeds the 5m default and raises."""
    with pytest.raises(ZoneTooFarError, match="IMG_0001:0"):
        assign_spatial(far_geo_df, str(shapefile))


def test_assign_spatial_raises_when_nearest_zone_beyond_custom_threshold(geo_df, shapefile):
    """A custom, stricter max_nearest_distance_m can flag a point normally within tolerance."""
    with pytest.raises(ZoneTooFarError, match="IMG_0001:2"):
        assign_spatial(geo_df, str(shapefile), max_nearest_distance_m=1.0)


def test_assign_spatial_nearest_fallback_within_custom_threshold(far_geo_df, shapefile):
    """Raising max_nearest_distance_m lets a farther point still fall back normally."""
    result = assign_spatial(far_geo_df, str(shapefile), max_nearest_distance_m=200.0)

    assert result.loc[0, "species_id"] == "BETVU"
    assert result.loc[0, "assignment_method"] == "nearest_polygon"


def test_assign_spatial_no_cultivar_columns_when_shapefile_lacks_them(geo_df, shapefile):
    """Shapefiles without cultc_id (most seasons) produce no cultivar columns at all."""
    result = assign_spatial(geo_df, str(shapefile))

    cultivar_cols = [c for c in result.columns if c.startswith("cultivar_")]
    assert cultivar_cols == [], f"unexpected cultivar columns: {cultivar_cols}"


def test_assign_spatial_within_assigns_species_name(geo_df, common_name_shapefile):
    """Zones with a comm_name attribute get species_name alongside species_id."""
    result = assign_spatial(geo_df, str(common_name_shapefile))

    inside = result[result["bounding_box_id"].isin([0, 1])]
    assert set(inside["species_name"]) == {"Velvetleaf", "Sugarbeet"}
    # comm_name shapefiles never have cultivar columns
    assert "cultivar_id" not in result.columns
    assert "cultivar_name" not in result.columns


def test_assign_spatial_carries_class_id_when_present(geo_df, class_id_shapefile):
    """class_id is joined through independently of species_name/cultivar."""
    result = assign_spatial(geo_df, str(class_id_shapefile))

    inside = result[result["bounding_box_id"].isin([0, 1])]
    assert set(inside["class_id"]) == {"75", "76"}


def test_assign_spatial_no_class_id_column_when_shapefile_lacks_it(geo_df, shapefile):
    result = assign_spatial(geo_df, str(shapefile))
    assert "class_id" not in result.columns


def test_assign_spatial_within_assigns_cultivar(geo_df, cultivar_shapefile):
    """Zones with a cultc_id attribute get cultivar_id/cultivar_name alongside species_id."""
    result = assign_spatial(geo_df, str(cultivar_shapefile))

    inside = result[result["bounding_box_id"].isin([0, 1])]
    assert set(inside["species_id"]) == {"ARHY"}
    assert set(inside["cultivar_id"]) == {"107", "102"}
    assert set(inside["cultivar_name"]) == {"Peanut - EXP-OLEIC-001", "Peanut - TifNV-HG"}
    # cultivar shapefiles never have a comm_name column
    assert "species_name" not in result.columns


def test_assign_spatial_nearest_fallback_assigns_cultivar(geo_df, cultivar_shapefile):
    """Nearest-polygon fallback carries cultivar columns too, not just species."""
    result = assign_spatial(geo_df, str(cultivar_shapefile))

    outside = result[result["bounding_box_id"] == 2]
    assert (outside["assignment_method"] == "nearest_polygon").all()
    assert outside["cultivar_id"].notna().all()
    assert outside["cultivar_name"].notna().all()


def test_assign_monoculture_sets_species_and_method(det_df):
    """Monoculture -> detections receive given species code and monoculture_config method."""
    result = assign_monoculture(det_df, "BETVU")

    assert (result["species_id"] == "BETVU").all()
    assert (result["assignment_method"] == "monoculture_config").all()


def test_assign_monoculture_no_world_columns(det_df):
    """Monoculture output contains no world coordinate columns."""
    result = assign_monoculture(det_df, "BETVU")

    # no "world_" columns
    world_cols = [c for c in result.columns if c.startswith("world_")]
    assert world_cols == [], f"unexpected world columns: {world_cols}"


@pytest.fixture
def catalog():
    """Minimal species_catalog.generated.json-shaped dict (see orchestrator/species_catalog.py)."""
    return {
        "species": {
            "ARHY": {
                "common_name": "peanut",
                "family": "Fabaceae",
                "genus": "Arachis",
                "growth_habit": "forb/herb",
                "category": "cash crop",
                "hex": "#a5482f",
                "r": 165,
                "g": 72,
                "b": 47,
            },
        },
        "cultivars": {
            "107": {
                "display_name": "Peanut - EXP-OLEIC-001",
                "line_name": "oleic",
                "registered": 0,
                "hex": "#f105f2",
                "r": 241,
                "g": 5,
                "b": 242,
            },
            "102": {
                "display_name": "Peanut - TifNV-HG",
                "line_name": "high-oleic",
                "registered": 1,
                "hex": "#0f52f1",
                "r": 15,
                "g": 82,
                "b": 241,
            },
        },
    }


def test_load_catalog_reads_json_file(tmp_path):
    path = tmp_path / "species_catalog.generated.json"
    path.write_text(json.dumps({"species": {}, "cultivars": {}}), encoding="utf-8")

    assert load_catalog(path) == {"species": {}, "cultivars": {}}


def test_enrich_with_catalog_adds_species_columns(catalog):
    dets = pd.DataFrame({"species_id": ["ARHY"], "assignment_method": ["monoculture_config"]})

    result = enrich_with_catalog(dets, catalog)

    assert result.loc[0, "species_common_name"] == "peanut"
    assert result.loc[0, "species_family"] == "Fabaceae"
    assert (result.loc[0, "species_r"], result.loc[0, "species_g"], result.loc[0, "species_b"]) == (165, 72, 47)


def test_enrich_with_catalog_raises_on_unmatched_species(catalog):
    dets = pd.DataFrame({"species_id": ["UNKNOWN_CODE"], "assignment_method": ["monoculture_config"]})

    with pytest.raises(UnknownSpeciesCodeError, match="UNKNOWN_CODE"):
        enrich_with_catalog(dets, catalog)


def test_enrich_with_catalog_raises_on_unmatched_cultivar(catalog):
    dets = pd.DataFrame({
        "species_id": ["ARHY"],
        "cultivar_id": ["UNKNOWN_CULTIVAR"],
    })

    with pytest.raises(UnknownSpeciesCodeError, match="UNKNOWN_CULTIVAR"):
        enrich_with_catalog(dets, catalog)


def test_enrich_with_catalog_adds_cultivar_columns_only_when_present(catalog):
    dets = pd.DataFrame({
        "species_id": ["ARHY", "ARHY"],
        "cultivar_id": ["107", None],
    })

    result = enrich_with_catalog(dets, catalog)

    assert result.loc[0, "cultivar_display_name"] == "Peanut - EXP-OLEIC-001"
    assert pd.isna(result.loc[1, "cultivar_display_name"])


def test_enrich_with_catalog_no_cultivar_columns_when_shapefile_lacked_them(catalog):
    dets = pd.DataFrame({"species_id": ["ARHY"]})

    result = enrich_with_catalog(dets, catalog)

    cultivar_cols = [c for c in result.columns if c.startswith("cultivar_")]
    assert cultivar_cols == []


def test_enrich_with_catalog_after_assign_spatial(geo_df, cultivar_shapefile, catalog):
    """End-to-end: assign_spatial's cultivar_id feeds straight into enrichment."""
    assigned = assign_spatial(geo_df, str(cultivar_shapefile))

    result = enrich_with_catalog(assigned, catalog)

    exp_oleic_rows = result[result["cultivar_id"] == "107"]
    assert (exp_oleic_rows["cultivar_display_name"] == "Peanut - EXP-OLEIC-001").all()
    assert (exp_oleic_rows["species_common_name"] == "peanut").all()
