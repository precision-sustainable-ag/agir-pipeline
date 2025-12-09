# AgirDB API - Complete System Overview (Phases 1-9)

## Executive Summary

The AgirDB API provides a comprehensive, production-ready interface for managing agricultural image processing workflows. Built around a "pipeline gaps" methodology, the system tracks files through multiple processing stages while maintaining detailed metadata, event logs, transfer records, and analytics.

**Current Status: 9/10 Phases Complete (90%)**

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    AgirDB Facade                             │
│                   (Main Entry Point)                          │
└─────────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Foundation  │  │  Discovery   │  │  Execution   │
├──────────────┤  ├──────────────┤  ├──────────────┤
│ Connection   │  │ Gaps         │  │ Stages       │
│ Config       │  │ Inventory    │  │ Events       │
│ Exceptions   │  │              │  │              │
└──────────────┘  └──────────────┘  └──────────────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Metadata   │  │   Transfer   │  │  Analytics   │
├──────────────┤  ├──────────────┤  ├──────────────┤
│ Images       │  │ Transfers    │  │ Analytics    │
│ Batches      │  │              │  │ Migration    │
└──────────────┘  └──────────────┘  └──────────────┘
```

---

## Phase-by-Phase Breakdown

### **Phase 1: Foundation** (1,450 lines)

**Purpose:** Core infrastructure for database connectivity and configuration.

**Components:**
- `ConnectionManager` - PostgreSQL connection handling
- Configuration system - Environment variables, defaults
- Exception hierarchy - Custom error types
- Logging setup - Structured logging

**Key Features:**
- Context manager support (`with AgirDB()`)
- Transaction management (commit/rollback)
- Connection pooling
- Configurable via environment or parameters

**Usage:**
```python
from agir_db import AgirDB

with AgirDB() as db:
    # All operations here
    db.commit()  # Or rollback on error
```

**Database Schema:**
- `source` schema - For globus_file_index
- `processed` schema - For pipeline data

---

### **Phase 2: Pipeline Gaps** (1,000 lines)

**Purpose:** "Source of truth" for discovering work - finds missing output files.

**Core Method:**
```python
gaps = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
# Returns batches that need processing
```

**Philosophy:**
- Pipeline gaps (missing files) = work to do
- More reliable than status flags
- Self-correcting when files appear

**Methods (7 total):**
1. `get_batches_with_gaps(stage)` - Find batches needing work
2. `get_files_with_gaps(batch_id, stage)` - Files needing processing
3. `get_gap_summary(stage)` - Statistics
4. `get_all_stages()` - List configured stages
5. `is_stage_complete(batch_id, stage)` - Check completion
6. `get_batch_stage_status(batch_id)` - All stages for batch
7. `count_gaps(stage)` - Total gaps count

**Integration Pattern:**
```python
# Discover work
batches = db.gaps.get_batches_with_gaps('raw_to_jpg')

for batch in batches:
    files = db.gaps.get_files_with_gaps(batch['batch_id'], 'raw_to_jpg')
    # Process these files...
```

---

### **Phase 3: Stage Status** (1,200 lines)

**Purpose:** Track execution of processing stages with timing and metrics.

**Core Workflow:**
```python
# Start stage
db.stages.start(batch_id, 'raw_to_jpg', job_id='12345')
db.commit()

# Process files...

# Complete stage
db.stages.complete(batch_id, 'raw_to_jpg', 
                   success=True, files_processed=150)
db.commit()
```

**Methods (9 total):**
1. `start(batch_id, stage, job_id)` - Start stage execution
2. `complete(batch_id, stage, success, files_processed)` - Mark complete
3. `update_progress(batch_id, stage, files_processed)` - Update metrics
4. `get_by_batch_and_stage(batch_id, stage)` - Get status
5. `get_by_batch(batch_id)` - All stages for batch
6. `get_active()` - Currently running stages
7. `get_recent()` - Recent executions
8. `get_failed()` - Failed stages
9. `cleanup_old_records(days)` - Purge old data

**Status Values:**
- `pending` - Queued but not started
- `running` - Currently executing
- `completed` - Finished successfully
- `failed` - Finished with errors

**Auto-calculated Fields:**
- `duration_seconds` - Calculated on completion
- `files_per_second` - Throughput metric

---

### **Phase 4: Event Logging** (1,550 lines)

**Purpose:** Comprehensive event tracking for debugging and auditing.

**Core Method:**
```python
db.events.log_event(
    event_type='stage.started',
    message='Started processing batch MD_2024-06-01',
    severity='INFO',
    batch_id='MD_2024-06-01',
    stage='raw_to_jpg',
    metadata={'file_count': 150}
)
```

**Event Types:**
- `stage.*` - Stage lifecycle events
- `gap.*` - Pipeline gap queries
- `transfer.*` - Transfer operations
- `error.*` - Error conditions
- `system.*` - System events

**Severity Levels:**
- `DEBUG` - Verbose diagnostics
- `INFO` - Normal operations
- `WARNING` - Potential issues
- `ERROR` - Failures
- `CRITICAL` - Severe problems

**Methods (8 total):**
1. `log_event()` - Create event
2. `log_error()` - Log error with stack trace
3. `get_by_batch()` - Events for batch
4. `get_by_type()` - Events by type
5. `get_by_severity()` - Filter by severity
6. `get_recent()` - Recent events
7. `search()` - Full-text search
8. `cleanup_old_events()` - Purge old data

**Retention:**
- INFO/DEBUG: 7 days default
- WARNING: 30 days
- ERROR/CRITICAL: 90 days
- Configurable per severity

---

### **Phase 5: Image & Batch Metadata** (3,750 lines)

**Purpose:** Rich metadata tracking for batches and individual images.

**Two Main Components:**

#### **BatchMetadata** (11 methods)
```python
# Create batch
db.batches.create(
    batch_id='MD_2024-06-01',
    batch_state='MD',
    batch_date=date(2024, 6, 1),
    location='JUNO',
    file_count_raw=150
)

# Query batches
batches = db.batches.get_by_state('MD')
batch = db.batches.get_by_id('MD_2024-06-01')

# Update completion flags
db.batches.update_completion_flags(
    'MD_2024-06-01',
    raw_to_jpg_complete=True
)
```

**Completion Flags:**
- `raw_to_dng_complete`
- `dng_to_jpg_complete`
- `raw_to_jpg_complete`
- `extracted_cutouts_complete`

#### **ImageMetadata** (12 methods)
```python
# Create image
db.images.create(
    image_id='MD_1234',
    batch_id='MD_2024-06-01',
    file_name='MD_1234.raw',
    file_size_bytes=25000000
)

# Update status
db.images.update_status('MD_1234', 'completed')

# Set EXIF data
db.images.set_exif_data('MD_1234', {
    'camera_make': 'Canon',
    'camera_model': 'EOS R5',
    'width': 8192,
    'height': 5464
})

# Query images
images = db.images.get_by_batch('MD_2024-06-01')
image = db.images.get_by_id('MD_1234')
```

**Image Processing Status:**
- `pending` - Not yet processed
- `processing` - Currently being processed
- `completed` - Successfully processed
- `failed` - Processing failed
- `skipped` - Intentionally skipped

---

### **Phase 6: Inventory Sync** (2,750 lines)

**Purpose:** Synchronize processed tables with Globus file index.

**Core Operations:**
```python
# Sync specific batch
db.inventory.sync_batch('MD_2024-06-01')

# Sync recent batches
db.inventory.sync_recent(days=7)

# Full reconciliation
result = db.inventory.reconcile()
```

**Methods (5 total):**
1. `sync_batch(batch_id)` - Sync one batch
2. `sync_recent(days)` - Sync recent batches
3. `reconcile()` - Full reconciliation
4. `get_sync_status(batch_id)` - Check sync state
5. `get_unsynced_batches()` - Find batches needing sync

**What It Does:**
- Creates missing batch records
- Creates missing image records
- Updates file sizes and paths
- Extracts EXIF data (camera make/model, dimensions)
- Handles multiple file extensions (RAW, ARW, DNG, JPG)

**File Extension Support:**
- RAW/ARW (Sony) - Source files
- DNG - Intermediate format
- JPG - Final output
- PP3/XMP - Sidecar files (handled correctly)

---

### **Phase 7: Transfer Management** (2,750 lines)

**Purpose:** Track Globus transfers between storage locations.

**Transfer Lifecycle:**
```python
# 1. Start transfer
transfer_id = db.transfers.start_transfer(
    batch_id='MD_2024-06-01',
    source_location='JUNO',
    destination_location='CERES',
    file_count=150,
    bytes_total=3750000000
)

# 2. Update with Globus task ID
db.transfers.update_globus_task(transfer_id, 'abc-123-def')

# 3. Update progress
db.transfers.update_progress(
    transfer_id,
    files_transferred=75,
    bytes_transferred=1875000000,
    transfer_rate_mbps=125.5
)

# 4. Mark complete
db.transfers.complete(transfer_id, success=True)
```

**Methods (11 total):**
1. `start_transfer()` - Create transfer record
2. `update_globus_task()` - Store Globus task ID
3. `update_progress()` - Update metrics
4. `complete()` - Mark complete/failed
5. `cancel()` - Cancel transfer
6. `retry()` - Create retry transfer
7. `get_by_id()` - Get single transfer
8. `get_by_batch()` - Transfers for batch
9. `get_active()` - In-progress transfers
10. `get_failed()` - Failed transfers
11. `get_pending()` - Pending transfers

**Transfer Statuses:**
- `pending` - Queued but not started
- `in_progress` - Currently transferring
- `completed` - Finished successfully
- `failed` - Transfer failed
- `cancelled` - User cancelled

**Helper Views:**
- `active_transfers` - Currently running
- `failed_transfers` - Needing retry
- `completed_transfers` - Finished
- `transfer_stats_by_location` - Statistics
- `pending_transfers` - Queue

---

### **Phase 8: Analytics** (2,350 lines)

**Purpose:** Reporting and analytics on pipeline performance.

**16 SQL Views:**

#### **Core Views:**
- `pipeline_overview` - High-level status
- `daily_batch_summary` - Daily volumes
- `daily_throughput` - Daily performance

#### **Stage Analytics:**
- `stage_performance` - Individual executions
- `stage_performance_summary` - Aggregate stats

#### **Error Analysis:**
- `recent_errors` - Recent failures
- `error_summary_by_stage` - Error statistics
- `recent_critical_events` - Critical issues

#### **Transfer Analytics:**
- `transfer_performance` - Individual transfers
- `transfer_summary_by_route` - Route statistics

#### **Storage Analytics:**
- `storage_by_location` - Current storage
- `storage_growth` - Monthly growth

#### **Batch Tracking:**
- `batch_completion_status` - Batch progress

#### **Other:**
- `event_summary` - Event statistics
- `camera_usage_stats` - Camera statistics

**Methods (14 total):**
```python
# High-level status
overview = db.analytics.get_pipeline_overview()

# Processing stats
stats = db.analytics.get_processing_stats(days=7)
throughput = db.analytics.get_throughput(days=30, stage='raw_to_jpg')

# Error monitoring
error_rate = db.analytics.get_error_rate('raw_to_jpg', days=30)
errors = db.analytics.get_error_summary(days=7)
recent = db.analytics.get_recent_errors(limit=10)

# Transfer performance
perf = db.analytics.get_transfer_performance(days=7)
summary = db.analytics.get_transfer_summary_by_route()

# Storage analytics
storage = db.analytics.get_storage_by_location()
growth = db.analytics.get_storage_growth(months=12)

# Batch details
batch = db.analytics.get_batch_summary('MD_2024-06-01')

# Camera stats
cameras = db.analytics.get_camera_stats()
```

**Example Output (pipeline_overview):**
```python
{
    'total_batches': 1250,
    'pending_batches': 45,
    'processing_batches': 12,
    'completed_batches': 1193,
    'total_images': 187500,
    'active_stages': 8,
    'failed_stages_24h': 3,
    'total_storage_gb': 4687.5
}
```

---

### **Phase 9: Migration Tools** (1,800 lines)

**Purpose:** Import data from legacy SQLite databases.

**Core Operation:**
```python
# Import from SQLite
stats = db.migration.import_sqlite_db(
    '/path/to/legacy_batch.db',
    batch_id='MD_2024-06-01',
    dry_run=False,
    skip_existing=True
)

# Returns:
# {
#     'batches_imported': 1,
#     'images_imported': 150,
#     'batches_skipped': 0,
#     'errors': []
# }
```

**Methods (3 total):**
1. `import_sqlite_db()` - Import from SQLite
2. `validate_migration()` - Verify data integrity
3. `get_migration_summary()` - Migration overview

**Features:**
- Automatic schema detection
- Data transformation (legacy → new format)
- Dry run mode for testing
- Bulk insert for efficiency (1000 images at a time)
- Idempotent (skip_existing prevents duplicates)
- Metadata preservation (original data saved)

**Validation:**
```python
result = db.migration.validate_migration('MD_2024-06-01')
# {
#     'valid': True,
#     'batch_exists': True,
#     'image_count': 150,
#     'missing_required_fields': [],
#     'issues': []
# }
```

---

## Complete Workflow Example

Here's how all phases work together in a production pipeline:

```python
from agir_db import AgirDB
from datetime import datetime

def process_pipeline():
    """Complete pipeline workflow using all phases."""
    
    with AgirDB() as db:
        
        # ====================================================
        # PHASE 8: Monitor overall system health
        # ====================================================
        overview = db.analytics.get_pipeline_overview()
        print(f"System Status:")
        print(f"  Total batches: {overview['total_batches']}")
        print(f"  Active stages: {overview['active_stages']}")
        print(f"  Failed stages (24h): {overview['failed_stages_24h']}")
        
        # ====================================================
        # PHASE 6: Sync recent inventory
        # ====================================================
        print("\nSyncing inventory...")
        db.inventory.sync_recent(days=7)
        db.commit()
        
        # ====================================================
        # PHASE 2: Discover work (pipeline gaps)
        # ====================================================
        print("\nFinding work...")
        batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
        print(f"Found {len(batches)} batches needing processing")
        
        for batch in batches:
            batch_id = batch['batch_id']
            print(f"\nProcessing {batch_id}...")
            
            # Get files needing work
            files = db.gaps.get_files_with_gaps(batch_id, 'raw_to_jpg')
            print(f"  {len(files)} files need processing")
            
            # ============================================
            # PHASE 3: Start stage
            # ============================================
            db.stages.start(
                batch_id=batch_id,
                stage='raw_to_jpg',
                job_id='worker-001'
            )
            
            # ============================================
            # PHASE 4: Log start event
            # ============================================
            db.events.log_event(
                event_type='stage.started',
                message=f'Started raw_to_jpg for {batch_id}',
                severity='INFO',
                batch_id=batch_id,
                stage='raw_to_jpg',
                metadata={'file_count': len(files)}
            )
            
            db.commit()
            
            # ============================================
            # YOUR PROCESSING CODE HERE
            # ============================================
            try:
                processed_count = 0
                failed_count = 0
                
                for file in files:
                    try:
                        # Process file (your code)
                        result = process_raw_to_jpg(file['file_path'])
                        
                        # ====================================
                        # PHASE 5: Update image status
                        # ====================================
                        db.images.update_status(
                            file['base_name'],
                            'completed'
                        )
                        processed_count += 1
                        
                    except Exception as e:
                        # ====================================
                        # PHASE 4: Log error
                        # ====================================
                        db.events.log_error(
                            event_type='error.processing',
                            message=f'Failed to process {file["base_name"]}',
                            error=e,
                            batch_id=batch_id,
                            stage='raw_to_jpg'
                        )
                        failed_count += 1
                
                # ============================================
                # PHASE 3: Update progress
                # ============================================
                db.stages.update_progress(
                    batch_id=batch_id,
                    stage='raw_to_jpg',
                    files_processed=processed_count
                )
                
                db.commit()
                
                # ============================================
                # PHASE 3: Complete stage
                # ============================================
                db.stages.complete(
                    batch_id=batch_id,
                    stage='raw_to_jpg',
                    success=(failed_count == 0),
                    files_processed=processed_count,
                    files_failed=failed_count
                )
                
                # ============================================
                # PHASE 5: Update batch completion flags
                # ============================================
                if failed_count == 0:
                    db.batches.update_completion_flags(
                        batch_id,
                        raw_to_jpg_complete=True
                    )
                
                # ============================================
                # PHASE 4: Log completion
                # ============================================
                db.events.log_event(
                    event_type='stage.completed',
                    message=f'Completed raw_to_jpg for {batch_id}',
                    severity='INFO',
                    batch_id=batch_id,
                    stage='raw_to_jpg',
                    metadata={
                        'files_processed': processed_count,
                        'files_failed': failed_count
                    }
                )
                
                db.commit()
                
                # ============================================
                # PHASE 7: Initiate transfer
                # ============================================
                if failed_count == 0:
                    transfer_id = db.transfers.start_transfer(
                        batch_id=batch_id,
                        source_location='JUNO',
                        destination_location='CERES',
                        source_path=f'/juno/data/{batch_id}',
                        destination_path=f'/ceres/data/{batch_id}',
                        file_count=processed_count
                    )
                    db.commit()
                    print(f"  ✓ Transfer initiated: {transfer_id}")
                
            except Exception as e:
                # ============================================
                # Error handling
                # ============================================
                db.rollback()
                
                db.stages.complete(
                    batch_id=batch_id,
                    stage='raw_to_jpg',
                    success=False,
                    error_message=str(e)
                )
                
                db.events.log_error(
                    event_type='error.stage',
                    message=f'Stage failed for {batch_id}',
                    error=e,
                    batch_id=batch_id,
                    stage='raw_to_jpg'
                )
                
                db.commit()
        
        # ====================================================
        # PHASE 8: Generate daily report
        # ====================================================
        print("\nGenerating report...")
        
        stats = db.analytics.get_processing_stats(days=1)
        throughput = db.analytics.get_throughput(days=1)
        errors = db.analytics.get_recent_errors(limit=10)
        
        print(f"\nDaily Statistics:")
        print(f"  Batches processed: {stats['batches_processed']}")
        print(f"  Files processed: {stats['files_processed']}")
        print(f"  GB processed: {stats['total_gb_processed']:.2f}")
        
        if errors:
            print(f"\nRecent Errors: {len(errors)}")
            for error in errors[:5]:
                print(f"  - {error['batch_id']}: {error['error_message']}")

# Run it
process_pipeline()
```

---

## Data Flow Through System

```
1. Files appear on disk
   │
   ▼
2. PHASE 6 (Inventory): Sync from globus_file_index
   │ ├─ Create batch records
   │ └─ Create image records
   │
   ▼
3. PHASE 2 (Gaps): Detect missing outputs
   │ └─ Returns batches needing work
   │
   ▼
4. PHASE 3 (Stages): Start stage execution
   │ └─ Track timing and progress
   │
   ▼
5. PHASE 4 (Events): Log start event
   │
   ▼
6. YOUR CODE: Process files
   │
   ▼
7. PHASE 5 (Metadata): Update image status
   │ └─ Update batch completion flags
   │
   ▼
8. PHASE 3 (Stages): Complete stage
   │ └─ Calculate duration
   │
   ▼
9. PHASE 4 (Events): Log completion
   │
   ▼
10. PHASE 7 (Transfers): Initiate transfer
    │ └─ Track Globus operations
    │
    ▼
11. PHASE 8 (Analytics): Generate reports
    └─ Performance metrics, error rates, etc.
```

---

## Cumulative Statistics

```
Phase 1:  1,450 lines  (Foundation)
Phase 2:  1,000 lines  (Pipeline Gaps)
Phase 3:  1,200 lines  (Stage Status)
Phase 4:  1,550 lines  (Event Logging)
Phase 5:  3,750 lines  (Image & Batch Metadata)
Phase 6:  2,750 lines  (Inventory Sync)
Phase 7:  2,750 lines  (Transfer Management)
Phase 8:  2,350 lines  (Analytics)
Phase 9:  1,800 lines  (Migration Tools)
────────────────────────────────────────
Total:   18,600 lines  (9 phases complete)

Components:
  - 9 Python classes
  - 10 SQL schemas (tables, views, triggers)
  - 50+ helper views and functions
  - 9 comprehensive test suites
  - 27 documentation files
```

---

## What Phase 10 Will Add

**Phase 10: Orchestration Helpers** (~600 lines)

High-level workflow automation:

```python
# Simple: Process single batch end-to-end
db.orchestration.process_batch('MD_2024-06-01', stages=['raw_to_jpg'])

# Run specific stage across multiple batches
db.orchestration.run_stage('raw_to_jpg', limit=10)

# Monitor progress
status = db.orchestration.get_progress()

# Error recovery
db.orchestration.retry_failed_stages(days=1)
```

This will tie all 9 phases together into easy-to-use, production-ready workflows.

---

## Key Design Principles

1. **"Pipeline Gaps" as Source of Truth**
   - Missing output files = work to do
   - More reliable than status flags
   - Self-correcting

2. **Separation of Concerns**
   - Each phase has distinct responsibility
   - Clean interfaces between phases
   - Minimal coupling

3. **Production-Ready**
   - Transaction management
   - Error handling
   - Logging and monitoring
   - Performance optimization

4. **Idempotent Operations**
   - Safe to re-run
   - Handles duplicates gracefully
   - Recovers from failures

5. **Comprehensive Tracking**
   - Every operation logged
   - Detailed metrics
   - Full audit trail

---

## Integration Checklist

When building your processing pipeline, you should:

- ✅ Use `gaps` to discover work (Phase 2)
- ✅ Use `stages` to track execution (Phase 3)
- ✅ Use `events` to log operations (Phase 4)
- ✅ Use `images` and `batches` for metadata (Phase 5)
- ✅ Use `inventory` to sync from file index (Phase 6)
- ✅ Use `transfers` to track Globus operations (Phase 7)
- ✅ Use `analytics` for monitoring (Phase 8)
- ✅ Use `migration` for legacy data (Phase 9)
- ⏳ Use `orchestration` for workflows (Phase 10 - coming next)

---

## Next: Phase 10 (Final Phase!)

Phase 10 will provide the "glue" that ties everything together:
- High-level workflow functions
- Batch queue management
- Parallel processing support
- Error recovery automation
- Progress monitoring
- Production deployment helpers

**After Phase 10, the AgirDB API will be 100% complete!**
