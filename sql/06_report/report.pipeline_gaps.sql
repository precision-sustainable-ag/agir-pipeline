/* ============================================================
 * report.pipeline_file_gaps.sql
 *
 * Views to identify FILE-LEVEL gaps in the pipeline:
 *  - RAW files that are missing a developed JPG (images)
 *  - JPG files that are missing a metadata JSON
 *
 * Matching is done by:
 *  - same batch_id
 *  - same base filename (file_name without extension)
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS report;

-- ------------------------------------------------------------
-- Helper note:
-- regexp_replace(file_name, '\.[^.]+$', '') strips the final
-- extension, e.g. 'MD_1234.raw' -> 'MD_1234'
-- ------------------------------------------------------------


-- ------------------------------------------------------------
-- 1) RAW files that are missing a developed JPG image
--    (upload_raw → developed_jpg/images)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW report.files_needing_raw_to_jpg AS
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
    r.*
FROM raw_files r
LEFT JOIN jpg_files j
    ON  j.batch_id  = r.batch_id
    AND j.base_name = r.base_name
WHERE j.batch_id IS NULL;   -- no matching JPG found



-- ------------------------------------------------------------
-- 2) JPG files that are missing a metadata JSON
--    (developed_jpg/images → developed_jpg/metadata)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW report.files_needing_jpg_to_metadata AS
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
    j.*
FROM jpg_files j
LEFT JOIN metadata_files m
    ON  m.batch_id  = j.batch_id
    AND m.base_name = j.base_name
WHERE m.batch_id IS NULL;   -- no matching JSON metadata found

/* ============================================================
 * 3) METADATA JSON files that are missing ANY CUTOUT files
 *    (developed_jpg/metadata → cutouts)
 * ============================================================
 */

CREATE OR REPLACE VIEW report.files_needing_metadata_to_cutouts AS
WITH batches_with_cutouts AS (
    SELECT DISTINCT batch_id
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND data_state = 'cutouts'
        AND entry_type = 'file'
)
SELECT
    m.*
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
