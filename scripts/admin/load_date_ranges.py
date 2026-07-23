#!/usr/bin/env python3
"""
scripts/admin/load_date_ranges.py
==================================

Load configs/date_ranges.yaml into the season_date_ranges / season_aliases
tables of the pipeline SQLite DB (schemas/sqlite/pipeline.sql).

Idempotent: each run replaces the full contents of both tables, so the DB
always mirrors the current YAML file exactly (including removals).

Usage
-----
python scripts/admin/load_date_ranges.py \\
    --db /project/dash_agir/globus_index/globus_file_index.sqlite3 \\
    --yaml configs/date_ranges.yaml
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from orchestrator.sqlite_db import open_db

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "sqlite" / "pipeline.sql"

_NON_SITE_KEYS = {"season_mappings"}
_REQUIRED_SEASON_FIELDS = ("start", "end", "bbot_version", "crs", "note")


def _migrate_season_date_ranges_columns(conn: sqlite3.Connection) -> None:
    """Add columns to a pre-existing season_date_ranges table that predates them.

    CREATE TABLE IF NOT EXISTS in pipeline.sql only creates the table on a
    fresh DB — it does not retrofit new columns onto an existing table.
    """
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(season_date_ranges)").fetchall()
    }
    if not existing_cols:
        return  # table doesn't exist yet; executescript will create it with all columns
    if "note" not in existing_cols:
        conn.execute("ALTER TABLE season_date_ranges ADD COLUMN note TEXT")
        conn.commit()


def _load_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"date_ranges YAML root must be a mapping: {path}")
    return data


def _parse_date_ranges(data: Dict[str, Any]) -> Tuple[List[Tuple], List[Tuple]]:
    """Flatten the YAML structure into season_date_ranges / season_aliases rows."""
    season_rows: List[Tuple] = []
    for site, seasons in data.items():
        if site in _NON_SITE_KEYS:
            continue
        if not isinstance(seasons, dict):
            raise ValueError(f"Expected a mapping of seasons under site {site!r}")
        for season_name, fields in seasons.items():
            missing = [k for k in _REQUIRED_SEASON_FIELDS if k not in fields]
            if missing:
                raise ValueError(
                    f"{site}/{season_name!r} is missing required fields: {missing}"
                )
            season_rows.append((
                site,
                season_name,
                fields["start"],
                fields["end"],
                str(fields["bbot_version"]),
                str(fields["crs"]),
                fields.get("pipeline_season"),
                fields.get("gh_reviewer"),
                fields.get("note")
            ))

    alias_rows: List[Tuple] = []
    for pipeline_season, aliases in (data.get("season_mappings") or {}).items():
        for alias in aliases:
            alias_rows.append((pipeline_season, alias))

    return season_rows, alias_rows


def load_date_ranges(conn: sqlite3.Connection, data: Dict[str, Any]) -> Dict[str, int]:
    """Replace the contents of season_date_ranges / season_aliases from *data*."""
    season_rows, alias_rows = _parse_date_ranges(data)

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM season_date_ranges")
        conn.execute("DELETE FROM season_aliases")
        conn.executemany(
            """
            INSERT INTO season_date_ranges (
                site, season_name, start_date, end_date,
                bbot_version, crs, pipeline_season, gh_reviewer, note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            season_rows,
        )
        conn.executemany(
            "INSERT INTO season_aliases (pipeline_season, alias) VALUES (?, ?)",
            alias_rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"seasons": len(season_rows), "aliases": len(alias_rows)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load configs/date_ranges.yaml into the pipeline SQLite DB."
    )
    parser.add_argument(
        "--db", required=True, type=Path,
        help="Path to the SQLite pipeline database.",
    )
    parser.add_argument(
        "--yaml", default=Path("configs/date_ranges.yaml"), type=Path,
        help="Path to date_ranges.yaml (default: configs/date_ranges.yaml).",
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

    data = _load_yaml(args.yaml)

    conn = open_db(args.db)
    try:
        _migrate_season_date_ranges_columns(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        counts = load_date_ranges(conn, data)
        logger.info(
            "Loaded %d season rows and %d alias rows from %s into %s",
            counts["seasons"], counts["aliases"], args.yaml, args.db,
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
