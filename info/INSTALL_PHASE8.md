# Phase 8 Installation Guide

## Quick Install

```bash
# 1. Connect to database
source /project/dash_agir/postgres/pg_coords.env

# 2. Run analytics schema
psql -f analytics_schema.sql

# 3. Verify views were created
psql -c "\dv processed.*"
```

Expected output includes 16+ views:
```
 Schema    |           Name                  | Type
-----------+---------------------------------+------
 processed | batch_completion_status         | view
 processed | camera_usage_stats              | view
 processed | daily_batch_summary             | view
 processed | daily_throughput                | view
 processed | error_summary_by_stage          | view
 processed | event_summary                   | view
 processed | pipeline_overview               | view
 processed | recent_critical_events          | view
 processed | recent_errors                   | view
 processed | stage_performance               | view
 processed | stage_performance_summary       | view
 processed | storage_by_location             | view
 processed | storage_growth                  | view
 processed | transfer_performance            | view
 processed | transfer_summary_by_route       | view
 ...
```

## Test Queries

After installation, test the views:

```sql
-- Test pipeline overview
SELECT * FROM processed.pipeline_overview;

-- Test daily summary
SELECT * FROM processed.daily_batch_summary 
ORDER BY processing_date DESC 
LIMIT 7;

-- Test stage performance
SELECT * FROM processed.stage_performance_summary;

-- Test storage
SELECT * FROM processed.storage_by_location;

-- Test helper function
SELECT * FROM get_processing_stats(
    '2025-01-01'::DATE,
    CURRENT_DATE
);
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
# Run Phase 8 tests
python test_phase8.py

# Should see:
# ✓ All Phase 8 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 8 Complete!
```

## Quick Usage Test

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Test analytics
    overview = db.analytics.get_pipeline_overview()
    print(f"Total batches: {overview['total_batches']}")
    print(f"Storage: {overview['total_storage_gb']} GB")
    
    # Test processing stats
    stats = db.analytics.get_processing_stats(days=7)
    print(f"Batches processed: {stats.get('batches_processed', 0)}")
    
    # Test throughput
    throughput = db.analytics.get_throughput(days=7)
    print(f"Throughput records: {len(throughput)}")
```

## Troubleshooting

### Error: "view already exists"

Drop and recreate:
```sql
DROP VIEW IF EXISTS processed.daily_batch_summary CASCADE;
DROP VIEW IF EXISTS processed.stage_performance CASCADE;
-- ... (drop all views)

-- Then rerun schema
\i analytics_schema.sql
```

Or use CASCADE when dropping:
```sql
DROP SCHEMA processed CASCADE;
CREATE SCHEMA processed;
-- Rerun ALL schemas (processed tables + views)
```

### Error: "relation does not exist"

The views depend on tables from previous phases. Make sure you have:
- processed.batches (Phase 5)
- processed.images (Phase 5)
- processed.stage_status (Phase 3)
- processed.transfers (Phase 7)
- processed.events (Phase 4)

Install missing phases first.

### No data in views

Views show data only if you have:
- Batches in processed.batches
- Stage executions in processed.stage_status
- Some processing history

Run inventory sync to populate:
```python
from agir_db import AgirDB
with AgirDB() as db:
    db.inventory.sync_recent(days=30)
    db.commit()
```

### Slow view queries

Analyze tables:
```sql
ANALYZE processed.batches;
ANALYZE processed.images;
ANALYZE processed.stage_status;
ANALYZE processed.transfers;
ANALYZE processed.events;
```

Check view performance:
```sql
EXPLAIN ANALYZE 
SELECT * FROM processed.daily_batch_summary;
```

## View Maintenance

### Refresh Statistics

```sql
-- Refresh table statistics (run weekly)
ANALYZE processed.batches;
ANALYZE processed.images;
ANALYZE processed.stage_status;
ANALYZE processed.transfers;
ANALYZE processed.events;
```

### Materialized Views (Optional)

For very large datasets, consider converting some views to materialized:

```sql
-- Convert to materialized view
DROP VIEW processed.daily_batch_summary;

CREATE MATERIALIZED VIEW processed.daily_batch_summary AS
-- (view definition here)
WITH DATA;

-- Refresh daily via cron
REFRESH MATERIALIZED VIEW processed.daily_batch_summary;
```

## Monitoring

### Check view usage:

```sql
SELECT schemaname, viewname
FROM pg_views
WHERE schemaname = 'processed'
ORDER BY viewname;
```

### Monitoring queries:

```sql
-- Pipeline health check
SELECT * FROM processed.pipeline_overview;

-- Recent errors
SELECT * FROM processed.recent_errors LIMIT 10;

-- Storage growth
SELECT * FROM processed.storage_by_location;
```

## Cron Jobs (Optional)

Set up automated reporting:

```bash
# Daily summary report
0 8 * * * /path/to/daily_report.py

# Weekly performance report
0 9 * * 1 /path/to/weekly_report.py

# Monthly storage report
0 10 1 * * /path/to/storage_report.py
```

Example daily_report.py:
```python
#!/usr/bin/env python3
from agir_db import AgirDB

with AgirDB() as db:
    stats = db.analytics.get_processing_stats(days=1)
    print(f"Yesterday: {stats.get('batches_processed', 0)} batches")
    
    errors = db.analytics.get_recent_errors(limit=10)
    if errors:
        print(f"Errors: {len(errors)}")
```

## What Changed

Phase 8 added:
1. **SQL Views**: 16 analytics views
2. **SQL Function**: `get_processing_stats()` for date ranges
3. **Python Class**: `Analytics` with 14 methods
4. **Integration**: Added `db.analytics` to AgirDB facade

## Next Steps

After Phase 8 installation:
1. Run `test_phase8.py` to verify
2. Try the usage examples in PHASE8_README.md
3. Set up monitoring dashboards
4. Ready to proceed to Phase 9 (Migration Tools)
