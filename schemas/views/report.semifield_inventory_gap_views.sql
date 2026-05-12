-- Semifield inventory gap/report views
-- Purpose:
--   1) Identify RAW batches/files that still need JPG conversion
--   2) Identify developed-image JPG batches/files that still need detection metadata
--
-- Grain:
--   endpoint, site, storage_domain, namespace, storage_root, batch_id
--
-- Notes:
--   - These views intentionally do NOT collapse across sites/storage locations.
--   - They rely only on source.globus_file_index.
--   - Adjust the RAW extension list and detection metadata filename convention as needed.

BEGIN;

CREATE SCHEMA IF NOT EXISTS report;

-- ============================================================================
-- Optional helper indexes
-- Comment these out if you do not want to manage indexes here.
-- ============================================================================

CREATE INDEX IF NOT EXISTS ix_gfi_gap_report_core
ON source.globus_file_index
(data_state, entry_type, batch_id, file_ext);

-- CREATE INDEX IF NOT EXISTS idx_gfi_state_batch_entry_ext
--     ON source.globus_file_index (data_state, batch_id, entry_type, file_ext);

-- CREATE INDEX IF NOT EXISTS idx_gfi_location_batch
--     ON source.globus_file_index (
--         endpoint, site, storage_domain, namespace, storage_root, batch_id
--     );

-- CREATE INDEX IF NOT EXISTS idx_gfi_rel_path
--     ON source.globus_file_index (rel_path);

-- ============================================================================
-- Drop views so the file is easy to rerun during development
-- ============================================================================

DROP VIEW IF EXISTS report.jpg_files_in_batches_missing_metadata;
DROP VIEW IF EXISTS report.raw_files_missing_jpg;
DROP VIEW IF EXISTS report.jpg_batches_needing_detection_metadata;
DROP VIEW IF EXISTS report.raw_batches_needing_jpg;
DROP VIEW IF EXISTS report.semifield_batch_inventory_status;

-- ============================================================================
-- View: report.raw_batches_needing_jpg
--
-- Meaning:
--   Batch/location has RAW files under semifield-upload, but no JPG files under
--   semifield-developed-images/{batch_id}/images/*.jpg at the same location.
--
-- If you prefer "partial conversion" logic, use the summary view below.
-- ============================================================================

CREATE VIEW report.raw_batches_needing_jpg AS
WITH raw_batches AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        COUNT(*) AS raw_file_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND file_ext IS NOT NULL
      AND lower(file_ext) IN ('raw', 'arw')
    GROUP BY
        endpoint, site, storage_domain, namespace, storage_root, batch_id
),
jpg_batches AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        COUNT(*) AS jpg_file_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/images/[^/]+\.jpg$')
    GROUP BY
        endpoint, site, storage_domain, namespace, storage_root, batch_id
)
SELECT
    r.endpoint,
    r.site,
    r.storage_domain,
    r.namespace,
    r.storage_root,
    r.batch_id,
    r.raw_file_count,
    COALESCE(j.jpg_file_count, 0) AS jpg_file_count
FROM raw_batches r
LEFT JOIN jpg_batches j
    ON  r.endpoint       = j.endpoint
    AND r.site           = j.site
    AND r.storage_domain = j.storage_domain
    AND r.namespace      = j.namespace
    AND r.storage_root   = j.storage_root
    AND r.batch_id       = j.batch_id
WHERE COALESCE(j.jpg_file_count, 0) = 0;

COMMENT ON VIEW report.raw_batches_needing_jpg IS
'Batches/locations where RAW files exist in semifield-upload but no developed JPG files exist in semifield-developed-images/{batch_id}/images.';

-- ============================================================================
-- View: report.jpg_batches_needing_detection_metadata
--
-- Meaning:
--   Batch/location has JPGs under semifield-developed-images/{batch_id}/images,
--   but no JSON metadata files under semifield-developed-images/{batch_id}/metadata.
--
-- IMPORTANT:
--   If metadata/ contains multiple JSON types, tighten the JSON predicate by
--   filename convention (for example file_name LIKE ''%_detections.json'').
-- ============================================================================

CREATE VIEW report.jpg_batches_needing_detection_metadata AS
WITH jpg_batches AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        COUNT(*) AS jpg_file_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/images/[^/]+\.jpg$')
    GROUP BY
        endpoint, site, storage_domain, namespace, storage_root, batch_id
),
metadata_batches AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        COUNT(*) AS metadata_json_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'json'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/metadata/[^/]+\.json$')
    GROUP BY
        endpoint, site, storage_domain, namespace, storage_root, batch_id
)
SELECT
    j.endpoint,
    j.site,
    j.storage_domain,
    j.namespace,
    j.storage_root,
    j.batch_id,
    j.jpg_file_count,
    COALESCE(m.metadata_json_count, 0) AS metadata_json_count
FROM jpg_batches j
LEFT JOIN metadata_batches m
    ON  j.endpoint       = m.endpoint
    AND j.site           = m.site
    AND j.storage_domain = m.storage_domain
    AND j.namespace      = m.namespace
    AND j.storage_root   = m.storage_root
    AND j.batch_id       = m.batch_id
WHERE COALESCE(m.metadata_json_count, 0) = 0;

COMMENT ON VIEW report.jpg_batches_needing_detection_metadata IS
'Batches/locations where developed JPG images exist but no metadata JSON files exist under semifield-developed-images/{batch_id}/metadata.';

-- ============================================================================
-- View: report.semifield_batch_inventory_status
--
-- Meaning:
--   Unified per-batch/location summary with counts and a simple derived status.
--
-- Status values:
--   needs_jpg
--   needs_detection_metadata
--   partial_jpg
--   partial_detection_metadata
--   ok
-- ============================================================================

CREATE OR REPLACE VIEW report.semifield_batch_inventory_status AS
WITH raw_batches AS (
    SELECT
        endpoint, site, storage_domain, namespace, storage_root, batch_id,
        COUNT(*) AS raw_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) IN ('raw', 'arw', 'nef', 'cr2', 'cr3', 'dng', 'rw2')
    GROUP BY endpoint, site, storage_domain, namespace, storage_root, batch_id
),
jpg_batches AS (
    SELECT
        endpoint, site, storage_domain, namespace, storage_root, batch_id,
        COUNT(*) AS jpg_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/images/[^/]+\.jpg$')
    GROUP BY endpoint, site, storage_domain, namespace, storage_root, batch_id
),
metadata_batches AS (
    SELECT
        endpoint, site, storage_domain, namespace, storage_root, batch_id,
        COUNT(*) AS metadata_json_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'json'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/metadata/[^/]+\.json$')
    GROUP BY endpoint, site, storage_domain, namespace, storage_root, batch_id
),
plant_detection_batches AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        COUNT(*) AS plant_detection_csv_count
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'csv'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/plant-detections/[^/]+\.csv$')
    GROUP BY endpoint, site, storage_domain, namespace, storage_root, batch_id
),
all_batches AS (
    SELECT endpoint, site, storage_domain, namespace, storage_root, batch_id FROM raw_batches
    UNION
    SELECT endpoint, site, storage_domain, namespace, storage_root, batch_id FROM jpg_batches
    UNION
    SELECT endpoint, site, storage_domain, namespace, storage_root, batch_id FROM metadata_batches
    UNION
    SELECT endpoint, site, storage_domain, namespace, storage_root, batch_id FROM plant_detection_batches
)
SELECT
    a.endpoint,
    a.site,
    a.storage_domain,
    a.namespace,
    a.storage_root,
    a.batch_id,
    COALESCE(r.raw_count, 0) AS raw_count,
    COALESCE(j.jpg_count, 0) AS jpg_count,
    COALESCE(m.metadata_json_count, 0) AS metadata_json_count,
    COALESCE(p.plant_detection_csv_count, 0) AS plant_detection_csv_count,
    (COALESCE(p.plant_detection_csv_count, 0) > 0) AS has_plant_detections_csv,
    CASE
        WHEN COALESCE(r.raw_count, 0) > 0 AND COALESCE(j.jpg_count, 0) = 0
            THEN 'needs_jpg'
        WHEN COALESCE(j.jpg_count, 0) > 0
             AND COALESCE(m.metadata_json_count, 0) = 0
             AND COALESCE(p.plant_detection_csv_count, 0) > 0
            THEN 'needs_metadata_formatting'
        WHEN COALESCE(j.jpg_count, 0) > 0 AND COALESCE(m.metadata_json_count, 0) = 0
            THEN 'needs_detection_metadata'
        WHEN COALESCE(r.raw_count, 0) > COALESCE(j.jpg_count, 0)
            THEN 'partial_jpg'
        WHEN COALESCE(j.jpg_count, 0) > 0
             AND COALESCE(m.metadata_json_count, 0) < COALESCE(p.plant_detection_csv_count, 0)
             AND COALESCE(p.plant_detection_csv_count, 0) > 0
            THEN 'partial_detection_metadata_formatting'
        WHEN COALESCE(j.jpg_count, 0) > COALESCE(m.metadata_json_count, 0)
            THEN 'partial_detection_metadata'
        ELSE 'ok'
    END AS pipeline_status
FROM all_batches a
LEFT JOIN raw_batches r
    ON  a.endpoint = r.endpoint
    AND a.site = r.site
    AND a.storage_domain = r.storage_domain
    AND a.namespace = r.namespace
    AND a.storage_root = r.storage_root
    AND a.batch_id = r.batch_id
LEFT JOIN jpg_batches j
    ON  a.endpoint = j.endpoint
    AND a.site = j.site
    AND a.storage_domain = j.storage_domain
    AND a.namespace = j.namespace
    AND a.storage_root = j.storage_root
    AND a.batch_id = j.batch_id
LEFT JOIN metadata_batches m
    ON  a.endpoint = m.endpoint
    AND a.site = m.site
    AND a.storage_domain = m.storage_domain
    AND a.namespace = m.namespace
    AND a.storage_root = m.storage_root
    AND a.batch_id = m.batch_id
LEFT JOIN plant_detection_batches p
    ON  a.endpoint = p.endpoint
    AND a.site = p.site
    AND a.storage_domain = p.storage_domain
    AND a.namespace = p.namespace
    AND a.storage_root = p.storage_root
    AND a.batch_id = p.batch_id;

COMMENT ON VIEW report.semifield_batch_inventory_status IS
'Unified per-batch/location inventory summary for semifield-upload and semifield-developed-images, including RAW count, JPG count, metadata JSON count, and a derived pipeline status.';

-- ============================================================================
-- View: report.raw_files_missing_jpg
--
-- Meaning:
--   Specific RAW files whose filename stem does not have a matching JPG stem
--   under semifield-developed-images/{batch_id}/images at the same location.
--
-- Assumption:
--   RAW -> JPG preserves filename stem.
-- ============================================================================

CREATE VIEW report.raw_files_missing_jpg AS
WITH raw_files AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        rel_path AS raw_rel_path,
        file_name AS raw_file_name,
        regexp_replace(file_name, '\.[^.]+$', '') AS stem
    FROM source.globus_file_index
    WHERE data_state = 'semifield-upload'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) IN ('raw', 'arw', 'nef', 'cr2', 'cr3', 'dng', 'rw2')
),
jpg_files AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        rel_path AS jpg_rel_path,
        file_name AS jpg_file_name,
        regexp_replace(file_name, '\.[^.]+$', '') AS stem
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/images/[^/]+\.jpg$')
)
SELECT
    r.endpoint,
    r.site,
    r.storage_domain,
    r.namespace,
    r.storage_root,
    r.batch_id,
    r.raw_rel_path,
    r.raw_file_name
FROM raw_files r
LEFT JOIN jpg_files j
    ON  r.endpoint       = j.endpoint
    AND r.site           = j.site
    AND r.storage_domain = j.storage_domain
    AND r.namespace      = j.namespace
    AND r.storage_root   = j.storage_root
    AND r.batch_id       = j.batch_id
    AND r.stem           = j.stem
WHERE j.jpg_rel_path IS NULL;

COMMENT ON VIEW report.raw_files_missing_jpg IS
'Specific RAW files that do not have a matching JPG filename stem under semifield-developed-images/{batch_id}/images at the same storage location.';

-- ============================================================================
-- View: report.jpg_files_in_batches_missing_metadata
--
-- Meaning:
--   Specific JPG files that belong to batches/locations where no metadata JSON
--   files exist under semifield-developed-images/{batch_id}/metadata.
-- ============================================================================

CREATE VIEW report.jpg_files_in_batches_missing_metadata AS
WITH jpg_files AS (
    SELECT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id,
        rel_path,
        file_name
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'jpg'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/images/[^/]+\.jpg$')
),
metadata_batches AS (
    SELECT DISTINCT
        endpoint,
        site,
        storage_domain,
        namespace,
        storage_root,
        batch_id
    FROM source.globus_file_index
    WHERE data_state = 'semifield-developed-images'
      AND entry_type = 'file'
      AND batch_id IS NOT NULL
      AND lower(file_ext) = 'json'
      AND rel_path ~ ('^semifield-developed-images/' || batch_id || '/metadata/[^/]+\.json$')
)
SELECT
    j.endpoint,
    j.site,
    j.storage_domain,
    j.namespace,
    j.storage_root,
    j.batch_id,
    j.rel_path AS jpg_rel_path,
    j.file_name AS jpg_file_name
FROM jpg_files j
LEFT JOIN metadata_batches m
    ON  j.endpoint       = m.endpoint
    AND j.site           = m.site
    AND j.storage_domain = m.storage_domain
    AND j.namespace      = m.namespace
    AND j.storage_root   = m.storage_root
    AND j.batch_id       = m.batch_id
WHERE m.batch_id IS NULL;

COMMENT ON VIEW report.jpg_files_in_batches_missing_metadata IS
'Specific JPG files in developed-image batches that have no metadata JSON files under semifield-developed-images/{batch_id}/metadata at the same storage location.';

COMMIT;

-- ============================================================================
-- Example queries
-- ============================================================================
--
-- SELECT *
-- FROM report.semifield_batch_inventory_status
-- WHERE pipeline_status = 'needs_jpg'
-- ORDER BY site, storage_domain, namespace, storage_root, batch_id;
--
-- SELECT *
-- FROM report.semifield_batch_inventory_status
-- WHERE pipeline_status = 'needs_detection_metadata'
-- ORDER BY site, storage_domain, namespace, storage_root, batch_id;
--
-- SELECT *
-- FROM report.raw_batches_needing_jpg
-- ORDER BY site, storage_domain, namespace, storage_root, batch_id;
--
-- SELECT *
-- FROM report.jpg_batches_needing_detection_metadata
-- ORDER BY site, storage_domain, namespace, storage_root, batch_id;

-- SELECT *
-- FROM report.semifield_batch_inventory_status
-- WHERE jpg_count > 0
--   AND metadata_json_count = 0
--   AND has_plant_detections_csv = true
-- ORDER BY site, storage_domain, namespace, storage_root, batch_id;

-- SELECT *
-- FROM report.semifield_batch_inventory_status
-- WHERE pipeline_status = 'needs_metadata_formatting'
-- ORDER BY site, storage_domain, namespace, storage_root, batch_id;