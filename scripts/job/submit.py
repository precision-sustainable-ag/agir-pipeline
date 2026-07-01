#!/usr/bin/env python3
"""
scripts/job/submit.py
==================

Login-node entry point for the AgIR pipeline.

Does exactly two things:
  1. Query SQLite to find batches that need processing.
  2. Submit one Slurm job per batch (via orchestrator/submit_jobs.py).

Everything else (Globus transfer, stage CLI, promote, ingest) happens
inside the Slurm job on the compute node.

Usage
-----
# Find batches and write to file:
python scripts/job/submit.py --stage jpg_to_det --config configs/atlas_jpg_to_det.yaml --find-only --out batches.txt

# Submit from that file:
python scripts/job/submit.py --stage jpg_to_det --config configs/atlas_jpg_to_det.yaml --batches batches.txt

# Find + submit in one step:
python scripts/job/submit.py --stage jpg_to_det --config configs/atlas_jpg_to_det.yaml

# Dry-run (render scripts, no sbatch):
python scripts/job/submit.py --stage jpg_to_det --config configs/atlas_jpg_to_det.yaml --dry-run --limit 2
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List

import yaml

from orchestrator.sqlite_db import (
    open_db,
    get_batches_needing_raw_to_jpg,
    get_batches_needing_jpg_to_det,
)
from orchestrator.submit_jobs import submit_jobs, JobResult

logger = logging.getLogger(__name__)

SUPPORTED_STAGES = ("raw_to_jpg", "jpg_to_det")


# ---------------------------------------------------------------------------
# Batch discovery
# ---------------------------------------------------------------------------

def find_batches(cfg: dict, stage: str, *, site: str, limit: int) -> List[str]:
    db_path    = Path(cfg["paths"]["db"])
    locks_root = Path(cfg["paths"]["locks_root"])

    conn = open_db(db_path, readonly=True, local_copy=True)
    try:
        if stage == "raw_to_jpg":
            rows = get_batches_needing_raw_to_jpg(conn, site=site, limit=limit * 2)
        elif stage == "jpg_to_det":
            rows = get_batches_needing_jpg_to_det(conn, site=site, limit=limit * 2)
        else:
            raise ValueError(f"Unsupported stage: {stage!r}")
    finally:
        conn.close()

    lock_dir = locks_root / stage
    batch_ids, skipped = [], 0
    for r in rows:
        bid = r["batch_id"]
        if (lock_dir / f"{bid}.lock").exists():
            skipped += 1
            logger.debug("Skipping locked batch: %s", bid)
            continue
        batch_ids.append(bid)
        if len(batch_ids) >= limit:
            break

    logger.info(
        "Found %d batch(es) needing %s (site=%s, %d locked/skipped)",
        len(batch_ids), stage, site, skipped,
    )
    return batch_ids


def load_batch_file(path: Path) -> List[str]:
    lines = path.read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def write_batch_file(path: Path, batch_ids: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{b}\n" for b in batch_ids))
    logger.info("Wrote %d batch(es) to %s", len(batch_ids), path)


# ---------------------------------------------------------------------------
# Result printing
# ---------------------------------------------------------------------------

def print_results(results: List[JobResult]) -> bool:
    print("\n── submission results ──────────────────────────────────────────")
    any_error = False
    for r in results:
        if r.status in {"submitted", "dry_run"}:
            icon = "✓"
        elif r.status == "lease_conflict":
            icon = "~"
        else:
            icon = "✗"
            any_error = True

        job = f"  job={r.slurm_job_id}" if r.slurm_job_id else ""
        err = f"  ({r.error[:80]})" if r.error else ""
        print(f"  {icon}  {r.batch_id:<24}  {r.status}{job}{err}")
    print()
    return any_error


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find batches and submit one Slurm job each."
    )
    parser.add_argument(
        "--stage", required=True, choices=SUPPORTED_STAGES,
        help="Pipeline stage to run.",
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="Stage config YAML.",
    )
    parser.add_argument(
        "--batches", type=Path, default=None,
        help="Batch list file (one batch_id per line). Skips DB query when provided.",
    )
    parser.add_argument(
        "--find-only", action="store_true",
        help="Query and print batches but do not submit.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write found batch_ids to this file (one per line). Use with --find-only.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Render Slurm scripts but do not call sbatch.",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max batches to find/submit (default: 50).",
    )
    parser.add_argument(
        "--site", default="JUNO",
        help="Site filter for DB query (default: JUNO).",
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

    cfg = yaml.safe_load(args.config.read_text())

    # ── Find batches ──────────────────────────────────────────────────────────
    if args.batches:
        batch_ids = load_batch_file(args.batches)
        logger.info("Loaded %d batch(es) from %s", len(batch_ids), args.batches)
    else:
        batch_ids = find_batches(cfg, args.stage, site=args.site, limit=args.limit)

    if not batch_ids:
        logger.info("No batches to process.")
        return 0

    if args.find_only:
        for b in batch_ids:
            print(b)
        if args.out:
            write_batch_file(args.out, batch_ids)
        return 0

    if args.out:
        write_batch_file(args.out, batch_ids)

    if args.limit:
        batch_ids = batch_ids[:args.limit]

    # ── Submit ────────────────────────────────────────────────────────────────
    logger.info("Submitting %d batch(es) for stage %s", len(batch_ids), args.stage)
    results = submit_jobs(
        batch_ids=batch_ids,
        config_path=str(args.config),
        dry_run=args.dry_run,
    )

    any_error = print_results(results)
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())