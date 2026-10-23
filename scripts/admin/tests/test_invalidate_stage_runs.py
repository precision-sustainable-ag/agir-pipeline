from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.sqlite_db import open_db

_MODULE_PATH = Path(__file__).resolve().parents[1] / "invalidate_stage_runs.py"
_spec = importlib.util.spec_from_file_location("invalidate_stage_runs", _MODULE_PATH)
invalidate_stage_runs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(invalidate_stage_runs)

find_candidates = invalidate_stage_runs.find_candidates
apply_invalidations = invalidate_stage_runs.apply_invalidations

SCHEMA = Path("schemas/sqlite/pipeline.sql")
BATCH = "NC_2025-01-01"

DEV = "semifield-developed-images"
GEOREF_CSV = f"{BATCH}_georeferenced.csv"

# Files each stage's readiness view needs as input, as
# (data_state, parent_dir, file_ext, file_name).
STAGE_INPUTS = {
    "raw_to_jpg": [("semifield-upload", "raws", "arw", "a.arw")],
    "jpg_to_det": [(DEV, "images", "jpg", "a.jpg")],
    "det_to_world": [
        (DEV, "images", "jpg", "a.jpg"),
        (DEV, "detections", "txt", "a.txt"),
        ("semifield-asfm", "pixel_world_grids", "npz", "a.npz"),
    ],
    "det_to_seg": [
        (DEV, "images", "jpg", "a.jpg"),
        (DEV, "detections", "txt", "a.txt"),
        (DEV, "georeferenced", "csv", GEOREF_CSV),
    ],
    "seg_to_cut": [
        (DEV, "images", "jpg", "a.jpg"),
        (DEV, "segmentations", "png", "a.png"),
        (DEV, "georeferenced", "csv", GEOREF_CSV),
    ],
}

# One output file per stage (what STAGE_OUTPUTS matches), never overlapping
# that stage's own inputs above.
STAGE_OUTPUT_FILE = {
    "raw_to_jpg": (DEV, "images", "jpg", "a.jpg"),
    "jpg_to_det": (DEV, "detections", "txt", "a.txt"),
    "det_to_world": (DEV, "georeferenced", "csv", GEOREF_CSV),
    "det_to_seg": (DEV, "segmentations", "png", "a.png"),
    "seg_to_cut": ("semifield-cutouts", BATCH, "png", "cut.png"),
}


def make_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    db_path = tmp_path / "pipeline.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return db_path, conn


def insert_file(
    conn: sqlite3.Connection,
    data_state: str,
    parent_dir: str,
    file_ext: str,
    file_name: str,
    *,
    is_current: int = 1,
    batch_id: str = BATCH,
    missing_since: str | None = "2025-01-03T00:00:00Z",
    site: str = "JUNO",
) -> None:
    rel_path = f"{data_state}/{batch_id}/{parent_dir}/{file_name}"
    conn.execute(
        """
        INSERT INTO globus_file_index (
            endpoint, site, storage_domain, namespace, storage_root,
            rel_path, full_path, parent_dir, file_name, entry_type, file_ext,
            batch_id, batch_state, batch_date, data_state, is_current,
            missing_since_ts_iso
        )
        VALUES (?, ?, 'dash_agir', 'LTS', '/LTS',
                ?, ?, ?, ?, 'file', ?, ?, 'NC', '2025-01-01', ?, ?, ?)
        """,
        (f"{site.lower()}-endpoint", site, rel_path, f"/LTS/{rel_path}", parent_dir,
         file_name, file_ext, batch_id, data_state, is_current,
         None if is_current else missing_since),
    )
    conn.commit()


def insert_success_run(conn: sqlite3.Connection, stage: str, run_id: str = "run-1") -> None:
    conn.execute(
        """
        INSERT INTO stage_runs (run_id, batch_id, stage, status, exit_code, started_at, ended_at)
        VALUES (?, ?, ?, 'success', 0, '2025-01-02T00:00:00Z', '2025-01-02T01:00:00Z')
        """,
        (run_id, BATCH, stage),
    )
    conn.commit()


def insert_completed_staged_input(conn: sqlite3.Connection, stage: str) -> None:
    conn.execute(
        """
        INSERT INTO staged_inputs (staging_id, batch_id, stage, src_path, dst_path, status)
        VALUES ('stg-1', ?, ?, '/src', '/dst', 'completed')
        """,
        (BATCH, stage),
    )
    conn.commit()


def view_batch_ids(conn: sqlite3.Connection, stage: str) -> set[str]:
    return {r["batch_id"] for r in conn.execute(f"SELECT batch_id FROM v_batches_needing_{stage}")}


def candidate(conn: sqlite3.Connection, stage: str, *, force: bool = False, **kwargs) -> dict:
    rows = find_candidates(conn, stages=[stage], batch_ids=None, force=force, **kwargs)
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("stage", list(STAGE_INPUTS))
def test_lost_outputs_are_invalidated_and_batch_requeued(tmp_path: Path, stage: str) -> None:
    _, conn = make_db(tmp_path)
    for f in STAGE_INPUTS[stage]:
        insert_file(conn, *f)
    insert_file(conn, *STAGE_OUTPUT_FILE[stage], is_current=0)  # indexed once, now gone
    insert_success_run(conn, stage)
    insert_completed_staged_input(conn, stage)

    assert BATCH not in view_batch_ids(conn, stage)

    c = candidate(conn, stage)
    assert (c["action"], c["current_n"], c["stale_n"]) == ("invalidate", 0, 1)

    written = apply_invalidations(conn, [c], reason=None, force=False)
    assert written == {"stage_runs": 1, "staged_inputs": 1}

    assert BATCH in view_batch_ids(conn, stage)
    run = conn.execute("SELECT status, invalidated_at, invalidated_reason FROM stage_runs").fetchone()
    assert run["status"] == "success"
    assert run["invalidated_at"] is not None
    assert run["invalidated_reason"].startswith("outputs missing from index since 2025-01-03T00:00:00Z")
    staged = conn.execute("SELECT status, error_summary FROM staged_inputs").fetchone()
    assert staged["status"] == "canceled"
    assert staged["error_summary"].startswith("stage run invalidated")

    # Invalidated runs are no longer candidates.
    assert find_candidates(conn, stages=[stage], batch_ids=None, force=False) == []


def test_one_pass_counts_each_stage_separately(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    insert_file(conn, DEV, "images", "jpg", "a.jpg", is_current=1)
    insert_file(conn, DEV, "detections", "txt", "a.txt", is_current=0)
    insert_file(conn, DEV, "georeferenced", "csv", GEOREF_CSV, is_current=0)
    insert_file(conn, DEV, "georeferenced", "csv", "old.csv", is_current=1, site="CERES")
    for i, stage in enumerate(STAGE_INPUTS):
        insert_success_run(conn, stage, run_id=f"run-{i}")

    rows = find_candidates(conn, stages=list(STAGE_INPUTS), batch_ids=None, force=False)
    got = {r["stage"]: (r["action"], r["current_n"], r["stale_n"]) for r in rows}
    assert got == {
        "raw_to_jpg": ("skip_present", 1, 0),
        "jpg_to_det": ("invalidate", 0, 1),
        "det_to_world": ("skip_present", 1, 1),  # still current on CERES
        "det_to_seg": ("skip_never_indexed", 0, 0),
        "seg_to_cut": ("skip_never_indexed", 0, 0),
    }
    assert [r["stage"] for r in rows] == list(STAGE_INPUTS)  # pipeline order


def test_grace_period_uses_most_recent_disappearance(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    # Lost on CERES long ago, but the JUNO copy only went missing a day ago.
    insert_file(conn, DEV, "images", "jpg", "a.jpg", is_current=0,
                site="CERES", missing_since="2025-01-01T00:00:00Z")
    insert_file(conn, DEV, "images", "jpg", "a.jpg", is_current=0,
                missing_since="2025-01-09T00:00:00Z")
    insert_success_run(conn, "raw_to_jpg")
    now = datetime(2025, 1, 10, tzinfo=timezone.utc)

    c = candidate(conn, "raw_to_jpg", min_missing_days=2, now=now)
    assert (c["action"], c["last_missing_since"]) == ("skip_grace", "2025-01-09T00:00:00Z")
    assert candidate(conn, "raw_to_jpg", min_missing_days=0.5, now=now)["action"] == "invalidate"
    assert candidate(conn, "raw_to_jpg", now=now)["action"] == "invalidate"


def test_grace_period_skips_unknown_missing_since(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    insert_file(conn, DEV, "detections", "txt", "a.txt", is_current=0, missing_since=None)
    insert_success_run(conn, "jpg_to_det")

    assert candidate(conn, "jpg_to_det", min_missing_days=2)["action"] == "skip_grace"
    assert candidate(conn, "jpg_to_det")["action"] == "invalidate"


def test_current_outputs_are_left_alone(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    insert_file(conn, *STAGE_OUTPUT_FILE["raw_to_jpg"], is_current=1)
    insert_file(conn, DEV, "images", "jpg", "b.jpg", is_current=0)
    insert_success_run(conn, "raw_to_jpg")

    assert candidate(conn, "raw_to_jpg")["action"] == "skip_present"


def test_never_indexed_outputs_need_force(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    insert_success_run(conn, "jpg_to_det")

    assert candidate(conn, "jpg_to_det")["action"] == "skip_never_indexed"

    c = candidate(conn, "jpg_to_det", force=True)
    assert c["action"] == "invalidate"
    apply_invalidations(conn, [c], reason="operator: root wiped", force=True)
    reason = conn.execute("SELECT invalidated_reason FROM stage_runs").fetchone()[0]
    assert reason == "operator: root wiped"


def test_active_lease_is_skipped_even_with_force(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    insert_file(conn, *STAGE_OUTPUT_FILE["det_to_seg"], is_current=0)
    insert_success_run(conn, "det_to_seg")
    conn.execute(
        """
        INSERT INTO stage_leases (lease_id, batch_id, stage, orchestrator_id, expires_at)
        VALUES ('l1', ?, 'det_to_seg', 'test', '2999-01-01T00:00:00Z')
        """,
        (BATCH,),
    )
    conn.commit()

    assert candidate(conn, "det_to_seg")["action"] == "skip_leased"
    assert candidate(conn, "det_to_seg", force=True)["action"] == "skip_leased"


def test_batch_filter_and_other_stages_untouched(tmp_path: Path) -> None:
    _, conn = make_db(tmp_path)
    insert_file(conn, *STAGE_OUTPUT_FILE["raw_to_jpg"], is_current=0)
    insert_file(conn, DEV, "images", "jpg", "a.jpg", is_current=0, batch_id="MD_2025-01-01")
    insert_success_run(conn, "raw_to_jpg", run_id="run-nc")
    conn.execute(
        """
        INSERT INTO stage_runs (run_id, batch_id, stage, status, exit_code, started_at, ended_at)
        VALUES ('run-md', 'MD_2025-01-01', 'raw_to_jpg', 'success', 0, 'x', 'x')
        """
    )
    conn.commit()

    rows = find_candidates(conn, stages=["raw_to_jpg"], batch_ids=[BATCH], force=False)
    assert [r["batch_id"] for r in rows] == [BATCH]


def test_open_db_adds_columns_to_pre_v13_stage_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    # stage_runs as created before v13, then the current schema applied on top:
    # CREATE TABLE IF NOT EXISTS leaves the old table, so the views reference a
    # column that isn't there yet.
    conn.execute(
        """
        CREATE TABLE stage_runs (
            run_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, stage TEXT NOT NULL,
            stage_version TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            exit_code INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(sqlite3.OperationalError, match="invalidated_at"):
        conn.execute("SELECT * FROM v_batches_needing_raw_to_jpg").fetchall()
    conn.close()

    conn = open_db(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stage_runs)")}
    assert {"invalidated_at", "invalidated_reason"} <= cols
    conn.execute("SELECT * FROM v_batches_needing_raw_to_jpg").fetchall()
    conn.close()

    # Second open is a no-op.
    open_db(db_path).close()
