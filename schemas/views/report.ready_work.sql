CREATE SCHEMA IF NOT EXISTS report;
CREATE SCHEMA IF NOT EXISTS source;
CREATE SCHEMA IF NOT EXISTS logs;

CREATE OR REPLACE VIEW report.ready_work AS
WITH raw_inputs AS (
    SELECT
        g.batch_id,
        MIN(g.batch_date) AS batch_date,
        MIN(g.storage_root || '/' || g.rel_path) AS staging_input_ref
    FROM source.globus_file_index g
    WHERE g.entry_type = 'file'
      AND g.batch_id IS NOT NULL
      AND g.namespace = '90daydata'
      AND g.data_state = 'semifield-upload'
    GROUP BY g.batch_id
),
latest_runs AS (
    SELECT DISTINCT ON (r.batch_id, r.stage)
        r.batch_id,
        r.stage,
        r.status,
        r.ended_at
    FROM logs.stage_runs r
    WHERE r.stage = 'raw_to_jpg'
    ORDER BY r.batch_id, r.stage, r.ended_at DESC
),
active_leases AS (
    SELECT l.batch_id, l.stage
    FROM logs.stage_leases l
    WHERE l.stage = 'raw_to_jpg'
      AND l.state = 'active'
      AND l.expires_at > now()
)
SELECT
    i.batch_id,
    'raw_to_jpg'::TEXT AS stage,
    i.staging_input_ref,
    100::INTEGER AS priority,
    'raw_to_jpg.default'::TEXT AS resource_profile_id,
    'raw_to_jpg.default'::TEXT AS config_id
FROM raw_inputs i
LEFT JOIN latest_runs lr
    ON lr.batch_id = i.batch_id
   AND lr.stage = 'raw_to_jpg'
LEFT JOIN active_leases al
    ON al.batch_id = i.batch_id
   AND al.stage = 'raw_to_jpg'
WHERE COALESCE(lr.status, 'not_run') <> 'success'
  AND al.batch_id IS NULL
ORDER BY i.batch_date ASC, i.batch_id ASC;
