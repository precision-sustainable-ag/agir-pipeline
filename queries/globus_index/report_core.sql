/* ============================================================
 * TABLE OF CONTENTS
 * ============================================================
 * 1. INVENTORY AND COUNTS
 * 2. JUNO SYNC AND REPLICATION
 * 3. MISSING OR INCOMPLETE DATA BY BATCH
 * 4. EXPLORATION AND UTILITY
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

-- name: cnt_batch_location_state_counts
SELECT
    batch_id,
    location,
    SUM(CASE WHEN data_state = 'upload_raw'    THEN 1 ELSE 0 END) AS n_upload_raw,
    SUM(CASE WHEN data_state = 'developed_jpg' THEN 1 ELSE 0 END) AS n_developed_jpg,
    SUM(CASE WHEN data_state = 'cutouts'       THEN 1 ELSE 0 END) AS n_cutouts
FROM source.globus_file_index
WHERE batch_id IS NOT NULL
GROUP BY batch_id, location
ORDER BY batch_id, location;

-- name: cnt_batches_raws_count_metadata_count
SELECT
    batch_id,
    SUM(
        CASE
            WHEN data_state = 'upload_raw'
             AND LOWER(file_ext) IN ('raw','arw')
             AND entry_type = 'file'
            THEN 1 ELSE 0
        END
    ) AS n_raw,
    SUM(
        CASE
            WHEN data_state = 'developed_jpg'
             AND LOWER(file_ext) = 'json'
             AND entry_type = 'file'
             AND (
                     parent_dir = 'metadata'
                     OR parent_dir LIKE '%/metadata'
                 )
            THEN 1 ELSE 0
        END
    ) AS n_json
FROM source.globus_file_index
WHERE batch_id IS NOT NULL
GROUP BY batch_id
ORDER BY batch_id;

/* ============================================================
 * 2. JUNO SYNC AND REPLICATION
 *    - Files missing images on JUNO
 *    - Files missing metadata on JUNO
 *    - Batches needing images and metadata copied to JUNO
 *    - Batches needing images only copied to JUNO
 *    - Batches needing metadata only copied to JUNO
 * ============================================================
 */

-- name: sync_files_missing_images_on_juno
WITH images_elsewhere AS (
    SELECT
        batch_id,
        root_path,
        rel_path,
        file_name
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND location <> 'JUNO'
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) IN ('jpg','jpeg')
        AND (
            parent_dir = 'images'
            OR parent_dir LIKE '%/images'
        )
),
images_juno AS (
    SELECT
        batch_id,
        root_path,
        rel_path,
        file_name
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND location = 'JUNO'
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) IN ('jpg','jpeg')
        AND (
            parent_dir = 'images'
            OR parent_dir LIKE '%/images'
        )
)
SELECT e.*
FROM images_elsewhere e
LEFT JOIN images_juno j
  ON  e.batch_id  = j.batch_id
  AND e.root_path = j.root_path
  AND e.rel_path  = j.rel_path
  AND e.file_name = j.file_name
WHERE j.batch_id IS NULL
ORDER BY e.batch_id, e.rel_path, e.file_name;

-- name: sync_files_missing_metadata_on_juno
WITH meta_elsewhere AS (
    SELECT
        batch_id,
        root_path,
        rel_path,
        file_name
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND location <> 'JUNO'
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) = 'json'
        AND (
            parent_dir = 'metadata'
            OR parent_dir LIKE '%/metadata'
        )
),
meta_juno AS (
    SELECT
        batch_id,
        root_path,
        rel_path,
        file_name
    FROM source.globus_file_index
    WHERE
        batch_id IS NOT NULL
        AND location = 'JUNO'
        AND data_state = 'developed_jpg'
        AND entry_type = 'file'
        AND LOWER(file_ext) = 'json'
        AND (
            parent_dir = 'metadata'
            OR parent_dir LIKE '%/metadata'
        )
)
SELECT e.*
FROM meta_elsewhere e
LEFT JOIN meta_juno j
  ON  e.batch_id  = j.batch_id
  AND e.root_path = j.root_path
  AND e.rel_path  = j.rel_path
  AND e.file_name = j.file_name
WHERE j.batch_id IS NULL
ORDER BY e.batch_id, e.rel_path, e.file_name;

-- name: sync_batches_needing_juno_copy_images_and_metadata
WITH per_loc AS (
    SELECT
        batch_id,
        location,

        -- RAWs in upload_raw
        SUM(
            CASE
                WHEN data_state = 'upload_raw'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('raw','arw')
                THEN 1 ELSE 0
            END
        ) AS n_upload_raw,

        -- Developed JPGs in images folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('jpg','jpeg')
                 AND (
                     parent_dir = 'images'
                     OR parent_dir LIKE '%/images'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_dev_images_jpg,

        -- Metadata JSONs in metadata folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) = 'json'
                 AND (
                     parent_dir = 'metadata'
                     OR parent_dir LIKE '%/metadata'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_dev_metadata_json

    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL
    GROUP BY batch_id, location
),
global AS (
    -- For each batch, do we have each piece *anywhere*?
    SELECT
        batch_id,
        SUM(n_upload_raw)          > 0 AS has_upload_raw_any,
        SUM(n_dev_images_jpg)      > 0 AS has_dev_images_jpg_any,
        SUM(n_dev_metadata_json)   > 0 AS has_dev_metadata_json_any
    FROM per_loc
    GROUP BY batch_id
),
juno AS (
    -- Counts specifically on JUNO
    SELECT
        batch_id,
        n_upload_raw        AS n_upload_raw_juno,
        n_dev_images_jpg    AS n_dev_images_jpg_juno,
        n_dev_metadata_json AS n_dev_metadata_json_juno
    FROM per_loc
    WHERE location = 'JUNO'   -- adjust if your JUNO label is different
)
SELECT
    g.batch_id,
    g.has_upload_raw_any,
    g.has_dev_images_jpg_any,
    g.has_dev_metadata_json_any,
    COALESCE(j.n_upload_raw_juno,        0) AS n_upload_raw_juno,
    COALESCE(j.n_dev_images_jpg_juno,    0) AS n_dev_images_jpg_juno,
    COALESCE(j.n_dev_metadata_json_juno, 0) AS n_dev_metadata_json_juno
FROM global g
LEFT JOIN juno j USING (batch_id)
WHERE
    -- Only consider batches that are "complete" somewhere
    g.has_upload_raw_any
    AND g.has_dev_images_jpg_any
    AND g.has_dev_metadata_json_any
    AND (
        -- And that are incomplete on JUNO
        j.batch_id IS NULL
        OR j.n_upload_raw_juno        = 0
        OR j.n_dev_images_jpg_juno    = 0
        OR j.n_dev_metadata_json_juno = 0
    )
ORDER BY g.batch_id;


-- name: sync_batches_needing_juno_images_only
WITH per_loc AS (
    SELECT
        batch_id,
        location,

        -- RAWs in upload_raw
        SUM(
            CASE
                WHEN data_state = 'upload_raw'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('raw','arw')
                THEN 1 ELSE 0
            END
        ) AS n_upload_raw,

        -- Developed JPGs in images folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('jpg','jpeg')
                 AND (
                     parent_dir = 'images'
                     OR parent_dir LIKE '%/images'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_dev_images_jpg,

        -- Metadata JSONs in metadata folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) = 'json'
                 AND (
                     parent_dir = 'metadata'
                     OR parent_dir LIKE '%/metadata'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_dev_metadata_json
    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL
    GROUP BY batch_id, location
),
global AS (
    SELECT
        batch_id,
        SUM(n_dev_images_jpg)    > 0 AS has_dev_images_jpg_any
    FROM per_loc
    GROUP BY batch_id
),
juno AS (
    SELECT
        batch_id,
        n_dev_images_jpg AS n_dev_images_jpg_juno
    FROM per_loc
    WHERE location = 'JUNO'
)
SELECT
    g.batch_id,
    g.has_dev_images_jpg_any,
    COALESCE(j.n_dev_images_jpg_juno, 0) AS n_dev_images_jpg_juno
FROM global g
LEFT JOIN juno j USING (batch_id)
WHERE
    g.has_dev_images_jpg_any        -- images exist somewhere
    AND (
        j.batch_id IS NULL          -- but no JUNO row
        OR j.n_dev_images_jpg_juno = 0  -- or JUNO has zero images
    )
ORDER BY g.batch_id;

-- name: sync_batches_needing_juno_metadata_only
WITH per_loc AS (
    SELECT
        batch_id,
        location,

        -- Metadata JSONs in metadata folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) = 'json'
                 AND (
                     parent_dir = 'metadata'
                     OR parent_dir LIKE '%/metadata'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_dev_metadata_json
    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL
    GROUP BY batch_id, location
),
global AS (
    SELECT
        batch_id,
        SUM(n_dev_metadata_json) > 0 AS has_dev_metadata_json_any
    FROM per_loc
    GROUP BY batch_id
),
juno AS (
    SELECT
        batch_id,
        n_dev_metadata_json AS n_dev_metadata_json_juno
    FROM per_loc
    WHERE location = 'JUNO'
)
SELECT
    g.batch_id,
    g.has_dev_metadata_json_any,
    COALESCE(j.n_dev_metadata_json_juno, 0) AS n_dev_metadata_json_juno
FROM global g
LEFT JOIN juno j USING (batch_id)
WHERE
    g.has_dev_metadata_json_any             -- metadata exists somewhere
    AND (
        j.batch_id IS NULL                  -- but no JUNO row
        OR j.n_dev_metadata_json_juno = 0   -- or JUNO has zero metadata
    )
ORDER BY g.batch_id;


/* ============================================================
 * 3. MISSING OR INCOMPLETE DATA BY BATCH
 *    - Batches with no RAWs in upload_raw but other data exists
 *    - Batches with RAWs but missing metadata JSONs
 *    - Batches with RAWs but missing image JPGs
 * ============================================================
 */


-- name: miss_batches_missing_upload_raw
WITH per_batch AS (
    SELECT
        batch_id,

        -- Count RAW files in upload_raw (any location)
        SUM(
            CASE
                WHEN data_state = 'upload_raw'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('raw','arw')
                THEN 1 ELSE 0
            END
        ) AS n_upload_raw_files,

        -- Count any non-upload_raw records (developed_jpg, cutouts, etc.)
        SUM(
            CASE
                WHEN data_state <> 'upload_raw'
                THEN 1 ELSE 0
            END
        ) AS n_other_records
    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL
    GROUP BY batch_id
)
SELECT
    batch_id,
    n_upload_raw_files,
    n_other_records
FROM per_batch
WHERE
    n_upload_raw_files = 0      -- no RAWs in upload_raw anywhere
    AND n_other_records > 0     -- but batch *does* exist in some other data_state
ORDER BY batch_id;


-- name: miss_batches_raw_missing_metadata_json
WITH per_batch AS (
    SELECT
        batch_id,

        -- RAW files in upload_raw
        SUM(
            CASE
                WHEN data_state = 'upload_raw'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('raw','arw')
                THEN 1 ELSE 0
            END
        ) AS n_raw,

        -- JSON metadata in developed_jpg under a "metadata" parent folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) = 'json'
                 AND (
                     parent_dir = 'metadata'
                     OR parent_dir LIKE '%/metadata'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_metadata_json
    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL
    GROUP BY batch_id
)
SELECT
    batch_id,
    n_raw,
    n_metadata_json
FROM per_batch
WHERE n_raw > 0              -- has RAWs in upload_raw
  AND n_metadata_json = 0    -- no JSON metadata in a "metadata" folder in developed_jpg
ORDER BY batch_id;

-- name: miss_batches_raw_missing_image_jpg
WITH per_batch AS (
    SELECT
        batch_id,

        -- RAW files in upload_raw
        SUM(
            CASE
                WHEN data_state = 'upload_raw'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('raw','arw')
                THEN 1 ELSE 0
            END
        ) AS n_raw,

        -- JPGs in developed_jpg under an "image" parent folder
        SUM(
            CASE
                WHEN data_state = 'developed_jpg'
                 AND entry_type = 'file'
                 AND LOWER(file_ext) IN ('jpg','jpeg')
                 AND (
                     parent_dir = 'images'
                     OR parent_dir LIKE '%/images'
                 )
                THEN 1 ELSE 0
            END
        ) AS n_image_jpg
    FROM source.globus_file_index
    WHERE batch_id IS NOT NULL
    GROUP BY batch_id
)
SELECT
    batch_id,
    n_raw,
    n_image_jpg
FROM per_batch
WHERE n_raw > 0          -- has RAWs in upload_raw
  AND n_image_jpg = 0    -- no JPGs in an "image" folder in developed_jpg
ORDER BY batch_id;

/* ============================================================
 * 4. EXPLORATION AND UTILITY
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
    UNION SELECT 'location',    location::text    FROM source.globus_file_index
    UNION SELECT 'lts_root',    lts_root::text    FROM source.globus_file_index
    UNION SELECT 'root_path',   root_path::text   FROM source.globus_file_index
    UNION SELECT 'parent_dir',  parent_dir::text  FROM source.globus_file_index
    UNION SELECT 'entry_type',  entry_type::text  FROM source.globus_file_index
    UNION SELECT 'file_ext',    file_ext::text    FROM source.globus_file_index
    UNION SELECT 'data_state',  data_state::text  FROM source.globus_file_index
)
SELECT DISTINCT column_name, value
FROM uniq
ORDER BY column_name, value;
