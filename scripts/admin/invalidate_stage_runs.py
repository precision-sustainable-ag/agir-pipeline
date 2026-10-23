#!/usr/bin/env python3
"""
Invalidate successful stage runs whose outputs were lost, so the readiness
views re-queue those batches.

Every ``v_batches_needing_<stage>`` view skips a batch that has a successful
``stage_runs`` row. If that run's outputs later disappear from every site, the
batch is never offered again. This script sets ``stage_runs.invalidated_at``
(schema v13) on those rows, which the views ignore; status, provenance and
``run_report_json`` are left as-is, so result-sync reconciliation still finds
a matching row. It also cancels the stage's ``completed`` ``staged_inputs``
rows for the batch, so the next ``stage_inputs.py`` run re-stages (or, when
still resident, re-confirms) the inputs instead of trusting old copies.

Selection
---------
Default (detect): a successful, non-invalidated run is invalidated when its
stage's outputs -- the same files its readiness view treats as "already has
output" -- have no current rows in ``globus_file_index`` but do have stale
(``is_current = 0``) rows, i.e. they were indexed once and a later scan found
them gone from every site. Runs whose outputs were never indexed are listed
but left alone, since they may just not have been scanned yet.

``--min-missing-days N`` additionally requires the most recent of those
disappearances (``missing_since_ts_iso``) to be at least N days old. The
nightly ``scripts/admin/globus_index.sh`` job runs this with ``--apply
--min-missing-days 2``, so a single scan that failed to list some files (which
marks them stale too) is corrected by the next scan before anything is
invalidated.

``--force`` (requires ``--batches``): invalidate every successful run for the
named batches/stages whatever the index shows. Use it when a whole storage
root vanished (``globus_index.py`` skips stale-marking when a scan sees
nothing) or outputs were never indexed.

Detection only knows what the last inventory scans saw, so rescan the affected
roots with ``scripts/admin/globus_index.py`` first. Batches with an active
lease for a stage are skipped for that stage. No rows change without
``--apply`` (opening the DB does add the v13 columns if they are missing).

Usage
-----
  # Report success runs whose outputs are gone (all stages)
  python scripts/admin/invalidate_stage_runs.py --db /path/to/globus_file_index.sqlite3

  # Invalidate them
  python scripts/admin/invalidate_stage_runs.py --db ... --apply

  # Force every stage for a list of batches
  python scripts/admin/invalidate_stage_runs.py --db ... --batches lost.txt --force --apply

Undo for one run:
  UPDATE stage_runs SET invalidated_at = NULL, invalidated_reason = NULL
  WHERE run_id = '<run_id>';
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.sqlite_db import open_db

logger = logging.getLogger(__name__)

# Each stage's output files as (data_state, extra globus_file_index predicate
# on alias ``g``), in pipeline order. Mirrors the "no output yet" CTE of the
# stage's readiness view in schemas/sqlite/pipeline.sql. seg_to_cut's view has
# no such CTE; its outputs are the PNGs published under
# semifield-cutouts/<batch_id>/.
STAGE_OUTPUTS: Dict[str, Tuple[str, str]] = {
    "raw_to_jpg": (
        "semifield-developed-images",
        "g.parent_dir = 'images' AND g.file_ext IN ('jpg', 'jpeg')",
    ),
    "jpg_to_det": (
        "semifield-developed-images",
        "g.parent_dir IN ('detections', 'plant-detections', 'metadata')",
    ),
    "det_to_world": (
        "semifield-developed-images",
        "g.parent_dir = 'georeferenced'",
    ),
    "det_to_seg": (
        "semifield-developed-images",
        "g.parent_dir = 'segmentations' AND g.file_ext = 'png'",
    ),
    "seg_to_cut": ("semifield-cutouts", "g.file_ext = 'png'"),
}


def _output_sql(stage: str) -> str:
    data_state, predicate = STAGE_OUTPUTS[stage]
    return f"g.data_state = '{data_state}' AND {predicate}"


def _parse_utc(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_batch_file(path: Path) -> List[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def find_candidates(
    conn: sqlite3.Connection,
    *,
    stages: Sequence[str],
    batch_ids: Optional[Sequence[str]],
    force: bool,
    min_missing_days: float = 0,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """
    Return one row per (batch_id, stage) with successful, non-invalidated
    runs, with output counts from the index and an ``action``:
    ``invalidate``, ``skip_leased``, ``skip_grace``, ``skip_never_indexed`` or
    ``skip_present``.
    """
    stages = [s for s in STAGE_OUTPUTS if s in stages]  # pipeline order
    batch_filter_sql = ""
    batch_params: List = []
    if batch_ids:
        batch_filter_sql = f"AND {{alias}}.batch_id IN ({','.join('?' for _ in batch_ids)})"
        batch_params = list(batch_ids)

    runs = conn.execute(
        f"""
        SELECT sr.batch_id,
               sr.stage,
               COUNT(*)         AS n_runs,
               MAX(sr.ended_at) AS last_success,
               EXISTS (
                   SELECT 1 FROM stage_leases l
                   WHERE  l.batch_id = sr.batch_id
                     AND  l.stage = sr.stage
                     AND  l.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               ) AS leased
        FROM   stage_runs sr
        WHERE  sr.status = 'success'
          AND  sr.invalidated_at IS NULL
          AND  sr.stage IN ({','.join('?' for _ in stages)})
          {batch_filter_sql.format(alias='sr')}
        GROUP BY sr.batch_id, sr.stage
        """,
        (*stages, *batch_params),
    ).fetchall()
    if not runs:
        return []

    # Count every stage's current/stale outputs per batch in one pass. Every
    # referenced column is in idx_gfi_state_type_ext, so this is a single
    # index-only scan per data_state instead of one scan per stage.
    data_states = sorted({STAGE_OUTPUTS[s][0] for s in stages})
    sums = ",\n".join(
        f"SUM(CASE WHEN {_output_sql(s)} THEN g.is_current = 1 ELSE 0 END) AS {s}_current,\n"
        f"SUM(CASE WHEN {_output_sql(s)} THEN g.is_current = 0 ELSE 0 END) AS {s}_stale"
        for s in stages
    )
    outputs = {
        row["batch_id"]: row
        for row in conn.execute(
            f"""
            SELECT g.batch_id,
                   {sums}
            FROM   globus_file_index g
            WHERE  g.entry_type = 'file'
              AND  g.batch_id IS NOT NULL
              AND  g.data_state IN ({','.join('?' for _ in data_states)})
              {batch_filter_sql.format(alias='g')}
            GROUP BY g.batch_id
            """,
            (*data_states, *batch_params),
        )
    }

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=min_missing_days)
    rows: List[Dict] = []
    for run in runs:
        row = dict(run)
        stage = row["stage"]
        out = outputs.get(row["batch_id"])
        row["current_n"] = out[f"{stage}_current"] if out else 0
        row["stale_n"] = out[f"{stage}_stale"] if out else 0
        row["last_missing_since"] = None
        if row["leased"]:
            row["action"] = "skip_leased"
        elif force:
            row["action"] = "invalidate"
        elif row["current_n"] > 0:
            row["action"] = "skip_present"
        elif row["stale_n"] == 0:
            row["action"] = "skip_never_indexed"
        else:
            # Lost; rare, so a per-batch lookup of when it went missing is cheap.
            row["last_missing_since"] = conn.execute(
                f"""
                SELECT MAX(g.missing_since_ts_iso)
                FROM   globus_file_index g
                WHERE  g.batch_id = ? AND g.entry_type = 'file' AND g.is_current = 0
                  AND  {_output_sql(stage)}
                """,
                (row["batch_id"],),
            ).fetchone()[0]
            missing_since = _parse_utc(row["last_missing_since"])
            if min_missing_days > 0 and (missing_since is None or missing_since > cutoff):
                row["action"] = "skip_grace"
            else:
                row["action"] = "invalidate"
        rows.append(row)

    rows.sort(key=lambda r: (stages.index(r["stage"]), r["batch_id"]))
    return rows


def apply_invalidations(
    conn: sqlite3.Connection,
    candidates: Sequence[Dict],
    *,
    reason: Optional[str],
    force: bool,
) -> Dict[str, int]:
    """Invalidate success runs and cancel completed staged_inputs for each candidate."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = {"stage_runs": 0, "staged_inputs": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for c in candidates:
            row_reason = reason or (
                f"forced (index: current={c['current_n']}, stale={c['stale_n']})"
                if force
                else f"outputs missing from index since {c['last_missing_since']} (stale={c['stale_n']})"
            )
            cur = conn.execute(
                """
                UPDATE stage_runs
                SET    invalidated_at = ?, invalidated_reason = ?
                WHERE  batch_id = ? AND stage = ?
                  AND  status = 'success' AND invalidated_at IS NULL
                """,
                (now, row_reason, c["batch_id"], c["stage"]),
            )
            counts["stage_runs"] += cur.rowcount
            cur = conn.execute(
                """
                UPDATE staged_inputs
                SET    status = 'canceled', error_summary = ?, updated_at = ?
                WHERE  batch_id = ? AND stage = ? AND status = 'completed'
                """,
                (f"stage run invalidated: {row_reason}", now, c["batch_id"], c["stage"]),
            )
            counts["staged_inputs"] += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return counts


def print_candidates(rows: Sequence[Dict], *, show_present: bool) -> None:
    shown = [r for r in rows if show_present or r["action"] != "skip_present"]
    if not shown:
        return
    header = (
        f"{'action':<20} {'stage':<13} {'batch_id':<16} {'runs':>4} {'current':>8} {'stale':>8}  "
        f"{'missing_since':<21} last_success"
    )
    print(header)
    print("-" * len(header))
    for r in shown:
        print(
            f"{r['action']:<20} {r['stage']:<13} {r['batch_id']:<16} {r['n_runs']:>4} "
            f"{r['current_n']:>8} {r['stale_n']:>8}  {r['last_missing_since'] or '':<21} {r['last_success']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Invalidate successful stage runs whose outputs were lost so the "
            "readiness views re-queue those batches. Dry run unless --apply."
        )
    )
    parser.add_argument("--db", required=True, type=Path, help="Path to the SQLite pipeline database.")
    parser.add_argument(
        "--stages",
        nargs="+",
        choices=list(STAGE_OUTPUTS),
        default=list(STAGE_OUTPUTS),
        help="Stages to check (default: all).",
    )
    parser.add_argument(
        "--batches",
        type=Path,
        default=None,
        help="Optional batch list file. Limits checking to these batch_ids.",
    )
    parser.add_argument(
        "--min-missing-days",
        type=float,
        default=0,
        help=(
            "Only invalidate detected losses whose outputs have been missing for "
            "at least this many days (default: 0). Not applied with --force."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Invalidate success runs for --batches even if outputs look current. Requires --batches.",
    )
    parser.add_argument("--apply", action="store_true", help="Write the invalidations (default: dry run).")
    parser.add_argument("--reason", default=None, help="Text stored in stage_runs.invalidated_reason.")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    if args.force and not args.batches:
        parser.error("--force requires --batches")
    if args.min_missing_days < 0:
        parser.error("--min-missing-days must be >= 0")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    batch_ids = load_batch_file(args.batches) if args.batches else None
    if args.batches and not batch_ids:
        parser.error(f"No batch_ids found in {args.batches}")

    conn = open_db(args.db)
    try:
        rows = find_candidates(
            conn,
            stages=args.stages,
            batch_ids=batch_ids,
            force=args.force,
            min_missing_days=args.min_missing_days,
        )
        print_candidates(rows, show_present=bool(batch_ids))

        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        to_invalidate = [r for r in rows if r["action"] == "invalidate"]
        print(f"\nSummary: {counts or 'no successful, non-invalidated runs matched'}")

        if not to_invalidate:
            return 0
        if not args.apply:
            print(f"Dry run: re-run with --apply to invalidate {len(to_invalidate)} (batch, stage) pair(s).")
            return 0

        written = apply_invalidations(conn, to_invalidate, reason=args.reason, force=args.force)
        logger.info(
            "Invalidated %d stage_runs row(s) and canceled %d completed staged_inputs row(s) "
            "across %d (batch, stage) pair(s)",
            written["stage_runs"], written["staged_inputs"], len(to_invalidate),
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
