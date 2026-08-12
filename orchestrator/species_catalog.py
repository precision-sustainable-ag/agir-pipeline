"""
orchestrator/species_catalog.py
=================================

Exports the species / cultivars reference tables (schemas/sqlite/pipeline.sql)
into a flat, stage-consumable JSON file.

The species/cultivars tables are the source of truth (loaded by
scripts/admin/load_species_reference.py from
/project/dash_agir/semifield-utils/species_information/), but
stages/det_to_world must never open a DB connection. This module is the one
piece of pipeline code that queries those tables on det_to_world's behalf:
load_species_reference.py calls export_catalog() right after loading the DB,
and det_to_world reads the resulting flat file directly (see
stages/det_to_world/species.py's load_catalog()).

Public API
----------
build_catalog(conn)            -> dict   in-memory catalog, keyed for lookup
export_catalog(conn, out_path) -> dict   build_catalog() + atomic write to disk

Catalog shape
-------------
{
  "species":   {<USDA_symbol>: {...columns, "alias": [...]}, ...},
  "cultivars": {<str(cultivar_class_id)>: {...columns, "alias": [...]}, ...},
}

Keys match the zone shapefile attributes det_to_world joins against:
species by USDA_symbol ("species" column), cultivars by cultivar_class_id
("cultc_id" column, stringified for JSON).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List


def _aliases_by_key(rows: List[sqlite3.Row], key_col: str) -> Dict[int, List[str]]:
    aliases: Dict[int, List[str]] = {}
    for row in rows:
        aliases.setdefault(row[key_col], []).append(row["alias"])
    return aliases


def build_catalog(conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Query species/cultivars (+ aliases) into the flat lookup dict described above."""
    species_aliases = _aliases_by_key(
        conn.execute("SELECT class_id, alias FROM species_aliases").fetchall(),
        "class_id",
    )
    species = {}
    for row in conn.execute("SELECT * FROM species").fetchall():
        entry = dict(row)
        entry["alias"] = species_aliases.get(row["class_id"], [])
        species[row["USDA_symbol"]] = entry

    cultivar_aliases = _aliases_by_key(
        conn.execute("SELECT cultivar_class_id, alias FROM cultivar_aliases").fetchall(),
        "cultivar_class_id",
    )
    cultivars = {}
    for row in conn.execute("SELECT * FROM cultivars").fetchall():
        entry = dict(row)
        entry["alias"] = cultivar_aliases.get(row["cultivar_class_id"], [])
        cultivars[str(row["cultivar_class_id"])] = entry

    return {"species": species, "cultivars": cultivars}


def export_catalog(conn: sqlite3.Connection, out_path: str | Path) -> Dict[str, Dict]:
    """Build the catalog and atomically write it to *out_path* as JSON."""
    catalog = build_catalog(conn)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(out_path)
    return catalog
