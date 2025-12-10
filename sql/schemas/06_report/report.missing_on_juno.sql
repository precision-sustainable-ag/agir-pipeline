-- Fixed version of report.missing_on_juno
-- The original joined on root_path, which differs between locations
-- This version only joins on batch_id + rel_path + file_name

CREATE SCHEMA IF NOT EXISTS report;

-- Drop existing view
DROP VIEW IF EXISTS report.missing_on_juno;

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
    SELECT DISTINCT
        batch_id,
        data_state,
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
    ON  j.batch_id = e.batch_id      -- Same batch
    AND j.data_state = e.data_state  -- Same data type
    AND j.rel_path = e.rel_path      -- Same relative path
    AND j.file_name = e.file_name    -- Same filename
WHERE j.file_name IS NULL;           -- Not found on JUNO = missing

COMMENT ON VIEW report.missing_on_juno IS 
'Files that exist at source locations (NCSU, CERES) but are missing from JUNO.
Join is on batch_id + data_state + rel_path + file_name (NOT root_path).';