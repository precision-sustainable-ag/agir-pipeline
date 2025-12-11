/* ============================================================
 * TABLE OF CONTENTS
 * ============================================================
 * 1. INVENTORY AND COUNTS
 * 2. EXPLORATION AND UTILITY
 * ============================================================
 */


/* ============================================================
 * 1. INVENTORY AND COUNTS
 *    - Global stats
 *    - Batch-level counts
 *    - Value space / random samples
 * ============================================================
 */

-- name: cnt_total_stats
SELECT
    COUNT(*)               AS total_files,
    SUM(size_bytes)        AS total_bytes,
    MIN(created_at_ts_iso) AS first_indexed,
    MAX(created_at_ts_iso) AS last_indexed
FROM source.globus_file_index;

-- name: cnt_batch_site_state_counts
SELECT
    batch_id,
    site,
    SUM(CASE WHEN data_state = 'upload_raw'    THEN 1 ELSE 0 END) AS n_upload_raw,
    SUM(CASE WHEN data_state = 'developed_jpg' THEN 1 ELSE 0 END) AS n_developed_jpg,
    SUM(CASE WHEN data_state = 'cutouts'       THEN 1 ELSE 0 END) AS n_cutouts
FROM source.globus_file_index
WHERE batch_id IS NOT NULL
GROUP BY batch_id, site
ORDER BY batch_id, site;

/* ============================================================
 * 2. EXPLORATION AND UTILITY
 *    - Random samples
 *    - Unique column values
 * ============================================================
 */

-- name: util_random_samples_10000
SELECT *
FROM source.globus_file_index
ORDER BY random()
LIMIT 10000;

-- name: util_select_unique_column_values
WITH uniq AS (
    SELECT 'endpoint'    AS column_name, endpoint::text    AS value FROM source.globus_file_index
    UNION SELECT 'site',    site::text    FROM source.globus_file_index
    UNION SELECT 'storage_root',    storage_root::text    FROM source.globus_file_index
    UNION SELECT 'parent_dir',  parent_dir::text  FROM source.globus_file_index
    UNION SELECT 'entry_type',  entry_type::text  FROM source.globus_file_index
    UNION SELECT 'file_ext',    file_ext::text    FROM source.globus_file_index
    UNION SELECT 'data_state',  data_state::text  FROM source.globus_file_index
)
SELECT DISTINCT column_name, value
FROM uniq
ORDER BY column_name, value;
