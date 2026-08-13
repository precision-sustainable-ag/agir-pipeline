"""
orchestrator/sqlite_db.py
==========================

Minimal SQLite orchestration helpers for the AgIR pipeline.

These functions operate on the schema defined in schemas/sqlite/pipeline.sql
and use only the Python standard-library sqlite3 module.

Public API
----------
open_db(db_path, *, readonly=False, local_copy=False, temp_dir=None)
    → sqlite3.Connection
create_sqlite_snapshot(db_path, snapshot_path) → Path
claim_stage_lease(conn, batch_id, stage, orchestrator_id, ttl_seconds)  → dict
release_stage_lease(conn, lease_id, orchestrator_id)  → bool
request_input_staging(conn, ...)  → dict
mark_input_staging_status(conn, staging_id, status, ...)  → dict
get_completed_input_staging_batch_ids(conn, stage, batch_ids)  → set[str]
register_result_sync(conn, ...)  → dict
mark_result_sync_status(conn, run_id, status, ...)  → dict
get_result_syncs(conn, ...)  → list[dict]
reopen_result_sync(conn, run_id)  → dict
ingest_run_report(conn, run_report_path)  → dict
get_batches_needing_raw_to_jpg(conn, *, site=None, limit=200)  → list[dict]
get_batches_needing_det_to_seg(conn, *, limit=200)  → list[dict]
resolve_season_for_batch(conn, *, site, batch_date)  → dict | None
resolve_file_path_with_priority(conn, rel_path, *, priority=...)  → str | None

Lease semantics
---------------
* claim_stage_lease uses BEGIN IMMEDIATE to serialise concurrent callers.
* An expired lease (expires_at <= now) is silently replaced.
* release_stage_lease deletes the lease row; calling it twice is safe
  (second call returns False, no exception).

Ingest semantics
----------------
* ingest_run_report is idempotent: INSERT ... ON CONFLICT (run_id) DO UPDATE
  so rerunning after a crash never duplicates rows.
* The caller must commit after calling ingest_run_report.
"""

from __future__ import annotations

import json
import tempfile
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


_JPG_EXTS = "('jpg','jpeg','JPG','JPEG')"
_ACTIVE_STAGING_STATUSES = ("planned", "requested", "submitted", "active")
_TERMINAL_STAGING_STATUSES = ("completed", "failed", "canceled")
_VALID_STAGING_STATUSES = _ACTIVE_STAGING_STATUSES + _TERMINAL_STAGING_STATUSES
_VALID_RESULT_SYNC_STATUSES = (
    "requested",
    "transferring",
    "transferred",
    "verified",
    "ingested",
    "failed",
    "canceled",
)
_RESULT_SYNC_TRANSITIONS = {
    "requested": {"requested", "transferring", "failed", "canceled"},
    "transferring": {"transferring", "transferred", "failed", "canceled"},
    "transferred": {"transferred", "verified", "failed", "canceled"},
    "verified": {"verified", "ingested", "failed", "canceled"},
    "ingested": {"ingested"},
    "failed": {"failed"},
    "canceled": {"canceled"},
}


def get_batches_needing_jpg_to_det(
    conn: sqlite3.Connection,
    *,
    site: Optional[str] = None,
    limit: int = 200,
    batch_ids: Optional[Sequence[str]] = None,
) -> List[Dict]:
    batch_filter_sql = ""
    filter_params: List = []
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        batch_filter_sql = f"AND v.batch_id IN ({placeholders})"
        filter_params = list(batch_ids)

    if site:
        rows = conn.execute(
            f"""
            SELECT
                v.batch_id, v.batch_date, v.jpg_count, v.det_count,
                g.site, g.storage_domain, g.storage_root
            FROM v_batches_needing_jpg_to_det v
            JOIN globus_file_index g
              ON  g.batch_id   = v.batch_id
             AND  g.data_state = 'semifield-developed-images'
             AND  g.entry_type = 'file'
             AND  g.is_current = 1
             AND  g.file_ext   IN {_JPG_EXTS}
             AND  g.parent_dir = 'images'
             AND  g.site       = ?
            WHERE 1=1 {batch_filter_sql}
            GROUP BY v.batch_id
            ORDER BY v.batch_date ASC, v.batch_id ASC
            LIMIT ?
            """,
            (site, *filter_params, limit),
        ).fetchall()
    else:
        # v_batches_needing_jpg_to_det now has one row per (batch_id, site,
        # storage_domain, namespace) location holding the batch's JPGs;
        # jpg_count/det_count are identical across those rows, so DISTINCT
        # on this projection collapses back to one row per batch.
        rows = conn.execute(
            f"""
            SELECT DISTINCT v.batch_id, v.batch_date, v.jpg_count, v.det_count
            FROM   v_batches_needing_jpg_to_det v
            WHERE  1=1 {batch_filter_sql}
            ORDER  BY v.batch_date ASC, v.batch_id ASC
            LIMIT  ?
            """,
            (*filter_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_batches_needing_det_to_seg(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    batch_ids: Optional[Sequence[str]] = None,
) -> List[Dict]:
    """
    Return logically ready detection-to-segmentation batches.

    The readiness view considers current JPG and detection inventories across
    JUNO and ATLAS together. Input-location precedence and materialization are
    intentionally handled later by the input-staging workflow.
    """
    batch_filter_sql = ""
    filter_params: List = []
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        batch_filter_sql = f"WHERE v.batch_id IN ({placeholders})"
        filter_params = list(batch_ids)

    rows = conn.execute(
        f"""
        SELECT
            v.batch_id,
            v.batch_date,
            v.jpg_count,
            v.det_count,
            v.mask_count
        FROM v_batches_needing_det_to_seg v
        {batch_filter_sql}
        ORDER BY v.batch_date ASC, v.batch_id ASC
        LIMIT ?
        """,
        (*filter_params, limit),
    ).fetchall()
    return [dict(r) for r in rows]



def get_batches_needing_det_to_world(
    conn: sqlite3.Connection,
    *,
    site: Optional[str] = None,
    limit: int = 200,
    batch_ids: Optional[Sequence[str]] = None,
) -> List[Dict]:
    """
    Return rows from ``v_batches_needing_det_to_world``.

    The view already requires that a batch has current detection files,
    developed-image files, and pixel-to-world NPZ grids (at any site) and no
    current georeferenced output. Passing ``site`` additionally restricts to
    batches whose detection files are currently indexed at that site (mirrors
    ``get_batches_needing_jpg_to_det``'s use of ``site`` for its images join).
    """
    batch_filter_sql = ""
    filter_params: List = []
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        batch_filter_sql = f"AND v.batch_id IN ({placeholders})"
        filter_params = list(batch_ids)

    if site:
        rows = conn.execute(
            f"""
            SELECT
                v.batch_id, v.batch_date, v.img_count, v.det_count,
                v.grid_count, v.georef_count,
                g.site, g.storage_domain, g.storage_root
            FROM v_batches_needing_det_to_world v
            JOIN globus_file_index g
              ON  g.batch_id   = v.batch_id
             AND  g.data_state = 'semifield-developed-images'
             AND  g.entry_type = 'file'
             AND  g.is_current = 1
             AND  g.parent_dir IN ('detections', 'plant-detections', 'metadata')
             AND  g.site       = ?
            WHERE 1=1 {batch_filter_sql}
            GROUP BY v.batch_id
            ORDER BY v.batch_date ASC, v.batch_id ASC
            LIMIT ?
            """,
            (site, *filter_params, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT v.batch_id, v.batch_date, v.img_count, v.det_count,
                   v.grid_count, v.georef_count
            FROM   v_batches_needing_det_to_world v
            WHERE  1=1 {batch_filter_sql}
            ORDER  BY v.batch_date ASC, v.batch_id ASC
            LIMIT  ?
            """,
            (*filter_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_det_to_world_staged_batch_ids(
    conn: sqlite3.Connection,
    *,
    stage: str,
    expected_dst_paths: Dict[str, Sequence[str]],
) -> set[str]:
    """
    Return the subset of batch_ids (keys of ``expected_dst_paths``) for which
    every expected ``staged_inputs`` dst_path has ``status='completed'``.

    Multi-piece stages (det_to_world: images + detections + grids;
    det_to_seg: images + detections) each have independent dst_paths per
    piece (see ``orchestrator.input_staging_planner.det_to_world_expected_dst_paths``
    / ``det_to_seg_expected_dst_paths``). Unlike
    ``get_completed_input_staging_batch_ids`` (which only checks "any row
    completed" — correct for raw_to_jpg/jpg_to_det's single fixed route),
    this requires ALL of a batch's expected pieces to show completed. Pieces
    already resident at the destination when planned are recorded as
    immediately-completed rows too (see ``StagingRequest.already_satisfied``),
    so this reflects readiness the moment
    ``poll_stage_inputs.py``/``stage_inputs.py`` mark them — no fresh
    ``globus_file_index`` inventory scan required.
    """
    ready: set[str] = set()
    for batch_id, dst_paths in expected_dst_paths.items():
        dst_paths = list(dst_paths)
        if not dst_paths:
            continue
        placeholders = ",".join("?" for _ in dst_paths)
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT dst_path) AS n
            FROM staged_inputs
            WHERE stage = ?
              AND status = 'completed'
              AND batch_id = ?
              AND dst_path IN ({placeholders})
            """,
            (stage, batch_id, *dst_paths),
        ).fetchone()
        if row and row["n"] == len(dst_paths):
            ready.add(batch_id)
    return ready


class _TempConnection:
    """
    Wraps a sqlite3.Connection opened against a temp file copy of the DB.
    Deletes the temp file when close() is called.
    Delegates all attribute access to the underlying connection.
    """
    def __init__(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_tmp_path", tmp_path)

    def close(self) -> None:
        self._conn.close()
        try:
            self._tmp_path.unlink()
            logger.debug("Removed local scratch copy: %s", self._tmp_path)
        except OSError:
            pass

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()



# RAW extensions recognised by globus_file_index and v_batches_needing_raw_to_jpg.
_RAW_EXTS = "('RAW','raw','arw','nef','cr2','cr3','dng','rw2','raf','orf')"

# Maps run_report.status values to the SQLite stage_runs CHECK constraint values.
_STATUS_MAP: Dict[str, str] = {
    "success":        "success",
    "partial":        "partial_success",
    "partial_success":"partial_success",
    "failed":         "failed",
    "canceled":       "canceled",
    "skipped":        "skipped",
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def open_db(
    db_path: str | Path,
    *,
    readonly: bool = False,
    local_copy: bool = False,
    temp_dir: str | Path | None = None,
) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database.

    Parameters
    ----------
    db_path : str or Path
        Path to the SQLite file.
    readonly : bool
        If True, enforce read-only access with SQLite URI mode and query_only.
    local_copy : bool
        If True (and readonly=True), create a consistent SQLite snapshot in a
        temporary file before opening. Use this when querying the original DB
        directly is too slow. The temporary file is deleted when the connection
        is closed.
    temp_dir : str or Path, optional
        Directory in which to create the temporary database copy. If omitted,
        use the system temporary directory. This is useful on HPC login nodes
        where the system temporary filesystem is too small for the index.

    Returns
    -------
    sqlite3.Connection
        Row factory set to sqlite3.Row for dict-like access.
    """
    db_path = Path(db_path)

    if readonly and local_copy:
        if not db_path.exists():
            raise FileNotFoundError(f"SQLite DB not found: {db_path}")

        copy_dir = Path(temp_dir) if temp_dir is not None else None
        if copy_dir is not None:
            copy_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            prefix=f"{db_path.stem}.",
            suffix=".sqlite3",
            dir=copy_dir,
            delete=False,
        ) as tmp_file:
            tmp = Path(tmp_file.name)
        logger.debug(
            "Creating SQLite snapshot in temporary storage: %s -> %s",
            db_path,
            tmp,
        )
        source = None
        destination = None
        try:
            try:
                source_uri = f"{db_path.resolve().as_uri()}?mode=ro"
                source = sqlite3.connect(source_uri, uri=True, timeout=60)
                destination = sqlite3.connect(str(tmp), timeout=60)
                source.backup(destination, pages=1000, sleep=0.1)
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        inner = sqlite3.connect(str(tmp), timeout=60)
        inner.execute("PRAGMA query_only=ON;")
        inner.execute("PRAGMA busy_timeout=60000;")
        inner.row_factory = sqlite3.Row
        return _TempConnection(inner, tmp)

    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"SQLite DB not found: {db_path}")
        source_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(source_uri, uri=True, timeout=60)
        conn.execute("PRAGMA query_only=ON;")
        conn.execute("PRAGMA busy_timeout=60000;")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=120)
        # NFS mount doesn't support WAL memory-mapped files; use DELETE mode
        # with NFS byte-range locks instead. Only change journal_mode if needed
        # to avoid contending for exclusive locks on every open_db() call.
        current_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if current_mode.lower() != "delete":
            conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=120000;")
        conn.execute("PRAGMA foreign_keys=ON;")

    conn.row_factory = sqlite3.Row
    logger.debug("Opened SQLite DB: %s (readonly=%s)", db_path, readonly)
    return conn


def create_sqlite_snapshot(
    db_path: str | Path,
    snapshot_path: str | Path,
) -> Path:
    """Create and atomically publish a consistent snapshot of a live SQLite DB.

    SQLite's backup API includes committed WAL contents. The destination is
    built beside the requested path, checked for integrity, and then replaced
    atomically so readers never observe a partially written database.
    """
    source_path = Path(db_path)
    destination_path = Path(snapshot_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite DB not found: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("Snapshot path must differ from the canonical database path")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
        delete=False,
    ) as tmp_file:
        temporary_path = Path(tmp_file.name)

    source = None
    destination = None
    try:
        source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=60)
        destination = sqlite3.connect(str(temporary_path), timeout=60)
        source.backup(destination, pages=1000, sleep=0.1)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.DatabaseError(
                f"Snapshot integrity check failed: {integrity[0] if integrity else 'no result'}"
            )
        destination.close()
        destination = None
        source.close()
        source = None
        temporary_path.replace(destination_path)
        return destination_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


# ---------------------------------------------------------------------------
# Lease management
# ---------------------------------------------------------------------------

def claim_stage_lease(
    conn: sqlite3.Connection,
    batch_id: str,
    stage: str,
    orchestrator_id: str,
    ttl_seconds: int,
) -> Dict:
    """
    Atomically claim a stage lease for (batch_id, stage).

    Uses BEGIN IMMEDIATE to serialise concurrent callers on the same DB file.

    Returns
    -------
    dict
        ``{"claimed": True, "lease_id": <uuid>, "batch_id": ..., "stage": ...,
           "expires_at": <iso8601>}``
        or
        ``{"claimed": False, "lease_id": <existing>, ...}`` if an active lease
        already exists.
    """
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (now_utc + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_lease_id = str(uuid.uuid4())

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT lease_id, expires_at FROM stage_leases WHERE batch_id = ? AND stage = ?",
            (batch_id, stage),
        ).fetchone()

        if row:
            existing_lease_id = row["lease_id"]
            existing_expires  = row["expires_at"]
            if existing_expires > now_str:
                # Active lease held by another orchestrator.
                conn.execute("ROLLBACK")
                logger.debug(
                    "Lease already active for %s/%s (lease_id=%s, expires=%s)",
                    batch_id, stage, existing_lease_id, existing_expires,
                )
                return {
                    "claimed": False,
                    "lease_id": existing_lease_id,
                    "batch_id": batch_id,
                    "stage":    stage,
                    "expires_at": existing_expires,
                }
            # Expired lease — replace it.
            logger.info(
                "Replacing expired lease for %s/%s (was %s, expired %s)",
                batch_id, stage, existing_lease_id, existing_expires,
            )
            conn.execute(
                "DELETE FROM stage_leases WHERE batch_id = ? AND stage = ?",
                (batch_id, stage),
            )

        conn.execute(
            """
            INSERT INTO stage_leases
                (lease_id, batch_id, stage, orchestrator_id, claimed_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_lease_id, batch_id, stage, orchestrator_id, now_str, expires_at),
        )
        conn.execute("COMMIT")
        logger.info(
            "Claimed lease %s for %s/%s (expires %s)",
            new_lease_id, batch_id, stage, expires_at,
        )
        return {
            "claimed":    True,
            "lease_id":   new_lease_id,
            "batch_id":   batch_id,
            "stage":      stage,
            "expires_at": expires_at,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def update_lease_slurm_job_id(
    conn: sqlite3.Connection,
    lease_id: str,
    slurm_job_id: str,
) -> None:
    """Record the SLURM job ID on the lease row after sbatch succeeds."""
    conn.execute(
        "UPDATE stage_leases SET slurm_job_id = ? WHERE lease_id = ?",
        (slurm_job_id, lease_id),
    )
    conn.commit()
    logger.debug("Recorded slurm_job_id=%s on lease %s", slurm_job_id, lease_id)


def release_stage_lease(
    conn: sqlite3.Connection,
    lease_id: str,
    orchestrator_id: str,
) -> bool:
    """
    Delete the lease row for *lease_id* owned by *orchestrator_id*.

    Returns True if a row was deleted, False if not found or not owned.
    Calling twice is safe — the second call returns False.
    """
    cursor = conn.execute(
        "DELETE FROM stage_leases WHERE lease_id = ? AND orchestrator_id = ?",
        (lease_id, orchestrator_id),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    if deleted:
        logger.info("Released lease %s", lease_id)
    else:
        logger.warning(
            "Lease %s not found or not owned by %s; may have already been released",
            lease_id, orchestrator_id,
        )
    return deleted


# ---------------------------------------------------------------------------
# Input staging tracking
# ---------------------------------------------------------------------------

def request_input_staging(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    stage: str,
    src_path: str,
    dst_path: str,
    src_endpoint: str = "",
    dst_endpoint: str = "",
    requested_by: str = "",
    priority: int = 100,
) -> Dict:
    """
    Idempotently record an input-staging request.

    A request is unique by ``(batch_id, stage, src_path, dst_path)``. Existing
    completed requests are returned as ``already_completed``. Existing active
    requests are returned as ``already_active``. Failed or canceled requests are
    reopened with a fresh ``requested`` status and cleared error/task metadata.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT staging_id, status, globus_task_id
            FROM staged_inputs
            WHERE batch_id = ? AND stage = ? AND src_path = ? AND dst_path = ?
            """,
            (batch_id, stage, src_path, dst_path),
        ).fetchone()

        if row:
            staging_id = row["staging_id"]
            status = row["status"]
            if status == "completed":
                conn.execute("COMMIT")
                return {
                    "accepted": False,
                    "state": "already_completed",
                    "staging_id": staging_id,
                    "batch_id": batch_id,
                    "stage": stage,
                    "status": status,
                    "globus_task_id": row["globus_task_id"],
                }
            if status in _ACTIVE_STAGING_STATUSES:
                conn.execute("COMMIT")
                return {
                    "accepted": False,
                    "state": "already_active",
                    "staging_id": staging_id,
                    "batch_id": batch_id,
                    "stage": stage,
                    "status": status,
                    "globus_task_id": row["globus_task_id"],
                }

            conn.execute(
                """
                UPDATE staged_inputs
                SET status = 'requested',
                    src_endpoint = ?,
                    dst_endpoint = ?,
                    requested_by = ?,
                    priority = ?,
                    globus_task_id = NULL,
                    error_summary = NULL,
                    requested_at = ?,
                    submitted_at = NULL,
                    completed_at = NULL,
                    updated_at = ?
                WHERE staging_id = ?
                """,
                (
                    src_endpoint,
                    dst_endpoint,
                    requested_by,
                    int(priority),
                    now_str,
                    now_str,
                    staging_id,
                ),
            )
            conn.execute("COMMIT")
            return {
                "accepted": True,
                "state": "reopened",
                "staging_id": staging_id,
                "batch_id": batch_id,
                "stage": stage,
                "status": "requested",
            }

        staging_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO staged_inputs (
                staging_id, batch_id, stage,
                src_endpoint, dst_endpoint, src_path, dst_path,
                status, requested_by, priority, requested_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?)
            """,
            (
                staging_id,
                batch_id,
                stage,
                src_endpoint,
                dst_endpoint,
                src_path,
                dst_path,
                requested_by,
                int(priority),
                now_str,
                now_str,
            ),
        )
        conn.execute("COMMIT")
        return {
            "accepted": True,
            "state": "created",
            "staging_id": staging_id,
            "batch_id": batch_id,
            "stage": stage,
            "status": "requested",
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def mark_input_staging_status(
    conn: sqlite3.Connection,
    *,
    staging_id: str,
    status: str,
    globus_task_id: Optional[str] = None,
    error_summary: Optional[str] = None,
) -> Dict:
    """Update one staged-input row after transfer submission or polling."""
    if status not in _VALID_STAGING_STATUSES:
        raise ValueError(
            f"Invalid input staging status {status!r}; expected one of {_VALID_STAGING_STATUSES}"
        )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    submitted_at = now_str if status in {"submitted", "active", "completed"} else None
    completed_at = now_str if status in _TERMINAL_STAGING_STATUSES else None

    row = conn.execute(
        """
        SELECT submitted_at, completed_at
        FROM staged_inputs
        WHERE staging_id = ?
        """,
        (staging_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown staging_id: {staging_id}")

    conn.execute(
        """
        UPDATE staged_inputs
        SET status = ?,
            globus_task_id = COALESCE(?, globus_task_id),
            error_summary = ?,
            submitted_at = COALESCE(submitted_at, ?),
            completed_at = COALESCE(completed_at, ?),
            updated_at = ?
        WHERE staging_id = ?
        """,
        (
            status,
            globus_task_id,
            error_summary,
            submitted_at,
            completed_at,
            now_str,
            staging_id,
        ),
    )
    conn.commit()

    updated = conn.execute(
        "SELECT * FROM staged_inputs WHERE staging_id = ?",
        (staging_id,),
    ).fetchone()
    return dict(updated)


def get_input_staging_requests(
    conn: sqlite3.Connection,
    *,
    statuses: Optional[Sequence[str]] = None,
    stage: Optional[str] = None,
    require_globus_task_id: bool = False,
    limit: int = 200,
) -> List[Dict]:
    """Return staged-input rows ordered for transfer execution/polling."""
    clauses: List[str] = []
    params: List[object] = []

    if statuses:
        invalid = [s for s in statuses if s not in _VALID_STAGING_STATUSES]
        if invalid:
            raise ValueError(f"Invalid input staging statuses: {invalid}")
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if stage:
        clauses.append("stage = ?")
        params.append(stage)

    if require_globus_task_id:
        clauses.append("globus_task_id IS NOT NULL")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT *
        FROM staged_inputs
        {where}
        ORDER BY priority ASC, requested_at ASC, batch_id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_completed_input_staging_batch_ids(
    conn: sqlite3.Connection,
    *,
    stage: str,
    batch_ids: Sequence[str],
) -> set[str]:
    """Return batch_ids with completed input staging for the given stage."""
    unique_batch_ids = list(dict.fromkeys(batch_ids))
    if not unique_batch_ids:
        return set()

    placeholders = ",".join("?" for _ in unique_batch_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT batch_id
        FROM staged_inputs
        WHERE stage = ?
          AND status = 'completed'
          AND batch_id IN ({placeholders})
        """,
        [stage, *unique_batch_ids],
    ).fetchall()
    return {row["batch_id"] for row in rows}


# ---------------------------------------------------------------------------
# Atlas-to-Ceres result synchronization tracking
# ---------------------------------------------------------------------------


def register_result_sync(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    batch_id: str,
    stage: str,
    run_status: str,
    promotion_succeeded: bool,
    src_endpoint: str,
    dst_endpoint: str,
    src_path: str,
    dst_path: str,
    request_path: str,
    request_json: Dict | str,
    request_created_at: str,
    recursive: bool = True,
    source_site: str = "ATLAS",
    destination_site: str = "CERES",
) -> Dict:
    """
    Idempotently register one Atlas-to-Ceres result synchronization.

    One row owns one immutable run-bundle transfer. Reimporting the same
    request returns the existing row; conflicting identity, routing, run
    outcome, or request content raises ``ValueError``.
    """
    normalized_run_status = _STATUS_MAP.get(run_status)
    if normalized_run_status is None:
        raise ValueError(
            f"Invalid result sync run status {run_status!r}; "
            f"expected one of {sorted(_STATUS_MAP)}"
        )
    if promotion_succeeded and normalized_run_status != "success":
        raise ValueError("Promotion cannot succeed unless run_status is success")

    required_strings = {
        "run_id": run_id,
        "batch_id": batch_id,
        "stage": stage,
        "src_endpoint": src_endpoint,
        "dst_endpoint": dst_endpoint,
        "src_path": src_path,
        "dst_path": dst_path,
        "request_path": request_path,
        "request_created_at": request_created_at,
    }
    empty = sorted(
        name
        for name, value in required_strings.items()
        if not isinstance(value, str) or not value.strip()
    )
    if empty:
        raise ValueError(f"Result sync fields must be non-empty strings: {empty}")

    if isinstance(request_json, str):
        json.loads(request_json)
        serialized_request = request_json
    else:
        serialized_request = json.dumps(request_json, sort_keys=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM result_syncs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if existing is not None:
            identity_conflicts = {
                "batch_id": batch_id,
                "stage": stage,
                "run_status": normalized_run_status,
                "promotion_succeeded": int(bool(promotion_succeeded)),
            }
            conflicting_identity = [
                name for name, value in identity_conflicts.items() if existing[name] != value
            ]
            if conflicting_identity:
                raise ValueError(
                    f"Conflicting result sync identity for run_id {run_id!r}: "
                    f"fields={conflicting_identity}"
                )
            route_conflicts = {
                "source_site": source_site,
                "destination_site": destination_site,
                "src_endpoint": src_endpoint,
                "dst_endpoint": dst_endpoint,
                "src_path": src_path,
                "dst_path": dst_path,
                "recursive": int(bool(recursive)),
            }
            conflicting_route = [
                name for name, value in route_conflicts.items() if existing[name] != value
            ]
            if conflicting_route:
                raise ValueError(
                    f"Conflicting result sync route for run_id {run_id!r}: "
                    f"fields={conflicting_route}"
                )
            if json.loads(existing["request_json"]) != json.loads(serialized_request):
                raise ValueError(f"Conflicting result sync request content for run_id {run_id!r}")
            conn.execute("COMMIT")
            result = dict(existing)
            result.update({"accepted": False, "state": "existing"})
            return result

        conn.execute(
            """
            INSERT INTO result_syncs (
                run_id, batch_id, stage, run_status, promotion_succeeded,
                source_site, destination_site,
                src_endpoint, dst_endpoint, src_path, dst_path, recursive,
                request_path, request_json,
                status, attempt_count, request_created_at, registered_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?,
                'requested', 0, ?, ?, ?
            )
            """,
            (
                str(run_id),
                batch_id,
                stage,
                normalized_run_status,
                int(bool(promotion_succeeded)),
                source_site,
                destination_site,
                src_endpoint,
                dst_endpoint,
                src_path,
                dst_path,
                int(bool(recursive)),
                request_path,
                serialized_request,
                request_created_at,
                now_str,
                now_str,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    created = conn.execute(
        "SELECT * FROM result_syncs WHERE run_id = ?",
        (str(run_id),),
    ).fetchone()
    result = dict(created)
    result.update({"accepted": True, "state": "created"})
    return result


def mark_result_sync_status(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    globus_task_id: Optional[str] = None,
    error_summary: Optional[str] = None,
) -> Dict:
    """Atomically advance one run-bundle synchronization lifecycle."""
    if status not in _VALID_RESULT_SYNC_STATUSES:
        raise ValueError(
            f"Invalid result sync status {status!r}; "
            f"expected one of {_VALID_RESULT_SYNC_STATUSES}"
        )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """
            SELECT status, globus_task_id, attempt_count
            FROM result_syncs
            WHERE run_id = ?
            """,
            (str(run_id),),
        ).fetchone()
        if current is None:
            raise ValueError(f"Unknown result sync run_id: {run_id}")
        if status not in _RESULT_SYNC_TRANSITIONS[current["status"]]:
            raise ValueError(
                f"Invalid result sync transition for {run_id!r}: "
                f"{current['status']!r} -> {status!r}"
            )

        new_attempt = status == "transferring" and current["status"] != "transferring"
        attempt_count = int(current["attempt_count"]) + int(new_attempt)
        resolved_task_id = (
            None if status == "requested" else globus_task_id or current["globus_task_id"]
        )

        conn.execute(
            """
            UPDATE result_syncs
            SET status = ?,
                globus_task_id = ?,
                attempt_count = ?,
                error_summary = ?,
                transfer_started_at = CASE
                    WHEN ? = 'transferring'
                    THEN COALESCE(transfer_started_at, ?)
                    ELSE transfer_started_at
                END,
                transferred_at = CASE
                    WHEN ? = 'transferred' THEN ?
                    ELSE transferred_at
                END,
                verified_at = CASE
                    WHEN ? = 'verified' THEN ?
                    ELSE verified_at
                END,
                ingested_at = CASE
                    WHEN ? = 'ingested' THEN ?
                    ELSE ingested_at
                END,
                updated_at = ?
            WHERE run_id = ?
            """,
            (
                status,
                resolved_task_id,
                attempt_count,
                error_summary,
                status,
                now_str,
                status,
                now_str,
                status,
                now_str,
                status,
                now_str,
                now_str,
                str(run_id),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    updated = conn.execute(
        "SELECT * FROM result_syncs WHERE run_id = ?",
        (str(run_id),),
    ).fetchone()
    return dict(updated)


def get_result_syncs(
    conn: sqlite3.Connection,
    *,
    statuses: Optional[Sequence[str]] = None,
    stage: Optional[str] = None,
    require_globus_task_id: bool = False,
    limit: int = 200,
) -> List[Dict]:
    """Return result synchronizations ordered by registration time."""
    clauses: List[str] = []
    params: List[object] = []
    if statuses:
        invalid = [status for status in statuses if status not in _VALID_RESULT_SYNC_STATUSES]
        if invalid:
            raise ValueError(f"Invalid result sync statuses: {invalid}")
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if stage:
        clauses.append("stage = ?")
        params.append(stage)
    if require_globus_task_id:
        clauses.append("globus_task_id IS NOT NULL")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT *
        FROM result_syncs
        {where}
        ORDER BY registered_at ASC, run_id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def reopen_result_sync(conn: sqlite3.Connection, *, run_id: str) -> Dict:
    """Reset a failed or canceled run-bundle synchronization for retry."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn.execute("BEGIN IMMEDIATE")
        sync = conn.execute(
            "SELECT status FROM result_syncs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if sync is None:
            raise ValueError(f"Unknown result sync run_id: {run_id}")
        if sync["status"] not in {"failed", "canceled"}:
            raise ValueError(
                f"Result sync {run_id!r} is {sync['status']!r}; "
                "only failed or canceled syncs can be reopened"
            )

        conn.execute(
            """
            UPDATE result_syncs
            SET status = 'requested',
                globus_task_id = NULL,
                error_summary = NULL,
                transfer_started_at = NULL,
                transferred_at = NULL,
                verified_at = NULL,
                ingested_at = NULL,
                updated_at = ?
            WHERE run_id = ?
            """,
            (now_str, str(run_id)),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    updated = conn.execute(
        "SELECT * FROM result_syncs WHERE run_id = ?",
        (str(run_id),),
    ).fetchone()
    return dict(updated)


# ---------------------------------------------------------------------------
# Run report ingestion
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string, handling fractional seconds."""
    ts = ts.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts!r}")


def ingest_run_report(conn: sqlite3.Connection, run_report_path: str | Path) -> Dict:
    """
    Ingest a ``run_report.json`` into the ``stage_runs`` table.

    Idempotent: ``INSERT ... ON CONFLICT (run_id) DO UPDATE`` so rerunning
    after a crash is safe.

    Does **not** commit — the caller should call ``conn.commit()`` after this
    function returns (allows batching with other writes).

    Parameters
    ----------
    conn : sqlite3.Connection
        Open, writable connection to the pipeline SQLite DB.
    run_report_path : str or Path
        Path to the ``run_report.json`` produced by the stage CLI.

    Returns
    -------
    dict
        ``{"run_id": ..., "batch_id": ..., "stage": ..., "status": ...}``

    Raises
    ------
    ValueError
        If required fields are missing or status is unrecognised.
    """
    path = Path(run_report_path)
    report: Dict = json.loads(path.read_text(encoding="utf-8"))

    required = ["run_id", "batch_id", "stage", "status", "started_at", "ended_at"]
    missing = [k for k in required if k not in report]
    if missing:
        raise ValueError(f"run_report missing required fields: {missing}")

    status_raw = report["status"]
    status = _STATUS_MAP.get(status_raw)
    if status is None:
        raise ValueError(
            f"Unrecognised run_report status {status_raw!r}; "
            f"expected one of {sorted(_STATUS_MAP)}"
        )

    provenance = report.get("provenance") or {}
    inputs_d   = report.get("inputs")    or {}
    outputs_d  = report.get("outputs")   or {}
    pointers   = report.get("pointers")  or {}

    # duration_ms: prefer explicit field; recompute from timestamps if absent.
    duration_ms: int = report.get("duration_ms") or 0
    if not duration_ms:
        try:
            duration_ms = int(
                (_parse_iso(report["ended_at"]) - _parse_iso(report["started_at"]))
                .total_seconds() * 1000
            )
        except Exception as exc:
            logger.warning("Could not compute duration_ms: %s", exc)
            duration_ms = 0

    run_root = str(path.parent)

    conn.execute(
        """
        INSERT INTO stage_runs (
            run_id, batch_id, stage, stage_version, status, exit_code,
            started_at, ended_at, duration_ms,
            code_commit, config_path, config_hash, model_id,
            input_root, n_units_discovered, n_units_succeeded, n_units_failed,
            run_root, artifacts_dir, manifest_path, logs_path,
            run_report_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id) DO UPDATE SET
            status            = excluded.status,
            exit_code         = excluded.exit_code,
            ended_at          = excluded.ended_at,
            duration_ms       = excluded.duration_ms,
            n_units_succeeded = excluded.n_units_succeeded,
            n_units_failed    = excluded.n_units_failed,
            run_root          = excluded.run_root,
            artifacts_dir     = excluded.artifacts_dir,
            manifest_path     = excluded.manifest_path,
            logs_path         = excluded.logs_path,
            run_report_json   = excluded.run_report_json
        """,
        (
            str(report["run_id"]),
            report["batch_id"],
            report["stage"],
            report.get("stage_version") or "",
            status,
            report.get("exit_code", -1),
            report["started_at"],
            report["ended_at"],
            duration_ms,
            provenance.get("code_commit")  or "",
            provenance.get("config_path")  or "",
            provenance.get("config_hash")  or "",
            provenance.get("model_id")     or "",
            inputs_d.get("input_root")     or "",
            int(inputs_d.get("n_units_discovered") or 0),
            int(outputs_d.get("n_units_succeeded") or 0),
            int(outputs_d.get("n_units_failed")    or 0),
            run_root,
            str(outputs_d.get("artifacts_dir") or ""),
            str(pointers.get("manifest_path") or ""),
            str(pointers.get("logs_path")     or ""),
            json.dumps(report),
        ),
    )
    logger.info(
        "Ingested run_report: run_id=%s batch_id=%s stage=%s status=%s",
        report["run_id"], report["batch_id"], report["stage"], status,
    )
    return {
        "run_id":   str(report["run_id"]),
        "batch_id": report["batch_id"],
        "stage":    report["stage"],
        "status":   status,
    }


# ---------------------------------------------------------------------------
# Season / reference-data resolution
# ---------------------------------------------------------------------------

# Priority order for resolving a batch-independent reference file (e.g. a
# species-zone shapefile) to an absolute path: both tiers are direct
# filesystem reads on CERES (no Globus transfer needed, unlike JUNO LTS), so
# this is a pure globus_file_index lookup — see resolve_file_path_with_priority.
DEFAULT_FILE_SOURCE_PRIORITY: tuple[tuple[str, str], ...] = (
    ("CERES", "90daydata"),
    ("CERES", "project"),
)


def resolve_season_for_batch(
    conn: sqlite3.Connection,
    *,
    site: str,
    batch_date: str,
) -> Optional[Dict]:
    """
    Resolve the ``season_date_ranges`` row covering ``batch_date`` for ``site``.

    ``batch_date`` and the table's start_date/end_date are ISO-8601
    'YYYY-MM-DD' strings, which compare correctly as TEXT. If a site's
    configured windows overlap (shouldn't normally happen, but
    configs/date_ranges.yaml is hand-maintained), the window with the latest
    start_date wins.

    Returns
    -------
    dict with season_name/start_date/end_date/bbot_version/crs/
    pipeline_season/gh_reviewer, or None if no configured window in
    configs/date_ranges.yaml covers this (site, batch_date) — the caller
    should treat that as a config gap, not silently default anything.
    """
    row = conn.execute(
        """
        SELECT season_name, start_date, end_date, bbot_version, crs,
               pipeline_season, gh_reviewer
        FROM season_date_ranges
        WHERE site = ?
          AND ? BETWEEN start_date AND end_date
        ORDER BY start_date DESC
        LIMIT 1
        """,
        (site, batch_date),
    ).fetchone()
    return dict(row) if row else None


def resolve_file_path_with_priority(
    conn: sqlite3.Connection,
    rel_path: str,
    *,
    priority: Sequence[tuple[str, str]] = DEFAULT_FILE_SOURCE_PRIORITY,
) -> Optional[str]:
    """
    Resolve ``rel_path`` to an absolute path by checking ``globus_file_index``
    for a current file row at each ``(site, namespace)`` in ``priority`` order.

    Returns the ``full_path`` of the first match, or None if ``rel_path``
    isn't currently indexed at any of the given locations.
    """
    for site, namespace in priority:
        row = conn.execute(
            """
            SELECT full_path
            FROM globus_file_index
            WHERE rel_path   = ?
              AND site       = ?
              AND namespace  = ?
              AND entry_type = 'file'
              AND is_current = 1
            LIMIT 1
            """,
            (rel_path, site, namespace),
        ).fetchone()
        if row:
            return row["full_path"]
    return None


# ---------------------------------------------------------------------------
# Batch discovery
# ---------------------------------------------------------------------------

def get_batches_needing_raw_to_jpg(
    conn: sqlite3.Connection,
    *,
    site: Optional[str] = None,
    limit: int = 200,
    batch_ids: Optional[Sequence[str]] = None,
) -> List[Dict]:
    """
    Return rows from ``v_batches_needing_raw_to_jpg``.
    ...
    batch_ids : sequence of str, optional
        If given, restrict to these batch_ids. Applied in SQL *before*
        ``limit`` so targeted batches are never truncated out by the
        default row limit.
    """
    batch_filter_sql = ""
    filter_params: List = []
    if batch_ids:
        placeholders = ",".join("?" for _ in batch_ids)
        batch_filter_sql = f"AND v.batch_id IN ({placeholders})"
        filter_params = list(batch_ids)

    if site:
        rows = conn.execute(
            f"""
            SELECT
                v.batch_id,
                v.batch_date,
                v.raw_file_count,
                v.jpg_file_count,
                g.site,
                g.storage_domain,
                g.storage_root
            FROM v_batches_needing_raw_to_jpg v
            JOIN globus_file_index g
              ON  g.batch_id   = v.batch_id
             AND  g.data_state = 'semifield-upload'
             AND  g.entry_type = 'file'
             AND  g.is_current = 1
             AND  g.file_ext   IN {_RAW_EXTS}
             AND  g.site       = ?
            WHERE 1=1 {batch_filter_sql}
            GROUP BY v.batch_id
            ORDER BY v.batch_date DESC, v.batch_id DESC
            LIMIT ?
            """,
            (site, *filter_params, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT v.batch_id, v.batch_date, v.raw_file_count, v.jpg_file_count
            FROM   v_batches_needing_raw_to_jpg v
            WHERE  1=1 {batch_filter_sql}
            ORDER  BY v.batch_date DESC, v.batch_id DESC
            LIMIT  ?
            """,
            (*filter_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]
