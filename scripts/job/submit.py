#!/usr/bin/env python3
"""
scripts/job/submit.py
==================

Login-node entry point for the AgIR pipeline.

Does exactly three things:
  1. Query SQLite to find batches that need processing.
  2. Keep only batches whose inputs are completed in staged_inputs.
  3. Submit one Slurm job per batch (via orchestrator/submit_jobs.py).

Input staging and Globus polling happen before this script, through the
stage-input workflow. The Slurm job assumes inputs are already staged and
handles downstream compute work: stage CLI, promote, artifact copy, ingest, and
lease release.

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
import sys
from pathlib import Path
from typing import List

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.input_staging_planner import (
    det_to_seg_expected_dst_paths,
    det_to_world_expected_dst_paths,
)
from orchestrator.sqlite_db import (
    open_db,
    get_batches_needing_raw_to_jpg,
    get_batches_needing_jpg_to_det,
    get_batches_needing_det_to_world,
    get_batches_needing_det_to_seg,
    get_completed_input_staging_batch_ids,
    get_det_to_world_staged_batch_ids,
)
from orchestrator.submit_jobs import submit_jobs, JobResult

logger = logging.getLogger(__name__)

SUPPORTED_STAGES = ("raw_to_jpg", "jpg_to_det", "det_to_world", "det_to_seg")


# ---------------------------------------------------------------------------
# Batch discovery
# ---------------------------------------------------------------------------

def open_submission_read_db(cfg: dict):
    """Open the shared DB directly by default, with snapshot mode as a fallback."""
    paths = cfg["paths"]
    db_path = Path(paths["db"])
    read_mode = str(paths.get("db_read_mode", "direct")).strip().lower()
    if read_mode not in {"direct", "snapshot"}:
        raise ValueError(
            f"Invalid paths.db_read_mode {read_mode!r}; expected 'direct' or 'snapshot'"
        )

    use_snapshot = read_mode == "snapshot"
    db_temp_dir = paths.get("db_temp_dir") if use_snapshot else None
    logger.info("Opening SQLite for submission reads (mode=%s, db=%s)", read_mode, db_path)
    return open_db(
        db_path,
        readonly=True,
        local_copy=use_snapshot,
        temp_dir=db_temp_dir,
    )


def find_batches(cfg: dict, stage: str, *, site: str, limit: int) -> List[str]:
    locks_root = Path(cfg["paths"]["locks_root"])

    conn = open_submission_read_db(cfg)
    try:
        if stage == "raw_to_jpg":
            rows = get_batches_needing_raw_to_jpg(conn, site=site, limit=limit * 2)
        elif stage == "jpg_to_det":
            # Site-agnostic like det_to_world below: jpg_to_det's multi-site
            # resolver (orchestrator/input_staging_planner.py) independently
            # checks destination (ATLAS) -> CERES -> JUNO, so scoping this
            # query to --site would wrongly exclude batches whose images
            # landed somewhere other than that one site.
            rows = get_batches_needing_jpg_to_det(conn, site=None, limit=limit * 2)
        elif stage == "det_to_world":
            # Unlike raw_to_jpg/jpg_to_det, det_to_world's readiness isn't
            # scoped to one site — its multi-site resolver (see
            # orchestrator/input_staging_planner.py) independently checks
            # each subdir against destination/CERES/JUNO. Restricting the
            # readiness query to --site here would just wrongly exclude
            # batches whose data landed somewhere other than that one site.
            rows = get_batches_needing_det_to_world(conn, site=None, limit=limit * 2)
        elif stage == "det_to_seg":
            # Site-agnostic, same reasoning as det_to_world above —
            # det_to_seg's images/detections subdirs are each resolved
            # independently by orchestrator/input_staging_planner.py.
            rows = get_batches_needing_det_to_seg(conn, limit=limit * 2)
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


def filter_completed_staged_inputs(cfg: dict, stage: str, batch_ids: List[str]) -> List[str]:
    """Keep only batches whose inputs are marked completed in staged_inputs."""
    if not batch_ids:
        return []

    conn = open_submission_read_db(cfg)
    try:
        completed = get_completed_input_staging_batch_ids(
            conn,
            stage=stage,
            batch_ids=batch_ids,
        )
    finally:
        conn.close()

    staged = [batch_id for batch_id in batch_ids if batch_id in completed]
    skipped = [batch_id for batch_id in batch_ids if batch_id not in completed]
    if skipped:
        logger.info(
            "Skipping %d batch(es) without completed input staging for %s: %s",
            len(skipped),
            stage,
            ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""),
        )
    logger.info(
        "Input staging gate kept %d/%d batch(es) for %s",
        len(staged),
        len(batch_ids),
        stage,
    )
    return staged


def filter_det_to_world_staged_ready(cfg: dict, batch_ids: List[str]) -> List[str]:
    """
    Keep only batches where every expected staged_inputs piece (images,
    detections, grids) has status='completed'.

    Unlike filter_completed_staged_inputs (which only requires ANY row
    completed — correct for raw_to_jpg/jpg_to_det's single fixed route),
    det_to_world has multiple independent pieces, each with its own
    staged_inputs dst_path (see
    orchestrator.input_staging_planner.det_to_world_expected_dst_paths), so
    readiness requires ALL of them to show completed. Pieces already
    resident at the destination when planned are recorded as
    immediately-completed rows too (StagingRequest.already_satisfied), so
    this reflects readiness the moment stage_inputs.py/poll_stage_inputs.py
    mark them — no fresh globus_file_index inventory scan required.
    """
    if not batch_ids:
        return []

    expected = {
        batch_id: det_to_world_expected_dst_paths(cfg, batch_id) for batch_id in batch_ids
    }

    conn = open_submission_read_db(cfg)
    try:
        ready = get_det_to_world_staged_batch_ids(
            conn, stage="det_to_world", expected_dst_paths=expected
        )
    finally:
        conn.close()

    staged = [batch_id for batch_id in batch_ids if batch_id in ready]
    skipped = [batch_id for batch_id in batch_ids if batch_id not in ready]
    if skipped:
        logger.info(
            "Skipping %d batch(es) not fully staged (images/detections/grids) for det_to_world: %s",
            len(skipped),
            ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""),
        )
    logger.info(
        "Staged-inputs gate kept %d/%d batch(es) for det_to_world",
        len(staged),
        len(batch_ids),
    )
    return staged


def filter_det_to_seg_staged_ready(cfg: dict, batch_ids: List[str]) -> List[str]:
    """
    Keep only batches where every expected staged_inputs piece (images,
    detections) has status='completed'.

    Same reasoning as filter_det_to_world_staged_ready() — det_to_seg has
    multiple independent input pieces (no grids, unlike det_to_world), each
    with its own staged_inputs dst_path (see
    orchestrator.input_staging_planner.det_to_seg_expected_dst_paths), so
    readiness requires ALL of them to show completed.
    """
    if not batch_ids:
        return []

    expected = {
        batch_id: det_to_seg_expected_dst_paths(cfg, batch_id) for batch_id in batch_ids
    }

    conn = open_submission_read_db(cfg)
    try:
        ready = get_det_to_world_staged_batch_ids(
            conn, stage="det_to_seg", expected_dst_paths=expected
        )
    finally:
        conn.close()

    staged = [batch_id for batch_id in batch_ids if batch_id in ready]
    skipped = [batch_id for batch_id in batch_ids if batch_id not in ready]
    if skipped:
        logger.info(
            "Skipping %d batch(es) not fully staged (images/detections) for det_to_seg: %s",
            len(skipped),
            ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""),
        )
    logger.info(
        "Staged-inputs gate kept %d/%d batch(es) for det_to_seg",
        len(staged),
        len(batch_ids),
    )
    return staged


# det_to_world and det_to_seg each need multiple independent inputs (images +
# detections, plus grids for det_to_world), each with its own staged_inputs
# dst_path, so readiness requires ALL of them to show completed rather than
# the "any row completed" check filter_completed_staged_inputs uses for
# raw_to_jpg/jpg_to_det's single fixed route.
_MULTI_PIECE_STAGED_READY_FILTERS = {
    "det_to_world": filter_det_to_world_staged_ready,
    "det_to_seg": filter_det_to_seg_staged_ready,
}


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

    multi_piece_filter = _MULTI_PIECE_STAGED_READY_FILTERS.get(args.stage)
    if multi_piece_filter:
        batch_ids = multi_piece_filter(cfg, batch_ids)
    else:
        batch_ids = filter_completed_staged_inputs(cfg, args.stage, batch_ids)

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
