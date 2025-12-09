# AgirDB API - Quick Reference Guide

## One-Page Cheat Sheet

```python
from agir_db import AgirDB

with AgirDB() as db:
    # PHASE 2: DISCOVER WORK
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    files = db.gaps.get_files_with_gaps('MD_2024-06-01', 'raw_to_jpg')
    
    # PHASE 3: TRACK EXECUTION
    db.stages.start('MD_2024-06-01', 'raw_to_jpg', job_id='worker-1')
    db.stages.update_progress('MD_2024-06-01', 'raw_to_jpg', files_processed=75)
    db.stages.complete('MD_2024-06-01', 'raw_to_jpg', success=True, files_processed=150)
    
    # PHASE 4: LOG EVENTS
    db.events.log_event('stage.started', 'Processing started', batch_id='MD_2024-06-01')
    db.events.log_error('error.processing', 'Failed', error=e)
    
    # PHASE 5: MANAGE METADATA
    db.batches.create('MD_2024-06-01', 'MD', date(2024,6,1), location='JUNO')
    db.batches.update_completion_flags('MD_2024-06-01', raw_to_jpg_complete=True)
    db.images.create('MD_1234', 'MD_2024-06-01', 'MD_1234.raw')
    db.images.update_status('MD_1234', 'completed')
    
    # PHASE 6: SYNC INVENTORY
    db.inventory.sync_batch('MD_2024-06-01')
    db.inventory.sync_recent(days=7)
    
    # PHASE 7: TRACK TRANSFERS
    tid = db.transfers.start_transfer('MD_2024-06-01', 'JUNO', 'CERES')
    db.transfers.update_progress(tid, files_transferred=75)
    db.transfers.complete(tid, success=True)
    
    # PHASE 8: ANALYTICS
    overview = db.analytics.get_pipeline_overview()
    stats = db.analytics.get_processing_stats(days=7)
    errors = db.analytics.get_recent_errors(limit=10)
    
    # PHASE 9: MIGRATION
    stats = db.migration.import_sqlite_db('/path/to/legacy.db')
    result = db.migration.validate_migration('MD_2024-06-01')
    
    db.commit()
```

---

## Phase-by-Phase Method Reference

### Phase 2: Pipeline Gaps (Discovery)

| Method | Purpose | Example |
|--------|---------|---------|
| `get_batches_with_gaps(stage, limit)` | Find batches needing work | `db.gaps.get_batches_with_gaps('raw_to_jpg', 10)` |
| `get_files_with_gaps(batch_id, stage)` | Files to process | `db.gaps.get_files_with_gaps('MD_2024-06-01', 'raw_to_jpg')` |
| `get_gap_summary(stage)` | Stage statistics | `db.gaps.get_gap_summary('raw_to_jpg')` |
| `is_stage_complete(batch_id, stage)` | Check completion | `db.gaps.is_stage_complete('MD_2024-06-01', 'raw_to_jpg')` |
| `get_batch_stage_status(batch_id)` | All stages | `db.gaps.get_batch_stage_status('MD_2024-06-01')` |
| `count_gaps(stage)` | Total gaps | `db.gaps.count_gaps('raw_to_jpg')` |
| `get_all_stages()` | List stages | `db.gaps.get_all_stages()` |

### Phase 3: Stage Status (Execution)

| Method | Purpose | Example |
|--------|---------|---------|
| `start(batch_id, stage, job_id)` | Start stage | `db.stages.start('MD_2024-06-01', 'raw_to_jpg', 'worker-1')` |
| `complete(batch_id, stage, success, ...)` | Finish stage | `db.stages.complete('MD_2024-06-01', 'raw_to_jpg', True, files_processed=150)` |
| `update_progress(batch_id, stage, count)` | Update metrics | `db.stages.update_progress('MD_2024-06-01', 'raw_to_jpg', 75)` |
| `get_by_batch_and_stage(batch_id, stage)` | Get status | `db.stages.get_by_batch_and_stage('MD_2024-06-01', 'raw_to_jpg')` |
| `get_by_batch(batch_id)` | All stages | `db.stages.get_by_batch('MD_2024-06-01')` |
| `get_active()` | Running stages | `db.stages.get_active()` |
| `get_recent(limit, stage)` | Recent history | `db.stages.get_recent(10)` |
| `get_failed(days, stage)` | Failed stages | `db.stages.get_failed(7)` |
| `cleanup_old_records(days)` | Purge old data | `db.stages.cleanup_old_records(90)` |

### Phase 4: Event Logging (Audit Trail)

| Method | Purpose | Example |
|--------|---------|---------|
| `log_event(type, message, severity, ...)` | Log event | `db.events.log_event('stage.started', 'Started', batch_id='MD_2024-06-01')` |
| `log_error(type, message, error, ...)` | Log error | `db.events.log_error('error.processing', 'Failed', error=e)` |
| `get_by_batch(batch_id, severity, limit)` | Batch events | `db.events.get_by_batch('MD_2024-06-01')` |
| `get_by_type(type, limit)` | Type filter | `db.events.get_by_type('stage.started', 100)` |
| `get_by_severity(severity, limit)` | Severity filter | `db.events.get_by_severity('ERROR', 50)` |
| `get_recent(limit, severity)` | Recent events | `db.events.get_recent(100)` |
| `search(query, limit)` | Full-text search | `db.events.search('timeout', 50)` |
| `cleanup_old_events(days, severity)` | Purge old | `db.events.cleanup_old_events(30, 'INFO')` |

### Phase 5: Metadata (Batches)

| Method | Purpose | Example |
|--------|---------|---------|
| `create(batch_id, state, date, location, ...)` | Create batch | `db.batches.create('MD_2024-06-01', 'MD', date(2024,6,1), 'JUNO')` |
| `get_by_id(batch_id)` | Get batch | `db.batches.get_by_id('MD_2024-06-01')` |
| `get_by_state(state, limit)` | Filter by state | `db.batches.get_by_state('MD', 10)` |
| `get_by_location(location, limit)` | Filter location | `db.batches.get_by_location('JUNO', 10)` |
| `get_by_date_range(start, end)` | Date range | `db.batches.get_by_date_range(date(2024,6,1), date(2024,6,30))` |
| `get_pending()` | Pending batches | `db.batches.get_pending()` |
| `get_processing()` | Processing | `db.batches.get_processing()` |
| `get_completed()` | Completed | `db.batches.get_completed()` |
| `update(batch_id, **kwargs)` | Update batch | `db.batches.update('MD_2024-06-01', file_count_raw=150)` |
| `update_completion_flags(batch_id, **flags)` | Set flags | `db.batches.update_completion_flags('MD_2024-06-01', raw_to_jpg_complete=True)` |
| `delete(batch_id)` | Delete batch | `db.batches.delete('MD_2024-06-01')` |

### Phase 5: Metadata (Images)

| Method | Purpose | Example |
|--------|---------|---------|
| `create(image_id, batch_id, file_name, ...)` | Create image | `db.images.create('MD_1234', 'MD_2024-06-01', 'MD_1234.raw')` |
| `get_by_id(image_id)` | Get image | `db.images.get_by_id('MD_1234')` |
| `get_by_batch(batch_id, limit)` | Batch images | `db.images.get_by_batch('MD_2024-06-01')` |
| `get_by_status(status, limit)` | Status filter | `db.images.get_by_status('pending', 100)` |
| `update_status(image_id, status)` | Update status | `db.images.update_status('MD_1234', 'completed')` |
| `set_exif_data(image_id, data)` | Set EXIF | `db.images.set_exif_data('MD_1234', {'camera_make': 'Canon'})` |
| `set_metadata(image_id, data)` | Set metadata | `db.images.set_metadata('MD_1234', {'custom': 'data'})` |
| `update(image_id, **kwargs)` | Update image | `db.images.update('MD_1234', file_size_bytes=25000000)` |
| `bulk_update_status(image_ids, status)` | Bulk update | `db.images.bulk_update_status(['MD_1234', 'MD_1235'], 'completed')` |
| `get_pending_by_batch(batch_id)` | Pending images | `db.images.get_pending_by_batch('MD_2024-06-01')` |
| `get_failed_by_batch(batch_id)` | Failed images | `db.images.get_failed_by_batch('MD_2024-06-01')` |
| `delete(image_id)` | Delete image | `db.images.delete('MD_1234')` |

### Phase 6: Inventory Sync

| Method | Purpose | Example |
|--------|---------|---------|
| `sync_batch(batch_id)` | Sync one batch | `db.inventory.sync_batch('MD_2024-06-01')` |
| `sync_recent(days)` | Sync recent | `db.inventory.sync_recent(7)` |
| `reconcile()` | Full reconcile | `db.inventory.reconcile()` |
| `get_sync_status(batch_id)` | Check status | `db.inventory.get_sync_status('MD_2024-06-01')` |
| `get_unsynced_batches(limit)` | Need sync | `db.inventory.get_unsynced_batches(10)` |

### Phase 7: Transfer Management

| Method | Purpose | Example |
|--------|---------|---------|
| `start_transfer(batch_id, src, dst, ...)` | Start transfer | `db.transfers.start_transfer('MD_2024-06-01', 'JUNO', 'CERES')` |
| `update_globus_task(id, task_id)` | Set Globus ID | `db.transfers.update_globus_task(1, 'abc-123-def')` |
| `update_progress(id, files, bytes, rate)` | Update metrics | `db.transfers.update_progress(1, files_transferred=75)` |
| `complete(id, success)` | Finish transfer | `db.transfers.complete(1, success=True)` |
| `cancel(id)` | Cancel transfer | `db.transfers.cancel(1)` |
| `retry(id)` | Create retry | `db.transfers.retry(1)` |
| `get_by_id(id)` | Get transfer | `db.transfers.get_by_id(1)` |
| `get_by_batch(batch_id)` | Batch transfers | `db.transfers.get_by_batch('MD_2024-06-01')` |
| `get_active()` | Running | `db.transfers.get_active()` |
| `get_failed()` | Failed | `db.transfers.get_failed()` |
| `get_pending()` | Pending | `db.transfers.get_pending()` |

### Phase 8: Analytics

| Method | Purpose | Example |
|--------|---------|---------|
| `get_pipeline_overview()` | High-level status | `db.analytics.get_pipeline_overview()` |
| `get_processing_stats(days)` | Processing metrics | `db.analytics.get_processing_stats(7)` |
| `get_daily_volumes(days, state)` | Daily volumes | `db.analytics.get_daily_volumes(30)` |
| `get_throughput(days, stage)` | Throughput | `db.analytics.get_throughput(30, 'raw_to_jpg')` |
| `get_stage_performance(stage)` | Stage stats | `db.analytics.get_stage_performance('raw_to_jpg')` |
| `get_error_summary(stage, days)` | Error summary | `db.analytics.get_error_summary(days=7)` |
| `get_recent_errors(limit)` | Recent errors | `db.analytics.get_recent_errors(10)` |
| `get_error_rate(stage, days)` | Error rate | `db.analytics.get_error_rate('raw_to_jpg', 30)` |
| `get_transfer_performance(days, ...)` | Transfer metrics | `db.analytics.get_transfer_performance(7)` |
| `get_transfer_summary_by_route()` | Route stats | `db.analytics.get_transfer_summary_by_route()` |
| `get_storage_by_location()` | Storage usage | `db.analytics.get_storage_by_location()` |
| `get_storage_growth(months, state)` | Growth trends | `db.analytics.get_storage_growth(12)` |
| `get_batch_summary(batch_id)` | Batch details | `db.analytics.get_batch_summary('MD_2024-06-01')` |
| `get_camera_stats()` | Camera usage | `db.analytics.get_camera_stats()` |

### Phase 9: Migration

| Method | Purpose | Example |
|--------|---------|---------|
| `import_sqlite_db(path, batch_id, ...)` | Import SQLite | `db.migration.import_sqlite_db('/data/legacy.db')` |
| `validate_migration(batch_id)` | Validate data | `db.migration.validate_migration('MD_2024-06-01')` |
| `get_migration_summary()` | Migration stats | `db.migration.get_migration_summary()` |

---

## Common Workflows

### Daily Processing Workflow

```python
with AgirDB() as db:
    # 1. Sync inventory
    db.inventory.sync_recent(days=1)
    db.commit()
    
    # 2. Find work
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    # 3. Process each batch
    for batch in batches:
        db.stages.start(batch['batch_id'], 'raw_to_jpg', job_id='worker-1')
        db.commit()
        
        # Your processing...
        
        db.stages.complete(batch['batch_id'], 'raw_to_jpg', success=True)
        db.commit()
    
    # 4. Check results
    stats = db.analytics.get_processing_stats(days=1)
```

### Error Recovery Workflow

```python
with AgirDB() as db:
    # Find failed stages
    failed = db.stages.get_failed(days=7)
    
    for stage in failed:
        # Check errors
        errors = db.events.get_by_batch(stage['batch_id'], severity='ERROR')
        
        # Retry if transient error
        if is_transient_error(errors):
            # Restart stage
            db.stages.start(stage['batch_id'], stage['stage'], job_id='retry-worker')
            # Process...
```

### Transfer Monitoring Workflow

```python
with AgirDB() as db:
    # Check active transfers
    active = db.transfers.get_active()
    
    for transfer in active:
        # Update progress (from Globus API)
        progress = get_globus_progress(transfer['globus_task_id'])
        db.transfers.update_progress(
            transfer['transfer_id'],
            files_transferred=progress['files'],
            bytes_transferred=progress['bytes']
        )
    
    db.commit()
    
    # Handle failed transfers
    failed = db.transfers.get_failed()
    for transfer in failed:
        # Create retry
        db.transfers.retry(transfer['transfer_id'])
    
    db.commit()
```

### Daily Report Workflow

```python
with AgirDB() as db:
    # Get overview
    overview = db.analytics.get_pipeline_overview()
    
    # Get daily stats
    stats = db.analytics.get_processing_stats(days=1)
    throughput = db.analytics.get_throughput(days=1)
    errors = db.analytics.get_recent_errors(limit=10)
    
    # Generate report
    print(f"Daily Report - {date.today()}")
    print(f"  Batches: {stats['batches_processed']}")
    print(f"  Files: {stats['files_processed']}")
    print(f"  GB: {stats['total_gb_processed']:.2f}")
    print(f"  Errors: {len(errors)}")
```

---

## Status Values Reference

### Stage Status
- `pending` - Queued but not started
- `running` - Currently executing
- `completed` - Finished successfully
- `failed` - Finished with errors

### Image Processing Status
- `pending` - Not yet processed
- `processing` - Currently being processed
- `completed` - Successfully processed
- `failed` - Processing failed
- `skipped` - Intentionally skipped

### Transfer Status
- `pending` - Queued but not started
- `in_progress` - Currently transferring
- `completed` - Finished successfully
- `failed` - Transfer failed
- `cancelled` - User cancelled

### Event Severity
- `DEBUG` - Verbose diagnostics
- `INFO` - Normal operations
- `WARNING` - Potential issues
- `ERROR` - Failures
- `CRITICAL` - Severe problems

### Batch Processing Status
- `pending` - Not yet processed
- `processing` - Currently processing
- `completed` - All stages complete
- `failed` - Processing failed

---

## Database Schema Quick Reference

### Tables

**source schema:**
- `globus_file_index` - File inventory from Globus

**processed schema:**
- `batches` - Batch metadata
- `images` - Image metadata
- `stage_status` - Stage execution tracking
- `events` - Event logs
- `transfers` - Transfer tracking

### Key Columns

**batches:**
- `batch_id` (PK)
- `batch_state` (MD, TX, etc.)
- `batch_date`
- `location` (JUNO, CERES, etc.)
- Completion flags: `raw_to_dng_complete`, `dng_to_jpg_complete`, etc.

**images:**
- `image_id` (PK)
- `batch_id` (FK)
- `file_name`
- `processing_status`
- `exif_data` (JSONB)

**stage_status:**
- `batch_id` (PK with stage)
- `stage` (PK with batch_id)
- `status`
- `started_at`, `completed_at`
- `duration_seconds` (auto-calculated)
- `files_processed`, `files_failed`

**events:**
- `event_id` (PK)
- `event_type`
- `severity`
- `batch_id`, `stage`
- `message`
- `created_at`

**transfers:**
- `transfer_id` (PK)
- `batch_id` (FK)
- `source_location`, `destination_location`
- `status`
- `globus_task_id`
- `files_transferred`, `bytes_transferred`

---

## Environment Variables

```bash
# Required
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agir
export PGUSER=your_user
export PGPASSWORD=your_password

# Optional
export AGIR_LOG_LEVEL=INFO
export AGIR_LOG_FILE=/var/log/agir-db.log
```

---

## Complete Example Script

```python
#!/usr/bin/env python3
"""
Complete pipeline processing script using all AgirDB phases.
"""

from agir_db import AgirDB
from datetime import date
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

def process_pipeline():
    """Process batches through raw_to_jpg stage."""
    
    with AgirDB() as db:
        # Phase 8: Check system health
        overview = db.analytics.get_pipeline_overview()
        logging.info(f"Pipeline overview: {overview['total_batches']} batches")
        
        # Phase 6: Sync recent inventory
        db.inventory.sync_recent(days=7)
        db.commit()
        
        # Phase 2: Find work
        batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
        logging.info(f"Found {len(batches)} batches to process")
        
        for batch in batches:
            batch_id = batch['batch_id']
            logging.info(f"Processing {batch_id}")
            
            try:
                # Phase 3: Start stage
                db.stages.start(batch_id, 'raw_to_jpg', job_id='worker-1')
                
                # Phase 4: Log event
                db.events.log_event(
                    'stage.started',
                    f'Started {batch_id}',
                    batch_id=batch_id
                )
                db.commit()
                
                # Phase 2: Get files
                files = db.gaps.get_files_with_gaps(batch_id, 'raw_to_jpg')
                
                # YOUR PROCESSING CODE HERE
                count = process_files(files)
                
                # Phase 5: Update metadata
                db.batches.update_completion_flags(
                    batch_id,
                    raw_to_jpg_complete=True
                )
                
                # Phase 3: Complete stage
                db.stages.complete(
                    batch_id,
                    'raw_to_jpg',
                    success=True,
                    files_processed=count
                )
                
                # Phase 4: Log completion
                db.events.log_event(
                    'stage.completed',
                    f'Completed {batch_id}',
                    batch_id=batch_id
                )
                db.commit()
                
                # Phase 7: Start transfer
                transfer_id = db.transfers.start_transfer(
                    batch_id,
                    'JUNO',
                    'CERES'
                )
                db.commit()
                
                logging.info(f"✓ Completed {batch_id}")
                
            except Exception as e:
                logging.error(f"✗ Failed {batch_id}: {e}")
                db.rollback()
                
                # Phase 3: Mark failed
                db.stages.complete(batch_id, 'raw_to_jpg', success=False)
                
                # Phase 4: Log error
                db.events.log_error('error.stage', str(e), error=e, batch_id=batch_id)
                db.commit()
        
        # Phase 8: Generate report
        stats = db.analytics.get_processing_stats(days=1)
        logging.info(f"Daily stats: {stats}")

if __name__ == '__main__':
    process_pipeline()
```

---

## Getting Help

1. **View source code:** All classes have comprehensive docstrings
2. **Run tests:** Each phase has a test suite showing usage
3. **Read documentation:** 27 documentation files with examples
4. **Check SYSTEM_OVERVIEW.md:** Complete architecture guide

**Next:** Phase 10 (Orchestration Helpers) will provide even simpler high-level workflows!
