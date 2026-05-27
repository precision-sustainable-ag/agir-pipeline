-- =============================================================================
-- schemas/sqlite/pipeline.sql
--
-- SQLite pipeline registry for the AGIR semifield pipeline.
--
-- Purpose
-- -------
-- Lightweight orchestration state store that runs on any cluster (Atlas,
-- CERES, login nodes) without a database server.  CERES is the authoritative
-- writer; Atlas reads from a Globus-synced copy and writes run artifacts back
-- to disk for CERES to sweep up via scripts/ceres/sweep_atlas_runs.py.
--
-- Relationship to Postgres
-- ------------------------
-- This file mirrors the orchestration-relevant subset of the Postgres cluster:
--
--   Postgres                              SQLite
--   ──────────────────────────────────    ──────────────────────────────────
--   source.globus_file_index          →   file_index
--   logs.stage_runs                   →   stage_runs
--   logs.stage_leases                 →   stage_leases
--   report.raw_batches_needing_jpg    →   v_raw_batches_needing_jpg
--   report.jpg_batches_needing_*      →   v_jpg_batches_needing_detection
--   report.semifield_batch_inv_status →   v_semifield_batch_status
--   report.raw_files_missing_jpg      →   v_raw_files_missing_jpg
--   report.jpg_files_missing_metadata →   v_jpg_files_missing_metadata
--
-- Data product tables (processed.images, processed.detections, etc.) are NOT
-- here — they stay in Postgres.
--
-- Key simplifications vs Postgres
-- --------------------------------
-- The Postgres views group by (endpoint, site, storage_domain, namespace,
-- storage_root, batch_id) to handle files spread across multiple storage
-- locations.  The SQLite DB is a single-site index so grouping is just
-- batch_id.  The parent_dir column replaces rel_path regex matching.
--
-- File location
-- -------------
--   /90daydata/dash_agir/semifield-tools/pipeline_registry.db
--
-- Applying this file
-- ------------------
-- Idempotent — safe to re-run on an existing DB at any time.
-- Tables use CREATE IF NOT EXISTS (data preserved).
-- Views use DROP + CREATE (logic always updated to latest version).
--
--   sqlite3 /90daydata/dash_agir/semifield-tools/pipeline_registry.db \
--       < schemas/sqlite/pipeline.sql
-- =============================================================================

PRAGMA journal_mode = WAL;     -- safe concurrent readers + one writer
PRAGMA synchronous   = NORMAL; -- durable enough for HPC scratch
PRAGMA foreign_keys  = ON;


-- =============================================================================
-- TABLE: file_index
--
-- Mirrors source.globus_file_index.
-- Populated by scripts/sqlite/migrate_postgres_to_sqlite.py (one-time) and
-- scripts/sqlite/scan_filesystem.py (recovery / new sites).
--
-- Key columns
-- -----------
--   data_state   Lifecycle stage of the file's directory:
--                  'semifield-upload'            RAW camera files
--                  'semifield-developed-images'  Developed JPGs + outputs
--                  'semifield-cutouts'           Cropped plant cutouts
--
--   parent_dir   Immediate subfolder under the batch directory.
--                Replaces the rel_path regex used in Postgres views:
--                  'images'           JPG inputs
--                  'metadata'         Detection metadata JSONs   (output)
--                  'plant-detections' Plant detection CSVs       (output)
--                  'detections'       Alternate detection folder (output)
--
--   batch_id     e.g. 'MD_2025-04-25'
--   batch_state  e.g. 'MD'
--   batch_date   e.g. '2025-04-25'
-- =============================================================================

CREATE TABLE IF NOT EXISTS file_index (
    id            INTEGER PRIMARY KEY,

    -- Storage location (mirrors source.globus_file_index)
    endpoint      TEXT,               -- Globus endpoint UUID
    site          TEXT NOT NULL,      -- 'CERES' | 'ATLAS' | 'JUNO' | 'NCSU'
    storage_domain TEXT,              -- e.g. '90daydata' | 'LTS' | 'project'
    namespace     TEXT NOT NULL,      -- filesystem namespace
    storage_root  TEXT NOT NULL,      -- e.g. /90daydata/dash_agir
    rel_path      TEXT NOT NULL,      -- relative to storage_root
    full_path     TEXT NOT NULL UNIQUE,

    -- Classification
    entry_type    TEXT NOT NULL CHECK (entry_type IN ('file', 'dir')),
    data_state    TEXT,
    parent_dir    TEXT,
    file_name     TEXT,
    file_ext      TEXT,               -- extension without dot, lowercased

    -- File attributes
    size_bytes    INTEGER,
    permissions   TEXT,
    checksum      TEXT,               -- 'sha256:<hex>' or NULL

    -- Batch identity
    batch_id      TEXT,               -- e.g. 'MD_2025-04-25'
    batch_state   TEXT,               -- e.g. 'MD'
    batch_date    TEXT,               -- ISO date e.g. '2025-04-25'

    -- Timestamps from Globus / filesystem
    mtime_iso     TEXT,               -- file modification time (ISO-8601)
    fname_ts_epoch INTEGER,           -- timestamp parsed from filename (epoch)
    fname_ts_iso  TEXT,               -- timestamp parsed from filename (ISO-8601)

    -- Housekeeping
    discovered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Mirrors the Postgres indexes on globus_file_index
CREATE INDEX IF NOT EXISTS idx_fi_batch_id
    ON file_index (batch_id);

CREATE INDEX IF NOT EXISTS idx_fi_gap_report_core
    ON file_index (data_state, entry_type, batch_id, file_ext);

CREATE INDEX IF NOT EXISTS idx_fi_dev_parent_ext
    ON file_index (data_state, parent_dir, file_ext)
    WHERE entry_type = 'file' AND batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fi_site_ns
    ON file_index (site, namespace);


-- =============================================================================
-- TABLE: stage_runs
--
-- Mirrors logs.stage_runs.
-- Written by scripts/sqlite/ingest_run.py (CERES jobs) and
-- scripts/ceres/sweep_atlas_runs.py (Atlas jobs synced back).
--
-- status values:
--   'success'          All units processed successfully
--   'partial_success'  Some units failed but run completed  (Postgres: 'partial')
--   'failed'           Run failed entirely
--   'canceled'         Job was canceled
--   'skipped'          Stage skipped (no inputs found)
-- =============================================================================

CREATE TABLE IF NOT EXISTS stage_runs (
    run_id        TEXT PRIMARY KEY,     -- UUID from run_report.json

    -- Identity
    batch_id      TEXT NOT NULL,
    stage         TEXT NOT NULL,        -- 'raw_to_jpg' | 'jpg_to_det' | ...
    stage_version TEXT NOT NULL DEFAULT '',

    -- Status
    status        TEXT NOT NULL CHECK (
                      status IN ('success','partial_success','failed','canceled','skipped')
                  ),
    exit_code     INTEGER NOT NULL,

    -- Timing
    started_at    TEXT NOT NULL,        -- ISO-8601
    ended_at      TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL DEFAULT 0,

    -- Provenance
    code_commit   TEXT NOT NULL DEFAULT '',
    config_path   TEXT NOT NULL DEFAULT '',
    config_hash   TEXT NOT NULL DEFAULT '',
    model_id      TEXT NOT NULL DEFAULT '',

    -- Input / output summary
    input_root         TEXT NOT NULL DEFAULT '',
    n_units_discovered INTEGER NOT NULL DEFAULT 0,
    n_units_succeeded  INTEGER NOT NULL DEFAULT 0,
    n_units_failed     INTEGER NOT NULL DEFAULT 0,

    -- Artifact pointers
    run_root      TEXT NOT NULL DEFAULT '',
    artifacts_dir TEXT NOT NULL DEFAULT '',
    manifest_path TEXT,
    logs_path     TEXT,

    -- Full JSON blob for forward-compatibility
    run_report_json TEXT,

    -- Housekeeping
    ingested_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_sr_batch_stage
    ON stage_runs (batch_id, stage);

CREATE INDEX IF NOT EXISTS idx_sr_stage_status
    ON stage_runs (stage, status);

CREATE INDEX IF NOT EXISTS idx_sr_ended_at
    ON stage_runs (ended_at DESC);


-- =============================================================================
-- TABLE: stage_leases
--
-- Advisory lock table used by CERES-side orchestration to prevent duplicate
-- job submissions.
--
-- NOTE: Atlas does NOT write to this table.  Atlas uses filesystem lockfiles
-- at /90daydata/dash_agir/locks/jpg_to_det/<batch_id>.lock instead, since
-- CERES is the sole writer to the DB.
-- =============================================================================

CREATE TABLE IF NOT EXISTS stage_leases (
    lease_id        TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    stage           TEXT NOT NULL,
    orchestrator_id TEXT NOT NULL,      -- hostname / SLURM job id
    claimed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at      TEXT NOT NULL,
    slurm_job_id    TEXT,

    UNIQUE (batch_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_sl_expires
    ON stage_leases (expires_at);


-- =============================================================================
-- =============================================================================
-- VIEWS
--
-- All views are dropped and recreated on every run of this file so the logic
-- is always up to date.  Table data is never affected.
--
-- These mirror the Postgres views in report_semifield_inventory_gap_views.sql.
--
-- Grouping grain
-- --------------
-- The Postgres views group by (endpoint, site, storage_domain, namespace,
-- storage_root, batch_id) because the same batch can exist at multiple
-- storage locations (JUNO LTS, CERES /90daydata, Atlas /90daydata).
-- These SQLite views use (site, storage_domain, storage_root, batch_id)
-- as the grain — endpoint is omitted since it can change and storage location
-- is already fully identified by the three path columns.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- VIEW: v_raw_batches_needing_jpg
--
-- Batch/locations that have RAW files in semifield-upload but no JPG files in
-- semifield-developed-images/<batch_id>/images/.
--
-- Mirrors: report.raw_batches_needing_jpg
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS v_raw_batches_needing_jpg;

CREATE VIEW v_raw_batches_needing_jpg AS
WITH raw_batches AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        COUNT(*) AS raw_file_count
    FROM file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) IN ('raw', 'arw', 'nef', 'cr2', 'cr3', 'dng', 'rw2')
    GROUP BY site, storage_domain, storage_root, batch_id
),
jpg_batches AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        COUNT(*) AS jpg_file_count
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND parent_dir = 'images'
    GROUP BY site, storage_domain, storage_root, batch_id
)
SELECT
    r.site,
    r.storage_domain,
    r.storage_root,
    r.batch_id,
    r.raw_file_count,
    COALESCE(j.jpg_file_count, 0) AS jpg_file_count
FROM raw_batches r
LEFT JOIN jpg_batches j
    ON  r.site           = j.site
    AND r.storage_domain = j.storage_domain
    AND r.storage_root   = j.storage_root
    AND r.batch_id       = j.batch_id
WHERE COALESCE(j.jpg_file_count, 0) = 0
ORDER BY r.site, r.storage_domain, r.batch_id;


-- -----------------------------------------------------------------------------
-- VIEW: v_jpg_batches_needing_detection
--
-- Batch/locations that have JPGs in semifield-developed-images/<batch_id>/images/
-- but no metadata JSON files in semifield-developed-images/<batch_id>/metadata/.
--
-- Includes batches that have CSVs but no JSONs (needs_metadata_formatting) —
-- use v_semifield_batch_status to distinguish these from needs_jpg_to_det.
--
-- Mirrors: report.jpg_batches_needing_detection_metadata
-- Used by: find_jpg_to_det.py
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS v_jpg_batches_needing_detection;

CREATE VIEW v_jpg_batches_needing_detection AS
WITH jpg_batches AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        COUNT(*) AS jpg_file_count
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND parent_dir = 'images'
    GROUP BY site, storage_domain, storage_root, batch_id
),
metadata_batches AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        COUNT(*) AS metadata_json_count
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'json'
      AND parent_dir = 'metadata'
    GROUP BY site, storage_domain, storage_root, batch_id
)
SELECT
    j.site,
    j.storage_domain,
    j.storage_root,
    j.batch_id,
    j.jpg_file_count,
    COALESCE(m.metadata_json_count, 0) AS metadata_json_count
FROM jpg_batches j
LEFT JOIN metadata_batches m
    ON  j.site           = m.site
    AND j.storage_domain = m.storage_domain
    AND j.storage_root   = m.storage_root
    AND j.batch_id       = m.batch_id
WHERE COALESCE(m.metadata_json_count, 0) = 0
ORDER BY j.site, j.storage_domain, j.batch_id;


-- -----------------------------------------------------------------------------
-- VIEW: v_semifield_batch_status
--
-- Unified per-batch/location pipeline status summary across all stages.
--
-- pipeline_status values
-- ----------------------
--   'needs_jpg'                   Has RAWs, no JPGs
--   'needs_jpg_to_det'            Has JPGs, no CSVs, no metadata JSONs
--   'needs_metadata_formatting'   Has JPGs + CSVs, but no metadata JSONs
--   'partial_jpg'                 Has RAWs but fewer JPGs than RAWs
--   'partial_detection_metadata'  Has JPGs but fewer metadata JSONs than CSVs
--   'ok'                          Has JPGs + metadata JSONs
--
-- Mirrors: report.semifield_batch_inventory_status
--
-- Example queries
-- ---------------
--   SELECT pipeline_status, COUNT(*)
--   FROM v_semifield_batch_status GROUP BY pipeline_status;
--
--   SELECT * FROM v_semifield_batch_status
--   WHERE pipeline_status = 'needs_jpg_to_det';
--
--   SELECT * FROM v_semifield_batch_status
--   WHERE batch_id = 'MD_2025-04-25';
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS v_semifield_batch_status;

CREATE VIEW v_semifield_batch_status AS
WITH raw_batches AS (
    SELECT site, storage_domain, storage_root, batch_id,
           COUNT(*) AS raw_count
    FROM file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) IN ('raw', 'arw', 'nef', 'cr2', 'cr3', 'dng', 'rw2')
    GROUP BY site, storage_domain, storage_root, batch_id
),
jpg_batches AS (
    SELECT site, storage_domain, storage_root, batch_id,
           COUNT(*) AS jpg_count
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND parent_dir = 'images'
    GROUP BY site, storage_domain, storage_root, batch_id
),
metadata_batches AS (
    SELECT site, storage_domain, storage_root, batch_id,
           COUNT(*) AS metadata_json_count
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'json'
      AND parent_dir = 'metadata'
    GROUP BY site, storage_domain, storage_root, batch_id
),
plant_detection_batches AS (
    SELECT site, storage_domain, storage_root, batch_id,
           COUNT(*) AS plant_detection_csv_count
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'csv'
      AND parent_dir IN ('plant-detections', 'detections')
    GROUP BY site, storage_domain, storage_root, batch_id
),
all_batches AS (
    SELECT site, storage_domain, storage_root, batch_id FROM raw_batches
    UNION
    SELECT site, storage_domain, storage_root, batch_id FROM jpg_batches
    UNION
    SELECT site, storage_domain, storage_root, batch_id FROM metadata_batches
    UNION
    SELECT site, storage_domain, storage_root, batch_id FROM plant_detection_batches
)
SELECT
    a.site,
    a.storage_domain,
    a.storage_root,
    a.batch_id,
    COALESCE(r.raw_count, 0)                 AS raw_count,
    COALESCE(j.jpg_count, 0)                 AS jpg_count,
    COALESCE(m.metadata_json_count, 0)       AS metadata_json_count,
    COALESCE(p.plant_detection_csv_count, 0) AS plant_detection_csv_count,
    CASE WHEN COALESCE(p.plant_detection_csv_count, 0) > 0
         THEN 1 ELSE 0 END                   AS has_plant_detections_csv,
    CASE
        WHEN COALESCE(r.raw_count, 0) > 0
             AND COALESCE(j.jpg_count, 0) = 0
            THEN 'needs_jpg'
        WHEN COALESCE(j.jpg_count, 0) > 0
             AND COALESCE(m.metadata_json_count, 0) = 0
             AND COALESCE(p.plant_detection_csv_count, 0) > 0
            THEN 'needs_metadata_formatting'
        WHEN COALESCE(j.jpg_count, 0) > 0
             AND COALESCE(m.metadata_json_count, 0) = 0
             AND COALESCE(p.plant_detection_csv_count, 0) = 0
            THEN 'needs_jpg_to_det'
        WHEN COALESCE(r.raw_count, 0) > COALESCE(j.jpg_count, 0)
            THEN 'partial_jpg'
        WHEN COALESCE(j.jpg_count, 0) > 0
             AND COALESCE(m.metadata_json_count, 0) < COALESCE(p.plant_detection_csv_count, 0)
             AND COALESCE(p.plant_detection_csv_count, 0) > 0
            THEN 'partial_detection_metadata'
        ELSE 'ok'
    END AS pipeline_status
FROM all_batches a
LEFT JOIN raw_batches             r ON r.site=a.site AND r.storage_domain=a.storage_domain AND r.storage_root=a.storage_root AND r.batch_id=a.batch_id
LEFT JOIN jpg_batches             j ON j.site=a.site AND j.storage_domain=a.storage_domain AND j.storage_root=a.storage_root AND j.batch_id=a.batch_id
LEFT JOIN metadata_batches        m ON m.site=a.site AND m.storage_domain=a.storage_domain AND m.storage_root=a.storage_root AND m.batch_id=a.batch_id
LEFT JOIN plant_detection_batches p ON p.site=a.site AND p.storage_domain=a.storage_domain AND p.storage_root=a.storage_root AND p.batch_id=a.batch_id
ORDER BY a.site, a.storage_domain, a.batch_id;


-- -----------------------------------------------------------------------------
-- VIEW: v_raw_files_missing_jpg
--
-- Specific RAW files whose filename stem has no matching JPG stem under
-- semifield-developed-images/<batch_id>/images/.
-- Assumes raw_to_jpg preserves the filename stem.
--
-- Mirrors: report.raw_files_missing_jpg
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS v_raw_files_missing_jpg;

CREATE VIEW v_raw_files_missing_jpg AS
WITH raw_files AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        rel_path  AS raw_rel_path,
        file_name AS raw_file_name,
        SUBSTR(file_name, 1, LENGTH(file_name) - LENGTH(file_ext) - 1) AS stem
    FROM file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) IN ('raw', 'arw', 'nef', 'cr2', 'cr3', 'dng', 'rw2')
),
jpg_files AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        file_name AS jpg_file_name,
        SUBSTR(file_name, 1, LENGTH(file_name) - LENGTH(file_ext) - 1) AS stem
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND parent_dir = 'images'
)
SELECT
    r.site, r.storage_domain, r.storage_root,
    r.batch_id, r.raw_rel_path, r.raw_file_name
FROM raw_files r
LEFT JOIN jpg_files j
    ON  r.site           = j.site
    AND r.storage_domain = j.storage_domain
    AND r.storage_root   = j.storage_root
    AND r.batch_id       = j.batch_id
    AND r.stem           = j.stem
WHERE j.jpg_file_name IS NULL
ORDER BY r.site, r.batch_id, r.raw_file_name;


-- -----------------------------------------------------------------------------
-- VIEW: v_jpg_files_missing_metadata
--
-- Specific JPG files in batches that have no metadata JSON files under
-- semifield-developed-images/<batch_id>/metadata/.
--
-- Mirrors: report.jpg_files_in_batches_missing_metadata
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS v_jpg_files_missing_metadata;

CREATE VIEW v_jpg_files_missing_metadata AS
WITH jpg_files AS (
    SELECT
        site, storage_domain, storage_root, batch_id,
        rel_path  AS jpg_rel_path,
        file_name AS jpg_file_name
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND parent_dir = 'images'
),
metadata_batches AS (
    SELECT DISTINCT site, storage_domain, storage_root, batch_id
    FROM file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'json'
      AND parent_dir = 'metadata'
)
SELECT
    j.site, j.storage_domain, j.storage_root,
    j.batch_id, j.jpg_rel_path, j.jpg_file_name
FROM jpg_files j
LEFT JOIN metadata_batches m
    ON  j.site           = m.site
    AND j.storage_domain = m.storage_domain
    AND j.storage_root   = m.storage_root
    AND j.batch_id       = m.batch_id
WHERE m.batch_id IS NULL
ORDER BY j.site, j.batch_id, j.jpg_file_name;


-- =============================================================================
-- Quick-reference example queries
-- =============================================================================
--
-- Pipeline health summary:
--   SELECT pipeline_status, COUNT(*)
--   FROM v_semifield_batch_status
--   GROUP BY pipeline_status ORDER BY COUNT(*) DESC;
--
-- Batches needing jpg_to_det:
--   SELECT site, storage_domain, batch_id, jpg_count
--   FROM v_semifield_batch_status
--   WHERE pipeline_status = 'needs_jpg_to_det';
--
-- Batches needing metadata formatting:
--   SELECT site, storage_domain, batch_id
--   FROM v_semifield_batch_status
--   WHERE pipeline_status = 'needs_metadata_formatting';
--
-- Batches needing raw→jpg:
--   SELECT * FROM v_raw_batches_needing_jpg;
--
-- Specific RAW files missing a JPG:
--   SELECT * FROM v_raw_files_missing_jpg WHERE batch_id = 'MD_2025-04-25';
--
-- All JPG files in batches still missing metadata:
--   SELECT * FROM v_jpg_files_missing_metadata WHERE batch_id = 'MD_2025-04-25';
--
-- Full status for one batch across all locations:
--   SELECT * FROM v_semifield_batch_status WHERE batch_id = 'MD_2025-04-25';