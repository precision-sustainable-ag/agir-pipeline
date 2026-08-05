"""
Species assignment logic
"""

import geopandas as gpd
import pandas as pd


def assign_spatial(geo_csv: str, shapefile: str) -> pd.DataFrame:
    """Assign species to detections via spatial join against a zone shapefile."""
    dets = pd.read_csv(geo_csv)
    zones = gpd.read_file(shapefile)

    gdf = gpd.GeoDataFrame(
        dets,
        geometry=gpd.points_from_xy(dets.world_centroid_x, dets.world_centroid_y),
        crs=dets["crs"].iloc[0],
    ).to_crs(zones.crs)

    # perform a spatial join... "does this point fall inside this polygon?"
    joined = gpd.sjoin(gdf, zones[["species", "geometry"]], how="left", predicate="within")
    joined["assignment_method"] = "spatial_join"

    # if any unmatched detections, assign nearest polygon's species as fallback
    unmatched = joined["species"].isna()
    if unmatched.any():
        nearest = gpd.sjoin_nearest(gdf[unmatched], zones[["species", "geometry"]], how="left")
        joined.loc[unmatched, "species"] = nearest["species"].values
        joined.loc[unmatched, "assignment_method"] = "nearest_polygon"

    return joined.rename(columns={"species": "species_id"})


def assign_monoculture(det_csv: str, species_code: str) -> pd.DataFrame:
    """Assign a single species code to all detections (monoculture batch, no spatial join)."""
    dets = pd.read_csv(det_csv)
    dets["species_id"] = species_code
    dets["assignment_method"] = "monoculture_config"
    return dets
