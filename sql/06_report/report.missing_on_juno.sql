CREATE SCHEMA IF NOT EXISTS report;

CREATE VIEW report.missing_on_juno AS
WITH elsewhere AS (
    SELECT
        file_id,
        endpoint,
        location,
        lts_root,
        root_path,
        rel_path,
        parent_dir,
        file_name,
        entry_type,
        file_ext,
        size_bytes,
        checksum,
        batch_id,
        batch_state,
        batch_date,
        data_state
    FROM source.globus_file_index
    WHERE
        location <> 'JUNO'
        AND entry_type = 'file'
        AND batch_id IS NOT NULL
),
juno AS (
    SELECT
        data_state,
        root_path,
        rel_path,
        file_name
    FROM source.globus_file_index
    WHERE
        location = 'JUNO'
        AND entry_type = 'file'
        AND batch_id IS NOT NULL
)
SELECT
    e.*
FROM elsewhere e
LEFT JOIN juno j
    ON  j.data_state = e.data_state
    AND j.root_path  = e.root_path
    AND j.rel_path   = e.rel_path
    AND j.file_name  = e.file_name
WHERE j.file_name IS NULL;
