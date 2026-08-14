-- =============================================================================
-- schemas/sqlite/pipeline.sql
--
-- SQLite schema for the AgIR Globus inventory + pipeline orchestration DB.
--
-- Tables
-- ------
--   Reference:      season_date_ranges, season_aliases,
--                   species, species_aliases, species_multi_symbols,
--                   cultivars, cultivar_aliases, color_palette
--   Inventory:      inventory_runs, globus_file_index,
--                   batch_inventory_summary, storage_gap_summary
--   Orchestration:  stage_runs, stage_leases, staged_inputs, result_syncs
--
-- Views
-- -----
--   v_batches_needing_raw_to_jpg   What to run next for stage 1
--   v_batches_needing_jpg_to_det   What to run next for stage 2
--   v_batches_needing_det_to_world What to run next for stage 3 (det_to_world)
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
-- journal_mode is DELETE, not WAL: this DB lives on an NFS-mounted shared
-- filesystem (/project, accessed from ATLAS/CERES/JUNO), and WAL's
-- shared-memory (-shm) locking is unreliable there — SQLite's own docs call
-- out network filesystems as unsupported for WAL. Confirmed in practice: a
-- CERES compute node hit "OperationalError: locking protocol" once this DB
-- got switched into WAL mode by an earlier apply of this file. DELETE (the
-- traditional rollback journal) only needs plain POSIX advisory locks, which
-- this mount's NFS lock manager handles correctly. Keep in sync with
-- orchestrator/sqlite_db.py's open_db() and the comment in
-- scripts/admin/globus_index.py's init_db().
PRAGMA foreign_keys  = ON;
PRAGMA journal_mode  = DELETE;
PRAGMA synchronous   = NORMAL;
PRAGMA temp_store    = MEMORY;
PRAGMA busy_timeout  = 120000;

BEGIN;

-- =============================================================================
-- STATIC REFERENCE TABLES
-- Populated by scripts/admin/load_date_ranges.py from configs/date_ranges.yaml
-- =============================================================================

-- One row per (site, season) date window defined in configs/date_ranges.yaml.
-- Lets callers resolve "what season/application was batch_date X in for site Y"
-- and look up the bbot_version/crs/pipeline_season used for that window.
CREATE TABLE IF NOT EXISTS season_date_ranges (
    site               TEXT NOT NULL,   -- 'MD' | 'NC' | 'TX'
    season_name        TEXT NOT NULL,   -- e.g. 'weeds 2022', 'cover crops 2022/2023'
    start_date         TEXT NOT NULL,   -- ISO-8601 date
    end_date           TEXT NOT NULL,   -- ISO-8601 date
    bbot_version       TEXT NOT NULL,   -- e.g. '2.0', '3.0', '3.1'
    crs                TEXT NOT NULL,   -- 'LOCAL' or an EPSG code, e.g. '4326', '32618'
    pipeline_season    TEXT,            -- canonical season slug used in pipeline configs, if any
    gh_reviewer        TEXT,            -- default PR reviewer GitHub username for this season, if any
    note               TEXT,            -- free-text note, if any
    updated_at_ts_iso  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (site, season_name)
);

-- Alternate spellings/slugs that resolve to a canonical pipeline_season.
-- From the season_mappings block in configs/date_ranges.yaml.
CREATE TABLE IF NOT EXISTS season_aliases (
    pipeline_season TEXT NOT NULL,
    alias           TEXT NOT NULL,

    PRIMARY KEY (pipeline_season, alias)
);

-- =============================================================================
-- SPECIES / CULTIVAR REFERENCE TABLES
-- Canonical taxonomy + segmentation-mask color data. This DB is the source
-- of truth: populated by scripts/admin/load_species_reference.py from
-- /project/dash_agir/semifield-utils/species_information/{species_info.json,
-- cultivars.json, colors.py, cultivar_colors.py}. That same script calls
-- orchestrator/species_catalog.py's export_catalog() to regenerate a flat
-- species_catalog.generated.json alongside the source files — that generated
-- file, not this DB, is what stages/det_to_world reads, so stage code never
-- opens a DB connection. Column names that collide with SQL keywords in the
-- source JSON ("class", "group", "order", "species") are renamed
-- (taxon_class, species_group, taxon_order, species_epithet) to avoid
-- needing to quote them everywhere.
-- =============================================================================

-- One row per species/background/non-plant class. class_id doubles as the
-- segmentation-mask class index; (r, g, b) is that class's mask color.
CREATE TABLE IF NOT EXISTS species (
    class_id            INTEGER PRIMARY KEY,
    species_key         TEXT    NOT NULL UNIQUE,  -- species_info.json dict key, e.g. 'Brassica_complex_0'
    USDA_symbol         TEXT    NOT NULL UNIQUE,  -- zone shapefile 'species' attribute join key
    EPPO                TEXT,
    species_group       TEXT,    -- JSON "group"
    taxon_class         TEXT,    -- JSON "class"
    subclass            TEXT,
    taxon_order         TEXT,    -- JSON "order"
    family              TEXT,
    genus               TEXT,
    species_epithet     TEXT,    -- JSON "species"
    common_name         TEXT,
    authority           TEXT,
    growth_habit        TEXT,
    duration            TEXT,
    collection_location TEXT,
    category            TEXT,
    collection_timing   TEXT,
    link                TEXT,
    note                TEXT,
    hex                 TEXT    NOT NULL,
    r                   INTEGER NOT NULL,
    g                   INTEGER NOT NULL,
    b                   INTEGER NOT NULL,
    updated_at_ts_iso   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Alternate common names/spellings for a species. From species_info.json's
-- per-entry "alias" list.
CREATE TABLE IF NOT EXISTS species_aliases (
    class_id INTEGER NOT NULL REFERENCES species (class_id),
    alias    TEXT    NOT NULL,

    PRIMARY KEY (class_id, alias)
);

-- Component USDA symbols folded into a multi-species class (e.g. the
-- Brassica complex). From species_info.json's "multi_species_USDA_symbol".
CREATE TABLE IF NOT EXISTS species_multi_symbols (
    class_id              INTEGER NOT NULL REFERENCES species (class_id),
    component_usda_symbol TEXT    NOT NULL,

    PRIMARY KEY (class_id, component_usda_symbol)
);

-- One row per registered cultivar / experimental line. cultivar_class_id
-- doubles as the segmentation-mask class index for cultivar-season batches,
-- same role class_id plays for species; (r, g, b) is that class's mask
-- color, unique across species AND cultivars.
CREATE TABLE IF NOT EXISTS cultivars (
    cultivar_class_id  INTEGER PRIMARY KEY,
    cultivar_key       TEXT    NOT NULL UNIQUE,  -- cultivars.json dict key, e.g. 'PEANUT_BAILEY_II'
    parent_USDA_symbol TEXT    NOT NULL REFERENCES species (USDA_symbol),
    entity_type        TEXT    NOT NULL CHECK (entity_type IN ('cultivar', 'experimental_line')),
    display_name       TEXT    NOT NULL,
    cultivar_name      TEXT,
    line_name          TEXT,
    registered         INTEGER NOT NULL CHECK (registered IN (0, 1)),
    collection_location TEXT,
    cultivar_category  TEXT,
    link               TEXT,
    note               TEXT,
    hex                TEXT    NOT NULL,
    r                  INTEGER NOT NULL,
    g                  INTEGER NOT NULL,
    b                  INTEGER NOT NULL,
    updated_at_ts_iso  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Alternate names for a cultivar. From cultivars.json's per-entry "alias" list.
CREATE TABLE IF NOT EXISTS cultivar_aliases (
    cultivar_class_id INTEGER NOT NULL REFERENCES cultivars (cultivar_class_id),
    alias             TEXT    NOT NULL,

    PRIMARY KEY (cultivar_class_id, alias)
);

-- Full color pool the species/cultivar hex+rgb values are drawn from
-- (colors.py / cultivar_colors.py), including colors not yet assigned to any
-- class (assigned_* both NULL). Lets a future new species/cultivar pick an
-- unused color while querying for collisions, instead of the manual
-- set-intersection asserts colors.py/cultivar_colors.py used to run at
-- import time.
CREATE TABLE IF NOT EXISTS color_palette (
    palette                    TEXT    NOT NULL CHECK (palette IN ('species', 'cultivar')),
    seq_index                  INTEGER NOT NULL,  -- position in the source colors.py/cultivar_colors.py list
    hex                        TEXT    NOT NULL,
    r                          INTEGER NOT NULL,
    g                          INTEGER NOT NULL,
    b                          INTEGER NOT NULL,
    assigned_class_id          INTEGER REFERENCES species (class_id),
    assigned_cultivar_class_id INTEGER REFERENCES cultivars (cultivar_class_id),

    PRIMARY KEY (palette, seq_index)
);


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

-- Atlas-to-Ceres result synchronization. One row represents the complete
-- transfer, verification, and canonical-ingestion lifecycle for one stage run.
-- This table is authoritative on Ceres; Atlas receives a read-only copy in the
-- nightly database snapshot.
CREATE TABLE IF NOT EXISTS result_syncs (
    run_id                TEXT PRIMARY KEY,
    batch_id              TEXT NOT NULL,
    stage                 TEXT NOT NULL,
    run_status            TEXT NOT NULL
                               CHECK (run_status IN (
                                   'success',
                                   'partial_success',
                                   'failed',
                                   'canceled',
                                   'skipped'
                               )),
    promotion_succeeded   INTEGER NOT NULL DEFAULT 0
                               CHECK (promotion_succeeded IN (0, 1)),

    source_site           TEXT NOT NULL DEFAULT 'ATLAS',
    destination_site      TEXT NOT NULL DEFAULT 'CERES',

    -- Each synchronization transfers exactly one immutable run bundle.
    src_endpoint          TEXT NOT NULL,
    dst_endpoint          TEXT NOT NULL,
    src_path              TEXT NOT NULL,
    dst_path              TEXT NOT NULL,
    recursive             INTEGER NOT NULL DEFAULT 1
                               CHECK (recursive IN (0, 1)),
    globus_task_id        TEXT,

    request_path          TEXT NOT NULL,
    request_json          TEXT NOT NULL,

    status                TEXT NOT NULL DEFAULT 'requested'
                               CHECK (status IN (
                                   'requested',
                                   'transferring',
                                   'transferred',
                                   'verified',
                                   'ingested',
                                   'failed',
                                   'canceled'
                               )),
    attempt_count         INTEGER NOT NULL DEFAULT 0
                               CHECK (attempt_count >= 0),
    error_summary         TEXT,

    request_created_at    TEXT NOT NULL,
    registered_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    transfer_started_at   TEXT,
    transferred_at       TEXT,
    verified_at           TEXT,
    ingested_at           TEXT,
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);


-- =============================================================================
-- INDEXES
-- =============================================================================

-- season_date_ranges / season_aliases  ────────────────────────────────────────

-- batch_date -> season lookups (BETWEEN start_date AND end_date scans).
CREATE INDEX IF NOT EXISTS idx_sdr_site_dates
    ON season_date_ranges (site, start_date, end_date);

-- Resolve a season_date_ranges row by its canonical pipeline_season.
CREATE INDEX IF NOT EXISTS idx_sdr_pipeline_season
    ON season_date_ranges (pipeline_season);

-- Resolve an alias string back to its canonical pipeline_season.
CREATE INDEX IF NOT EXISTS idx_sa_alias
    ON season_aliases (alias);

-- species / cultivars  ────────────────────────────────────────────────────────

-- Resolve an alias string back to its class_id / cultivar_class_id.
CREATE INDEX IF NOT EXISTS idx_species_aliases_alias
    ON species_aliases (alias);

CREATE INDEX IF NOT EXISTS idx_cultivar_aliases_alias
    ON cultivar_aliases (alias);

-- Cultivar -> parent species joins (det_to_world enrichment).
CREATE INDEX IF NOT EXISTS idx_cultivars_parent
    ON cultivars (parent_USDA_symbol);

-- Find an unassigned reserve color in a given palette.
CREATE INDEX IF NOT EXISTS idx_color_palette_unassigned
    ON color_palette (palette, assigned_class_id, assigned_cultivar_class_id);

-- globus_file_index  ──────────────────────────────────────────────────────────

-- Point lookups by batch_id (e.g. joining a known batch back to its site).
-- NOT used by the v_batches_needing_* CTEs below, which have no batch_id
-- equality predicate (they GROUP BY batch_id) — see idx_gfi_state_type_ext.
CREATE INDEX IF NOT EXISTS idx_gfi_batch_state_current
    ON globus_file_index (batch_id, data_state, site, is_current);

-- Storage-scoped current-state queries (used by build_batch_inventory_summary).
CREATE INDEX IF NOT EXISTS idx_gfi_storage_current
    ON globus_file_index (site, storage_domain, namespace, storage_root, data_state, is_current);

-- Extension + parent_dir lookups (file-type counts in summaries and views).
CREATE INDEX IF NOT EXISTS idx_gfi_ext_parent
    ON globus_file_index (data_state, file_ext, parent_dir);

-- Covering index for the raw_batches/jpg_batches CTEs in both
-- v_batches_needing_raw_to_jpg and v_batches_needing_jpg_to_det: those CTEs
-- filter on (data_state, entry_type, is_current, file_ext[, parent_dir]) and
-- GROUP BY batch_id, MIN(batch_date). Leading columns match the filters so
-- SQLite can seek instead of scan, and trailing batch_id/batch_date let the
-- GROUP BY run as an index-only scan without touching the table.
CREATE INDEX IF NOT EXISTS idx_gfi_state_type_ext
    ON globus_file_index (data_state, entry_type, is_current, file_ext, parent_dir, batch_id, batch_date);

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

-- result_syncs  ───────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_rs_status_updated
    ON result_syncs (status, updated_at);

CREATE INDEX IF NOT EXISTS idx_rs_batch_stage_status
    ON result_syncs (batch_id, stage, status);

CREATE INDEX IF NOT EXISTS idx_rs_globus_task
    ON result_syncs (globus_task_id);

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
--
-- One row per (batch_id, site, storage_domain, namespace) location that
-- currently holds this batch's JPGs — a batch whose images are indexed at
-- more than one location (e.g. both JUNO and CERES) appears once per
-- location, all sharing the same batch-level jpg_count/det_count. This is
-- deliberate fan-out, not an accidental duplicate: callers that need one row
-- per batch should DISTINCT/GROUP BY batch_id themselves (see
-- get_batches_needing_jpg_to_det's site=None branch in orchestrator/sqlite_db.py).
--
-- Unlike an earlier version of this view, jpg_batches/jpg_locations are NOT
-- restricted to site IN ('JUNO', 'CERES') — jpg_to_det's multi-site resolver
-- (orchestrator/input_staging_planner.py's _plan_multi_site_requests) checks
-- destination (ATLAS) → CERES → JUNO itself, so a batch whose JPGs only
-- exist on ATLAS (e.g. already staged from a prior run) must still surface
-- here, the same way v_batches_needing_det_to_world's img_batches CTE below
-- has no site restriction at all.
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
jpg_locations AS (
    SELECT DISTINCT
        batch_id,
        site,
        storage_domain,
        namespace
    FROM globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND file_ext   IN ('jpg', 'jpeg')
      AND parent_dir = 'images'
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
    COALESCE(d.det_count, 0) AS det_count,
    l.site,
    l.storage_domain,
    l.namespace
FROM  jpg_batches   j
JOIN  jpg_locations l  ON j.batch_id = l.batch_id
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
ORDER BY j.batch_date ASC, j.batch_id ASC, l.site ASC, l.storage_domain ASC, l.namespace ASC;


-- -----------------------------------------------------------------------------
-- v_batches_needing_det_to_world
-- Batches with current detection files, current developed-image files, AND
-- current pixel-to-world NPZ grids (anywhere, any site) that have no current
-- georeferenced output files yet.
--
-- Unlike the raw_to_jpg/jpg_to_det views, this does NOT gate on locality —
-- it answers "does this batch's data exist somewhere so det_to_world could
-- run", not "is it staged on the compute cluster". Submission readiness for
-- the actual compute cluster (CERES by default — det_to_world is CPU-only,
-- unlike GPU-bound jpg_to_det on ATLAS) is checked separately by
-- scripts/job/submit.py's filter_det_to_world_staged_ready(), which requires
-- all three pieces — images, detections, AND grids — to show
-- status='completed' in staged_inputs. All three are planned and staged by
-- orchestrator/input_staging_planner.py (images/detections via
-- _plan_multi_site_requests(), grids via _plan_grid_request()); pieces
-- already resident at the destination are recorded as immediately-completed
-- staged_inputs rows too, so readiness never depends on a fresh
-- globus_file_index rescan after a transfer completes. Images are staged as
-- only a small random sample (transfer.routes.det_to_world.image_sample_size)
-- for an optional visualization step, not the whole directory — the stage
-- CLI itself never reads image pixels.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_batches_needing_det_to_world;
CREATE VIEW v_batches_needing_det_to_world AS
WITH
img_batches AS (
    SELECT
        batch_id,
        MIN(batch_date) AS batch_date,
        COUNT(*)        AS img_count
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
grid_batches AS (
    SELECT
        batch_id,
        COUNT(*) AS grid_count
    FROM globus_file_index
    WHERE data_state = 'semifield-asfm'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND file_ext   = 'npz'
      AND parent_dir = 'pixel_world_grids'
    GROUP BY batch_id
),
georef_batches AS (
    SELECT
        batch_id,
        COUNT(*) AS georef_count
    FROM globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id   IS NOT NULL
      AND is_current = 1
      AND parent_dir = 'georeferenced'
    GROUP BY batch_id
),
active_leases AS (
    SELECT batch_id
    FROM   stage_leases
    WHERE  stage      = 'det_to_world'
      AND  expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
)
SELECT
    i.batch_id,
    i.batch_date,
    i.img_count,
    d.det_count,
    g.grid_count,
    COALESCE(w.georef_count, 0) AS georef_count
FROM  img_batches   i
JOIN  det_batches   d  ON i.batch_id = d.batch_id   -- need images and detections
JOIN  grid_batches  g  ON i.batch_id = g.batch_id   -- and pixel-to-world grids
LEFT JOIN georef_batches w  ON i.batch_id = w.batch_id
LEFT JOIN active_leases   al ON i.batch_id = al.batch_id
WHERE COALESCE(w.georef_count, 0) = 0   -- no georeferenced output exists yet
  AND al.batch_id IS NULL               -- no active lease
  AND NOT EXISTS (                      -- never succeeded
      SELECT 1 FROM stage_runs sr
      WHERE sr.batch_id = i.batch_id
        AND sr.stage    = 'det_to_world'
        AND sr.status   = 'success'
  )
ORDER BY i.batch_date ASC, i.batch_id ASC;


-- =============================================================================
-- v8: add v_batches_needing_det_to_world for stage 3 (det_to_world) readiness.
-- v9: add species/species_aliases/species_multi_symbols/cultivars/
--     cultivar_aliases/color_palette reference tables.
-- v10: v_batches_needing_jpg_to_det no longer restricts jpg_batches/
--      jpg_locations to site IN ('JUNO', 'CERES') — jpg_to_det now resolves
--      its images input via the same destination/CERES/JUNO priority chain
--      as det_to_world, so ATLAS-resident batches must surface here too.
PRAGMA user_version = 10;
COMMIT;
