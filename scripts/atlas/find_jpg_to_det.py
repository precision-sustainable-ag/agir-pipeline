#!/usr/bin/env python3
"""
scripts/atlas/find_jpg_to_det.py
==================================

Find batches that need jpg_to_det run on Atlas.

Reads db path and locks_root from the same config file used by
submit_jpg_to_det.py so there is one source of truth for all paths.

A batch needs jpg_to_det if it has JPG images (semifield-developed-images/
<batch_id>/images/) but is missing BOTH:
  - metadata JSONs       (.../metadata/*.json)
  - plant detection CSVs (.../plant-detections/*.csv or .../detections/*.csv)

Batches that have CSVs but no JSONs need a downstream metadata-formatting
stage — they are reported as info but not included in the output list.

By default only returns batches whose files are indexed under site=JUNO,
since those are the ones Atlas can pull via Globus. Use --site to override.

Queries v_batches_needing_jpg_to_det (defined in schemas/sqlite/pipeline.sql).

Usage
-----
python scripts/atlas/find_jpg_to_det.py --config configs/atlas_jpg_to_det.yaml
python scripts/atlas/find_jpg_to_det.py --config configs/atlas_jpg_to_det.yaml --limit 10
python scripts/atlas/find_jpg_to_det.py --config configs/atlas_jpg_to_det.yaml --out batches_needing_det.txt
python scripts/atlas/find_jpg_to_det.py --config configs/atlas_jpg_to_det.yaml --site JUNO
python scripts/atlas/find_jpg_to_det.py --config configs/atlas_jpg_to_det.yaml --quiet
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

_REQUIRED_CONFIG_KEYS = [
    "paths.db",
    "paths.locks_root",
]


def _get_nested(cfg: dict, dotted_key: str):
    keys = dotted_key.split(".")
    node = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def validate_config(cfg: dict, config_path: Path) -> None:
    missing = [k for k in _REQUIRED_CONFIG_KEYS if _get_nested(cfg, k) is None]
    if missing:
        lines = "\n  ".join(missing)
        raise SystemExit(
            f"Config file is missing required keys ({config_path}):\n  {lines}"
        )


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")
    cfg = yaml.safe_load(config_path.read_text())
    validate_config(cfg, config_path)
    return cfg


# ── Queries ───────────────────────────────────────────────────────────────────

_BACKLOG_QUERY = """
SELECT batch_id, jpg_count, site, storage_domain, storage_root
FROM v_batches_needing_jpg_to_det
WHERE pipeline_status = 'needs_jpg_to_det'
  AND site = ?
ORDER BY batch_id
LIMIT ?
"""

_NEEDS_FORMATTING_QUERY = """
SELECT COUNT(*) FROM v_batches_needing_jpg_to_det
WHERE pipeline_status = 'needs_metadata_formatting'
  AND site = ?
"""


def query_backlog(db_path: Path, site: str, limit: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_BACKLOG_QUERY, (site, limit * 2)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_needs_formatting_count(db_path: Path, site: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(_NEEDS_FORMATTING_QUERY, (site,)).fetchone()[0]
    finally:
        conn.close()


# ── Lockfile helpers ──────────────────────────────────────────────────────────

def _is_locked(locks_root: Path, batch_id: str) -> bool:
    return (locks_root / "jpg_to_det" / f"{batch_id}.lock").exists()


# ── Output ────────────────────────────────────────────────────────────────────

def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("  (no batches need jpg_to_det)")
        return
    header = f"  {'BATCH_ID':<24}  {'JPGS':>7}  {'SITE':<8}  {'STORAGE_DOMAIN'}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print(
            f"  {r['batch_id']:<24}  {r['jpg_count']:>7}"
            f"  {r.get('site',''):<8}  {r.get('storage_domain','')}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find batches needing jpg_to_det on Atlas."
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="Stage config YAML (configs/atlas_jpg_to_det.yaml).",
    )
    parser.add_argument(
        "--site", default="JUNO",
        help="Only return batches indexed under this site (default: JUNO).",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Max batches to return (default: 200).",
    )
    parser.add_argument(
        "--out", type=Path,
        help="Write batch_ids to this file, one per line.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Print only batch_ids — no table (useful for piping).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    cfg        = load_config(args.config)
    db_path    = Path(cfg["paths"]["db"])
    locks_root = Path(cfg["paths"]["locks_root"])

    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 1

    # Query — filtered to requested site
    all_rows = query_backlog(db_path, site=args.site, limit=args.limit)

    # Filter locked (already submitted / in-flight)
    rows = []
    locked_count = 0
    for r in all_rows:
        if _is_locked(locks_root, r["batch_id"]):
            locked_count += 1
            logger.debug("Skipping locked: %s", r["batch_id"])
        else:
            rows.append(r)
        if len(rows) >= args.limit:
            break

    batch_ids = [r["batch_id"] for r in rows]
    needs_formatting = query_needs_formatting_count(db_path, site=args.site)

    if not args.quiet:
        print(f"\n── jpg_to_det backlog (site={args.site}) ───────────────────────")
        print(f"  needs jpg_to_det:                      {len(batch_ids)}")
        if locked_count:
            print(f"  locked / in-flight:                    {locked_count}")
        if needs_formatting:
            print(f"  needs metadata formatting (other stage): {needs_formatting}")
        print()
        _print_table(rows)
        print()

    if args.quiet:
        for b in batch_ids:
            print(b)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(f"{b}\n" for b in batch_ids))
        if not args.quiet:
            logger.info("Wrote %d batch(es) to %s", len(batch_ids), args.out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())