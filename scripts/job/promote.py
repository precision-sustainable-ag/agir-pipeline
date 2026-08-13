#!/usr/bin/env python3
"""
CLI entry point for promoting Slurm-job stage outputs to their final destination.

Promotion logic lives in orchestrator/promotion.py and orchestrator/artifact_validation.py.
Supports partial promotion for det_to_world stage (PARTIAL_PROMOTION_STAGES).

Exit codes:
  0  all checks passed and files copied
  1  promotion skipped or failed (details printed to stdout)

Usage
-----
python scripts/job/promote.py --run-dir /path/to/run_dir --dest /path/to/dest
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestrator.artifact_validation import ArtifactValidationError
from orchestrator.promotion import PARTIAL_PROMOTION_STAGES, promote_run_bundle


def _run_report_stage(run_dir: Path) -> str:
    run_report_path = run_dir / "run_report.json"
    if not run_report_path.exists():
        return ""
    try:
        return json.loads(run_report_path.read_text()).get("stage", "")
    except (OSError, json.JSONDecodeError):
        return ""


def promote(run_dir: Path, dest: Path) -> int:
    allow_partial = _run_report_stage(run_dir) in PARTIAL_PROMOTION_STAGES
    try:
        result = promote_run_bundle(run_dir, dest, allow_partial=allow_partial)
    except ArtifactValidationError as exc:
        prefix = "PROMOTE SKIP" if exc.outcome == "skip" else "PROMOTE FAIL"
        print(f"{prefix}: {exc}")
        return 1

    if result.skipped_items:
        print(
            f"PROMOTE PARTIAL: {len(result.skipped_items)} item(s) failed "
            f"— promoted the rest"
        )
        for item in result.skipped_items:
            message = item.get("error", {}).get("message", "")
            print(f"  SKIPPED (failed): {item.get('image_id')} — {message}")

    print(f"PROMOTE OK: {result.artifact_count} artifact file(s) copied to {result.destination}")
    print(f"PROMOTE OK: metadata copied to {result.metadata_destination}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote stage artifacts to the final destination."
    )
    parser.add_argument(
        "--run-dir", required=True, type=Path,
        help="Directory containing run_report.json, manifest.json, and artifacts/",
    )
    parser.add_argument(
        "--dest", required=True, type=Path,
        help="Destination directory (e.g. .../semifield-developed-images/<batch_id>/images)",
    )
    args = parser.parse_args()
    return promote(args.run_dir, args.dest)


if __name__ == "__main__":
    raise SystemExit(main())