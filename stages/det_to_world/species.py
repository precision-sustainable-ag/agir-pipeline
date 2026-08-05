"""
Species assignment logic (folded in from the former assign_species stage).
"""

import geopandas as gpd
import pandas as pd


# Optional per-zone attributes, joined in and renamed only when the
# shapefile's own columns actually have them — a shapefile missing any of
# these produces no corresponding output column, same as before any of
# them existed. comm_name and cultc_id/disp_name are mutually exclusive in
# practice (ordinary species zones vs. cultivar seasons, where every zone
# is the same species but a different cultivar); class_id has shown up in
# every zone shapefile checked so far but isn't assumed universal.
_OPTIONAL_ZONE_ATTRS = {
    "comm_name": "species_name",
    "cultc_id": "cultivar_id",
    "disp_name": "cultivar_name",
    "class_id": "class_id",
}


def assign_spatial(dets: pd.DataFrame, shapefile: str) -> pd.DataFrame:
    """Assign species to detections via spatial join against a zone shapefile.

    See _OPTIONAL_ZONE_ATTRS for the extra per-zone columns carried through
    when present in the shapefile (species_name, cultivar_id/cultivar_name,
    class_id).
    """
    zones = gpd.read_file(shapefile)

    gdf = gpd.GeoDataFrame(
        dets,
        geometry=gpd.points_from_xy(dets.world_centroid_x, dets.world_centroid_y),
        crs=dets["crs"].iloc[0],
    ).to_crs(zones.crs)

    present_attrs = [column for column in _OPTIONAL_ZONE_ATTRS if column in zones.columns]
    zone_columns = ["species", "geometry"] + present_attrs

    # perform a spatial join... "does this point fall inside this polygon?"
    joined = gpd.sjoin(gdf, zones[zone_columns], how="left", predicate="within")
    joined["assignment_method"] = "spatial_join"

    # if any unmatched detections, assign nearest polygon's species (and
    # whatever optional attrs are present) as fallback
    unmatched = joined["species"].isna()
    if unmatched.any():
        nearest = gpd.sjoin_nearest(gdf[unmatched], zones[zone_columns], how="left")
        # sjoin_nearest returns one row per tied nearest neighbor (e.g. a point
        # equidistant from two zones), so it can return more rows than inputs —
        # keep only the first match per point so the assignment aligns 1:1.
        nearest = nearest[~nearest.index.duplicated(keep="first")]
        joined.loc[nearest.index, "species"] = nearest["species"]
        joined.loc[nearest.index, "assignment_method"] = "nearest_polygon"
        for column in present_attrs:
            joined.loc[nearest.index, column] = nearest[column]

    rename_map = {"species": "species_id", **{c: _OPTIONAL_ZONE_ATTRS[c] for c in present_attrs}}
    return joined.rename(columns=rename_map)


def assign_monoculture(dets: pd.DataFrame, species_code: str) -> pd.DataFrame:
    """Assign a single species code to all detections (monoculture batch, no spatial join)."""
    dets = dets.copy()
    dets["species_id"] = species_code
    dets["assignment_method"] = "monoculture_config"
    return dets
