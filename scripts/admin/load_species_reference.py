#!/usr/bin/env python3
"""
scripts/admin/load_species_reference.py
=========================================

Load species_info.json / cultivars.json / colors.py / cultivar_colors.py
into the species / cultivars reference tables of the pipeline SQLite DB
(schemas/sqlite/pipeline.sql), then regenerate the flat
species_catalog.generated.json that stages/det_to_world reads (see
orchestrator/species_catalog.py — stages never open a DB connection).

Idempotent: each run replaces the full contents of all six reference
tables, so the DB always mirrors the current source files exactly
(including removals).

colors.py / cultivar_colors.py are read as data (their top-level hex/rgb
list literals are extracted with ast.literal_eval), never imported/executed
— that avoids needing their `colors`/`pandas` imports to succeed and skips
running the uniqueness asserts those files use at import time; the same
uniqueness check is done here instead, against the DB.

Usage
-----
python scripts/admin/load_species_reference.py \\
    --db /project/dash_agir/globus_index/globus_file_index.sqlite3
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from orchestrator.sqlite_db import open_db
from orchestrator.species_catalog import export_catalog

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "sqlite" / "pipeline.sql"
DEFAULT_SPECIES_DIR = Path("/project/dash_agir/semifield-utils/species_information")

_SPECIES_OPTIONAL_FIELDS = (
    "EPPO", "group", "class", "subclass", "order", "family", "genus",
    "species", "common_name", "authority", "growth_habit", "duration",
    "collection_location", "category", "collection_timing", "link", "note",
)
_CULTIVAR_OPTIONAL_FIELDS = (
    "cultivar_name", "line_name", "collection_location",
    "cultivar_category", "link", "note",
)


def _hex_to_rgb(hex_value: str) -> Tuple[int, int, int]:
    h = hex_value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _extract_list_literal(path: Path, name: str) -> List:
    """Pull a top-level `name = [...]` list literal out of a .py file without
    executing it (avoids needing the file's own imports to succeed and skips
    its import-time asserts — the loader does its own validation instead).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise ValueError(f"No top-level assignment named {name!r} found in {path}")


def _parse_species_info(species_json: Dict[str, Any]) -> Tuple[List[Tuple], List[Tuple], List[Tuple]]:
    """Flatten species_info.json's "species" block into species /
    species_aliases / species_multi_symbols rows.
    """
    species_rows: List[Tuple] = []
    alias_rows: List[Tuple] = []
    multi_symbol_rows: List[Tuple] = []

    for species_key, fields in species_json["species"].items():
        class_id = fields["class_id"]
        hex_value = fields["hex"]
        r, g, b = fields["rgb"]
        if _hex_to_rgb(hex_value) != (r, g, b):
            raise ValueError(
                f"species_info.json[{species_key!r}]: hex {hex_value!r} "
                f"does not match rgb {fields['rgb']!r}"
            )

        optional = [fields.get(f) for f in _SPECIES_OPTIONAL_FIELDS]
        species_rows.append((
            class_id, species_key, fields["USDA_symbol"], *optional,
            hex_value, r, g, b,
        ))

        for alias in fields.get("alias") or []:
            alias_rows.append((class_id, alias))

        for component in fields.get("multi_species_USDA_symbol") or []:
            multi_symbol_rows.append((class_id, component))

    return species_rows, alias_rows, multi_symbol_rows


def _parse_cultivars(cultivars_json: Dict[str, Any]) -> Tuple[List[Tuple], List[Tuple]]:
    """Flatten cultivars.json into cultivars / cultivar_aliases rows."""
    cultivar_rows: List[Tuple] = []
    alias_rows: List[Tuple] = []

    for cultivar_key, fields in cultivars_json.items():
        cultivar_class_id = fields["cultivar_class_id"]
        hex_value = fields["hex"]
        r, g, b = fields["rgb"]
        if _hex_to_rgb(hex_value) != (r, g, b):
            raise ValueError(
                f"cultivars.json[{cultivar_key!r}]: hex {hex_value!r} "
                f"does not match rgb {fields['rgb']!r}"
            )

        optional = [fields.get(f) for f in _CULTIVAR_OPTIONAL_FIELDS]
        cultivar_rows.append((
            cultivar_class_id, cultivar_key, fields["parent_USDA_symbol"],
            fields["entity_type"], fields["display_name"],
            optional[0], optional[1],
            1 if fields.get("registered") else 0,
            *optional[2:],
            hex_value, r, g, b,
        ))

        for alias in fields.get("alias") or []:
            alias_rows.append((cultivar_class_id, alias))

    return cultivar_rows, alias_rows


def _validate(species_rows: List[Tuple], cultivar_rows: List[Tuple]) -> None:
    class_ids = [row[0] for row in species_rows]
    if len(class_ids) != len(set(class_ids)):
        raise ValueError("Duplicate class_id values in species_info.json")

    usda_symbols = {row[2] for row in species_rows}

    cultivar_class_ids = [row[0] for row in cultivar_rows]
    if len(cultivar_class_ids) != len(set(cultivar_class_ids)):
        raise ValueError("Duplicate cultivar_class_id values in cultivars.json")

    missing_parents = {row[2] for row in cultivar_rows} - usda_symbols
    if missing_parents:
        raise ValueError(
            f"cultivars.json parent_USDA_symbol(s) not found in species_info.json: {missing_parents}"
        )

    species_colors = {(row[-3], row[-2], row[-1]) for row in species_rows}
    cultivar_colors = {(row[-3], row[-2], row[-1]) for row in cultivar_rows}
    collisions = species_colors & cultivar_colors
    if collisions:
        raise ValueError(f"(r, g, b) collisions between species and cultivars: {collisions}")


def _build_color_palette_rows(
    palette: str, hex_list: List[str], rgb_list: List[List[int]], assigned_rows: List[Tuple]
) -> List[Tuple[str, int, str, int, int, int, Any]]:
    """One row per color-pool entry; assigned_id is the class_id/cultivar_class_id
    of whichever *assigned_rows* entry currently uses that (r, g, b), else None.
    """
    if len(hex_list) != len(rgb_list):
        raise ValueError(
            f"{palette} color pool: hex/rgb length mismatch ({len(hex_list)} vs {len(rgb_list)})"
        )
    assigned_by_rgb = {(row[-3], row[-2], row[-1]): row[0] for row in assigned_rows}

    rows = []
    for seq_index, (hex_value, rgb_value) in enumerate(zip(hex_list, rgb_list)):
        r, g, b = rgb_value
        if _hex_to_rgb(hex_value) != (r, g, b):
            raise ValueError(
                f"{palette} color pool index {seq_index}: hex {hex_value!r} "
                f"does not match rgb {rgb_value!r}"
            )
        rows.append((palette, seq_index, hex_value, r, g, b, assigned_by_rgb.get((r, g, b))))
    return rows


def load_species_reference(
    conn: sqlite3.Connection,
    species_json: Dict[str, Any],
    cultivars_json: Dict[str, Any],
    species_hex: List[str],
    species_rgb: List[List[int]],
    cultivar_hex: List[str],
    cultivar_rgb: List[List[int]],
) -> Dict[str, int]:
    """Replace the contents of all six species/cultivar reference tables."""
    species_rows, species_alias_rows, species_multi_rows = _parse_species_info(species_json)
    cultivar_rows, cultivar_alias_rows = _parse_cultivars(cultivars_json)
    _validate(species_rows, cultivar_rows)

    species_palette = _build_color_palette_rows("species", species_hex, species_rgb, species_rows)
    cultivar_palette = _build_color_palette_rows("cultivar", cultivar_hex, cultivar_rgb, cultivar_rows)
    palette_rows = (
        [(p, i, h, r, g, b, aid, None) for (p, i, h, r, g, b, aid) in species_palette]
        + [(p, i, h, r, g, b, None, aid) for (p, i, h, r, g, b, aid) in cultivar_palette]
    )

    try:
        conn.execute("BEGIN IMMEDIATE")
        # Children before parents, so FK constraints never trip mid-replace.
        conn.execute("DELETE FROM color_palette")
        conn.execute("DELETE FROM cultivar_aliases")
        conn.execute("DELETE FROM cultivars")
        conn.execute("DELETE FROM species_multi_symbols")
        conn.execute("DELETE FROM species_aliases")
        conn.execute("DELETE FROM species")

        conn.executemany(
            """
            INSERT INTO species (
                class_id, species_key, USDA_symbol, EPPO, species_group,
                taxon_class, subclass, taxon_order, family, genus,
                species_epithet, common_name, authority, growth_habit,
                duration, collection_location, category, collection_timing,
                link, note, hex, r, g, b
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            species_rows,
        )
        conn.executemany(
            "INSERT INTO species_aliases (class_id, alias) VALUES (?, ?)",
            species_alias_rows,
        )
        conn.executemany(
            "INSERT INTO species_multi_symbols (class_id, component_usda_symbol) VALUES (?, ?)",
            species_multi_rows,
        )
        conn.executemany(
            """
            INSERT INTO cultivars (
                cultivar_class_id, cultivar_key, parent_USDA_symbol, entity_type,
                display_name, cultivar_name, line_name, registered,
                collection_location, cultivar_category, link, note, hex, r, g, b
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            cultivar_rows,
        )
        conn.executemany(
            "INSERT INTO cultivar_aliases (cultivar_class_id, alias) VALUES (?, ?)",
            cultivar_alias_rows,
        )
        conn.executemany(
            """
            INSERT INTO color_palette (
                palette, seq_index, hex, r, g, b,
                assigned_class_id, assigned_cultivar_class_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            palette_rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "species": len(species_rows),
        "species_aliases": len(species_alias_rows),
        "species_multi_symbols": len(species_multi_rows),
        "cultivars": len(cultivar_rows),
        "cultivar_aliases": len(cultivar_alias_rows),
        "color_palette": len(palette_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load species/cultivar reference data into the pipeline SQLite DB."
    )
    parser.add_argument(
        "--db", required=True, type=Path,
        help="Path to the SQLite pipeline database.",
    )
    parser.add_argument(
        "--species-json", default=DEFAULT_SPECIES_DIR / "species_info.json", type=Path,
    )
    parser.add_argument(
        "--cultivars-json", default=DEFAULT_SPECIES_DIR / "cultivars.json", type=Path,
    )
    parser.add_argument(
        "--colors-py", default=DEFAULT_SPECIES_DIR / "colors.py", type=Path,
    )
    parser.add_argument(
        "--cultivar-colors-py", default=DEFAULT_SPECIES_DIR / "cultivar_colors.py", type=Path,
    )
    parser.add_argument(
        "--catalog-out", default=DEFAULT_SPECIES_DIR / "species_catalog.generated.json", type=Path,
        help="Where to (re)write the flat catalog stages/det_to_world reads.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    species_json = json.loads(args.species_json.read_text(encoding="utf-8"))
    cultivars_json = json.loads(args.cultivars_json.read_text(encoding="utf-8"))
    species_hex = _extract_list_literal(args.colors_py, "hex")
    species_rgb = _extract_list_literal(args.colors_py, "rgb")
    cultivar_hex = _extract_list_literal(args.cultivar_colors_py, "cultivar_hex")
    cultivar_rgb = _extract_list_literal(args.cultivar_colors_py, "cultivar_rgb")

    conn = open_db(args.db)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        counts = load_species_reference(
            conn, species_json, cultivars_json,
            species_hex, species_rgb, cultivar_hex, cultivar_rgb,
        )
        logger.info("Loaded %s into %s", counts, args.db)

        catalog = export_catalog(conn, args.catalog_out)
        logger.info(
            "Exported catalog (%d species, %d cultivars) to %s",
            len(catalog["species"]), len(catalog["cultivars"]), args.catalog_out,
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
