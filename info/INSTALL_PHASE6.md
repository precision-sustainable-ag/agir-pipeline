# Phase 6 Installation Guide

## Quick Install

Phase 6 has **no SQL schema** - it uses existing tables:
- `source.globus_file_index` (your existing table)
- `processed.batches` (from Phase 5)
- `processed.images` (from Phase 5)

### Step 1: Verify Prerequisites

```bash
# Connect to database
source /project/dash_agir/postgres/pg_coords.env

# Verify globus_file_index exists and has data
psql -c "SELECT COUNT(DISTINCT batch_id) as batches FROM source.globus_file_index;"
psql -c "SELECT COUNT(*) as files FROM source.globus_file_index WHERE file_ext = 'raw';"

# Verify processed tables exist (from Phase 5)
psql -c "\d processed.batches"
psql -c "\d processed.images"
```

Expected output:
```
 batches
---------
     523

 files  
--------
  74832

                Table "processed.batches"
...

                Table "processed.images"
...
```

### Step 2: Install Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

### Step 3: Run Tests

```bash
python test_phase6.py
```

Expected output:
```
✓ All Phase 6 unit tests passed!
✓ All database integration tests passed!
✓ Phase 6 Complete!
```

## Initial Sync

After installation, perform initial sync:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Check status before
    status = db.inventory.get_sync_status()
    print(f"Before: {status['processed_batches']}/{status['source_batches']} batches")
    
    # Sync all (start with small limit for testing)
    stats = db.inventory.sync_all(limit=10)
    db.commit()
    
    print(f"Synced: {stats['batches_synced']} batches")
    print(f"Images: {stats['total_images_inserted']}")
    
    # Check status after
    status = db.inventory.get_sync_status()
    print(f"After: {status['processed_batches']}/{status['source_batches']} batches")
```

## Setup Daily Sync (Optional)

Create a daily sync script:

```bash
cat > /path/to/daily_sync.py << 'EOF'
#!/usr/bin/env python3
from agir_db import AgirDB
from datetime import datetime

with AgirDB() as db:
    print(f"[{datetime.now()}] Starting daily sync...")
    stats = db.inventory.sync_recent(days=7)
    db.commit()
    print(f"Synced {stats['batches_synced']} batches")
EOF

chmod +x /path/to/daily_sync.py
```

Add to cron:
```bash
# Edit crontab
crontab -e

# Add this line (runs at 2 AM daily):
0 2 * * * /path/to/daily_sync.py >> /path/to/logs/sync.log 2>&1
```

## Troubleshooting

### Error: "relation source.globus_file_index does not exist"

Make sure the source schema and table exist:
```sql
-- Check schema
SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'source';

-- Check table
\dt source.*

-- If missing, you need to create them or update the code
-- to point to your actual globus_file_index location
```

### Error: "processed.batches does not exist"

Install Phase 5 first:
```bash
psql -f metadata_schema.sql
```

### Slow sync performance

For large syncs, use batching:
```python
# Instead of sync_all() with no limit
# Use chunks:
for i in range(0, 1000, 100):  # 10 chunks of 100
    stats = db.inventory.sync_all(limit=100)
    db.commit()
    print(f"Synced batch {i//100 + 1}/10")
```

### No batches found in globus_file_index

Check that batch_id is populated:
```sql
SELECT 
    COUNT(*) as total_files,
    COUNT(DISTINCT batch_id) as batches,
    COUNT(*) FILTER (WHERE batch_id IS NULL) as files_without_batch
FROM source.globus_file_index;
```

If many files lack batch_id, you may need to populate it first.

## Verification

After initial sync, verify data:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get sync status
    status = db.inventory.get_sync_status()
    print(f"Sync: {status['sync_percentage']:.1f}%")
    
    # Reconcile to find differences
    results = db.inventory.reconcile()
    print(f"Missing: {len(results['missing_batches'])} batches")
    print(f"Orphaned: {len(results['orphaned_batches'])} batches")
    
    # Check a sample batch
    batches = db.batches.get_by_state('MD', limit=1)
    if batches:
        batch_id = batches[0]['batch_id']
        images = db.images.get_by_batch(batch_id)
        print(f"\nSample batch {batch_id}:")
        print(f"  Images: {len(images)}")
```

## What Changed

Phase 6 added:
1. **InventorySync class** - inventory.py (~650 lines)
2. **5 main methods** - sync_batch, sync_all, sync_recent, reconcile, get_sync_status
3. **Integration** - Added `db.inventory` to AgirDB facade
4. **No SQL changes** - Uses existing tables

## Next Steps

1. Run initial sync: `db.inventory.sync_all(limit=100)`
2. Setup daily sync cron job
3. Integrate with processing pipelines
4. Ready for Phase 7 (Transfer Management)
