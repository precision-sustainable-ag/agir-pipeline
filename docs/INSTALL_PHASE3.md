# Phase 3 Installation Guide

## Quick Install

```bash
# 1. Connect to database
source /project/dash_agir/postgres/pg_coords.env

# 2. Run schema file
psql -f stage_status_schema.sql

# 3. Verify installation
psql -c "\d processed.stage_status"
psql -c "\dv processed.*"
```

Expected output:
```
                Table "processed.stage_status"
     Column      |           Type           | Nullable | Default
-----------------+--------------------------+----------+---------
 batch_id        | text                     | not null |
 stage           | text                     | not null |
 status          | text                     | not null |
 job_id          | text                     |          |
 hostname        | text                     |          |
 started_at      | timestamp with time zone | not null | now()
 completed_at    | timestamp with time zone |          |
 duration_seconds| numeric                  |          |
 success         | boolean                  |          |
 files_processed | integer                  |          |
 files_failed    | integer                  |          |
 error_message   | text                     |          |
 metadata        | jsonb                    |          |
 created_at      | timestamp with time zone | not null | now()
 updated_at      | timestamp with time zone | not null | now()
Indexes:
    "stage_status_pkey" PRIMARY KEY, btree (batch_id, stage)
    "idx_stage_status_batch" btree (batch_id, updated_at DESC)
    "idx_stage_status_in_progress" btree (stage, status) WHERE status = 'in_progress'::text
    "idx_stage_status_job" btree (job_id, started_at DESC) WHERE job_id IS NOT NULL
    "idx_stage_status_status" btree (status, started_at DESC)
Triggers:
    trigger_stage_status_updated_at BEFORE UPDATE ON processed.stage_status FOR EACH ROW EXECUTE FUNCTION update_stage_status_timestamp()

 Schema    |        Name         | Type |      Owner
-----------+---------------------+------+-----------------
 processed | completed_stages    | view | matthew.kutugata
 processed | failed_stages       | view | matthew.kutugata
 processed | in_progress_stages  | view | matthew.kutugata
(3 rows)
```

## Test Queries

After installation, test the schema:

```sql
-- Test inserting a stage status
INSERT INTO processed.stage_status (batch_id, stage, status, job_id)
VALUES ('TEST_2025-01-01', 'raw_to_jpg', 'in_progress', 'test_123');

-- Query the stage
SELECT * FROM processed.stage_status WHERE batch_id = 'TEST_2025-01-01';

-- Check in-progress view
SELECT * FROM processed.in_progress_stages;

-- Update to completed
UPDATE processed.stage_status
SET status = 'completed', completed_at = NOW(), success = true, files_processed = 100
WHERE batch_id = 'TEST_2025-01-01' AND stage = 'raw_to_jpg';

-- Check completed view
SELECT * FROM processed.completed_stages WHERE batch_id = 'TEST_2025-01-01';

-- Clean up test data
DELETE FROM processed.stage_status WHERE batch_id = 'TEST_2025-01-01';
```

## Python Installation

```bash
# Make sure you're in the agir-db repository
cd /path/to/agir-db

# Install in editable mode
pip install -e .
```

## Run Tests

```bash
# Run Phase 3 tests
python test_phase3.py

# Should see:
# ✓ All Phase 3 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 3 Complete!
```

## Troubleshooting

### Error: "schema processed does not exist"

Create the schema first:
```sql
CREATE SCHEMA IF NOT EXISTS processed;
```

### Error: "table already exists"

The schema includes `DROP TABLE IF EXISTS`. If you still get this error:
```sql
DROP TABLE IF EXISTS processed.stage_status CASCADE;
```
Then rerun the schema file.

### Error: "permission denied"

Make sure you have permission to create tables:
```sql
-- Check permissions
SELECT has_schema_privilege('processed', 'CREATE');

-- If false, ask admin to grant permissions:
GRANT CREATE ON SCHEMA processed TO your_username;
```

## What Changed

Phase 3 added:
1. **SQL Table**: `processed.stage_status` for tracking stage execution
2. **SQL Views**: 3 helper views for querying stages
3. **Python Class**: `StageStatus` with 7 main methods
4. **Integration**: Added `db.stages` to AgirDB facade

## Next Steps

After Phase 3 installation:
1. Run `test_phase3.py` to verify
2. Try the usage examples in PHASE3_README.md
3. Ready to proceed to Phase 4 (Event Logging)
