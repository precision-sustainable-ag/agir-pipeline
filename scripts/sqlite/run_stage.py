#!/usr/bin/env python3
"""
scripts/sqlite/run_stage.py
=============================

SQLite-mode raw_to_jpg pipeline: find → stage → submit.

Usage
-----
# Full pipeline:
python scripts/sqlite/run_stage.py --config configs/scinet_raw_to_jpg.yaml

# Find only:
python scripts/sqlite/run_stage.py --config configs/scinet_raw_to_jpg.yaml --find-only

# Stage only (with a pre-built batch list):
python scripts/sqlite/run_stage.py --config configs/scinet_raw_to_jpg.yaml --batches batches.txt --stage-only

# Submit only (files already staged):
python scripts/sqlite/run_stage.py --config configs/scinet_raw_to_jpg.yaml --batches batches.txt --submit-only

# Dry-run (no Globus transfers submitted, no sbatch):
python scripts/sqlite/run_stage.py --config configs/scinet_raw_to_jpg.yaml --dry-run --limit 2
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List

import yaml

from orchestrator.batch_list import parse_batch_list, BatchEntry
from orchestrator.manual.submit_jobs import submit_stage_jobs
from orchestrator.sqlite_db import open_db, get_batches_needing_raw_to_jpg
from orchestrator.sqlite_stage_inputs import stage_batches

logger = logging.getLogger(__name__)


def find_batches(cfg: dict, *, site: str, limit: int) -> List[str]:
    db_path    = Path(cfg["paths"]["db"])
    locks_root = Path(cfg["paths"].get("locks_root", "/tmp/agir_locks"))
    conn = open_db(db_path, readonly=True, local_copy=True)
    try:
        rows = get_batches_needing_raw_to_jpg(conn, site=site, limit=limit * 2)
    finally:
        conn.close()

    batch_ids, skipped = [], 0
    for r in rows:
        if (locks_root / "raw_to_jpg" / f"{r['batch_id']}.lock").exists():
            skipped += 1
            continue
        batch_ids.append(r["batch_id"])
        if len(batch_ids) >= limit:
            break

    logger.info(
        "Found %d batch(es) needing raw_to_jpg (site=%s, %d locked/skipped)",
        len(batch_ids), site, skipped,
    )
    return batch_ids


def submit_batch_ids(batch_ids: List[str], config_path: Path) -> bool:
    entries = [BatchEntry(b, 0, 0) for b in batch_ids]
    results = submit_stage_jobs(
        batch_entries=entries,
        config_path=str(config_path),
        require_transfer_complete=False,
    )
    any_failed = False
    for r in results:
        if r.status == "submitted":
            logger.info("  ✓  %-25s  job=%s  lease=%s", r.batch_id, r.slurm_job_id, r.lease_id)
        else:
            logger.warning("  ✗  %-25s  %s  %s", r.batch_id, r.status, r.error or "")
            any_failed = True
    return not any_failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SQLite-mode raw_to_jpg: find → stage → submit."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--batches", type=Path, default=None)
    parser.add_argument("--find-only",   action="store_true")
    parser.add_argument("--stage-only",  action="store_true")
    parser.add_argument("--submit-only", action="store_true")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--site", default="JUNO")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    if sum([args.find_only, args.stage_only, args.submit_only]) > 1:
        parser.error("--find-only, --stage-only, and --submit-only are mutually exclusive.")
    if args.submit_only and not args.batches:
        parser.error("--submit-only requires --batches.")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    cfg = yaml.safe_load(Path(args.config).read_text())

    # ── Phase 0: find ─────────────────────────────────────────────────────────
    if args.batches:
        batch_ids = [e.batch_id for e in parse_batch_list(args.batches)]
        logger.info("Using provided batch list: %d batch(es)", len(batch_ids))
    else:
        batch_ids = find_batches(cfg, site=args.site, limit=args.limit)
        if not batch_ids:
            logger.info("Nothing to do.")
            return 0

    if args.find_only:
        for b in batch_ids:
            print(b)
        return 0

    # ── Phase 1: stage ────────────────────────────────────────────────────────
    if not args.submit_only:
        results = stage_batches(batch_ids, cfg, dry_run=args.dry_run, site=args.site)

        print("\n── Phase 1: stage_inputs ────────────────────────────────────")
        failed: List[str] = []
        for r in results:
            icon = "✓" if r.status in ("completed", "dry_run") else "✗"
            print(f"  {icon}  {r.batch_id:<24}  {r.status:<14}  {r.n_files:>5} files"
                  + (f"  task={r.globus_task_id}" if r.globus_task_id else "")
                  + (f"  ERROR: {r.error}" if r.error else ""))
            if r.status not in ("completed", "dry_run"):
                failed.append(r.batch_id)
        print()

        ready = [r.batch_id for r in results if r.status in ("completed", "dry_run")]
        if failed:
            logger.warning("Skipping submission for %d failed batch(es): %s", len(failed), failed)
        if not ready:
            logger.error("No batches staged successfully — nothing to submit.")
            return 1
        if args.stage_only:
            return 0
        batch_ids = ready

    # ── Phase 2: submit ───────────────────────────────────────────────────────
    print("\n── Phase 2: submit ──────────────────────────────────────────────")
    ok = submit_batch_ids(batch_ids, args.config)
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())