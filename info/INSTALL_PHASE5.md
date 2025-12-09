# Phase 5 Installation Guide

## Quick Install

```bash
# 1. Connect to database
source /project/dash_agir/postgres/pg_coords.env

# 2. Run schema file
psql -f metadata_schema.sql

# 3. Verify installation
psql -c "\d processed.batches"
psql -c "\d processed.images"
psql -c "\dv processed.*"
```

Expected output:
```
                Table "processed.batches"
       Column        |           Type           | Nullable | Default
---------------------+--------------------------+----------+---------
 batch_id            | text                     | not null |
 batch_state         | text                     | not null |
 batch_date          | date                     | not null |
 location            | text                     |          |
 processing_status   | text                     |          |
 file_count_raw      | integer                  |          |
 file_count_jpg      | integer                  |          |
 ...
Indexes:
    "batches_pkey" PRIMARY KEY, btree (batch_id)
    (+ 6 more indexes...)

                Table "processed.images"
       Column        |           Type           | Nullable | Default
---------------------+--------------------------+----------+---------
 image_id            | text                     | not null |
 batch_id            | text                     | not null |
 file_name           | text                     | not null |
 processing_status   | text                     |          |
 exif_data           | jsonb                    |          |
 camera_make         | text                     |          |
 bounding_boxes      | jsonb                    |          |
 detection_count     | integer                  |          |
 ...
Foreign-key constraints:
    "images_batch_id_fkey" FOREIGN KEY (batch_id) REFERENCES processed.batches(batch_id) ON DELETE CASCADE
Indexes:
    "images_pkey" PRIMARY KEY, btree (image_id)
    (+ 13 more indexes...)

 Schema    |           Name              | Type |      Owner
-----------+-----------------------------+------+-----------------
 processed | batch_summary               | view | matthew.kutugata
 processed | camera_stats                | view | matthew.kutugata
 processed | failed_images_by_batch      | view | matthew.kutugata
 processed | images_with_detections      | view | matthew.kutugata
 processed | pending_images_by_batch     | view | matthew.kutugata
(5 rows)
```

## Test Queries

After installation, test the schema:

```sql
-- Test batch insert
INSERT INTO processed.batches (batch_id, batch_state, batch_date)
VALUES ('TEST_2025-01-01', 'MD', '2025-01-01');

-- Test image insert
INSERT INTO processed.images (image_id, batch_id, file_name)
VALUES ('TEST_001', 'TEST_2025-01-01', 'TEST_001.raw');

-- Query batch
SELECT * FROM processed.batches WHERE batch_id = 'TEST_2025-01-01';

-- Query image
SELECT * FROM processed.images WHERE batch_id = 'TEST_2025-01-01';

-- Test batch summary view
SELECT * FROM processed.batch_summary WHERE batch_id = 'TEST_2025-01-01';

-- Test foreign key constraint
DELETE FROM processed.batches WHERE batch_id = 'TEST_2025-01-01';
-- Should cascade delete images

-- Clean up (if needed)
DELETE FROM processed.images WHERE batch_id LIKE 'TEST%';
DELETE FROM processed.batches WHERE batch_id LIKE 'TEST%';
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
# Run Phase 5 tests
python test_phase5.py

# Should see:
# ✓ All Phase 5 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 5 Complete!
```

## Quick Usage Test

```python
from agir_db import AgirDB
from datetime import date

with AgirDB() as db:
    # Test batch insert
    db.batches.insert(
        batch_id='TEST_2025-01-01',
        batch_state='MD',
        batch_date=date(2025, 1, 1),
        location='JUNO'
    )
    
    # Test image insert
    db.images.insert(
        image_id='TEST_001',
        batch_id='TEST_2025-01-01',
        file_name='TEST_001.raw'
    )
    
    db.commit()
    
    # Test queries
    batch = db.batches.get_by_id('TEST_2025-01-01')
    print(f"Batch: {batch['batch_id']}")
    
    images = db.images.get_by_batch('TEST_2025-01-01')
    print(f"Images: {len(images)}")
    
    # Clean up
    db._connection.execute(
        "DELETE FROM processed.batches WHERE batch_id = %s",
        ('TEST_2025-01-01',)
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
DROP TABLE IF EXISTS processed.images CASCADE;
DROP TABLE IF EXISTS processed.batches CASCADE;
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

### Error: "foreign key constraint violated"

When inserting images, the batch must exist first:
```python
# Correct order:
db.batches.insert(batch_id='MD_2025-01-01', ...)  # Batch first
db.images.insert(image_id='MD_001', batch_id='MD_2025-01-01', ...)  # Then images
```

### Slow queries

The schema includes 20+ indexes, but if you experience slow queries:

1. Check index usage:
```sql
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE tablename IN ('images', 'batches')
ORDER BY idx_scan DESC;
```

2. Analyze the tables:
```sql
ANALYZE processed.batches;
ANALYZE processed.images;
```

3. Check table sizes:
```sql
SELECT 
    pg_size_pretty(pg_total_relation_size('processed.images')) AS images_total,
    pg_size_pretty(pg_total_relation_size('processed.batches')) AS batches_total;
```

## Data Migration from Existing System

If you have existing data in globus_file_index or other tables:

### Option 1: Manual Population

```python
from agir_db import AgirDB
from datetime import date

with AgirDB() as db:
    # Get batches from globus_file_index
    query = """
        SELECT DISTINCT batch_id, batch_state, batch_date, location
        FROM source.globus_file_index
        WHERE batch_id IS NOT NULL
        ORDER BY batch_date DESC;
    """
    
    source_batches = db._connection.fetch_all(query)
    
    for batch in source_batches:
        # Insert into processed.batches
        db.batches.insert(
            batch_id=batch['batch_id'],
            batch_state=batch['batch_state'],
            batch_date=batch['batch_date'],
            location=batch['location']
        )
    
    db.commit()
```

### Option 2: Wait for Phase 6

Phase 6 (Inventory Sync) will provide automated synchronization:
```python
# Coming in Phase 6
db.inventory.sync_batch('MD_2025-01-01')
db.inventory.sync_all()
```

## What Changed

Phase 5 added:
1. **SQL Tables**: `processed.batches` and `processed.images`
2. **SQL Views**: 5 helper views for common queries
3. **SQL Indexes**: 20+ indexes for performance
4. **Python Classes**: `ImageMetadata` and `BatchMetadata`
5. **Integration**: Added `db.images` and `db.batches` to AgirDB facade

## Next Steps

After Phase 5 installation:
1. Run `test_phase5.py` to verify
2. Try the usage examples in PHASE5_README.md
3. Consider populating from existing data
4. Ready to proceed to Phase 6 (Inventory Sync)

## Maintenance

### Monitor table growth

```sql
-- Check row counts
SELECT 
    'batches' AS table_name,
    COUNT(*) AS row_count
FROM processed.batches
UNION ALL
SELECT 
    'images' AS table_name,
    COUNT(*) AS row_count
FROM processed.images;

-- Check table sizes
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS total_size,
    pg_size_pretty(pg_relation_size(schemaname||'.'||tablename)) AS table_size,
    pg_size_pretty(pg_indexes_size(schemaname||'.'||tablename)) AS indexes_size
FROM pg_tables
WHERE schemaname = 'processed'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

### Vacuum regularly

```sql
-- Vacuum to reclaim space and update statistics
VACUUM ANALYZE processed.batches;
VACUUM ANALYZE processed.images;

-- Or enable autovacuum (recommended)
ALTER TABLE processed.batches SET (autovacuum_enabled = true);
ALTER TABLE processed.images SET (autovacuum_enabled = true);
```

### Archive old data

```sql
-- Archive batches older than 1 year
-- (Move to archive schema first, then delete)
CREATE TABLE IF NOT EXISTS archive.batches (LIKE processed.batches INCLUDING ALL);
CREATE TABLE IF NOT EXISTS archive.images (LIKE processed.images INCLUDING ALL);

-- Move old data
WITH old_batches AS (
    DELETE FROM processed.batches
    WHERE batch_date < CURRENT_DATE - INTERVAL '1 year'
    RETURNING *
)
INSERT INTO archive.batches SELECT * FROM old_batches;

-- Images will cascade delete due to foreign key
```
