# Phase 2 Installation Guide

## Quick Install

```bash
# 1. Connect to database
source /project/dash_agir/postgres/pg_coords.env

# 2. Run schema file (now includes DROP VIEW statements)
psql -f pipeline_gaps_schema.sql

# 3. Verify installation
psql -c "\dv report.*"
```

Expected output:
```
 Schema |              Name                      | Type |      Owner
--------+----------------------------------------+------+-----------------
 report | batch_pipeline_status                  | view | matthew.kutugata
 report | batches_needing_jpg_to_metadata        | view | matthew.kutugata
 report | batches_needing_metadata_to_cutouts    | view | matthew.kutugata
 report | batches_needing_raw_to_jpg             | view | matthew.kutugata
 report | files_needing_jpg_to_metadata          | view | matthew.kutugata
 report | files_needing_metadata_to_cutouts      | view | matthew.kutugata
 report | files_needing_raw_to_jpg               | view | matthew.kutugata
 report | pipeline_gap_summary                   | view | matthew.kutugata
(8 rows)
```

## Test Queries

After installation, test the views:

```sql
-- Check overall gap summary
SELECT * FROM report.pipeline_gap_summary;

-- Find batches needing RAW → JPG
SELECT * FROM report.batches_needing_raw_to_jpg LIMIT 10;

-- Get complete pipeline status for a batch
SELECT * FROM report.batch_pipeline_status 
WHERE batch_id = 'MD_2025-01-01';
```

## Troubleshooting

### Error: "cannot drop columns from view"

This happens if you tried to run the old version. Solution:
```bash
# Option 1: Use the cleanup script
psql -f drop_pipeline_gap_views.sql
psql -f pipeline_gaps_schema.sql

# Option 2: Drop manually
psql -c "DROP VIEW IF EXISTS report.pipeline_gap_summary CASCADE;"
psql -f pipeline_gaps_schema.sql
```

### Error: "view does not exist"

Views were never created. Just run:
```bash
psql -f pipeline_gaps_schema.sql
```

## What Changed

The updated `pipeline_gaps_schema.sql` now:
1. Includes `DROP VIEW IF EXISTS` statements at the beginning
2. Drops views in reverse dependency order
3. Can be run multiple times safely (idempotent)
4. No longer uses `CREATE OR REPLACE VIEW`

This prevents the "cannot drop columns from view" error.
