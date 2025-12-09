# Phase 4: Event Logging - Implementation Summary

## Status: COMPLETE ✓

Phase 4 adds comprehensive event logging for complete audit trail and monitoring.

---

## Files Created (All in /mnt/user-data/outputs/)

### 1. **events_schema.sql** (~350 lines)
SQL schema for event logging:
- Table: `processed.events` with full event tracking
- Views: `recent_events`, `error_events`, `stage_events`, `warning_events`, `event_summary_24h`, `batch_event_summary`
- Indexes: 9 indexes for fast queries (time, type, severity, batch, stage, job, full-text search, JSONB)
- Functions: `cleanup_old_events()` for retention, `create_events_partition()` for partitioning
- Features: Full-text search, JSONB metadata, structured event types

### 2. **events.py** (~700 lines)
EventLogger class for logging and querying:
- `log_event()` - Record any event with metadata
- `get_events()` - Flexible filtering (type, severity, batch, stage, time)
- `get_recent_events()` - Time-based queries
- `get_errors()` - Find error/critical events
- `get_batch_events()` - All events for batch
- `get_stage_events()` - Stage operation events
- `get_event_summary()` - Aggregate counts
- `search_events()` - Full-text search

### 3. **Updated api.py**
- Imported EventLogger class
- Uncommented `self.events = EventLogger(self._connection)`
- Now accessible via `db.events`

### 4. **Updated __init__.py**
- Added EventLogger to imports
- Added EventLogger to __all__ list
- Now exportable: `from agir_db import EventLogger`

### 5. **test_phase4.py** (~500 lines)
Comprehensive test suite:
- Unit tests (no database required)
  - Valid severities/event types
  - Initialization and validation
  - Method signatures
  - Integration with AgirDB
- Database integration tests (with database)
  - Logging INFO and ERROR events
  - Querying with filters
  - Batch/stage event queries
  - Error queries
  - Full-text search
  - Event summary

### 6. **PHASE4_README.md** (~900 lines)
Complete documentation including:
- Component overview
- Installation instructions
- Usage examples (7 detailed examples)
- Event types and severity levels
- Integration with previous phases
- API reference
- Retention and maintenance
- Next steps (Phase 5)

### 7. **INSTALL_PHASE4.md** (~250 lines)
Installation guide with:
- Quick install steps
- Test queries
- Usage verification
- Troubleshooting
- Maintenance tips

---

## Total Code Added

```
SQL:        ~350 lines (schema, views, indexes, functions)
Python:     ~700 lines (EventLogger class)
Tests:      ~500 lines (unit + integration)
Docs:     ~1,150 lines (README + install guide)
────────────────────────────
Total:    ~2,700 lines
```

---

## Key Features

### 1. **Comprehensive Logging**
```python
db.events.log_event(
    event_type='stage.started',
    severity='INFO',
    message='Started processing',
    batch_id='MD_2025-01-01',
    stage='raw_to_jpg',
    metadata={'files_count': 150}
)
```

### 2. **Structured Event Types**
Events use dotted notation for organization:
- `stage.*` - Stage operations
- `gap.*` - Gap queries
- `error.*` - Errors
- `transfer.*` - Transfers
- `system.*` - System events

### 3. **Flexible Querying**
```python
# Recent errors
errors = db.events.get_errors(hours=24)

# Batch timeline
events = db.events.get_batch_events('MD_2025-01-01')

# Search
results = db.events.search_events('failed')

# Summary dashboard
summary = db.events.get_event_summary()
```

### 4. **JSONB Metadata**
Store structured data with events:
```python
metadata = {
    'config': {'quality': 95},
    'stats': {'duration': 123.45},
    'files': ['MD_001.raw', 'MD_002.raw']
}
```

### 5. **Performance**
- 9 indexes for fast queries
- Full-text search on messages
- GIN index on JSONB metadata
- Optional partitioning for high volume

### 6. **Retention Management**
```sql
-- Clean old events
SELECT cleanup_old_events(90);  -- Delete older than 90 days
```

---

## Event Severity Levels

| Level | Usage | Example |
|-------|-------|---------|
| DEBUG | Detailed diagnostic info | "Processing file 123/150" |
| INFO | Normal operations | "Started RAW to JPG conversion" |
| WARNING | Unusual but not wrong | "Retrying failed connection" |
| ERROR | Needs attention | "Failed to convert RAW file" |
| CRITICAL | Immediate action required | "Database connection lost" |

---

## Common Event Types

```python
# Stage lifecycle
'stage.started'      # Stage processing started
'stage.completed'    # Stage completed successfully
'stage.failed'       # Stage failed
'stage.reset'        # Stage reset for retry

# Gap queries
'gap.batches_query'  # Queried batches with gaps
'gap.files_query'    # Queried files with gaps
'gap.summary_query'  # Queried gap summary

# Status queries
'status.query'       # Queried stage status
'status.in_progress' # Queried in-progress stages
'status.failed'      # Queried failed stages

# Errors
'error.connection'   # Database connection error
'error.query'        # Query execution error
'error.processing'   # Processing error
'error.validation'   # Validation error
```

---

## Usage Pattern

```python
from agir_db import AgirDB
import os

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    stage = 'raw_to_jpg'
    job_id = os.environ.get('SLURM_JOB_ID', 'local')
    
    try:
        # Start stage
        db.stages.start(batch_id, stage, job_id)
        
        # Log start event
        db.events.log_event(
            event_type='stage.started',
            severity='INFO',
            message=f'Started {stage} for {batch_id}',
            batch_id=batch_id,
            stage=stage,
            job_id=job_id,
            source='processor'
        )
        db.commit()
        
        # Process files...
        count = process_files(batch_id, stage)
        
        # Complete stage
        db.stages.complete(batch_id, stage, success=True, files_processed=count)
        
        # Log completion
        db.events.log_event(
            event_type='stage.completed',
            severity='INFO',
            message=f'Completed {stage} for {batch_id}',
            batch_id=batch_id,
            stage=stage,
            metadata={'files_processed': count}
        )
        db.commit()
        
    except Exception as e:
        # Complete as failed
        db.stages.complete(batch_id, stage, success=False, error_message=str(e))
        
        # Log error
        db.events.log_event(
            event_type='stage.failed',
            severity='ERROR',
            message=f'Failed {stage} for {batch_id}: {str(e)}',
            batch_id=batch_id,
            stage=stage,
            error_type=type(e).__name__
        )
        db.commit()
        raise
```

---

## Installation Steps

1. **Install SQL schema:**
   ```bash
   source /project/dash_agir/postgres/pg_coords.env
   psql -f events_schema.sql
   ```

2. **Verify installation:**
   ```bash
   psql -c "\d processed.events"
   psql -c "\dv processed.*"
   ```

3. **Run tests:**
   ```bash
   python test_phase4.py
   ```

---

## Integration Points

### With Phase 2 (Pipeline Gaps)
Log gap queries for audit trail:
```python
batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
db.events.log_event(
    event_type='gap.batches_query',
    severity='INFO',
    message=f'Found {len(batches)} batches with gaps'
)
```

### With Phase 3 (Stage Status)
Log stage lifecycle (as shown in usage pattern above).

### With Future Phases
- Phase 5: Log image/batch metadata operations
- Phase 6: Log inventory sync operations
- Phase 7: Log transfer operations

---

## Views and Queries

### Helper Views

```sql
-- Recent events (last 24 hours)
SELECT * FROM processed.recent_events LIMIT 10;

-- Errors needing attention
SELECT * FROM processed.error_events LIMIT 20;

-- Stage operations
SELECT * FROM processed.stage_events WHERE batch_id = 'MD_2025-01-01';

-- Event summary by type
SELECT * FROM processed.event_summary_24h;

-- Events by batch
SELECT * FROM processed.batch_event_summary WHERE batch_id = 'MD_2025-01-01';
```

### Custom Queries

```sql
-- Find all errors for a batch
SELECT * FROM processed.events
WHERE batch_id = 'MD_2025-01-01' AND severity = 'ERROR'
ORDER BY created_at;

-- Count events by hour
SELECT 
    DATE_TRUNC('hour', created_at) AS hour,
    COUNT(*) AS event_count
FROM processed.events
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY hour
ORDER BY hour;

-- Top error types
SELECT 
    error_type,
    COUNT(*) AS error_count
FROM processed.events
WHERE severity IN ('ERROR', 'CRITICAL')
GROUP BY error_type
ORDER BY error_count DESC;
```

---

## What's Next: Phase 5 (Image & Batch Metadata)

Phase 5 will implement metadata management:

1. **SQL Tables**
   - `processed.images` - Image metadata (EXIF, bounding boxes)
   - `processed.batches` - Batch metadata (stats, processing info)

2. **ImageMetadata Class** - images.py
   - `insert()` / `insert_bulk()` - Add image records
   - `update()` - Update image metadata
   - `get_by_batch()` - Query images by batch
   - `get_by_id()` - Get specific image

3. **BatchMetadata Class** - batches.py
   - `insert()` - Add batch record
   - `update()` - Update batch metadata
   - `get_by_state()` - Query by state
   - `get_stats()` - Get processing statistics

---

## Phase Status

✓ **Phase 1**: Foundation (exceptions, connection, logging)
✓ **Phase 2**: Pipeline Gaps (work discovery)
✓ **Phase 3**: Stage Status (execution tracking)
✓ **Phase 4**: Event Logging (audit trail) ← YOU ARE HERE
☐ **Phase 5**: Image & Batch Metadata
☐ **Phase 6**: Inventory Sync
☐ **Phase 7**: Transfer Management
☐ **Phase 8**: Analytics
☐ **Phase 9**: Migration Tools

**Ready to proceed to Phase 5!**
