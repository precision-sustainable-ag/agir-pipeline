# Phase 7: Transfer Management - Complete ✓

## Overview

Phase 7 implements tracking and management of Globus file transfers between storage locations (JUNO, CERES, NCSU, etc.). This provides visibility into transfer operations, progress monitoring, and failure handling.

**Why Transfer Management?**
- **Track transfers**: Complete audit trail of all file movements
- **Monitor progress**: Real-time transfer status and metrics
- **Handle failures**: Automatic retry capabilities
- **Globus integration**: Store task IDs for API monitoring
- **Performance metrics**: Transfer rates, durations, statistics

## Components Created

### 1. **SQL Schema** (transfers_schema.sql, ~400 lines)

**Table: `processed.transfers`**

```sql
CREATE TABLE processed.transfers (
    transfer_id SERIAL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    
    -- Locations
    source_location TEXT NOT NULL,      -- 'JUNO', 'CERES', etc.
    destination_location TEXT NOT NULL,
    source_path TEXT,
    destination_path TEXT,
    
    -- Status
    status TEXT,  -- pending, in_progress, completed, failed, cancelled
    
    -- Globus
    globus_task_id TEXT UNIQUE,
    globus_status TEXT,
    
    -- Metrics
    file_count INTEGER,
    files_transferred INTEGER,
    bytes_total BIGINT,
    bytes_transferred BIGINT,
    transfer_rate_mbps NUMERIC,
    
    -- Timing
    requested_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_seconds NUMERIC,  -- Auto-calculated
    
    -- Error handling
    error_message TEXT,
    retry_count INTEGER,
    
    metadata JSONB,
    ...
);
```

**Helper Views:**
- `active_transfers` - Currently running with progress
- `failed_transfers` - Failed transfers needing attention
- `completed_transfers` - Successfully completed with stats
- `transfer_stats_by_location` - Aggregate statistics
- `pending_transfers` - Transfer queue

**Features:**
- 9 indexes for fast queries
- Auto-update timestamps via triggers
- Auto-calculate duration on completion
- Helper functions for progress and cleanup
- Foreign key to processed.batches

### 2. **TransferManager Class** (transfers.py, ~600 lines)

Manage transfer lifecycle:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Start transfer
    transfer_id = db.transfers.start_transfer(
        batch_id='MD_2025-01-01',
        source_location='JUNO',
        destination_location='CERES',
        source_path='/juno/md/MD_2025-01-01',
        destination_path='/ceres/md/MD_2025-01-01',
        file_count=150,
        bytes_total=3750000000
    )
    
    # Update with Globus task ID
    db.transfers.update_globus_task(transfer_id, 'abc-123-def')
    
    # Update progress
    db.transfers.update_progress(
        transfer_id,
        files_transferred=75,
        bytes_transferred=1875000000,
        transfer_rate_mbps=125.5
    )
    
    # Mark as complete
    db.transfers.complete(
        transfer_id,
        success=True,
        files_transferred=150,
        bytes_transferred=3750000000
    )
```

**Main Methods:**

1. **`start_transfer(batch_id, source_location, destination_location, ...)`**
   - Create pending transfer record
   - Returns transfer_id

2. **`update_globus_task(transfer_id, globus_task_id)`**
   - Store Globus task ID
   - Mark as in-progress

3. **`update_progress(transfer_id, files_transferred, bytes_transferred, ...)`**
   - Update transfer metrics
   - Track progress in real-time

4. **`complete(transfer_id, success, ...)`**
   - Mark as completed or failed
   - Record final metrics

5. **`cancel(transfer_id, reason)`**
   - Cancel pending/in-progress transfer

6. **`retry(transfer_id)`**
   - Create new transfer for retry
   - Increments retry_count

7. **`get_by_id(transfer_id)`**
   - Get single transfer

8. **`get_by_batch(batch_id, status)`**
   - Get transfers for batch

9. **`get_active(limit)`**
   - Get in-progress transfers

10. **`get_failed(limit)`**
    - Get failed transfers

11. **`get_pending(limit)`**
    - Get pending transfers

**Valid Statuses:**
- `pending` - Transfer queued but not started
- `in_progress` - Transfer actively running
- `completed` - Transfer completed successfully
- `failed` - Transfer failed
- `cancelled` - Transfer was cancelled

### 3. **Integration with AgirDB**

Transfer management is accessible through the main facade:

```python
from agir_db import AgirDB

with AgirDB() as db:
    db.transfers.start_transfer(...)
    db.transfers.get_active()
```

## Installation

### Step 1: Install SQL Schema

```bash
# Connect to database
source /project/dash_agir/postgres/pg_coords.env
psql

# Run schema file
\i /path/to/transfers_schema.sql

# Verify table exists
\d processed.transfers
\dv processed.*transfer*
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
 ...

 Schema    |           Name              | Type
-----------+-----------------------------+------
 processed | active_transfers            | view
 processed | completed_transfers         | view
 processed | failed_transfers            | view
 processed | pending_transfers           | view
 processed | transfer_stats_by_location  | view
```

### Step 2: Update Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

## Testing

```bash
python test_phase7.py
```

Expected output:
```
✓ All Phase 7 unit tests passed!
✓ All database integration tests passed!
✓ Phase 7 Complete!
```

## Usage Examples

### Example 1: Basic Transfer Workflow

```python
from agir_db import AgirDB

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    # 1. Start transfer
    transfer_id = db.transfers.start_transfer(
        batch_id=batch_id,
        source_location='JUNO',
        destination_location='CERES',
        source_path=f'/juno/md/{batch_id}',
        destination_path=f'/ceres/md/{batch_id}',
        file_count=150,
        bytes_total=3750000000,
        job_id='slurm_12345'
    )
    db.commit()
    
    print(f"Transfer started: {transfer_id}")
    
    # 2. Execute Globus transfer (your code)
    import subprocess
    result = subprocess.run([
        'globus', 'transfer',
        'source_endpoint:/path',
        'dest_endpoint:/path',
        '--label', batch_id
    ], capture_output=True, text=True)
    
    # Extract task ID from Globus output
    globus_task_id = result.stdout.strip()
    
    # 3. Update with Globus task ID
    db.transfers.update_globus_task(transfer_id, globus_task_id)
    db.commit()
    
    print(f"Globus task: {globus_task_id}")
    
    # 4. Monitor progress (polling loop)
    import time
    while True:
        # Check Globus status (your monitoring code)
        status = check_globus_status(globus_task_id)
        
        if status['active']:
            # Update progress
            db.transfers.update_progress(
                transfer_id,
                files_transferred=status['files_done'],
                bytes_transferred=status['bytes_done'],
                transfer_rate_mbps=status['rate']
            )
            db.commit()
            time.sleep(60)  # Check every minute
        else:
            # Transfer complete
            break
    
    # 5. Mark as complete
    db.transfers.complete(
        transfer_id,
        success=status['success'],
        files_transferred=status['files_done'],
        bytes_transferred=status['bytes_done'],
        error_message=status.get('error')
    )
    db.commit()
    
    print(f"Transfer completed: {status['success']}")
```

### Example 2: Query Active Transfers

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get all active transfers
    active = db.transfers.get_active()
    
    print(f"Active Transfers: {len(active)}")
    for t in active:
        print(f"\nTransfer {t['transfer_id']}:")
        print(f"  Batch: {t['batch_id']}")
        print(f"  Route: {t['source_location']} → {t['destination_location']}")
        print(f"  Progress: {t['percent_complete']:.1f}%")
        print(f"  Rate: {t['transfer_rate_mbps']:.1f} MB/s")
        print(f"  Elapsed: {t['elapsed_seconds']:.0f}s")
```

### Example 3: Handle Failed Transfers

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get failed transfers
    failed = db.transfers.get_failed(limit=10)
    
    print(f"Found {len(failed)} failed transfers")
    
    for t in failed:
        print(f"\nFailed Transfer {t['transfer_id']}:")
        print(f"  Batch: {t['batch_id']}")
        print(f"  Error: {t['error_message']}")
        print(f"  Retries: {t['retry_count']}")
        
        # Retry if not too many attempts
        if t['retry_count'] < 3:
            retry_id = db.transfers.retry(t['transfer_id'])
            print(f"  → Created retry: {retry_id}")
        else:
            print(f"  → Too many retries, skipping")
    
    db.commit()
```

### Example 4: Transfer Statistics

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Query statistics view
    query = """
        SELECT * FROM processed.transfer_stats_by_location
        ORDER BY total_transfers DESC;
    """
    
    stats = db._connection.fetch_all(query)
    
    print("Transfer Statistics by Location Pair:")
    print("=" * 80)
    
    for s in stats:
        print(f"\n{s['source_location']} → {s['destination_location']}")
        print(f"  Total transfers: {s['total_transfers']}")
        print(f"  Completed: {s['completed_count']}")
        print(f"  Failed: {s['failed_count']}")
        print(f"  In progress: {s['in_progress_count']}")
        
        if s['total_bytes_transferred']:
            gb = s['total_bytes_transferred'] / (1024**3)
            print(f"  Total transferred: {gb:.2f} GB")
        
        if s['avg_duration_seconds']:
            print(f"  Avg duration: {s['avg_duration_seconds']:.1f}s")
```

### Example 5: Integration with Stage Processing

```python
from agir_db import AgirDB

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    # 1. Complete processing on JUNO
    db.stages.complete(batch_id, 'raw_to_jpg', success=True)
    db.commit()
    
    # 2. Initiate transfer to CERES
    transfer_id = db.transfers.start_transfer(
        batch_id=batch_id,
        source_location='JUNO',
        destination_location='CERES',
        source_path=f'/juno/processed/{batch_id}',
        destination_path=f'/ceres/archive/{batch_id}'
    )
    db.commit()
    
    # 3. Log event
    db.events.log_event(
        event_type='transfer.initiated',
        severity='INFO',
        message=f'Transfer initiated for {batch_id}',
        batch_id=batch_id,
        metadata={'transfer_id': transfer_id}
    )
    db.commit()
    
    # 4. Execute transfer...
    # (transfer code here)
    
    # 5. Update batch metadata
    db.batches.update_status(batch_id, 'completed')
    db.commit()
```

### Example 6: Monitoring Dashboard

```python
from agir_db import AgirDB
import time

def print_dashboard(db):
    """Print transfer monitoring dashboard."""
    # Active transfers
    active = db.transfers.get_active()
    pending = db.transfers.get_pending()
    failed = db.transfers.get_failed(limit=5)
    
    print("\n" + "="*80)
    print("TRANSFER DASHBOARD")
    print("="*80)
    
    print(f"\n📊 Overview:")
    print(f"  Active: {len(active)}")
    print(f"  Pending: {len(pending)}")
    print(f"  Failed (recent): {len(failed)}")
    
    if active:
        print(f"\n🔄 Active Transfers:")
        for t in active:
            print(f"  {t['batch_id']}: {t['percent_complete']:.1f}% @ {t['transfer_rate_mbps']:.1f} MB/s")
    
    if pending:
        print(f"\n⏳ Pending Queue:")
        for t in pending[:5]:  # First 5
            print(f"  {t['batch_id']} ({t['source_location']} → {t['destination_location']})")
    
    if failed:
        print(f"\n❌ Recent Failures:")
        for t in failed:
            print(f"  {t['batch_id']}: {t['error_message']}")

# Monitor every 60 seconds
with AgirDB() as db:
    while True:
        print_dashboard(db)
        time.sleep(60)
```

## Integration with Previous Phases

### With Phase 5 (Metadata)
Transfers reference batches:
```python
# Batch must exist first
db.batches.insert(batch_id='MD_2025-01-01', ...)
db.transfers.start_transfer(batch_id='MD_2025-01-01', ...)
```

### With Phase 4 (Event Logging)
Log transfer events:
```python
db.transfers.start_transfer(...)
db.events.log_event(
    event_type='transfer.started',
    message=f'Transfer started for {batch_id}'
)
```

### With Phase 3 (Stage Status)
Transfer after processing:
```python
db.stages.complete(batch_id, stage, success=True)
if stage == 'final_stage':
    db.transfers.start_transfer(batch_id, ...)
```

## API Reference

### start_transfer(batch_id, source_location, destination_location, ...)
Create pending transfer record. Returns transfer_id.

### update_globus_task(transfer_id, globus_task_id)
Store Globus task ID and mark as in-progress.

### update_progress(transfer_id, files_transferred, bytes_transferred, ...)
Update transfer metrics.

### complete(transfer_id, success, ...)
Mark as completed or failed.

### cancel(transfer_id, reason)
Cancel pending/in-progress transfer.

### retry(transfer_id)
Create new transfer for retry. Returns new transfer_id.

### get_by_id(transfer_id)
Get single transfer record.

### get_by_batch(batch_id, status=None)
Get transfers for batch, optionally filtered by status.

### get_active(limit=None)
Get in-progress transfers with progress metrics.

### get_failed(limit=None)
Get failed transfers needing attention.

### get_pending(limit=None)
Get pending transfers in queue.

## Files Created

```
agir-db/
├── sql/schemas/03_processed/
│   └── transfers_schema.sql             # Table, views, functions (400 lines)
│
├── src/agir_db/
│   ├── transfers.py                     # TransferManager class (600 lines)
│   ├── api.py                           # Updated integration
│   └── __init__.py                      # Updated exports
│
└── tests/
    └── test_phase7.py                   # Test suite (500 lines)

Total new code: ~1,500 lines
```

## Status

**Phase 7: COMPLETE ✓**

All transfer management components are implemented and tested:
- ✓ SQL schema (table, 5 views, triggers, functions)
- ✓ TransferManager class (11 main methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 8 (Analytics)!**