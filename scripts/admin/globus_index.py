#!/usr/bin/env python3
"""
# scripts/admin/globus_index.py
Multiprocessing Globus file indexer for SQLite.

This is a SQLite-oriented inventory scanner for AgIR storage locations.
It preserves the complete source.globus_file_index column set from the
PostgreSQL design while adding run tracking and current/stale inventory state.

Core behavior:
- Full file/directory inventory from Globus `ls --format json`
- Non-destructive upserts instead of deleting/rebuilding by default
- inventory_runs table for scan history
- is_current / first_seen / last_seen / missing_since tracking
- batch_inventory_summary rebuilt per inventory run
- Optional clean-slate mode for a specific endpoint/root/state scope
- YAML endpoint config support for multi-root wrapper scripts

SQLite notes:
- Dates/timestamps are stored as ISO-8601 TEXT.
- checksum is preserved but remains NULL unless a future scanner populates it.
- The original PostgreSQL uniqueness rule is preserved:
  UNIQUE(endpoint, data_state, storage_root, rel_path)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ============================================================
#                         CONSTANTS
# ============================================================

BATCH_PATTERN = re.compile(r"^([A-Z]{2})_(\d{4}-\d{2}-\d{2})$")
VALID_SITES = {"JUNO", "NCSU", "CERES", "ATLAS"}
VALID_STORAGE_DOMAINS = {"screberg", "dash_agir", "national_plant_image_repository"}
VALID_DATA_STATES = {"semifield-upload", "semifield-developed-images", "semifield-cutouts"}

# Original globus_file_index columns from the PostgreSQL overview.
BASE_GFI_COLUMNS = [
    "file_id",
    "endpoint",
    "site",
    "storage_domain",
    "namespace",
    "storage_root",
    "rel_path",
    "full_path",
    "parent_dir",
    "file_name",
    "entry_type",
    "file_ext",
    "size_bytes",
    "permissions",
    "checksum",
    "batch_id",
    "batch_state",
    "batch_date",
    "data_state",
    "mtime_iso",
    "fname_ts_epoch",
    "fname_ts_iso",
    "created_at_ts_iso",
]

# Additional SQLite inventory-management columns.
TRACKING_COLUMNS = [
    "first_seen_ts_iso",
    "last_seen_ts_iso",
    "last_seen_run_id",
    "missing_since_ts_iso",
    "is_current",
]


# ============================================================
#                         DATA TYPES
# ============================================================

@dataclass(frozen=True)
class EndpointConfig:
    endpoint: str
    site: str
    storage_domain: str
    namespace: str
    storage_root: str
    data_state: str


@dataclass
class CrawlStats:
    total_seen: int = 0
    total_dirs_seen: int = 0
    total_files_seen: int = 0
    total_batches_written: int = 0
    total_errors: int = 0


# ============================================================
#                         LOGGING
# ============================================================


def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("globus_index_sqlite")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    fmt = logging.Formatter("[%(asctime)s][%(name)s][%(levelname)s] - %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_file, maxBytes=50_000_000, backupCount=5)
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


# ============================================================
#                      REGEX / PARSING HELPERS
# ============================================================


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_batch_id(path: str) -> Optional[str]:
    """Extract a batch ID like MD_2025-01-01 from any path component."""
    for part in path.split("/"):
        if BATCH_PATTERN.match(part):
            return part
    return None


def split_batch_id(batch_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (batch_state, batch_date) from a batch ID like MD_2025-01-01."""
    match = BATCH_PATTERN.match(batch_id)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def parse_last_modified_to_epoch(last_modified: Optional[str]) -> Optional[int]:
    """Parse Globus `last_modified` string into epoch seconds."""
    if not last_modified:
        return None

    value = last_modified.strip()
    if " " in value and "T" not in value:
        value = value.replace(" ", "T", 1)
    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp())


def extract_epoch_from_filename(filename: str) -> Tuple[Optional[int], Optional[datetime]]:
    """Extract a Unix epoch timestamp from a filename when present."""
    base = filename.split(".", 1)[0]
    matches = re.findall(r"(?<!\d)(\d{10,13})(?!\d)", base)
    if not matches:
        return None, None

    epoch = int(matches[0])
    if epoch > 1_000_000_000_000:
        epoch = epoch // 1000

    try:
        dt_epoch = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, OSError):
        return epoch, None

    return epoch, dt_epoch


def epoch_to_datetime(epoch: Optional[int]) -> Optional[datetime]:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def dt_to_sqlite_text(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def infer_file_ext(rel_path: str, entry_type: str) -> Optional[str]:
    """Derive file extension from path without the dot. Returns None for directories."""
    if entry_type == "dir":
        return None
    ext = Path(rel_path).suffix
    return ext.lstrip(".").lower() if ext else None


def get_parent_folder(rel_path: str, entry_type: str) -> Optional[str]:
    """Return the immediate parent folder name for a file. NULL-equivalent for dirs."""
    if entry_type == "dir" or not rel_path:
        return None

    path = rel_path.rstrip("/")
    if "/" not in path:
        return None

    parent_dir = path.rsplit("/", 1)[0]
    if not parent_dir:
        return None

    return parent_dir.split("/")[-1]


# ============================================================
#                         SCHEMA
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inventory_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    data_state TEXT NOT NULL,
    started_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at_ts_iso TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    total_seen INTEGER NOT NULL DEFAULT 0,
    total_files_seen INTEGER NOT NULL DEFAULT 0,
    total_dirs_seen INTEGER NOT NULL DEFAULT 0,
    total_batches_written INTEGER NOT NULL DEFAULT 0,
    total_errors INTEGER NOT NULL DEFAULT 0,
    total_marked_stale INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    UNIQUE (run_id)
);

CREATE TABLE IF NOT EXISTS globus_file_index (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,

    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    full_path TEXT NOT NULL,
    parent_dir TEXT,
    file_name TEXT NOT NULL,

    entry_type TEXT NOT NULL CHECK (entry_type IN ('file', 'dir')),
    file_ext TEXT,
    size_bytes INTEGER,
    permissions TEXT,
    checksum TEXT,

    batch_id TEXT,
    batch_state TEXT,
    batch_date TEXT,

    data_state TEXT NOT NULL,

    mtime_iso TEXT,
    fname_ts_epoch INTEGER,
    fname_ts_iso TEXT,
    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    first_seen_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_run_id INTEGER,
    missing_since_ts_iso TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),

    UNIQUE (endpoint, data_state, storage_root, rel_path),
    FOREIGN KEY (last_seen_run_id) REFERENCES inventory_runs(run_id)
);

CREATE TABLE IF NOT EXISTS batch_inventory_summary (
    run_id INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    storage_root TEXT NOT NULL,
    data_state TEXT NOT NULL,
    batch_id TEXT,
    batch_state TEXT,
    batch_date TEXT,

    file_count INTEGER NOT NULL,
    dir_count INTEGER NOT NULL,
    total_size_bytes INTEGER NOT NULL,
    raw_count INTEGER NOT NULL DEFAULT 0,
    jpg_count INTEGER NOT NULL DEFAULT 0,
    json_count INTEGER NOT NULL DEFAULT 0,
    csv_count INTEGER NOT NULL DEFAULT 0,
    png_count INTEGER NOT NULL DEFAULT 0,
    metadata_count INTEGER NOT NULL DEFAULT 0,
    detection_count INTEGER NOT NULL DEFAULT 0,
    min_mtime_iso TEXT,
    max_mtime_iso TEXT,
    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (run_id, endpoint, site, storage_domain, namespace, storage_root, data_state, batch_id),
    FOREIGN KEY (run_id) REFERENCES inventory_runs(run_id)
);

CREATE TABLE IF NOT EXISTS storage_gap_summary (
    run_id INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    batch_state TEXT,
    batch_date TEXT,
    storage_domain TEXT NOT NULL,
    namespace TEXT NOT NULL,
    site TEXT NOT NULL,
    storage_root TEXT NOT NULL,

    has_semifield_upload INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_upload IN (0, 1)),
    has_semifield_developed_images INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_developed_images IN (0, 1)),
    has_semifield_cutouts INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_cutouts IN (0, 1)),

    upload_file_count INTEGER NOT NULL DEFAULT 0,
    developed_file_count INTEGER NOT NULL DEFAULT 0,
    cutout_file_count INTEGER NOT NULL DEFAULT 0,

    raw_count INTEGER NOT NULL DEFAULT 0,
    jpg_count INTEGER NOT NULL DEFAULT 0,
    metadata_count INTEGER NOT NULL DEFAULT 0,
    detection_count INTEGER NOT NULL DEFAULT 0,
    cutout_png_count INTEGER NOT NULL DEFAULT 0,

    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (run_id, batch_id, storage_domain, namespace, site, storage_root),
    FOREIGN KEY (run_id) REFERENCES inventory_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_gfi_batch_state_current
    ON globus_file_index(batch_id, data_state, site, is_current);

CREATE INDEX IF NOT EXISTS idx_gfi_storage_current
    ON globus_file_index(site, storage_domain, namespace, storage_root, data_state, is_current);

CREATE INDEX IF NOT EXISTS idx_gfi_ext_parent
    ON globus_file_index(data_state, file_ext, parent_dir);

CREATE INDEX IF NOT EXISTS idx_gfi_run_current
    ON globus_file_index(last_seen_run_id, is_current);

CREATE INDEX IF NOT EXISTS idx_gfi_full_path
    ON globus_file_index(full_path);

CREATE INDEX IF NOT EXISTS idx_inventory_runs_scope_time
    ON inventory_runs(endpoint, site, storage_domain, namespace, storage_root, data_state, started_at_ts_iso DESC);

CREATE INDEX IF NOT EXISTS idx_batch_inventory_summary_batch
    ON batch_inventory_summary(batch_id, data_state, site, storage_domain, namespace);

CREATE INDEX IF NOT EXISTS idx_storage_gap_summary_batch
    ON storage_gap_summary(batch_id, site, storage_domain, namespace);
"""


# ============================================================
#                     DATABASE HELPERS
# ============================================================


def table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
    return [row[1] for row in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
        (table_name,),
    ).fetchone()
    return row is not None


def migrate_legacy_globus_file_index(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    """
    Handle older SQLite conversion tables that omitted file_id/created_at_ts_iso.

    SQLite cannot add an INTEGER PRIMARY KEY AUTOINCREMENT column to an existing
    table in place. If a legacy table is detected, rename it, create the full
    schema, and copy all matching inventory columns forward.
    """
    if not table_exists(conn, "globus_file_index"):
        return

    existing_cols = set(table_columns(conn, "globus_file_index"))
    required_original = set(BASE_GFI_COLUMNS)

    if required_original.issubset(existing_cols):
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    legacy_name = f"globus_file_index_legacy_{timestamp}"
    logger.warning(
        "Existing globus_file_index is missing original columns: %s. Renaming to %s and rebuilding.",
        sorted(required_original - existing_cols),
        legacy_name,
    )
    conn.execute(f"ALTER TABLE globus_file_index RENAME TO {legacy_name};")
    conn.executescript(SCHEMA_SQL)

    legacy_cols = set(table_columns(conn, legacy_name))
    copy_cols = [
        col for col in BASE_GFI_COLUMNS
        if col in legacy_cols and col not in {"file_id", "created_at_ts_iso"}
    ]
    if not copy_cols:
        logger.warning("No compatible columns found in %s; leaving legacy table untouched.", legacy_name)
        conn.commit()
        return

    insert_cols = ", ".join(copy_cols)
    select_cols = ", ".join(copy_cols)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO globus_file_index ({insert_cols})
        SELECT {select_cols}
        FROM {legacy_name};
        """
    )
    conn.commit()
    logger.info("Copied compatible legacy rows from %s into rebuilt globus_file_index.", legacy_name)


def init_db(db_path: str, logger: logging.Logger) -> sqlite3.Connection:
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Connecting to SQLite database: %s", db)
    conn = sqlite3.connect(str(db), timeout=120)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-200000;")  # about 200 MB
    conn.execute("PRAGMA busy_timeout=120000;")
    conn.execute("PRAGMA foreign_keys=ON;")

    migrate_legacy_globus_file_index(conn, logger)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.info("SQLite schema ready.")
    return conn


def begin_inventory_run(conn: sqlite3.Connection, cfg: EndpointConfig) -> int:
    cur = conn.execute(
        """
        INSERT INTO inventory_runs (
            endpoint, site, storage_domain, namespace, storage_root, data_state, status
        )
        VALUES (?, ?, ?, ?, ?, ?, 'running')
        """,
        (cfg.endpoint, cfg.site, cfg.storage_domain, cfg.namespace, cfg.storage_root, cfg.data_state),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_inventory_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    stats: CrawlStats,
    total_marked_stale: int = 0,
    error_summary: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE inventory_runs
        SET ended_at_ts_iso = ?,
            status = ?,
            total_seen = ?,
            total_files_seen = ?,
            total_dirs_seen = ?,
            total_batches_written = ?,
            total_errors = ?,
            total_marked_stale = ?,
            error_summary = ?
        WHERE run_id = ?
        """,
        (
            utc_now_iso(),
            status,
            stats.total_seen,
            stats.total_files_seen,
            stats.total_dirs_seen,
            stats.total_batches_written,
            stats.total_errors,
            total_marked_stale,
            error_summary,
            run_id,
        ),
    )
    conn.commit()


def clear_scope_index(conn: sqlite3.Connection, cfg: EndpointConfig, logger: logging.Logger) -> int:
    cur = conn.execute(
        """
        DELETE FROM globus_file_index
        WHERE endpoint = ?
          AND site = ?
          AND data_state = ?
          AND storage_root = ?
        """,
        (cfg.endpoint, cfg.site, cfg.data_state, cfg.storage_root),
    )
    conn.commit()
    deleted_count = cur.rowcount if cur.rowcount is not None else 0
    logger.info(
        "Clean slate removed %d existing records for %s/%s/%s",
        deleted_count,
        cfg.site,
        cfg.data_state,
        cfg.storage_root,
    )
    return deleted_count


def upsert_rows(
    conn: sqlite3.Connection,
    rows: List[Tuple],
    run_id: int,
    logger: logging.Logger,
) -> None:
    if not rows:
        return

    now = utc_now_iso()
    run_rows = [row + (now, now, run_id, None, 1) for row in rows]

    conn.executemany(
        """
        INSERT INTO globus_file_index (
            endpoint, site, storage_domain, namespace, storage_root,
            rel_path, full_path, parent_dir, file_name,
            entry_type, file_ext,
            size_bytes, permissions, checksum,
            batch_id, batch_state, batch_date,
            data_state,
            mtime_iso,
            fname_ts_epoch, fname_ts_iso,
            first_seen_ts_iso,
            last_seen_ts_iso,
            last_seen_run_id,
            missing_since_ts_iso,
            is_current
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(endpoint, data_state, storage_root, rel_path) DO UPDATE SET
            site = excluded.site,
            storage_domain = excluded.storage_domain,
            namespace = excluded.namespace,
            full_path = excluded.full_path,
            parent_dir = excluded.parent_dir,
            file_name = excluded.file_name,
            entry_type = excluded.entry_type,
            file_ext = excluded.file_ext,
            size_bytes = excluded.size_bytes,
            permissions = excluded.permissions,
            checksum = COALESCE(excluded.checksum, globus_file_index.checksum),
            batch_id = excluded.batch_id,
            batch_state = excluded.batch_state,
            batch_date = excluded.batch_date,
            mtime_iso = excluded.mtime_iso,
            fname_ts_epoch = excluded.fname_ts_epoch,
            fname_ts_iso = excluded.fname_ts_iso,
            last_seen_ts_iso = excluded.last_seen_ts_iso,
            last_seen_run_id = excluded.last_seen_run_id,
            missing_since_ts_iso = NULL,
            is_current = 1
        """,
        run_rows,
    )
    logger.info("Upserted %d rows into globus_file_index", len(rows))


def mark_stale_rows(conn: sqlite3.Connection, cfg: EndpointConfig, run_id: int, logger: logging.Logger) -> int:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE globus_file_index
        SET is_current = 0,
            missing_since_ts_iso = COALESCE(missing_since_ts_iso, ?)
        WHERE endpoint = ?
          AND site = ?
          AND storage_root = ?
          AND data_state = ?
          AND is_current = 1
          AND (last_seen_run_id IS NULL OR last_seen_run_id <> ?)
        """,
        (now, cfg.endpoint, cfg.site, cfg.storage_root, cfg.data_state, run_id),
    )
    changed = cur.rowcount if cur.rowcount is not None else 0
    logger.info("Marked %d rows stale for run_id=%d", changed, run_id)
    return changed


def build_batch_inventory_summary(conn: sqlite3.Connection, cfg: EndpointConfig, run_id: int, logger: logging.Logger) -> None:
    conn.execute(
        """
        DELETE FROM batch_inventory_summary
        WHERE run_id = ?
          AND endpoint = ?
          AND site = ?
          AND storage_domain = ?
          AND namespace = ?
          AND storage_root = ?
          AND data_state = ?
        """,
        (run_id, cfg.endpoint, cfg.site, cfg.storage_domain, cfg.namespace, cfg.storage_root, cfg.data_state),
    )
    conn.execute(
        """
        INSERT INTO batch_inventory_summary (
            run_id,
            endpoint,
            site,
            storage_domain,
            namespace,
            storage_root,
            data_state,
            batch_id,
            batch_state,
            batch_date,
            file_count,
            dir_count,
            total_size_bytes,
            raw_count,
            jpg_count,
            json_count,
            csv_count,
            png_count,
            metadata_count,
            detection_count,
            min_mtime_iso,
            max_mtime_iso
        )
        SELECT
            ?,
            endpoint,
            site,
            storage_domain,
            namespace,
            storage_root,
            data_state,
            COALESCE(batch_id, '__NO_BATCH__') AS batch_id,
            batch_state,
            batch_date,
            SUM(CASE WHEN entry_type = 'file' THEN 1 ELSE 0 END) AS file_count,
            SUM(CASE WHEN entry_type = 'dir' THEN 1 ELSE 0 END) AS dir_count,
            SUM(CASE WHEN entry_type = 'file' THEN COALESCE(size_bytes, 0) ELSE 0 END) AS total_size_bytes,
            SUM(CASE WHEN LOWER(COALESCE(file_ext, '')) IN ('raw', 'arw', 'rw2', 'nef', 'cr2', 'dng', 'raf', 'orf') THEN 1 ELSE 0 END) AS raw_count,
            SUM(CASE WHEN LOWER(COALESCE(file_ext, '')) IN ('jpg', 'jpeg') THEN 1 ELSE 0 END) AS jpg_count,
            SUM(CASE WHEN LOWER(COALESCE(file_ext, '')) = 'json' THEN 1 ELSE 0 END) AS json_count,
            SUM(CASE WHEN LOWER(COALESCE(file_ext, '')) = 'csv' THEN 1 ELSE 0 END) AS csv_count,
            SUM(CASE WHEN LOWER(COALESCE(file_ext, '')) = 'png' THEN 1 ELSE 0 END) AS png_count,
            SUM(CASE WHEN parent_dir = 'metadata' OR rel_path LIKE '%/metadata/%' THEN 1 ELSE 0 END) AS metadata_count,
            SUM(CASE WHEN parent_dir IN ('detections', 'plant-detections') THEN 1 ELSE 0 END) AS detection_count,
            MIN(mtime_iso),
            MAX(mtime_iso)
        FROM globus_file_index
        WHERE endpoint = ?
          AND site = ?
          AND storage_domain = ?
          AND namespace = ?
          AND storage_root = ?
          AND data_state = ?
          AND is_current = 1
        GROUP BY
            endpoint,
            site,
            storage_domain,
            namespace,
            storage_root,
            data_state,
            COALESCE(batch_id, '__NO_BATCH__'),
            batch_state,
            batch_date
        """,
        (run_id, cfg.endpoint, cfg.site, cfg.storage_domain, cfg.namespace, cfg.storage_root, cfg.data_state),
    )
    logger.info("Rebuilt batch_inventory_summary for run_id=%d", run_id)


def build_storage_gap_summary(conn: sqlite3.Connection, run_id: int, logger: logging.Logger) -> None:
    """
    Build a cross-data-state summary for all current rows touched by this run's storage scope.

    This is intentionally simple and rebuildable. It gives a quick batch-level view of
    whether upload/developed/cutout trees exist in a site/root/namespace and what major
    artifact counts are present.
    """
    run = conn.execute(
        """
        SELECT site, storage_domain, namespace, storage_root
        FROM inventory_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        logger.warning("Cannot build storage_gap_summary; run_id=%d not found", run_id)
        return

    site, storage_domain, namespace, storage_root = run
    conn.execute("DELETE FROM storage_gap_summary WHERE run_id = ?", (run_id,))
    conn.execute(
        """
        INSERT INTO storage_gap_summary (
            run_id,
            batch_id,
            batch_state,
            batch_date,
            storage_domain,
            namespace,
            site,
            storage_root,
            has_semifield_upload,
            has_semifield_developed_images,
            has_semifield_cutouts,
            upload_file_count,
            developed_file_count,
            cutout_file_count,
            raw_count,
            jpg_count,
            metadata_count,
            detection_count,
            cutout_png_count
        )
        SELECT
            ?,
            batch_id,
            MAX(batch_state),
            MAX(batch_date),
            storage_domain,
            namespace,
            site,
            storage_root,
            MAX(CASE WHEN data_state = 'semifield-upload' THEN 1 ELSE 0 END),
            MAX(CASE WHEN data_state = 'semifield-developed-images' THEN 1 ELSE 0 END),
            MAX(CASE WHEN data_state = 'semifield-cutouts' THEN 1 ELSE 0 END),
            SUM(CASE WHEN data_state = 'semifield-upload' AND entry_type = 'file' THEN 1 ELSE 0 END),
            SUM(CASE WHEN data_state = 'semifield-developed-images' AND entry_type = 'file' THEN 1 ELSE 0 END),
            SUM(CASE WHEN data_state = 'semifield-cutouts' AND entry_type = 'file' THEN 1 ELSE 0 END),
            SUM(CASE WHEN data_state = 'semifield-upload' AND LOWER(COALESCE(file_ext, '')) IN ('raw', 'arw', 'rw2', 'nef', 'cr2', 'dng', 'raf', 'orf') THEN 1 ELSE 0 END),
            SUM(CASE WHEN data_state = 'semifield-developed-images' AND LOWER(COALESCE(file_ext, '')) IN ('jpg', 'jpeg') THEN 1 ELSE 0 END),
            SUM(CASE WHEN parent_dir = 'metadata' OR rel_path LIKE '%/metadata/%' THEN 1 ELSE 0 END),
            SUM(CASE WHEN parent_dir IN ('detections', 'plant-detections') THEN 1 ELSE 0 END),
            SUM(CASE WHEN data_state = 'semifield-cutouts' AND LOWER(COALESCE(file_ext, '')) = 'png' THEN 1 ELSE 0 END)
        FROM globus_file_index
        WHERE site = ?
          AND storage_domain = ?
          AND namespace = ?
          AND storage_root = ?
          AND is_current = 1
          AND batch_id IS NOT NULL
        GROUP BY batch_id, storage_domain, namespace, site, storage_root
        """,
        (run_id, site, storage_domain, namespace, storage_root),
    )
    logger.info("Rebuilt storage_gap_summary for run_id=%d", run_id)


def vacuum_db(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    logger.info("Running SQLite VACUUM.")
    conn.execute("VACUUM;")
    logger.info("SQLite VACUUM complete.")


# ============================================================
#                         GLOBUS
# ============================================================


def globus_ls(endpoint: str, path: str) -> List[dict]:
    target = f"{endpoint}:{path}"
    proc = subprocess.run(
        ["globus", "ls", "--format", "json", target],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"globus ls failed for {target}: {proc.stderr[:500]}")

    obj = json.loads(proc.stdout)
    return obj.get("DATA") or []


def ls_worker(args: Tuple[str, str]) -> Tuple[str, List[dict]]:
    endpoint, path = args
    return path, globus_ls(endpoint, path)


def check_globus_path_exists(endpoint: str, path: str, logger: logging.Logger) -> bool:
    try:
        _ = globus_ls(endpoint, path)
        logger.info("Confirmed Globus path exists: %s:%s", endpoint, path)
        return True
    except Exception as exc:
        logger.warning("Globus path unavailable: %s:%s (%s)", endpoint, path, exc)
        return False


# ============================================================
#                         CRAWL
# ============================================================


def crawl_tree_mp(
    conn: sqlite3.Connection,
    cfg: EndpointConfig,
    run_id: int,
    batch_size: int,
    commit_batch_size: int,
    max_workers: int,
    logger: logging.Logger,
) -> CrawlStats:
    data_dir = str(Path(cfg.storage_root) / cfg.data_state)
    stats = CrawlStats()

    dir_queue = deque([data_dir])
    futures: Dict = {}
    rows: List[Tuple] = []
    rows_since_commit = 0
    max_inflight = max_workers * 4

    logger.info("Starting multiprocessing crawl at root: %s", data_dir)
    logger.info("max_workers=%d, max_inflight=%d", max_workers, max_inflight)

    conn.execute("BEGIN IMMEDIATE;")
    try:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            while dir_queue or futures:
                while dir_queue and len(futures) < max_inflight:
                    current = dir_queue.popleft()
                    future = executor.submit(ls_worker, (cfg.endpoint, current))
                    futures[future] = current
                    logger.info("Submitted ls for directory: %s", current)

                if not futures:
                    continue

                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)

                for fut in done:
                    current = futures.pop(fut)
                    try:
                        path, entries = fut.result()
                    except Exception as exc:
                        stats.total_errors += 1
                        logger.warning("Skipping directory due to error: %s: %s", current, exc)
                        continue

                    logger.info("Processing directory: %s (%d entries)", path, len(entries))

                    for item in entries:
                        name = item.get("name", "")
                        entry_type = item.get("type", "file")
                        if entry_type not in {"file", "dir"}:
                            entry_type = "file"

                        size_bytes = item.get("size")
                        last_modified = item.get("last_modified")
                        permissions = item.get("permissions")

                        full_path = f"{path.rstrip('/')}/{name}"
                        if entry_type == "dir":
                            full_path += "/"

                        rel_path = full_path[len(cfg.storage_root):].lstrip("/")
                        batch_id = extract_batch_id(full_path)

                        if batch_id:
                            batch_state, batch_date = split_batch_id(batch_id)
                        else:
                            batch_state, batch_date = None, None

                        mtime_epoch = parse_last_modified_to_epoch(last_modified)
                        fname_ts_epoch, fname_ts_dt = extract_epoch_from_filename(name)
                        mtime_iso = dt_to_sqlite_text(epoch_to_datetime(mtime_epoch))
                        fname_ts_iso = dt_to_sqlite_text(fname_ts_dt)
                        file_ext = infer_file_ext(rel_path, entry_type)
                        parent_dir = get_parent_folder(rel_path, entry_type)

                        rows.append(
                            (
                                cfg.endpoint,
                                cfg.site,
                                cfg.storage_domain,
                                cfg.namespace,
                                cfg.storage_root,
                                rel_path,
                                full_path,
                                parent_dir,
                                name,
                                entry_type,
                                file_ext,
                                size_bytes,
                                permissions,
                                None,
                                batch_id,
                                batch_state,
                                batch_date,
                                cfg.data_state,
                                mtime_iso,
                                fname_ts_epoch,
                                fname_ts_iso,
                            )
                        )
                        stats.total_seen += 1
                        if entry_type == "dir":
                            stats.total_dirs_seen += 1
                            dir_queue.append(full_path)
                        else:
                            stats.total_files_seen += 1

                        if len(rows) >= batch_size:
                            upsert_rows(conn, rows, run_id, logger)
                            stats.total_batches_written += 1
                            rows_since_commit += len(rows)
                            rows.clear()

                            if rows_since_commit >= commit_batch_size:
                                conn.commit()
                                conn.execute("BEGIN IMMEDIATE;")
                                logger.info("Committed after %d rows", rows_since_commit)
                                rows_since_commit = 0

            if rows:
                upsert_rows(conn, rows, run_id, logger)
                stats.total_batches_written += 1
                rows_since_commit += len(rows)
                rows.clear()

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    logger.info(
        "Crawl complete. total_seen=%d files=%d dirs=%d errors=%d",
        stats.total_seen,
        stats.total_files_seen,
        stats.total_dirs_seen,
        stats.total_errors,
    )
    return stats


# ============================================================
#                    ENDPOINT CONFIG FILES
# ============================================================


def validate_endpoint_config(cfg: EndpointConfig) -> None:
    if cfg.site not in VALID_SITES:
        raise ValueError(f"Invalid site {cfg.site!r}; expected one of {sorted(VALID_SITES)}")
    if cfg.storage_domain not in VALID_STORAGE_DOMAINS:
        raise ValueError(
            f"Invalid storage_domain {cfg.storage_domain!r}; expected one of {sorted(VALID_STORAGE_DOMAINS)}"
        )
    if cfg.data_state not in VALID_DATA_STATES:
        raise ValueError(f"Invalid data_state {cfg.data_state!r}; expected one of {sorted(VALID_DATA_STATES)}")
    if not cfg.endpoint:
        raise ValueError("endpoint is required")
    if not cfg.namespace:
        raise ValueError("namespace is required")
    if not cfg.storage_root.startswith("/"):
        raise ValueError(f"storage_root should be absolute: {cfg.storage_root!r}")


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for --endpoint-config-yaml. "
            "Install it in your environment, for example: python -m pip install PyYAML"
        ) from exc

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Endpoint config YAML root must be a mapping/object: {path}")
    return data


def read_endpoint_configs(path: str, logger: logging.Logger) -> List[EndpointConfig]:
    """Read endpoint configs from YAML.

    Expected shape:

        endpoints:
          - enabled: true
            endpoint: <globus-endpoint-uuid>
            site: CERES
            storage_domain: dash_agir
            namespace: 90daydata
            storage_root: /90daydata/dash_agir
            data_state: semifield-upload

    `enabled` defaults to true when omitted.
    """
    data = _load_yaml(path)
    endpoint_rows = data.get("endpoints", [])
    if not isinstance(endpoint_rows, list):
        raise ValueError(f"Endpoint config YAML key 'endpoints' must be a list: {path}")

    required = {"endpoint", "site", "storage_domain", "namespace", "storage_root", "data_state"}
    configs: List[EndpointConfig] = []

    for idx, row in enumerate(endpoint_rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Endpoint entry #{idx} must be a mapping/object in {path}")

        enabled = row.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "n", "off"}
        if not enabled:
            continue

        missing = required - set(row.keys())
        if missing:
            raise ValueError(f"Endpoint entry #{idx} missing keys {sorted(missing)} in {path}")

        cfg = EndpointConfig(
            endpoint=str(row["endpoint"]).strip(),
            site=str(row["site"]).strip(),
            storage_domain=str(row["storage_domain"]).strip(),
            namespace=str(row["namespace"]).strip(),
            storage_root=str(row["storage_root"]).strip().rstrip("/"),
            data_state=str(row["data_state"]).strip(),
        )
        validate_endpoint_config(cfg)
        configs.append(cfg)

    logger.info("Loaded %d enabled endpoint configs from %s", len(configs), path)
    return configs


# ============================================================
#                         RUNNER
# ============================================================


def run_one_config(
    conn: sqlite3.Connection,
    cfg: EndpointConfig,
    batch_size: int,
    commit_batch_size: int,
    max_workers: int,
    clean_slate: bool,
    mark_stale: bool,
    build_summaries: bool,
    vacuum_after_clean: bool,
    logger: logging.Logger,
) -> Tuple[int, str]:
    run_id = begin_inventory_run(conn, cfg)
    logger.info("Started inventory run_id=%d for %s:%s /%s", run_id, cfg.site, cfg.data_state, cfg.storage_root)

    stats = CrawlStats()
    total_marked_stale = 0

    try:
        root_path = str(Path(cfg.storage_root) / cfg.data_state)
        if not check_globus_path_exists(cfg.endpoint, root_path, logger):
            finish_inventory_run(conn, run_id, "skipped", stats, error_summary="Root path unavailable")
            return run_id, "skipped"

        if clean_slate:
            clear_scope_index(conn, cfg, logger)
            if vacuum_after_clean:
                vacuum_db(conn, logger)

        stats = crawl_tree_mp(
            conn=conn,
            cfg=cfg,
            run_id=run_id,
            batch_size=batch_size,
            commit_batch_size=commit_batch_size,
            max_workers=max_workers,
            logger=logger,
        )

        if mark_stale and stats.total_seen > 0:
            conn.execute("BEGIN IMMEDIATE;")
            total_marked_stale = mark_stale_rows(conn, cfg, run_id, logger)
            conn.commit()

        if build_summaries and stats.total_seen > 0:
            conn.execute("BEGIN IMMEDIATE;")
            build_batch_inventory_summary(conn, cfg, run_id, logger)
            build_storage_gap_summary(conn, run_id, logger)
            conn.commit()

        status = "success" if stats.total_errors == 0 else "partial"
        finish_inventory_run(conn, run_id, status, stats, total_marked_stale=total_marked_stale)
        return run_id, status

    except Exception as exc:
        logger.exception("Inventory run_id=%d failed", run_id)
        finish_inventory_run(conn, run_id, "failed", stats, total_marked_stale, error_summary=str(exc)[:1000])
        return run_id, "failed"


# ============================================================
#                          CLI
# ============================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Multiprocessing Globus inventory indexer for SQLite.")
    parser.add_argument("--db", required=True, help="Path to SQLite database file.")

    # Single-scope mode.
    parser.add_argument("--endpoint", help="Globus endpoint ID.")
    parser.add_argument("--site", choices=sorted(VALID_SITES), help="Physical site of the endpoint.")
    parser.add_argument("--storage-domain", choices=sorted(VALID_STORAGE_DOMAINS), help="Logical storage domain tag.")
    parser.add_argument("--namespace", help="Logical namespace tag.")
    parser.add_argument("--storage-root", help="Logical storage root path.")
    parser.add_argument("--state", choices=sorted(VALID_DATA_STATES), help="Logical data-state label for this tree.")

    # Multi-scope mode.
    parser.add_argument(
        "--endpoint-config-yaml",
        help="YAML file with an 'endpoints' list. Each entry may include enabled, endpoint, site, storage_domain, namespace, storage_root, data_state.",
    )

    parser.add_argument("--batch-size", type=int, default=10_000, help="Rows per executemany upsert batch.")
    parser.add_argument("--commit-batch-size", type=int, default=100_000, help="Rows between SQLite commits.")
    parser.add_argument("--max-workers", type=int, default=8, help="Number of worker processes for Globus ls.")
    parser.add_argument("--log-file", default="./globus_index_sqlite.log", help="Log file path.")

    parser.add_argument(
        "--clean-slate",
        action="store_true",
        help="Delete existing rows for each endpoint/site/state/root before indexing. Usually not needed.",
    )
    parser.add_argument(
        "--no-mark-stale",
        action="store_true",
        help="Do not mark previously current rows stale when not seen in this run.",
    )
    parser.add_argument(
        "--no-build-summaries",
        action="store_true",
        help="Do not rebuild summary tables after each run.",
    )
    parser.add_argument(
        "--vacuum-after-clean",
        action="store_true",
        help="Run VACUUM after clean-slate delete. Useful after large deletes but slow.",
    )
    parser.add_argument(
        "--optimize-at-end",
        action="store_true",
        help="Run PRAGMA optimize after all configured scans.",
    )
    return parser.parse_args()


def configs_from_args(args: argparse.Namespace, logger: logging.Logger) -> List[EndpointConfig]:
    if args.endpoint_config_yaml:
        return read_endpoint_configs(args.endpoint_config_yaml, logger)

    required = [args.endpoint, args.site, args.storage_domain, args.namespace, args.storage_root, args.state]
    if any(value is None for value in required):
        raise ValueError(
            "Provide either --endpoint-config-yaml or all of: "
            "--endpoint --site --storage-domain --namespace --storage-root --state"
        )

    cfg = EndpointConfig(
        endpoint=args.endpoint,
        site=args.site,
        storage_domain=args.storage_domain,
        namespace=args.namespace,
        storage_root=args.storage_root.rstrip("/"),
        data_state=args.state,
    )
    validate_endpoint_config(cfg)
    return [cfg]


def main() -> None:
    args = parse_args()
    logger = setup_logging(args.log_file)

    logger.info("========================================")
    logger.info(" STARTING GLOBUS FILE INDEXING (SQLite) ")
    logger.info("========================================")
    logger.info("SQLite DB: %s", args.db)
    logger.info("Batch size: %d", args.batch_size)
    logger.info("Commit batch size: %d", args.commit_batch_size)
    logger.info("Max workers: %d", args.max_workers)
    logger.info("Clean slate: %s", args.clean_slate)
    logger.info("Mark stale: %s", not args.no_mark_stale)
    logger.info("Build summaries: %s", not args.no_build_summaries)

    conn = init_db(args.db, logger)
    try:
        configs = configs_from_args(args, logger)
        total_success = total_partial = total_failed = total_skipped = 0

        for cfg in configs:
            logger.info(
                "Processing config: site=%s storage_domain=%s namespace=%s root=%s state=%s",
                cfg.site,
                cfg.storage_domain,
                cfg.namespace,
                cfg.storage_root,
                cfg.data_state,
            )
            run_id, status = run_one_config(
                conn=conn,
                cfg=cfg,
                batch_size=args.batch_size,
                commit_batch_size=args.commit_batch_size,
                max_workers=args.max_workers,
                clean_slate=args.clean_slate,
                mark_stale=not args.no_mark_stale,
                build_summaries=not args.no_build_summaries,
                vacuum_after_clean=args.vacuum_after_clean,
                logger=logger,
            )
            logger.info("Finished run_id=%d status=%s", run_id, status)
            if status == "success":
                total_success += 1
            elif status == "partial":
                total_partial += 1
            elif status == "skipped":
                total_skipped += 1
            else:
                total_failed += 1

        if args.optimize_at_end:
            logger.info("Running PRAGMA optimize")
            conn.execute("PRAGMA optimize;")
            conn.commit()

        logger.info("========================================")
        logger.info("SQLite Globus indexing complete")
        logger.info("Success: %d", total_success)
        logger.info("Partial: %d", total_partial)
        logger.info("Skipped: %d", total_skipped)
        logger.info("Failed: %d", total_failed)
        logger.info("========================================")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
