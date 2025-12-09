# Phase 4 Installation Guide

## Quick Install

```bash
# 1. Connect to database
source /project/dash_agir/postgres/pg_coords.env

# 2. Run schema file
psql -f events_schema.sql

# 3. Verify installation
psql -c "\d processed.events"
psql -c "\dv processed.*"
```

Expected output:
```
                    Table "processed.events"
    Column     |           Type           | Nullable |      Default
---------------+--------------------------+----------+-------------------
 event_id      | bigint                   | not null | nextval(...)
 event_type    | text                     | not null |
 severity      | text                     | not null |
 batch_id      | text                     |          |
 stage         | text                     |          |
 job_id        | text                     |          |
 message       | text                     | not null |
 metadata      | jsonb                    |          |
 hostname      | text                     |          |
 user_name     | text                     |          |
 source        | text                     |          |
 created_at    | timestamp with time zone | not null | now()
 error_type    | text                     |          |
 stack_trace   | text                     |          |
Indexes:
    "events_pkey" PRIMARY KEY, btree (event_id)
    (+ 8 more indexes...)

 Schema    |        Name            | Type |      Owner
-----------+------------------------+------+-----------------
 processed | batch_event_summary    | view | matthew.kutugata
 processed | error_events           | view | matthew.kutugata
 processed | event_summary_24h      | view | matthew.kutugata
 processed | recent_events          | view | matthew.kutugata
 processed | stage_events           | view | matthew.kutugata
 processed | warning_events         | view | matthew.kutugata
(6 rows)
```

## Test Queries

After installation, test the schema:

```sql
-- Test logging an event
INSERT INTO processed.events (event_type, severity, message, batch_id, stage)
VALUES ('test.event', 'INFO', 'Test event message', 'TEST_2025-01-01', 'raw_to_jpg');

-- Query the event
SELECT * FROM processed.events WHERE batch_id = 'TEST_2025-01-01';

-- Check recent events view
SELECT * FROM processed.recent_events LIMIT 10;

-- Check event summary
SELECT * FROM processed.event_summary_24h;

-- Full-text search test
SELECT * FROM processed.events 
WHERE to_tsvector('english', message) @@ plainto_tsquery('english', 'test');

-- Clean up
DELETE FROM processed.events WHERE batch_id = 'TEST_2025-01-01';
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
# Run Phase 4 tests
python test_phase4.py

# Should see:
# ✓ All Phase 4 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 4 Complete!
```

## Quick Usage Test

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Log a test event
    event_id = db.events.log_event(
        event_type='test.quickstart',
        severity='INFO',
        message='Testing event logging',
        metadata={'test': True}
    )
    db.commit()
    
    print(f"Logged event: {event_id}")
    
    # Query recent events
    recent = db.events.get_recent_events(hours=1, limit=5)
    print(f"Recent events: {len(recent)}")
    
    # Clean up
    db._connection.execute(
        "DELETE FROM processed.events WHERE event_id = %s",
        (event_id,)
    )
    db.commit()
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
DROP TABLE IF EXISTS processed.events CASCADE;
```
Then rerun the schema file.

### Error: "permission denied for schema processed"

Make sure you have permission:
```sql
-- Check permissions
SELECT has_schema_privilege('processed', 'CREATE');

-- If false, ask admin to grant permissions:
GRANT CREATE ON SCHEMA processed TO your_username;
```

### Slow queries

The schema includes 9 indexes, but if you experience slow queries:

1. Check index usage:
```sql
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE tablename = 'events'
ORDER BY idx_scan DESC;
```

2. Analyze the table:
```sql
ANALYZE processed.events;
```

3. Consider partitioning for high volume (see PHASE4_README.md)

## Maintenance

### Set up retention policy

For production, delete old events periodically:

```bash
# Create cron job to clean events older than 90 days
# Run daily at 2 AM
crontab -e

# Add this line:
0 2 * * * psql -d agir -c "SELECT cleanup_old_events(90);"
```

### Monitor table size

```sql
-- Check events table size
SELECT 
    pg_size_pretty(pg_total_relation_size('processed.events')) AS total_size,
    pg_size_pretty(pg_relation_size('processed.events')) AS table_size,
    pg_size_pretty(pg_indexes_size('processed.events')) AS indexes_size;

-- Check row count
SELECT COUNT(*) FROM processed.events;
```

### Vacuum regularly

```sql
-- Vacuum to reclaim space after deletions
VACUUM ANALYZE processed.events;

-- Or enable autovacuum (recommended)
ALTER TABLE processed.events SET (autovacuum_enabled = true);
```

## What Changed

Phase 4 added:
1. **SQL Table**: `processed.events` for event logging
2. **SQL Views**: 6 helper views for common queries
3. **SQL Functions**: cleanup_old_events(), create_events_partition()
4. **Python Class**: `EventLogger` with 8 main methods
5. **Integration**: Added `db.events` to AgirDB facade

## Next Steps

After Phase 4 installation:
1. Run `test_phase4.py` to verify
2. Try the usage examples in PHASE4_README.md
3. Consider setting up retention policy for production
4. Ready to proceed to Phase 5 (Image & Batch Metadata)
