#!/usr/bin/env python3
"""
scripts/atlas/find_raw_to_jpg.py
==================================

Find batches that need raw_to_jpg run on the compute cluster.

Reads ``paths.db`` and ``paths.locks_root`` from the same config file used by
submit_jobs.py so there is one source of truth for all paths.

A batch needs raw_to_jpg if it has current RAW files in semifield-upload but
has no current JPG files in semifield-developed-images/*/images/.

Internally queries ``v_batches_needing_raw_to_jpg`` (defined in
schemas/sqlite/pipeline.sql).  The view already excludes batches that have an
active lease or a prior successful stage_run, so the output is safe to feed
directly into the submission pipeline without additional filtering.

By default only returns batches whose RAW files are indexed under site=JUNO,
since those are the ones the cluster can pull via Globus.  Use --site to
override.

Output (one batch_id per line) is compatible with parse_batch_list when
bare batch_id lines are supported — pass to submit_jobs.py via --batches.

Usage
-----
python scripts/atlas/find_raw_to_jpg.py --config configs/scinet_raw_to_jpg.yaml
python scripts/atlas/find_raw_to_jpg.py --config configs/scinet_raw_to_jpg.yaml --limit 10
python scripts/atlas/find_raw_to_jpg.py --config configs/scinet_raw_to_jpg.yaml --out batches.txt
python scripts/atlas/find_raw_to_jpg.py --config configs/scinet_raw_to_jpg.yaml --site JUNO
python scripts/atlas/find_raw_to_jpg.py --config configs/scinet_raw_to_jpg.yaml --quiet
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from orchestrator.sqlite_db import open_db, get_batches_needing_raw_to_jpg

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


# ── Lockfile helpers ──────────────────────────────────────────────────────────

def _is_locked(locks_root: Path, batch_id: str) -> bool:
    """Return True if a filesystem lockfile exists for this batch/stage."""
    return (locks_root / "raw_to_jpg" / f"{batch_id}.lock").exists()


# ── Output ────────────────────────────────────────────────────────────────────

def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("  (no batches need raw_to_jpg)")
        return
    header = (
        f"  {'BATCH_ID':<24}  {'RAW_FILES':>9}  {'SITE':<8}  {'STORAGE_DOMAIN'}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print(
            f"  {r['batch_id']:<24}  {r.get('raw_file_count', '?'):>9}"
            f"  {r.get('site', '')::<8}  {r.get('storage_domain', '')}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find batches needing raw_to_jpg using the SQLite inventory DB."
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="Stage config YAML (e.g. configs/scinet_raw_to_jpg.yaml).",
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
        logger.error("SQLite DB not found: %s", db_path)
        return 1

    # Query v_batches_needing_raw_to_jpg, filtered to the requested site.
    conn = open_db(db_path, readonly=True)
    try:
        all_rows = get_batches_needing_raw_to_jpg(
            conn, site=args.site, limit=args.limit * 2
        )
    finally:
        conn.close()

    if not all_rows:
        logger.info(
            "v_batches_needing_raw_to_jpg returned 0 rows (site=%s)", args.site
        )

    # Filter out batches that already have a filesystem lockfile (in-flight jobs).
    rows: list[dict] = []
    locked_count = 0
    for r in all_rows:
        if _is_locked(locks_root, r["batch_id"]):
            locked_count += 1
            logger.debug("Skipping locked batch: %s", r["batch_id"])
        else:
            rows.append(r)
        if len(rows) >= args.limit:
            break

    batch_ids = [r["batch_id"] for r in rows]

    if not args.quiet:
        print(f"\n── raw_to_jpg backlog (site={args.site}) ─────────────────────")
        print(f"  needs raw_to_jpg:   {len(batch_ids)}")
        if locked_count:
            print(f"  locked / in-flight: {locked_count}")
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
