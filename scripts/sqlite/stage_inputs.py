#!/usr/bin/env python3
"""
scripts/sqlite/stage_inputs.py
================================

Entrypoint: stage RAW files from JUNO to 90daydata using globus_file_index.

Usage
-----
python scripts/sqlite/stage_inputs.py --config configs/scinet_raw_to_jpg.yaml --batches batches.txt
python scripts/sqlite/stage_inputs.py --config configs/scinet_raw_to_jpg.yaml --batches batches.txt --dry-run
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from orchestrator.batch_list import parse_batch_list
from orchestrator.sqlite_stage_inputs import stage_batches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage RAW files from JUNO to 90daydata using globus_file_index."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--batches", required=True, type=Path,
                        help="Bare batch_id list (one per line).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    cfg = yaml.safe_load(Path(args.config).read_text())
    entries = parse_batch_list(args.batches)
    batch_ids = [e.batch_id for e in entries]

    if not batch_ids:
        logging.getLogger(__name__).warning("Batch list is empty.")
        return 0

    results = stage_batches(batch_ids, cfg, dry_run=args.dry_run)

    any_failed = False
    print("\n── stage_inputs results ─────────────────────────────────────────")
    for r in results:
        icon = "✓" if r.status in ("completed", "dry_run") else "✗"
        print(f"  {icon}  {r.batch_id:<24}  {r.status:<14}  {r.n_files:>5} files"
              + (f"  task={r.globus_task_id}" if r.globus_task_id else "")
              + (f"  ERROR: {r.error}" if r.error else ""))
        if r.status not in ("completed", "dry_run", "no_files"):
            any_failed = True
    print()
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())