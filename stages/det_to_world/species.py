"""
Species assignment logic (folded in from the former assign_species stage).
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# Detections farther than this from every zone polygon (in the shapefile's
# own CRS units — meters, for the UTM shapefiles this pipeline uses) fail
# the batch instead of silently taking the nearest polygon's species. A
# normal boundary case (point just outside a polygon edge due to rounding)
# lands within a meter or two; anything past this is more likely a wrong
# shapefile, a georeferencing problem, or a real survey gap that needs a
# person to look at.
DEFAULT_MAX_NEAREST_DISTANCE_M = 5.0


class SpeciesAssignmentError(Exception):
    """Base class for species-assignment failures that should fail the batch."""


class ZoneTooFarError(SpeciesAssignmentError):
    """Raised when a detection has no zone polygon within max_nearest_distance_m."""


class UnknownSpeciesCodeError(SpeciesAssignmentError):
    """Raised when a species_id/cultivar_id assigned from the shapefile has no
    matching entry in the species catalog."""


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


def assign_spatial(
    dets: pd.DataFrame,
    shapefile: str,
    max_nearest_distance_m: float = DEFAULT_MAX_NEAREST_DISTANCE_M,
) -> pd.DataFrame:
    """Assign species to detections via spatial join against a zone shapefile.

    See _OPTIONAL_ZONE_ATTRS for the extra per-zone columns carried through
    when present in the shapefile (species_name, cultivar_id/cultivar_name,
    class_id).

    Detections outside every zone polygon fall back to the nearest one, but
    only within max_nearest_distance_m — see DEFAULT_MAX_NEAREST_DISTANCE_M.
    Raises ZoneTooFarError if any detection's nearest zone is farther than that.
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
        nearest = gpd.sjoin_nearest(
            gdf[unmatched], zones[zone_columns], how="left", distance_col="nearest_distance_m"
        )
        # sjoin_nearest returns one row per tied nearest neighbor (e.g. a point
        # equidistant from two zones), so it can return more rows than inputs —
        # keep only the first match per point so the assignment aligns 1:1.
        nearest = nearest[~nearest.index.duplicated(keep="first")]

        too_far = nearest[nearest["nearest_distance_m"] > max_nearest_distance_m]
        if not too_far.empty:
            offenders = ", ".join(
                f"{row.image_id}:{row.bounding_box_id} ({row.nearest_distance_m:.2f}m)"
                for row in too_far.itertuples()
            )
            raise ZoneTooFarError(
                f"{len(too_far)} detection(s) fall more than {max_nearest_distance_m}m outside "
                f"every zone polygon in {shapefile}: {offenders}"
            )

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


# species_catalog.generated.json fields to carry onto every row, renamed with
# a species_/cultivar_ prefix so they can't collide with the shapefile-sourced
# columns above (species_name, cultivar_id, cultivar_name, class_id) — those
# stay as the season's own human-curated labels; these are the canonical
# reference-data columns, namespaced so a consumer can tell the two apart and
# pick whichever it wants (mirrors the existing cultivar-over-species
# fallback in scripts/job/visualize.py's _detection_name_label).
_SPECIES_CATALOG_FIELDS = {
    "common_name": "species_common_name",
    "family": "species_family",
    "genus": "species_genus",
    "growth_habit": "species_growth_habit",
    "category": "species_category",
    "hex": "species_hex",
    "r": "species_r",
    "g": "species_g",
    "b": "species_b",
}
_CULTIVAR_CATALOG_FIELDS = {
    "display_name": "cultivar_display_name",
    "line_name": "cultivar_line_name",
    "registered": "cultivar_registered",
    "hex": "cultivar_hex",
    "r": "cultivar_r",
    "g": "cultivar_g",
    "b": "cultivar_b",
}


def load_catalog(path: str | Path) -> Dict[str, Any]:
    """Load species_catalog.generated.json (see orchestrator/species_catalog.py).

    Plain file read — never opens a DB connection. Stages must stay DB-free;
    the DB-derived flat file is the only reference data they consume.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def enrich_with_catalog(dets: pd.DataFrame, catalog: Dict[str, Any]) -> pd.DataFrame:
    """Attach species/cultivar reference columns (see _SPECIES_CATALOG_FIELDS /
    _CULTIVAR_CATALOG_FIELDS) by looking up species_id (USDA_symbol) and, when
    present, cultivar_id (cultivar_class_id) in *catalog*.

    A species_id/cultivar_id with no catalog entry (e.g. a shapefile code that
    predates the current reference data, or a zone shapefile out of sync with
    the reference tables) means the zone data and the catalog have drifted
    apart — that needs a person to reconcile, so this raises
    UnknownSpeciesCodeError rather than silently blanking the enrichment
    columns.
    """
    dets = dets.copy()
    species_catalog = catalog.get("species", {})
    cultivar_catalog = catalog.get("cultivars", {})

    unmatched_species = set(dets["species_id"].dropna().unique()) - set(species_catalog)
    if unmatched_species:
        message = f"species_catalog has no entry for species_id(s): {sorted(unmatched_species)}"
        logger.error(message)
        raise UnknownSpeciesCodeError(message)

    for src_field, out_col in _SPECIES_CATALOG_FIELDS.items():
        dets[out_col] = dets["species_id"].map(
            lambda species_id: species_catalog.get(species_id, {}).get(src_field)
        )

    if "cultivar_id" in dets.columns:
        present_cultivar_ids = set(dets["cultivar_id"].dropna().unique())
        unmatched_cultivars = present_cultivar_ids - set(cultivar_catalog)
        if unmatched_cultivars:
            message = f"species_catalog has no entry for cultivar_id(s): {sorted(unmatched_cultivars)}"
            logger.error(message)
            raise UnknownSpeciesCodeError(message)
        for src_field, out_col in _CULTIVAR_CATALOG_FIELDS.items():
            dets[out_col] = dets["cultivar_id"].map(
                lambda cultivar_id: (
                    cultivar_catalog.get(cultivar_id, {}).get(src_field)
                    if pd.notna(cultivar_id) else None
                )
            )

    return dets
