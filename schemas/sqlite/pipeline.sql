-- =============================================================================
-- schemas/sqlite/pipeline.sql
--
-- SQLite schema for the AgIR Globus inventory + pipeline orchestration DB.
--
-- Tables
-- ------
--   Inventory:      inventory_runs, globus_file_index,
--                   batch_inventory_summary, storage_gap_summary
--   Orchestration:  stage_runs, stage_leases, staged_inputs
--
-- Views
-- -----
--   v_batches_needing_raw_to_jpg   What to run next for stage 1
--   v_batches_needing_jpg_to_det   What to run next for stage 2
--
-- Views query globus_file_index WHERE is_current = 1 across ALL indexed
-- endpoints simultaneously, so cross-site inventory gaps resolve correctly
-- regardless of which scan run_id last touched each row.
--
-- Notes
-- -----
-- - All DATE/TIMESTAMPTZ values are stored as ISO-8601 TEXT.
-- - checksum is retained as a column but remains NULL until a future
--   scanner populates it.
-- - Legacy table migration (missing file_id / created_at_ts_iso) is
--   handled by the Python script, not this file.
--
-- Apply (idempotent):
--   sqlite3 /path/to/globus_file_index.sqlite3 < schemas/sqlite/pipeline.sql
--
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Connection PRAGMAs  (set per-connection; survive outside any transaction)
-- -----------------------------------------------------------------------------
PRAGMA foreign_keys  = ON;
PRAGMA journal_mode  = WAL;
PRAGMA synchronous   = NORMAL;
PRAGMA temp_store    = MEMORY;
PRAGMA busy_timeout  = 120000;

BEGIN;

-- =============================================================================
-- INVENTORY TABLES
-- Populated by scripts/admin/globus_index.py
-- =============================================================================

-- One row per Globus crawl of a single (endpoint, storage_root, data_state).
CREATE TABLE IF NOT EXISTS inventory_runs (
    run_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint              TEXT    NOT NULL,
    site                  TEXT    NOT NULL,
    storage_domain        TEXT    NOT NULL,
    namespace             TEXT    NOT NULL,
    storage_root          TEXT    NOT NULL,
    data_state            TEXT    NOT NULL,
    started_at_ts_iso     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at_ts_iso       TEXT,
    status                TEXT    NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running','success','partial','failed','skipped')),
    total_seen            INTEGER NOT NULL DEFAULT 0,
    total_files_seen      INTEGER NOT NULL DEFAULT 0,
    total_dirs_seen       INTEGER NOT NULL DEFAULT 0,
    total_batches_written INTEGER NOT NULL DEFAULT 0,
    total_errors          INTEGER NOT NULL DEFAULT 0,
    total_marked_stale    INTEGER NOT NULL DEFAULT 0,
    error_summary         TEXT,
    UNIQUE (run_id)
);

-- One row per file/dir discovered across all Globus endpoints.
-- is_current = 1  → seen in the most recent crawl of its scope.
-- is_current = 0  → no longer present; missing_since_ts_iso records when.
CREATE TABLE IF NOT EXISTS globus_file_index (
    file_id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Storage location
    endpoint             TEXT    NOT NULL,
    site                 TEXT    NOT NULL,
    storage_domain       TEXT    NOT NULL,
    namespace            TEXT    NOT NULL,
    storage_root         TEXT    NOT NULL,
    rel_path             TEXT    NOT NULL,
    full_path            TEXT    NOT NULL,
    parent_dir           TEXT,
    file_name            TEXT    NOT NULL,

    -- Classification
    entry_type           TEXT    NOT NULL CHECK (entry_type IN ('file', 'dir')),
    file_ext             TEXT,               -- extension without dot, lowercased
    size_bytes           INTEGER,
    permissions          TEXT,
    checksum             TEXT,               -- 'sha256:<hex>' or NULL

    -- Batch identity
    batch_id             TEXT,               -- e.g. 'MD_2025-04-25'
    batch_state          TEXT,               -- e.g. 'MD'
    batch_date           TEXT,               -- ISO date e.g. '2025-04-25'

    -- Pipeline lifecycle
    data_state           TEXT    NOT NULL,   -- 'semifield-upload' | 'semifield-developed-images' | ...

    -- Timestamps
    mtime_iso            TEXT,               -- last_modified from Globus (ISO-8601)
    fname_ts_epoch       INTEGER,            -- epoch parsed from filename
    fname_ts_iso         TEXT,               -- epoch as ISO-8601
    created_at_ts_iso    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    -- Inventory tracking
    first_seen_ts_iso    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_ts_iso     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_run_id     INTEGER,
    missing_since_ts_iso TEXT,
    is_current           INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),

    UNIQUE (endpoint, data_state, storage_root, rel_path),
    FOREIGN KEY (last_seen_run_id) REFERENCES inventory_runs (run_id)
);

-- Per-run, per-batch file-count summary. Used for reporting and dashboards.
-- Not used for orchestration decisions (views query globus_file_index directly).
CREATE TABLE IF NOT EXISTS batch_inventory_summary (
    run_id           INTEGER NOT NULL,
    endpoint         TEXT    NOT NULL,
    site             TEXT    NOT NULL,
    storage_domain   TEXT    NOT NULL,
    namespace        TEXT    NOT NULL,
    storage_root     TEXT    NOT NULL,
    data_state       TEXT    NOT NULL,
    batch_id         TEXT,
    batch_state      TEXT,
    batch_date       TEXT,

    file_count        INTEGER NOT NULL,
    dir_count         INTEGER NOT NULL,
    total_size_bytes  INTEGER NOT NULL,
    raw_count         INTEGER NOT NULL DEFAULT 0,
    jpg_count         INTEGER NOT NULL DEFAULT 0,
    json_count        INTEGER NOT NULL DEFAULT 0,
    csv_count         INTEGER NOT NULL DEFAULT 0,
    png_count         INTEGER NOT NULL DEFAULT 0,
    metadata_count    INTEGER NOT NULL DEFAULT 0,
    detection_count   INTEGER NOT NULL DEFAULT 0,
    min_mtime_iso     TEXT,
    max_mtime_iso     TEXT,
    created_at_ts_iso TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (run_id, endpoint, site, storage_domain, namespace,
                 storage_root, data_state, batch_id),
    FOREIGN KEY (run_id) REFERENCES inventory_runs (run_id)
);

-- Per-run cross-data-state gap summary. Used for reporting and dashboards.
-- Not used for orchestration decisions (views query globus_file_index directly).
CREATE TABLE IF NOT EXISTS storage_gap_summary (
    run_id           INTEGER NOT NULL,
    batch_id         TEXT    NOT NULL,
    batch_state      TEXT,
    batch_date       TEXT,
    storage_domain   TEXT    NOT NULL,
    namespace        TEXT    NOT NULL,
    site             TEXT    NOT NULL,
    storage_root     TEXT    NOT NULL,

    has_semifield_upload             INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_upload             IN (0, 1)),
    has_semifield_developed_images   INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_developed_images   IN (0, 1)),
    has_semifield_cutouts            INTEGER NOT NULL DEFAULT 0 CHECK (has_semifield_cutouts            IN (0, 1)),

    upload_file_count    INTEGER NOT NULL DEFAULT 0,
    developed_file_count INTEGER NOT NULL DEFAULT 0,
    cutout_file_count    INTEGER NOT NULL DEFAULT 0,

    raw_count        INTEGER NOT NULL DEFAULT 0,
    jpg_count        INTEGER NOT NULL DEFAULT 0,
    metadata_count   INTEGER NOT NULL DEFAULT 0,
    detection_count  INTEGER NOT NULL DEFAULT 0,
    cutout_png_count INTEGER NOT NULL DEFAULT 0,

    created_at_ts_iso TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (run_id, batch_id, storage_domain, namespace, site, storage_root),
    FOREIGN KEY (run_id) REFERENCES inventory_runs (run_id)
);


-- =============================================================================
-- ORCHESTRATION TABLES
-- Written by scripts/sqlite/ingest_run.py after each SLURM job completes.
-- =============================================================================

-- One row per completed pipeline stage run.  run_id is the UUID from
-- run_report.json, not an integer — kept distinct from inventory_runs.run_id.
CREATE TABLE IF NOT EXISTS stage_runs (
    run_id        TEXT    PRIMARY KEY,  -- UUID from run_report.json
    batch_id      TEXT    NOT NULL,
    stage         TEXT    NOT NULL,     -- 'raw_to_jpg' | 'jpg_to_det' | ...
    stage_version TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL
                      CHECK (status IN ('success','partial_success','failed','canceled','skipped')),
    exit_code     INTEGER NOT NULL,
    started_at    TEXT    NOT NULL,     -- ISO-8601
    ended_at      TEXT    NOT NULL,     -- ISO-8601
    duration_ms   INTEGER NOT NULL DEFAULT 0,

    -- Provenance
    code_commit   TEXT    NOT NULL DEFAULT '',
    config_path   TEXT    NOT NULL DEFAULT '',
    config_hash   TEXT    NOT NULL DEFAULT '',
    model_id      TEXT    NOT NULL DEFAULT '',

    -- Input / output summary
    input_root          TEXT    NOT NULL DEFAULT '',
    n_units_discovered  INTEGER NOT NULL DEFAULT 0,
    n_units_succeeded   INTEGER NOT NULL DEFAULT 0,
    n_units_failed      INTEGER NOT NULL DEFAULT 0,

    -- Artifact pointers
    run_root      TEXT    NOT NULL DEFAULT '',
    artifacts_dir TEXT    NOT NULL DEFAULT '',
    manifest_path TEXT,
    logs_path     TEXT,

    -- Full JSON blob for forward-compatibility
    run_report_json TEXT,

    ingested_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Advisory lock preventing duplicate SLURM job submissions for the same
-- (batch_id, stage).  Claimed before sbatch; released after ingest.
CREATE TABLE IF NOT EXISTS stage_leases (
    lease_id        TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    stage           TEXT NOT NULL,
    orchestrator_id TEXT NOT NULL,
    claimed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at      TEXT NOT NULL,
    slurm_job_id    TEXT,
    UNIQUE (batch_id, stage)
);

-- Input staging requests/results for pre-submit data movement.
-- This records JUNO/LTS -> 90daydata availability without making the
-- orchestrator infer staged state from the filesystem.
CREATE TABLE IF NOT EXISTS staged_inputs (
    staging_id        TEXT PRIMARY KEY,
    batch_id          TEXT NOT NULL,
    stage             TEXT NOT NULL,

    src_endpoint      TEXT NOT NULL DEFAULT '',
    dst_endpoint      TEXT NOT NULL DEFAULT '',
    src_path          TEXT NOT NULL,
    dst_path          TEXT NOT NULL,

    status            TEXT NOT NULL DEFAULT 'planned'
                          CHECK (status IN (
                              'planned',
                              'requested',
                              'submitted',
                              'active',
                              'completed',
                              'failed',
                              'canceled'
                          )),
    requested_by      TEXT NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 100,

    globus_task_id    TEXT,
    error_summary     TEXT,

    requested_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    submitted_at      TEXT,
    completed_at      TEXT,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    UNIQUE (batch_id, stage, src_path, dst_path)
);


-- =============================================================================
-- INDEXES
-- =============================================================================

-- globus_file_index  ──────────────────────────────────────────────────────────

-- Primary gap-report index: drives both orchestration views.
CREATE INDEX IF NOT EXISTS idx_gfi_batch_state_current
    ON globus_file_index (batch_id, data_state, site, is_current);

-- Storage-scoped current-state queries (used by build_batch_inventory_summary).
CREATE INDEX IF NOT EXISTS idx_gfi_storage_current
    ON globus_file_index (site, storage_domain, namespace, storage_root, data_state, is_current);

-- Extension + parent_dir lookups (file-type counts in summaries and views).
CREATE INDEX IF NOT EXISTS idx_gfi_ext_parent
    ON globus_file_index (data_state, file_ext, parent_dir);

-- Stale-marking: find rows not touched by the current run.
CREATE INDEX IF NOT EXISTS idx_gfi_run_current
    ON globus_file_index (last_seen_run_id, is_current);

-- Full-path lookups (point queries, dashboards).
CREATE INDEX IF NOT EXISTS idx_gfi_full_path
    ON globus_file_index (full_path);

-- inventory_runs  ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_ir_scope_time
    ON inventory_runs (endpoint, site, storage_domain, namespace,
                       storage_root, data_state, started_at_ts_iso DESC);

-- stage_runs  ─────────────────────────────────────────────────────────────────

-- Primary lookup for the NOT EXISTS subquery in both orchestration views.
CREATE INDEX IF NOT EXISTS idx_sr_batch_stage_status
    ON stage_runs (batch_id, stage, status);

-- Recency queries and dashboards.
CREATE INDEX IF NOT EXISTS idx_sr_ended_at
    ON stage_runs (ended_at DESC);

-- stage_leases  ───────────────────────────────────────────────────────────────

-- Covers the active-lease CTE in both views: stage + expiry + batch_id.
CREATE INDEX IF NOT EXISTS idx_sl_stage_expires
    ON stage_leases (stage, expires_at, batch_id);

-- staged_inputs  ──────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_si_batch_stage_status
    ON staged_inputs (batch_id, stage, status);

CREATE INDEX IF NOT EXISTS idx_si_stage_status_priority
    ON staged_inputs (stage, status, priority, requested_at);

-- Summary tables  ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_bis_batch
    ON batch_inventory_summary (batch_id, data_state, site, storage_domain, namespace);

CREATE INDEX IF NOT EXISTS idx_sgs_batch
    ON storage_gap_summary (batch_id, site, storage_domain, namespace);


-- =============================================================================
-- ORCHESTRATION VIEWS
--
-- Answer "what should I run next?" by querying globus_file_index WHERE
-- is_current = 1 across all indexed endpoints simultaneously.
--
-- Cross-site correctness: JUNO RAW files (is_current=1) and CERES JPGs
-- (is_current=1) from different scan runs are both visible in one query.
-- No per-run scoping.  storage_gap_summary is for reporting only.
--
-- Both views exclude batches that:
--   - already have output files (jpg_file_count > 0 / det_count > 0)
--   - have an active, unexpired lease in stage_leases
--   - have ever had a successful stage_run (NOT EXISTS on stage_runs)
--
-- Views are DROP + CREATE so re-applying this file always installs the
-- latest logic without touching any table data.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- v_batches_needing_raw_to_jpg
-- Batches with current RAW files in semifield-upload that have no current
-- JPG files in semifield-developed-images/*/images/.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_batches_needing_raw_to_jpg;
CREATE VIEW v_batches_needing_raw_to_jpg AS
WITH
raw_batches AS (
    SELECT
        batch_id,
        MIN(batch_date) AS batch_date,
        COUNT(*)        AS raw_file_count
    FROM globus_file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND file_ext   IN ('raw', 'arw', 'nef', 'cr2', 'cr3', 'dng', 'rw2', 'raf', 'orf')
    GROUP BY batch_id
),
jpg_batches AS (
    SELECT
        batch_id,
        COUNT(*) AS jpg_file_count
    FROM globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND file_ext   IN ('jpg', 'jpeg')
      AND parent_dir = 'images'
    GROUP BY batch_id
),
active_leases AS (
    SELECT batch_id
    FROM   stage_leases
    WHERE  stage      = 'raw_to_jpg'
      AND  expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
)
SELECT
    r.batch_id,
    r.batch_date,
    r.raw_file_count,
    COALESCE(j.jpg_file_count, 0) AS jpg_file_count
FROM  raw_batches   r
LEFT JOIN jpg_batches   j  ON r.batch_id = j.batch_id
LEFT JOIN active_leases al ON r.batch_id = al.batch_id
WHERE COALESCE(j.jpg_file_count, 0) = 0   -- no JPGs exist yet
  AND al.batch_id IS NULL                  -- no active lease
  AND NOT EXISTS (                         -- never succeeded
      SELECT 1 FROM stage_runs sr
      WHERE sr.batch_id = r.batch_id
        AND sr.stage    = 'raw_to_jpg'
        AND sr.status   = 'success'
  )
ORDER BY r.batch_date ASC, r.batch_id ASC;


-- -----------------------------------------------------------------------------
-- v_batches_needing_jpg_to_det
-- Batches with current JPG files in semifield-developed-images/*/images/
-- that have no current detection output files.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_batches_needing_jpg_to_det;
CREATE VIEW v_batches_needing_jpg_to_det AS
WITH
jpg_batches AS (
    SELECT
        batch_id,
        MIN(batch_date) AS batch_date,
        COUNT(*)        AS jpg_count
    FROM globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND file_ext   IN ('jpg', 'jpeg')
      AND parent_dir = 'images'
    GROUP BY batch_id
),
det_batches AS (
    SELECT
        batch_id,
        COUNT(*) AS det_count
    FROM globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND parent_dir IN ('detections', 'plant-detections', 'metadata')
    GROUP BY batch_id
),
active_leases AS (
    SELECT batch_id
    FROM   stage_leases
    WHERE  stage      = 'jpg_to_det'
      AND  expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
)
SELECT
    j.batch_id,
    j.batch_date,
    j.jpg_count,
    COALESCE(d.det_count, 0) AS det_count
FROM  jpg_batches   j
LEFT JOIN det_batches   d  ON j.batch_id = d.batch_id
LEFT JOIN active_leases al ON j.batch_id = al.batch_id
WHERE COALESCE(d.det_count, 0) = 0   -- no detections exist yet
  AND al.batch_id IS NULL             -- no active lease
  AND NOT EXISTS (                    -- never succeeded
      SELECT 1 FROM stage_runs sr
      WHERE sr.batch_id = j.batch_id
        AND sr.stage    = 'jpg_to_det'
        AND sr.status   = 'success'
  )
ORDER BY j.batch_date ASC, j.batch_id ASC;


-- =============================================================================
PRAGMA user_version = 4;
COMMIT;
