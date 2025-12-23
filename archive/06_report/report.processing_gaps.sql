
CREATE SCHEMA IF NOT EXISTS report;

DROP VIEW IF EXISTS report.raw_files_needing_jpg;

CREATE VIEW report.raw_files_needing_jpg AS
WITH raw AS (
    SELECT
        s.*,
        -- strip last extension: foo.bar.RAW -> foo.bar
        regexp_replace(s.file_name, '\.[^.]+$', '') AS file_stem
    FROM source.globus_file_index s
    WHERE
        s.entry_type = 'file'
        AND s.batch_id IS NOT NULL
        AND s.data_state = 'semifield-upload'
        AND s.site <> 'JUNO' OR s.site = 'JUNO'  -- keep RAW from anywhere
),
jpg AS (
    SELECT DISTINCT
        batch_id,
        regexp_replace(file_name, '\.[^.]+$', '') AS file_stem
    FROM source.globus_file_index
    WHERE
        entry_type = 'file'
        AND batch_id IS NOT NULL
        AND data_state = 'semifield-developed-images'
        AND LOWER(file_ext) IN ('jpg', 'jpeg')
        AND (
            parent_dir = 'images'
            OR parent_dir LIKE '%/images'
        )
)
SELECT
    r.*
FROM raw r
LEFT JOIN jpg j
  ON j.batch_id = r.batch_id
 AND j.file_stem = r.file_stem
WHERE j.file_stem IS NULL;

DROP VIEW IF EXISTS report.batches_needing_jpg;

CREATE VIEW report.batches_needing_jpg AS
SELECT
    batch_id,
    COUNT(*)        AS n_raw_missing_jpg,
    SUM(size_bytes) AS raw_bytes_missing_jpg,
    jsonb_agg(DISTINCT site ORDER BY site) AS raw_present_on_sites
FROM report.raw_files_needing_jpg
GROUP BY batch_id;

-- Helps scan RAW side quickly
CREATE INDEX IF NOT EXISTS ix_gfi_raw_batch_name
ON source.globus_file_index (batch_id, file_name)
WHERE entry_type='file' AND batch_id IS NOT NULL AND data_state='semifield-upload';

-- Helps scan developed JPG side quickly
CREATE INDEX IF NOT EXISTS ix_gfi_devjpg_batch_name
ON source.globus_file_index (batch_id, file_name)
WHERE entry_type='file'
  AND batch_id IS NOT NULL
  AND data_state='semifield-developed-images'
  AND LOWER(file_ext) IN ('jpg','jpeg');
