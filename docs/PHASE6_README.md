# Phase 6: Inventory Sync - Complete ✓

## Overview

Phase 6 implements automated synchronization from the existing `globus_file_index` table to the new `processed.batches` and `processed.images` tables. This eliminates manual data entry and ensures your metadata tables stay up-to-date with your file inventory.

**Why Inventory Sync?**
- **Automated population**: No manual batch/image registration
- **Incremental updates**: Sync only what's new
- **Reconciliation**: Identify missing or orphaned data
- **Bulk operations**: Sync thousands of batches efficiently
- **Source of truth**: globus_file_index remains authoritative

## Component Created

### **InventorySync Class** (inventory.py, ~650 lines)

Synchronize from `source.globus_file_index` to processed tables:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Sync one batch
    stats = db.inventory.sync_batch('MD_2025-01-01')
    print(f"Synced {stats['images_inserted']} images")
    
    # Sync recent batches
    stats = db.inventory.sync_recent(days=7)
    
    # Full sync
    stats = db.inventory.sync_all(limit=100)
    
    # Check sync status
    status = db.inventory.get_sync_status()
    print(f"Sync: {status['sync_percentage']:.1f}%")
    
    # Reconcile differences
    results = db.inventory.reconcile()
    print(f"Missing: {len(results['missing_batches'])} batches")
```

**Main Methods:**

1. **`sync_batch(batch_id, update_existing=False)`**
   - Sync single batch from globus_file_index
   - Create batch record in processed.batches
   - Create image records for RAW files
   - Returns statistics (images_inserted, images_skipped, etc.)

2. **`sync_all(batch_state=None, limit=None, update_existing=False)`**
   - Sync all batches (or filtered subset)
   - Progress logging every 10 batches
   - Returns overall statistics

3. **`sync_recent(days=7, update_existing=False)`**
   - Sync batches modified in last N days
   - Uses `mtime` field from globus_file_index
   - Useful for incremental updates

4. **`reconcile(batch_id=None)`**
   - Compare source vs processed tables
   - Find missing batches/images
   - Find orphaned records (in processed but not source)
   - Returns detailed differences

5. **`get_sync_status()`**
   - Overall sync statistics
   - Source vs processed counts
   - Sync percentage
   - Missing counts

## How It Works

### Data Flow

```
source.globus_file_index (existing)
         │
         ├─ batch metadata ──> processed.batches
         │                     (batch_id, state, date, location)
         │
         └─ RAW files ──────> processed.images
                              (image_id, file_name, size, path)
```

### Sync Logic

**For Batches:**
1. Query globus_file_index for batch metadata
2. Aggregate file counts and total bytes
3. Insert/update in processed.batches

**For Images:**
1. Query globus_file_index for RAW files in batch
2. Use `base_name` as `image_id`
3. Insert into processed.images (skip duplicates)

**Duplicate Handling:**
- By default: `ON CONFLICT DO NOTHING` (skip existing)
- With `update_existing=True`: `ON CONFLICT DO UPDATE`

## Installation

Phase 6 has no additional SQL schema - it uses existing tables from Phases 5 and your source.globus_file_index.

**Prerequisites:**
1. Phase 5 installed (processed.batches and processed.images tables)
2. source.globus_file_index table with data
3. Python package updated

```bash
# Install Python package
cd /path/to/agir-db
pip install -e .

# Verify
python -c "from agir_db import InventorySync; print('✓')"
```

## Usage Examples

### Example 1: Initial Full Sync

```python
from agir_db import AgirDB

with AgirDB() as db:
    print("Starting initial sync...")
    
    # Get current status
    status_before = db.inventory.get_sync_status()
    print(f"Before: {status_before['processed_batches']}/{status_before['source_batches']} batches")
    
    # Sync all batches (in chunks for safety)
    chunk_size = 100
    total_synced = 0
    
    while True:
        stats = db.inventory.sync_all(limit=chunk_size, update_existing=False)
        db.commit()
        
        total_synced += stats['batches_synced']
        print(f"Synced {total_synced} batches so far...")
        
        # Break if we synced fewer than chunk_size (means we're done)
        if stats['batches_synced'] < chunk_size:
            break
    
    # Check final status
    status_after = db.inventory.get_sync_status()
    print(f"After: {status_after['processed_batches']}/{status_after['source_batches']} batches")
    print(f"Sync: {status_after['sync_percentage']:.1f}%")
```

### Example 2: Incremental Sync (Daily Cron Job)

```python
#!/usr/bin/env python3
"""
Daily sync script - add to cron:
0 2 * * * /path/to/daily_sync.py
"""

from agir_db import AgirDB
from datetime import datetime

with AgirDB() as db:
    print(f"[{datetime.now()}] Starting daily sync...")
    
    # Sync batches modified in last 7 days
    stats = db.inventory.sync_recent(days=7, update_existing=True)
    db.commit()
    
    print(f"Synced {stats['batches_synced']} batches")
    print(f"Inserted {stats['total_images_inserted']} new images")
    
    if stats['batches_failed'] > 0:
        print(f"WARNING: {stats['batches_failed']} batches failed!")
    
    # Log to events
    db.events.log_event(
        event_type='inventory.daily_sync',
        severity='INFO',
        message=f"Daily sync: {stats['batches_synced']} batches",
        metadata=stats
    )
    db.commit()
```

### Example 3: Sync Specific State

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Sync all MD batches
    stats = db.inventory.sync_all(
        batch_state='MD',
        limit=None,  # No limit
        update_existing=False
    )
    db.commit()
    
    print(f"Synced {stats['batches_synced']} MD batches")
    print(f"Total images: {stats['total_images_inserted']}")
    print(f"Elapsed: {stats['elapsed_seconds']:.1f}s")
```

### Example 4: Reconciliation Report

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Full reconciliation
    results = db.inventory.reconcile()
    
    print("Reconciliation Report")
    print("=" * 60)
    
    # Missing data (in source but not processed)
    print(f"\nMissing Batches: {len(results['missing_batches'])}")
    if results['missing_batches']:
        print("  First 10:", results['missing_batches'][:10])
    
    print(f"\nMissing Images: {results['missing_images']}")
    
    # Orphaned data (in processed but not source)
    print(f"\nOrphaned Batches: {len(results['orphaned_batches'])}")
    if results['orphaned_batches']:
        print("  These may have been deleted from source")
        print("  First 10:", results['orphaned_batches'][:10])
    
    print(f"\nOrphaned Images: {results['orphaned_images']}")
    
    # Sync missing batches
    if results['missing_batches']:
        print(f"\nSyncing {len(results['missing_batches'])} missing batches...")
        for batch_id in results['missing_batches'][:10]:  # First 10
            try:
                db.inventory.sync_batch(batch_id)
                print(f"  ✓ {batch_id}")
            except Exception as e:
                print(f"  ✗ {batch_id}: {e}")
        
        db.commit()
```

### Example 5: Sync with Progress Updates

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get list of batches to sync
    batches_query = """
        SELECT DISTINCT batch_id
        FROM source.globus_file_index
        WHERE batch_state = 'MD'
        AND batch_id NOT IN (
            SELECT batch_id FROM processed.batches
        )
        ORDER BY batch_id;
    """
    
    batches = db._connection.fetch_all(batches_query)
    total = len(batches)
    
    print(f"Syncing {total} MD batches...")
    
    for i, batch in enumerate(batches, 1):
        batch_id = batch['batch_id']
        
        try:
            stats = db.inventory.sync_batch(batch_id)
            
            if i % 10 == 0:
                print(f"Progress: {i}/{total} ({i/total*100:.1f}%)")
                db.commit()  # Commit every 10 batches
                
        except Exception as e:
            print(f"Failed {batch_id}: {e}")
    
    db.commit()
    print("Sync complete!")
```

### Example 6: Integration with Pipeline

```python
from agir_db import AgirDB

with AgirDB() as db:
    # 1. Sync recent batches
    db.inventory.sync_recent(days=7)
    db.commit()
    
    # 2. Find work using gaps (Phase 2)
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    # 3. Process each batch
    for batch in batches:
        batch_id = batch['batch_id']
        
        # Make sure batch is synced
        if not db.batches.get_by_id(batch_id):
            db.inventory.sync_batch(batch_id)
            db.commit()
        
        # Start processing (Phase 3)
        db.stages.start(batch_id, 'raw_to_jpg', job_id='12345')
        
        # Get files (Phase 2)
        files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
        
        # Process files...
        # (your processing code here)
        
        # Complete (Phase 3)
        db.stages.complete(batch_id, 'raw_to_jpg', success=True)
        
        # Update batch status (Phase 5)
        db.batches.update_completion_flags(
            batch_id,
            raw_to_jpg_complete=True
        )
        
        db.commit()
```

## Integration with Previous Phases

### With Phase 2 (Pipeline Gaps)
Sync ensures batches/images exist before querying gaps:
```python
# Sync first
db.inventory.sync_batch(batch_id)

# Then find gaps
files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
```

### With Phase 5 (Metadata)
Sync populates metadata tables:
```python
# Sync creates batch + image records
db.inventory.sync_batch(batch_id)

# Then add additional metadata
db.images.update_status(image_id, 'completed')
db.batches.update_file_counts(batch_id, file_count_jpg=150)
```

## API Reference

### sync_batch(batch_id, update_existing=False)

Sync single batch from globus_file_index.

**Parameters:**
- `batch_id` (str): Batch to sync
- `update_existing` (bool): Update existing records

**Returns:** dict with keys:
- `batch_existed` (bool): Whether batch existed
- `images_inserted` (int): New images added
- `images_skipped` (int): Existing images skipped
- `files_found` (int): Total files in source
- `raw_files` (int): RAW files found

**Raises:**
- `BatchNotFoundError`: If batch_id not in globus_file_index
- `QueryError`: If sync fails

---

### sync_all(batch_state=None, limit=None, update_existing=False)

Sync all batches from globus_file_index.

**Parameters:**
- `batch_state` (str, optional): Filter by state
- `limit` (int, optional): Max batches to sync
- `update_existing` (bool): Update existing records

**Returns:** dict with keys:
- `batches_synced` (int): Batches successfully synced
- `batches_failed` (int): Batches that failed
- `total_images_inserted` (int): Total images added
- `elapsed_seconds` (float): Time taken

---

### sync_recent(days=7, update_existing=False)

Sync batches modified in last N days.

**Parameters:**
- `days` (int): Days to look back (default: 7)
- `update_existing` (bool): Update existing records

**Returns:** dict with sync statistics

---

### reconcile(batch_id=None)

Compare source and processed tables.

**Parameters:**
- `batch_id` (str, optional): Reconcile specific batch only

**Returns:** dict with keys:
- `missing_batches` (list): In source but not processed
- `missing_images` (int): Images count missing
- `orphaned_batches` (list): In processed but not source
- `orphaned_images` (int): Orphaned images count

---

### get_sync_status()

Get overall synchronization status.

**Returns:** dict with keys:
- `source_batches` (int): Total in globus_file_index
- `processed_batches` (int): Total in processed.batches
- `source_raw_files` (int): RAW files in source
- `processed_images` (int): Images in processed.images
- `sync_percentage` (float): Percentage synced
- `batches_missing` (int): Batches not yet synced
- `images_missing` (int): Images not yet synced

## Testing

```bash
python test_phase6.py
```

Expected output:
```
============================================================
Phase 6 - Inventory Sync Tests
============================================================
Testing InventorySync initialization...
✓ InventorySync initializes correctly

Testing AgirDB.inventory integration...
✓ AgirDB.inventory integration works correctly

============================================================
✓ All Phase 6 unit tests passed!
============================================================

============================================================
DATABASE INTEGRATION TESTS
============================================================
Note: These tests require source.globus_file_index with data.

✓ Database connection successful

Finding test batch in globus_file_index...
✓ Using test batch: MD_2025-01-01

...

✓ All database integration tests passed!
```

## Files Created

```
agir-db/
├── src/agir_db/
│   ├── inventory.py                     # InventorySync class (650 lines)
│   ├── api.py                           # Updated integration
│   └── __init__.py                      # Updated exports
│
└── tests/
    └── test_phase6.py                   # Test suite (450 lines)

Total new code: ~1,100 lines
```

## Status

**Phase 6: COMPLETE ✓**

All inventory sync components are implemented and tested:
- ✓ InventorySync class (5 main methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 7 (Transfer Management)!**