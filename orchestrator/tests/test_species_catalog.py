from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from orchestrator.species_catalog import build_catalog, export_catalog


def make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "pipeline.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema = Path("schemas/sqlite/pipeline.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


def seed_species_and_cultivar(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO species (
            class_id, species_key, USDA_symbol, EPPO, species_group,
            taxon_class, subclass, taxon_order, family, genus,
            species_epithet, common_name, authority, growth_habit,
            duration, collection_location, category, collection_timing,
            link, note, hex, r, g, b
        ) VALUES (
            75, 'ARHY', 'ARHY', 'ARHHY', 'dicot',
            'Magnoliopsida', 'Rosidae', 'Fabales', 'Fabaceae', 'Arachis',
            'hypogaea', 'peanut', 'Linnaeus', 'forb/herb',
            'perennial', NULL, 'cash crop', NULL,
            'https://plants.sc.egov.usda.gov/plant-profile/ARHY', NULL,
            '#a5482f', 165, 72, 47
        )
        """
    )
    conn.execute("INSERT INTO species_aliases (class_id, alias) VALUES (75, 'groundnut')")
    conn.execute(
        """
        INSERT INTO cultivars (
            cultivar_class_id, cultivar_key, parent_USDA_symbol, entity_type,
            display_name, cultivar_name, line_name, registered,
            collection_location, cultivar_category, link, note, hex, r, g, b
        ) VALUES (
            101, 'PEANUT_BAILEY_II', 'ARHY', 'cultivar',
            'Peanut - Bailey II', 'Bailey II', NULL, 1,
            'NC', 'crop variety', NULL, NULL, '#03039e', 3, 3, 158
        )
        """
    )
    conn.commit()


def test_build_catalog_shape(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    seed_species_and_cultivar(conn)

    catalog = build_catalog(conn)

    assert set(catalog.keys()) == {"species", "cultivars"}
    species = catalog["species"]["ARHY"]
    assert species["class_id"] == 75
    assert species["common_name"] == "peanut"
    assert species["alias"] == ["groundnut"]
    assert (species["r"], species["g"], species["b"]) == (165, 72, 47)

    cultivar = catalog["cultivars"]["101"]
    assert cultivar["cultivar_key"] == "PEANUT_BAILEY_II"
    assert cultivar["parent_USDA_symbol"] == "ARHY"
    assert cultivar["alias"] == []


def test_species_with_no_alias_gets_empty_list(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    seed_species_and_cultivar(conn)
    conn.execute("DELETE FROM species_aliases")
    conn.commit()

    catalog = build_catalog(conn)

    assert catalog["species"]["ARHY"]["alias"] == []


def test_export_catalog_writes_matching_json(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    seed_species_and_cultivar(conn)
    out_path = tmp_path / "species_catalog.generated.json"

    returned = export_catalog(conn, out_path)

    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk == returned

    # export_catalog writes via a .tmp sibling + replace; it must not linger.
    assert not out_path.with_suffix(out_path.suffix + ".tmp").exists()


def test_export_catalog_overwrites_previous_contents(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    seed_species_and_cultivar(conn)
    out_path = tmp_path / "species_catalog.generated.json"
    export_catalog(conn, out_path)

    conn.execute("DELETE FROM cultivars WHERE cultivar_class_id = 101")
    conn.commit()
    returned = export_catalog(conn, out_path)

    assert returned["cultivars"] == {}
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["cultivars"] == {}
