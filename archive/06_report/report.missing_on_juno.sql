CREATE SCHEMA IF NOT EXISTS report;

DROP VIEW IF EXISTS report.files_to_copy_upload_raw_to_juno CASCADE;

CREATE VIEW report.files_to_copy_upload_raw_to_juno AS
SELECT
    s.*,
    'upload_raw'::text AS copy_domain
FROM source.globus_file_index s
LEFT JOIN source.globus_file_index j
  ON j.batch_id   = s.batch_id
 AND j.data_state = s.data_state
 AND j.file_name  = s.file_name
 AND j.site       = 'JUNO'
 AND j.entry_type = 'file'
WHERE
    s.entry_type = 'file'
    AND s.batch_id IS NOT NULL
    AND s.site <> 'JUNO'
    AND s.data_state = 'semifield-upload'
    AND LOWER(s.file_ext) IN ('raw','arw')
    AND j.file_id IS NULL;

DROP VIEW IF EXISTS report.files_to_copy_dev_images_to_juno;

CREATE VIEW report.files_to_copy_dev_images_to_juno AS
SELECT
    s.*,
    'developed_images_jpg'::text AS copy_domain
FROM source.globus_file_index s
LEFT JOIN source.globus_file_index j
  ON j.batch_id   = s.batch_id
 AND j.data_state = s.data_state
 AND j.file_name  = s.file_name
 AND j.site       = 'JUNO'
 AND j.entry_type = 'file'
WHERE
    s.entry_type = 'file'
    AND s.batch_id IS NOT NULL
    AND s.site <> 'JUNO'
    AND s.data_state = 'semifield-developed-images'
    AND LOWER(s.file_ext) IN ('jpg','jpeg')
    AND (
        s.parent_dir = 'images'
        OR s.parent_dir LIKE '%/images'
    )
    AND j.file_id IS NULL;

DROP VIEW IF EXISTS report.files_to_copy_dev_metadata_to_juno;

CREATE VIEW report.files_to_copy_dev_metadata_to_juno AS
SELECT
    s.*,
    'developed_metadata_json'::text AS copy_domain
FROM source.globus_file_index s
LEFT JOIN source.globus_file_index j
  ON j.batch_id   = s.batch_id
 AND j.data_state = s.data_state
 AND j.file_name  = s.file_name
 AND j.site       = 'JUNO'
 AND j.entry_type = 'file'
WHERE
    s.entry_type = 'file'
    AND s.batch_id IS NOT NULL
    AND s.site <> 'JUNO'
    AND s.data_state = 'semifield-developed-images'
    AND LOWER(s.file_ext) = 'json'
    AND (
        s.parent_dir = 'metadata'
        OR s.parent_dir LIKE '%/metadata'
    )
    AND j.file_id IS NULL;

DROP VIEW IF EXISTS report.files_to_copy_cutouts_to_juno;

CREATE VIEW report.files_to_copy_cutouts_to_juno AS
SELECT
    s.*,
    'cutouts'::text AS copy_domain
FROM source.globus_file_index s
LEFT JOIN source.globus_file_index j
  ON j.batch_id   = s.batch_id
 AND j.data_state = s.data_state
 AND j.file_name  = s.file_name
 AND j.site       = 'JUNO'
 AND j.entry_type = 'file'
WHERE
    s.entry_type = 'file'
    AND s.batch_id IS NOT NULL
    AND s.site <> 'JUNO'
    AND s.data_state = 'semifield-cutouts'
    AND (
        LOWER(s.file_ext) IN ('png','jpg','json')
        -- masks are included via png, but this makes intent explicit:
        OR s.file_name LIKE '%_mask.png'
    )
    AND j.file_id IS NULL;

DROP VIEW IF EXISTS report.files_to_copy_to_juno;

CREATE VIEW report.files_to_copy_to_juno AS
SELECT * FROM report.files_to_copy_upload_raw_to_juno
UNION ALL
SELECT * FROM report.files_to_copy_dev_images_to_juno
UNION ALL
SELECT * FROM report.files_to_copy_dev_metadata_to_juno
UNION ALL
SELECT * FROM report.files_to_copy_cutouts_to_juno;

DROP VIEW IF EXISTS report.batches_to_copy_to_juno;

CREATE VIEW report.batches_to_copy_to_juno AS
SELECT
    copy_domain,
    data_state,
    batch_id,

    site,
    endpoint,
    storage_root,

    COUNT(*)        AS n_files_missing_on_juno,
    SUM(size_bytes) AS n_bytes_missing_on_juno
FROM report.files_to_copy_to_juno
GROUP BY
    copy_domain,
    data_state,
    batch_id,
    site,
    endpoint,
    storage_root;


-- For the JUNO match (batch_id, data_state, file_name) fast lookup
CREATE INDEX IF NOT EXISTS ix_gfi_juno_match
ON source.globus_file_index (batch_id, data_state, file_name)
WHERE site = 'JUNO' AND entry_type = 'file' AND batch_id IS NOT NULL;

-- For scanning candidate files elsewhere
CREATE INDEX IF NOT EXISTS ix_gfi_nonjuno_candidates
ON source.globus_file_index (data_state, batch_id, site, file_name)
WHERE site <> 'JUNO' AND entry_type = 'file' AND batch_id IS NOT NULL;

-- Helps the developed views that filter by parent_dir + ext
CREATE INDEX IF NOT EXISTS ix_gfi_dev_parent_ext
ON source.globus_file_index (data_state, parent_dir, file_ext)
WHERE entry_type = 'file' AND batch_id IS NOT NULL;