# Phase 6: Inventory Sync - Implementation Summary

## Status: COMPLETE ✓

Phase 6 implements automated synchronization from `source.globus_file_index` to the `processed.batches` and `processed.images` tables, eliminating manual data entry and ensuring metadata stays current.

---

## Files Created (All in /mnt/user-data/outputs/)

### 1. **inventory.py** (~650 lines)
InventorySync class with 5 main methods:
- `sync_batch()` - Sync single batch from globus_file_index
- `sync_all()` - Sync all batches (with filtering)
- `sync_recent()` - Sync batches modified in last N days
- `reconcile()` - Compare source vs processed tables
- `get_sync_status()` - Overall sync statistics

### 2. **Updated api.py**
- Imported InventorySync
- Uncommented `self.inventory`
- Now accessible via `db.inventory`

### 3. **Updated __init__.py**
- Added InventorySync to imports
- Added to __all__ list
- Now exportable: `from agir_db import InventorySync`

### 4. **test_phase6.py** (~450 lines)
Comprehensive test suite:
- Unit tests (no database required)
- Database integration tests (9 test scenarios)
- Tests sync_batch, sync_all, sync_recent, reconcile, get_sync_status
- Handles missing data gracefully

### 5. **PHASE6_README.md** (~900 lines)
Complete documentation:
- Component overview
- 6 detailed usage examples
- Integration patterns
- API reference
- Testing instructions

### 6. **INSTALL_PHASE6.md** (~250 lines)
Installation guide:
- Prerequisites verification
- Initial sync instructions
- Daily sync cron setup
- Troubleshooting
- Verification steps

---

## Total Code Added

```
Python:     ~650 lines (InventorySync class)
Tests:      ~450 lines (unit + integration)
Docs:     ~1,150 lines (README + install)
────────────────────────────
Total:    ~2,250 lines
```

---

## Key Features

### 1. **Automated Population**

```python
# Sync one batch
stats = db.inventory.sync_batch('MD_2025-01-01')
print(f"Synced {stats['images_inserted']} images")

# Batch + images created automatically from globus_file_index
```

### 2. **Bulk Synchronization**

```python
# Sync all MD batches
stats = db.inventory.sync_all(batch_state='MD', limit=100)
print(f"Synced {stats['batches_synced']} batches")
```

### 3. **Incremental Updates**

```python
# Sync recent changes (for cron jobs)
stats = db.inventory.sync_recent(days=7)
```

### 4. **Reconciliation**

```python
# Find differences
results = db.inventory.reconcile()
print(f"Missing: {len(results['missing_batches'])} batches")
print(f"Orphaned: {len(results['orphaned_batches'])} batches")
```

### 5. **Status Tracking**

```python
# Check sync progress
status = db.inventory.get_sync_status()
print(f"Sync: {status['sync_percentage']:.1f}%")
print(f"Missing: {status['batches_missing']} batches")
```

---

## Data Flow

```
source.globus_file_index (existing)
         │
         ├─ Batch Metadata ──────> processed.batches
         │  (batch_id, state,      (new records)
         │   date, location)
         │
         └─ RAW Files ───────────> processed.images
            (base_name, size,      (new records)
             path)
```

**Sync Logic:**
1. Query globus_file_index for batch/file data
2. Aggregate statistics (file counts, total bytes)
3. Insert into processed tables (ON CONFLICT DO NOTHING)
4. Return statistics (inserted, skipped, found)

---

## Usage Pattern

```python
from agir_db import AgirDB

with AgirDB() as db:
    # 1. Check current sync status
    status = db.inventory.get_sync_status()
    print(f"Sync: {status['sync_percentage']:.1f}%")
    
    # 2. Sync recent batches
    stats = db.inventory.sync_recent(days=7)
    db.commit()
    print(f"Synced {stats['batches_synced']} batches")
    
    # 3. Find work using gaps (Phase 2)
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
    
    # 4. Process batches...
    for batch in batches:
        # Ensure batch is synced
        if not db.batches.get_by_id(batch['batch_id']):
            db.inventory.sync_batch(batch['batch_id'])
        
        # Process...
```

---

## Integration Points

### With Phase 2 (Pipeline Gaps)
Sync before querying gaps:
```python
db.inventory.sync_batch(batch_id)
files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
```

### With Phase 5 (Metadata)
Sync populates base records, then add details:
```python
db.inventory.sync_batch(batch_id)  # Creates batch + images
db.images.update_status(image_id, 'completed')  # Add details
```

### With Phase 4 (Event Logging)
Log sync operations:
```python
stats = db.inventory.sync_all(limit=100)
db.events.log_event(
    event_type='inventory.sync_completed',
    message=f"Synced {stats['batches_synced']} batches",
    metadata=stats
)
```

---

## Common Workflows

### Initial Full Sync

```python
# Sync in chunks for safety
chunk_size = 100
while True:
    stats = db.inventory.sync_all(limit=chunk_size)
    db.commit()
    if stats['batches_synced'] < chunk_size:
        break  # Done
```

### Daily Incremental Sync (Cron Job)

```bash
#!/usr/bin/env python3
# /path/to/daily_sync.py

from agir_db import AgirDB

with AgirDB() as db:
    stats = db.inventory.sync_recent(days=7)
    db.commit()
    print(f"Synced {stats['batches_synced']} batches")
```

```bash
# crontab
0 2 * * * /path/to/daily_sync.py >> /logs/sync.log 2>&1
```

### Find and Fix Missing Data

```python
# Reconcile
results = db.inventory.reconcile()

# Sync missing batches
for batch_id in results['missing_batches']:
    db.inventory.sync_batch(batch_id)
    db.commit()
```

---

## API Reference Summary

| Method | Purpose | Returns |
|--------|---------|---------|
| `sync_batch(batch_id)` | Sync one batch | dict with insert stats |
| `sync_all(state, limit)` | Sync multiple batches | dict with overall stats |
| `sync_recent(days)` | Sync recent changes | dict with sync stats |
| `reconcile(batch_id)` | Find differences | dict with missing/orphaned |
| `get_sync_status()` | Overall progress | dict with percentages |

---

## Installation Steps

1. **Verify prerequisites:**
   ```bash
   psql -c "SELECT COUNT(*) FROM source.globus_file_index;"
   psql -c "\d processed.batches"
   ```

2. **Install package:**
   ```bash
   pip install -e .
   ```

3. **Run tests:**
   ```bash
   python test_phase6.py
   ```

4. **Initial sync:**
   ```python
   with AgirDB() as db:
       stats = db.inventory.sync_all(limit=10)
       db.commit()
   ```

---

## What's Next: Phase 7 (Transfer Management)

Phase 7 will implement Globus transfer tracking:

1. **TransferManager Class** - transfers.py
   - `start_transfer()` - Initiate Globus transfer
   - `get_status()` - Check transfer status
   - `get_active()` - List active transfers
   - `get_failed()` - Find failed transfers

2. **SQL Table:** `processed.transfers`
   - Track JUNO → CERES transfers
   - Store Globus task IDs
   - Monitor progress and completion

---

## Phase Status

✓ **Phase 1**: Foundation
✓ **Phase 2**: Pipeline Gaps
✓ **Phase 3**: Stage Status
✓ **Phase 4**: Event Logging
✓ **Phase 5**: Image & Batch Metadata
✓ **Phase 6**: Inventory Sync ← **YOU ARE HERE**
☐ **Phase 7**: Transfer Management
☐ **Phase 8**: Analytics
☐ **Phase 9**: Migration Tools
☐ **Phase 10**: Orchestration Helpers

**60% Complete! Ready for Phase 7!**
