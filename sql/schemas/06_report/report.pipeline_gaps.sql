/* ============================================================
 * report.pipeline_gaps.sql
 *
 * Views to identify FILE-LEVEL and BATCH-LEVEL gaps in the pipeline.
 *
 * Gap detection uses "missing outputs" as source of truth:
 *  - RAW files that are missing a developed JPG
 *  - JPG files that are missing a metadata JSON
 *  - Metadata files with no cutouts yet
 *
 * This approach is self-correcting and handles edge cases gracefully.
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS report;

-- Drop existing views first (in reverse dependency order)
-- This allows the script to be run multiple times
DROP VIEW IF EXISTS report.pipeline_gap_summary CASCADE;
DROP VIEW IF EXISTS report.batch_pipeline_status CASCADE;
DROP VIEW IF EXISTS report.batches_needing_metadata_to_cutouts CASCADE;
DROP VIEW IF EXISTS report.batches_needing_jpg_to_metadata CASCADE;
DROP VIEW IF EXISTS report.batches_needing_raw_to_jpg CASCADE;
DROP VIEW IF EXISTS report.files_needing_metadata_to_cutouts CASCADE;
DROP VIEW IF EXISTS report.files_needing_jpg_to_metadata CASCADE;
DROP VIEW IF EXISTS report.files_needing_raw_to_jpg CASCADE;

-- ------------------------------------------------------------
-- Helper note:
-- regexp_replace(file_name, '\.[^.]+$', '') strips the final
-- extension, e.g. 'MD_1234.raw' -> 'MD_1234'
-- ------------------------------------------------------------


-- ============================================================
-- FILE-LEVEL GAP VIEWS
-- ============================================================

-- ------------------------------------------------------------
-- 1) RAW files that are missing a developed JPG image
--    (upload_raw → developed_jpg/images)
-- ------------------------------------------------------------
CREATE VIEW report.files_needing_raw_to_jpg AS
WITH raw_files AS (
    SELECT
        f.*,
        regexp_replace(file_name, '\.[^.]+$', '') AS base_name
    FROM source.globus_file_index f
    WHERE
        batch_id IS NOT NULL
        AND data_state = 'upload_raw'
        AND entry_type = 'file'
        AND LOWER(file_ext) IN ('raw', 'arw')
),

jpg_files AS (
    SELECT
        batch_id,
        regexp_replace(file_name, '\.[^.]+$', '') AS base_name
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) IN ('jpg','jpeg')
        AND (
            parent_dir = 'images'
            OR parent_dir LIKE '%/images'
        )
)

SELECT
    r.file_id,
    r.batch_id,
    r.endpoint,
    r.site,
    r.storage_root,
    r.rel_path,
    r.file_name,
    r.file_ext,
    r.size_bytes,
    r.base_name,
    r.batch_state,
    r.batch_date
FROM raw_files r
LEFT JOIN jpg_files j
    ON  j.batch_id  = r.batch_id
    AND j.base_name = r.base_name
WHERE j.batch_id IS NULL;   -- no matching JPG found


-- ------------------------------------------------------------
-- 2) JPG files that are missing a metadata JSON
--    (developed_jpg/images → developed_jpg/metadata)
-- ------------------------------------------------------------
CREATE VIEW report.files_needing_jpg_to_metadata AS
WITH jpg_files AS (
    SELECT
        f.*,
        regexp_replace(file_name, '\.[^.]+$', '') AS base_name
    FROM source.globus_file_index f
    WHERE
        batch_id IS NOT NULL
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) IN ('jpg','jpeg')
        AND (
            parent_dir = 'images'
            OR parent_dir LIKE '%/images'
        )
),

metadata_files AS (
    SELECT
        batch_id,
        regexp_replace(file_name, '\.[^.]+$', '') AS base_name
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) = 'json'
        AND (
            parent_dir = 'metadata'
            OR parent_dir LIKE '%/metadata'
        )
)

SELECT
    j.file_id,
    j.batch_id,
    j.endpoint,
    j.site,
    j.storage_root,
    j.rel_path,
    j.file_name,
    j.file_ext,
    j.size_bytes,
    j.base_name,
    j.batch_state,
    j.batch_date
FROM jpg_files j
LEFT JOIN metadata_files m
    ON  m.batch_id  = j.batch_id
    AND m.base_name = j.base_name
WHERE m.batch_id IS NULL;   -- no matching JSON metadata found


-- ------------------------------------------------------------
-- 3) METADATA JSON files that are missing ANY CUTOUT files
--    (developed_jpg/metadata → cutouts)
-- ------------------------------------------------------------
CREATE VIEW report.files_needing_metadata_to_cutouts AS
WITH batches_with_cutouts AS (
    SELECT DISTINCT batch_id
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND data_state = 'cutouts'
        AND entry_type = 'file'
)

SELECT
    m.file_id,
    m.batch_id,
    m.endpoint,
    m.site,
    m.storage_root,
    m.rel_path,
    m.file_name,
    m.file_ext,
    m.size_bytes,
    m.batch_state,
    m.batch_date
FROM source.globus_file_index m
WHERE
    -- 1) metadata JSON files
    m.batch_id IS NOT NULL
    AND m.data_state = 'developed_jpg'
    AND m.entry_type = 'file'
    AND LOWER(m.file_ext) = 'json'
    AND (
        m.parent_dir = 'metadata'
        OR m.parent_dir LIKE '%/metadata'
    )
    -- 2) whose batch has NO cutouts at all
    AND NOT EXISTS (
        SELECT 1
        FROM batches_with_cutouts b
        WHERE b.batch_id = m.batch_id
    );


-- ============================================================
-- BATCH-LEVEL GAP VIEWS
-- ============================================================

-- ------------------------------------------------------------
-- 4) Batches needing RAW → JPG processing
--    Summary of gap at batch level
-- ------------------------------------------------------------
CREATE VIEW report.batches_needing_raw_to_jpg AS
SELECT
    batch_id,
    batch_state,
    batch_date,
    COUNT(*) AS files_needing_processing,
    MIN(site) AS primary_site,
    MIN(storage_root) AS primary_storage_root,
    SUM(size_bytes) AS total_bytes
FROM report.files_needing_raw_to_jpg
GROUP BY batch_id, batch_state, batch_date
ORDER BY batch_date DESC, batch_id;


-- ------------------------------------------------------------
-- 5) Batches needing JPG → Metadata processing
-- ------------------------------------------------------------
CREATE VIEW report.batches_needing_jpg_to_metadata AS
SELECT
    batch_id,
    batch_state,
    batch_date,
    COUNT(*) AS files_needing_processing,
    MIN(site) AS primary_site,
    MIN(storage_root) AS primary_storage_root,
    SUM(size_bytes) AS total_bytes
FROM report.files_needing_jpg_to_metadata
GROUP BY batch_id, batch_state, batch_date
ORDER BY batch_date DESC, batch_id;


-- ------------------------------------------------------------
-- 6) Batches needing Metadata → Cutouts processing
-- ------------------------------------------------------------
CREATE VIEW report.batches_needing_metadata_to_cutouts AS
SELECT
    batch_id,
    batch_state,
    batch_date,
    COUNT(*) AS files_needing_processing,
    MIN(site) AS primary_site,
    MIN(storage_root) AS primary_storage_root,
    SUM(size_bytes) AS total_bytes
FROM report.files_needing_metadata_to_cutouts
GROUP BY batch_id, batch_state, batch_date
ORDER BY batch_date DESC, batch_id;


-- ============================================================
-- MASTER PIPELINE STATUS VIEW
-- ============================================================

-- ------------------------------------------------------------
-- 7) Batch pipeline status - master view showing all stages
-- ------------------------------------------------------------
CREATE VIEW report.batch_pipeline_status AS
WITH batch_files AS (
    SELECT
        batch_id,
        batch_state,
        batch_date,
        COUNT(*) FILTER (WHERE data_state = 'upload_raw' AND LOWER(file_ext) IN ('raw','arw')) AS raw_count,
        COUNT(*) FILTER (WHERE data_state = 'developed_jpg' AND LOWER(file_ext) IN ('jpg','jpeg') AND parent_dir = 'images') AS jpg_count,
        COUNT(*) FILTER (WHERE data_state = 'developed_jpg' AND LOWER(file_ext) = 'json' AND parent_dir = 'metadata') AS metadata_count,
        COUNT(*) FILTER (WHERE data_state = 'cutouts') AS cutout_count,
        MIN(site) AS primary_site,
        MIN(storage_root) AS primary_storage_root
    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL AND entry_type = 'file'
    GROUP BY batch_id, batch_state, batch_date
),

gaps_raw_to_jpg AS (
    SELECT batch_id, files_needing_processing AS gap_count
    FROM report.batches_needing_raw_to_jpg
),

gaps_jpg_to_metadata AS (
    SELECT batch_id, files_needing_processing AS gap_count
    FROM report.batches_needing_jpg_to_metadata
),

gaps_metadata_to_cutouts AS (
    SELECT batch_id, files_needing_processing AS gap_count
    FROM report.batches_needing_metadata_to_cutouts
)

SELECT
    b.batch_id,
    b.batch_state,
    b.batch_date,
    b.raw_count,
    b.jpg_count,
    b.metadata_count,
    b.cutout_count,
    COALESCE(g1.gap_count, 0) AS raw_to_jpg_gap,
    COALESCE(g2.gap_count, 0) AS jpg_to_metadata_gap,
    COALESCE(g3.gap_count, 0) AS metadata_to_cutouts_gap,
    -- Pipeline completion flags
    (b.jpg_count = b.raw_count AND b.raw_count > 0) AS raw_to_jpg_complete,
    (b.metadata_count = b.jpg_count AND b.jpg_count > 0) AS jpg_to_metadata_complete,
    (b.cutout_count > 0) AS has_cutouts,
    b.primary_site,
    b.primary_storage_root
FROM batch_files b
LEFT JOIN gaps_raw_to_jpg g1 ON b.batch_id = g1.batch_id
LEFT JOIN gaps_jpg_to_metadata g2 ON b.batch_id = g2.batch_id
LEFT JOIN gaps_metadata_to_cutouts g3 ON b.batch_id = g3.batch_id
ORDER BY b.batch_date DESC, b.batch_id;


-- ============================================================
-- SUMMARY STATISTICS VIEW
-- ============================================================

-- ------------------------------------------------------------
-- 8) Overall gap summary across all stages
-- ------------------------------------------------------------
CREATE VIEW report.pipeline_gap_summary AS
SELECT
    'raw_to_jpg' AS stage,
    COUNT(DISTINCT batch_id) AS batches_with_gaps,
    COUNT(*) AS total_files_with_gaps,
    SUM(size_bytes) AS total_bytes
FROM report.files_needing_raw_to_jpg

UNION ALL

SELECT
    'jpg_to_metadata' AS stage,
    COUNT(DISTINCT batch_id) AS batches_with_gaps,
    COUNT(*) AS total_files_with_gaps,
    SUM(size_bytes) AS total_bytes
FROM report.files_needing_jpg_to_metadata

UNION ALL

SELECT
    'metadata_to_cutouts' AS stage,
    COUNT(DISTINCT batch_id) AS batches_with_gaps,
    COUNT(*) AS total_files_with_gaps,
    SUM(size_bytes) AS total_bytes
FROM report.files_needing_metadata_to_cutouts;