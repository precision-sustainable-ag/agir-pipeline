CREATE SCHEMA IF NOT EXISTS report;
CREATE SCHEMA IF NOT EXISTS source;
CREATE SCHEMA IF NOT EXISTS logs;

CREATE OR REPLACE VIEW report.batches_needing_input_staging AS
WITH lts_inputs AS (
    SELECT
        g.batch_id,
        MIN(g.batch_date) AS batch_date,
        MIN(regexp_replace(g.full_path, '/[^/]+$', '')) AS src_lts_ref,
        COUNT(*)::BIGINT AS lts_file_count
    FROM source.globus_file_index g
    WHERE g.entry_type = 'file'
      AND g.batch_id IS NOT NULL
      AND g.namespace = 'LTS'
      AND g.data_state = 'semifield-upload'
      AND g.full_path LIKE '/%'
    GROUP BY g.batch_id
),
staged_inputs AS (
    SELECT
        g.batch_id,
        COUNT(*)::BIGINT AS staged_file_count
    FROM source.globus_file_index g
    WHERE g.entry_type = 'file'
      AND g.batch_id IS NOT NULL
      AND g.namespace = '90daydata'
      AND g.data_state = 'semifield-upload'
    GROUP BY g.batch_id
),
active_input_transfers AS (
    SELECT DISTINCT t.batch_id, t.stage, t.dst_staging_ref
    FROM logs.transfer_runs t
    WHERE t.direction = 'input_stage'
      AND t.status IN ('requested', 'active')
)
SELECT
    l.batch_id,
    'raw_to_jpg'::TEXT AS stage,
    'raw_to_jpg.default'::TEXT AS transfer_profile_id,
    l.src_lts_ref,
    REPLACE(l.src_lts_ref, '/LTS/', '/90daydata/') AS dst_staging_ref,
    100::INTEGER AS priority,
    l.batch_date
FROM lts_inputs l
LEFT JOIN staged_inputs s
  ON s.batch_id = l.batch_id
LEFT JOIN active_input_transfers a
  ON a.batch_id = l.batch_id
 AND a.stage = 'raw_to_jpg'
 AND a.dst_staging_ref = REPLACE(l.src_lts_ref, '/LTS/', '/90daydata/')
WHERE (s.batch_id IS NULL OR s.staged_file_count < l.lts_file_count)
  AND a.batch_id IS NULL;
