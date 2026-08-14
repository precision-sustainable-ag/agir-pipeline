from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "load_species_reference.py"
_spec = importlib.util.spec_from_file_location("load_species_reference", _MODULE_PATH)
load_species_reference_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_species_reference_module)

_extract_list_literal = load_species_reference_module._extract_list_literal
_parse_species_info = load_species_reference_module._parse_species_info
_parse_cultivars = load_species_reference_module._parse_cultivars
_validate = load_species_reference_module._validate
load_species_reference = load_species_reference_module.load_species_reference


def make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "pipeline.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema = Path("schemas/sqlite/pipeline.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


SPECIES_JSON = {
    "species": {
        "ARHY": {
            "class_id": 75,
            "USDA_symbol": "ARHY",
            "EPPO": "ARHHY",
            "group": "dicot",
            "class": "Magnoliopsida",
            "subclass": "Rosidae",
            "order": "Fabales",
            "family": "Fabaceae",
            "genus": "Arachis",
            "species": "hypogaea",
            "common_name": "peanut",
            "authority": "Linnaeus",
            "growth_habit": "forb/herb",
            "duration": "perennial",
            "collection_location": None,
            "category": "cash crop",
            "collection_timing": None,
            "multi_species_USDA_symbol": None,
            "link": None,
            "note": None,
            "hex": "#a5482f",
            "rgb": [165, 72, 47],
            "alias": ["groundnut"],
        },
        "Brassica_complex_0": {
            "class_id": 33,
            "USDA_symbol": "BRASS2",
            "EPPO": "1BRSG",
            "group": "dicot",
            "class": "Magnoliopsida",
            "subclass": "Dilleniidae",
            "order": "Capparales",
            "family": "Brassicaceae",
            "genus": "Brassica",
            "species": "spp.",
            "common_name": "mustards",
            "authority": "Linnaeus",
            "growth_habit": "forb/herb",
            "duration": "annual biennial perennial",
            "collection_location": "TX",
            "category": "cool season cover crop",
            "collection_timing": "winter",
            "multi_species_USDA_symbol": ["BRNA", "BRRA"],
            "link": None,
            "note": None,
            "hex": "#e86928",
            "rgb": [232, 105, 40],
            "alias": [],
        },
    }
}

CULTIVARS_JSON = {
    "PEANUT_BAILEY_II": {
        "cultivar_class_id": 101,
        "parent_USDA_symbol": "ARHY",
        "entity_type": "cultivar",
        "display_name": "Peanut - Bailey II",
        "cultivar_name": "Bailey II",
        "line_name": None,
        "registered": True,
        "collection_location": "NC",
        "cultivar_category": "crop variety",
        "link": None,
        "note": None,
        "hex": "#03039e",
        "rgb": [3, 3, 158],
        "alias": [],
    }
}


def test_extract_list_literal(tmp_path: Path) -> None:
    py_file = tmp_path / "colors.py"
    py_file.write_text("hex = ['#ffffff', '#000000']\nrgb = [[255, 255, 255], [0, 0, 0]]\n")

    assert _extract_list_literal(py_file, "hex") == ["#ffffff", "#000000"]
    assert _extract_list_literal(py_file, "rgb") == [[255, 255, 255], [0, 0, 0]]


def test_extract_list_literal_missing_name(tmp_path: Path) -> None:
    py_file = tmp_path / "colors.py"
    py_file.write_text("hex = ['#ffffff']\n")

    with pytest.raises(ValueError, match="rgb"):
        _extract_list_literal(py_file, "rgb")


def test_parse_species_info_flattens_aliases_and_multi_symbols() -> None:
    species_rows, alias_rows, multi_symbol_rows = _parse_species_info(SPECIES_JSON)

    assert len(species_rows) == 2
    arhy_row = next(row for row in species_rows if row[2] == "ARHY")
    assert arhy_row[0] == 75  # class_id
    assert arhy_row[-3:] == (165, 72, 47)  # r, g, b

    assert alias_rows == [(75, "groundnut")]
    assert set(multi_symbol_rows) == {(33, "BRNA"), (33, "BRRA")}


def test_parse_species_info_rejects_hex_rgb_mismatch() -> None:
    bad = {"species": {**SPECIES_JSON["species"]}}
    bad["species"]["ARHY"] = {**bad["species"]["ARHY"], "rgb": [1, 2, 3]}

    with pytest.raises(ValueError, match="does not match"):
        _parse_species_info(bad)


def test_parse_cultivars_registered_bool_to_int() -> None:
    cultivar_rows, alias_rows = _parse_cultivars(CULTIVARS_JSON)

    assert len(cultivar_rows) == 1
    row = cultivar_rows[0]
    assert row[0] == 101  # cultivar_class_id
    assert row[3] == "cultivar"  # entity_type
    assert row[7] == 1  # registered, coerced from True
    assert alias_rows == []


def test_validate_detects_duplicate_class_id() -> None:
    species_rows = [
        (1, "A", "AAAA", *([None] * 17), "#000000", 0, 0, 0),
        (1, "B", "BBBB", *([None] * 17), "#111111", 1, 1, 1),
    ]
    with pytest.raises(ValueError, match="Duplicate class_id"):
        _validate(species_rows, [])


def test_validate_detects_missing_parent_symbol() -> None:
    species_rows = [(1, "A", "AAAA", *([None] * 17), "#000000", 0, 0, 0)]
    cultivar_rows = [
        (101, "CULT", "ZZZZ", "cultivar", "Cult", None, None, 1, None, None, None, None, "#111111", 1, 1, 1)
    ]
    with pytest.raises(ValueError, match="parent_USDA_symbol"):
        _validate(species_rows, cultivar_rows)


def test_validate_detects_species_cultivar_color_collision() -> None:
    species_rows = [(1, "A", "AAAA", *([None] * 17), "#a5482f", 165, 72, 47)]
    cultivar_rows = [
        (101, "CULT", "AAAA", "cultivar", "Cult", None, None, 1, None, None, None, None, "#a5482f", 165, 72, 47)
    ]
    with pytest.raises(ValueError, match="collisions"):
        _validate(species_rows, cultivar_rows)


def test_load_species_reference_end_to_end(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)

    species_hex = ["#a5482f", "#e86928", "#ffffff"]
    species_rgb = [[165, 72, 47], [232, 105, 40], [255, 255, 255]]
    cultivar_hex = ["#03039e", "#05f217"]
    cultivar_rgb = [[3, 3, 158], [5, 242, 23]]

    counts = load_species_reference(
        conn, SPECIES_JSON, CULTIVARS_JSON, species_hex, species_rgb, cultivar_hex, cultivar_rgb
    )

    assert counts == {
        "species": 2,
        "species_aliases": 1,
        "species_multi_symbols": 2,
        "cultivars": 1,
        "cultivar_aliases": 0,
        "color_palette": 5,
    }

    row = conn.execute("SELECT * FROM species WHERE USDA_symbol = 'ARHY'").fetchone()
    assert row["common_name"] == "peanut"

    unassigned = conn.execute(
        "SELECT hex FROM color_palette WHERE palette = 'species' AND assigned_class_id IS NULL"
    ).fetchall()
    assert [r["hex"] for r in unassigned] == ["#ffffff"]

    unassigned_cultivar = conn.execute(
        "SELECT hex FROM color_palette WHERE palette = 'cultivar' AND assigned_cultivar_class_id IS NULL"
    ).fetchall()
    assert [r["hex"] for r in unassigned_cultivar] == ["#05f217"]


def test_load_species_reference_is_idempotent(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    species_hex, species_rgb = ["#a5482f", "#e86928"], [[165, 72, 47], [232, 105, 40]]
    cultivar_hex, cultivar_rgb = ["#03039e"], [[3, 3, 158]]

    load_species_reference(conn, SPECIES_JSON, CULTIVARS_JSON, species_hex, species_rgb, cultivar_hex, cultivar_rgb)
    load_species_reference(conn, SPECIES_JSON, CULTIVARS_JSON, species_hex, species_rgb, cultivar_hex, cultivar_rgb)

    assert conn.execute("SELECT COUNT(*) FROM species").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM cultivars").fetchone()[0] == 1
