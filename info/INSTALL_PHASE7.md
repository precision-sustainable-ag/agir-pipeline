# Phase 7 Installation Guide

## Quick Install

```bash
# 1. Connect to database
source /project/dash_agir/postgres/pg_coords.env

# 2. Run schema file
psql -f transfers_schema.sql

# 3. Verify installation
psql -c "\d processed.transfers"
psql -c "\dv processed.*transfer*"
```

Expected output:
```
                Table "processed.transfers"
       Column        |           Type           | Nullable | Default
---------------------+--------------------------+----------+---------
 transfer_id         | integer                  | not null | nextval...
 batch_id            | text                     | not null |
 source_location     | text                     | not null |
 destination_location| text                     | not null |
 status              | text                     | not null |
 globus_task_id      | text                     |          |
 ...
Indexes:
    "transfers_pkey" PRIMARY KEY, btree (transfer_id)
    (+ 8 more indexes...)
Foreign-key constraints:
    "transfers_batch_id_fkey" FOREIGN KEY (batch_id) REFERENCES processed.batches(batch_id) ON DELETE CASCADE

 Schema    |           Name              | Type |      Owner
-----------+-----------------------------+------+-----------------
 processed | active_transfers            | view | matthew.kutugata
 processed | completed_transfers         | view | matthew.kutugata
 processed | failed_transfers            | view | matthew.kutugata
 processed | pending_transfers           | view | matthew.kutugata
 processed | transfer_stats_by_location  | view | matthew.kutugata
```

## Test Queries

After installation, test the schema:

```sql
-- Test transfer insert
INSERT INTO processed.transfers (
    batch_id, source_location, destination_location, status
) VALUES (
    'TEST_2025-01-01', 'JUNO', 'CERES', 'pending'
);

-- Query transfer
SELECT * FROM processed.transfers WHERE batch_id = 'TEST_2025-01-01';

-- Test views
SELECT * FROM processed.pending_transfers;
SELECT * FROM processed.transfer_stats_by_location;

-- Clean up
DELETE FROM processed.transfers WHERE batch_id = 'TEST_2025-01-01';
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
# Run Phase 7 tests
python test_phase7.py

# Should see:
# ✓ All Phase 7 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 7 Complete!
```

## Quick Usage Test

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Test transfer creation
    transfer_id = db.transfers.start_transfer(
        batch_id='MD_2025-01-01',  # Must exist in processed.batches
        source_location='JUNO',
        destination_location='CERES',
        file_count=100
    )
    
    db.commit()
    
    # Test queries
    transfer = db.transfers.get_by_id(transfer_id)
    print(f"Transfer: {transfer['transfer_id']}")
    
    pending = db.transfers.get_pending()
    print(f"Pending: {len(pending)}")
    
    # Clean up
    db._connection.execute(
        "DELETE FROM processed.transfers WHERE transfer_id = %s",
        (transfer_id,)
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
DROP TABLE IF EXISTS processed.transfers CASCADE;
```
Then rerun the schema file.

### Error: "batch_id violates foreign key constraint"

The batch must exist in processed.batches first:
```python
# Correct order:
db.batches.insert(batch_id='MD_2025-01-01', ...)  # Batch first
db.transfers.start_transfer(batch_id='MD_2025-01-01', ...)  # Then transfer
```

### Slow queries

The schema includes 9 indexes, but if you experience slow queries:

1. Analyze the table:
```sql
ANALYZE processed.transfers;
```

2. Check index usage:
```sql
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE tablename = 'transfers'
ORDER BY idx_scan DESC;
```

## Data Retention

The schema includes a cleanup function:

```sql
-- Clean up transfers older than 1 year
SELECT cleanup_old_transfers(365);

-- Or schedule as cron job:
-- 0 3 * * 0  psql -c "SELECT cleanup_old_transfers(365);"
```

## Monitoring

### Check transfer activity:

```sql
-- Active transfers
SELECT COUNT(*) FROM processed.transfers WHERE status = 'in_progress';

-- Failed transfers
SELECT COUNT(*) FROM processed.transfers WHERE status = 'failed';

-- Transfers by location
SELECT * FROM processed.transfer_stats_by_location;
```

### Table size:

```sql
SELECT 
    pg_size_pretty(pg_total_relation_size('processed.transfers')) AS total_size,
    pg_size_pretty(pg_relation_size('processed.transfers')) AS table_size,
    pg_size_pretty(pg_indexes_size('processed.transfers')) AS indexes_size;
```

## What Changed

Phase 7 added:
1. **SQL Table**: `processed.transfers`
2. **SQL Views**: 5 helper views for queries
3. **SQL Functions**: Helper functions and triggers
4. **Python Class**: `TransferManager` with 11 methods
5. **Integration**: Added `db.transfers` to AgirDB facade

## Next Steps

After Phase 7 installation:
1. Run `test_phase7.py` to verify
2. Try the usage examples in PHASE7_README.md
3. Integrate with your Globus transfer scripts
4. Ready to proceed to Phase 8 (Analytics)
