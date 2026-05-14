#!/usr/bin/env python3
"""
Stage inputs from LTS to 90daydata for a list of batches.

Reads a batch list file, queries source.globus_file_index for each batch's
time-windowed files, submits targeted Globus transfers, and polls until all
transfers reach a terminal state.  Must be run BEFORE submit_jobs.py.

The stage being transferred is read from the ``stage.name`` field in the
config YAML, so this script works for any pipeline stage without modification.

Usage
-----
python scripts/stage_inputs.py \\
    --batches path/to/batch_list.txt \\
    --config  configs/scinet_raw_to_jpg.yaml \\
    [--dry-run]

Batch list format (one batch per line, times in GMT):
    MD_2026-04-03 | 5:55:04 PM 7:33:16 PM
    TX_2026-03-15 | 8:00:00 AM 10:30:45 AM
    # comment lines are ignored
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.batch_list import parse_batch_list
from orchestrator.stage_inputs import stage_inputs_for_batches


def _read_stage_name(config_path: Path) -> str:
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    stage_name = cfg.get("stage", {}).get("name")
    if not stage_name:
        raise ValueError(
            f"Config '{config_path}' is missing required 'stage.name' field."
        )
    return stage_name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transfer time-windowed files from LTS to 90daydata staging."
    )
    parser.add_argument(
        "--batches", required=True, type=Path,
        help="Batch list file (batch_id | start_time end_time, times in GMT).",
    )
    parser.add_argument(
        "--config", required=True, type=Path,
        help="Stage config YAML (e.g. configs/scinet_raw_to_jpg.yaml).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="Override config dry_run to True — prints Globus commands without submitting.",
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

    try:
        stage_name = _read_stage_name(args.config)
    except (ValueError, OSError) as exc:
        logging.error("Failed to read stage name from config: %s", exc)
        return 1

    try:
        entries = parse_batch_list(args.batches)
        logging.info("Parsed %d batch(es) from batch list: %s", len(entries), args.batches)
    except ValueError as exc:
        logging.error("Failed to parse batch list: %s", exc)
        return 1

    if not entries:
        logging.warning("Batch list is empty — nothing to do.")
        return 0

    logging.info("Staging '%s' inputs for %d batch(es)", stage_name, len(entries))
    for e in entries:
        logging.info("  %s  [%d, %d]", e.batch_id, e.start_epoch, e.end_epoch)

    dry_run = True if args.dry_run else None  # None means "use config value"
    results = stage_inputs_for_batches(
        batch_entries=entries,
        config_path=str(args.config),
        stage=stage_name,
        requested_by="scripts.stage_inputs",
        dry_run=dry_run,
    )
    logging.info("Completed staging inputs for %d batch(es)", len(results))

    any_failed = False
    print(f"\n── Transfer results [{stage_name}] ──────────────────────────────")
    for window_key, status in results.items():
        icon = "✓" if status in {"completed", "already_completed"} else "✗"
        print(f"  {icon}  {window_key:<40}  {status}")
        if status not in {"completed", "already_completed", "already_active"}:
            any_failed = True
    print()

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
