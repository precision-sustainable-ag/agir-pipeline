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
claim_stage_lease(conn, batch_id, stage, orchestrator_id, ttl_seconds)  → dict
release_stage_lease(conn, lease_id, orchestrator_id)  → bool
request_input_staging(conn, ...)  → dict
mark_input_staging_status(conn, staging_id, status, ...)  → dict
get_completed_input_staging_batch_ids(conn, stage, batch_ids)  → set[str]
ingest_run_report(conn, run_report_path)  → dict
get_batches_needing_raw_to_jpg(conn, *, site=None, limit=200)  → list[dict]

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
import shutil
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
        rows = conn.execute(
            f"""
            SELECT v.batch_id, v.batch_date, v.jpg_count, v.det_count
            FROM   v_batches_needing_jpg_to_det v
            WHERE  1=1 {batch_filter_sql}
            ORDER  BY v.batch_date ASC, v.batch_id ASC
            LIMIT  ?
            """,
            (*filter_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]

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
        If True, open without write pragmas.  No data is written.
    local_copy : bool
        If True (and readonly=True), copy the DB to a temporary file before
        opening. Use this when querying the original DB directly is too slow.
        The temporary file is deleted when the connection is closed.
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
        logger.debug("Copying DB to temporary storage: %s -> %s", db_path, tmp)
        try:
            shutil.copy2(db_path, tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        inner = sqlite3.connect(str(tmp), timeout=60)
        inner.row_factory = sqlite3.Row
        return _TempConnection(inner, tmp)

    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"SQLite DB not found: {db_path}")
        conn = sqlite3.connect(str(db_path), timeout=60)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=120)
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=120000;")
        conn.execute("PRAGMA foreign_keys=ON;")

    conn.row_factory = sqlite3.Row
    logger.debug("Opened SQLite DB: %s (readonly=%s)", db_path, readonly)
    return conn


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
