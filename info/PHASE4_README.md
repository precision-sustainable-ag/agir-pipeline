# Phase 4: Event Logging - Complete ✓

## Overview

Phase 4 implements comprehensive event logging to track all system operations. This provides a complete audit trail for debugging, monitoring, and analysis.

**Why Event Logging?**
- **Audit trail**: Complete history of all operations
- **Debugging**: Track what happened when and why
- **Monitoring**: Identify patterns and issues
- **Analytics**: Understand system usage and performance
- **Troubleshooting**: Quickly find errors and their context

## Components Created

### 1. **SQL Schema** (events_schema.sql, ~350 lines)

**Main Table: `processed.events`**

Comprehensive event logging with:

```sql
CREATE TABLE processed.events (
    event_id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,           -- e.g., 'stage.started', 'gap.query'
    severity TEXT NOT NULL,             -- DEBUG, INFO, WARNING, ERROR, CRITICAL
    batch_id TEXT,
    stage TEXT,
    job_id TEXT,
    message TEXT NOT NULL,
    metadata JSONB,                     -- Structured event data
    hostname TEXT,
    user_name TEXT,
    source TEXT,                        -- Which component logged it
    created_at TIMESTAMPTZ DEFAULT NOW(),
    error_type TEXT,                    -- For error events
    stack_trace TEXT                    -- For error events
);
```

**Helper Views:**
- `processed.recent_events` - Last 24 hours
- `processed.error_events` - Errors needing attention
- `processed.stage_events` - Stage operations
- `processed.warning_events` - Warnings
- `processed.event_summary_24h` - Counts by type/severity
- `processed.batch_event_summary` - Events grouped by batch

**Features:**
- 8 indexes for fast queries (time, type, severity, batch, stage, job)
- Full-text search on messages
- JSONB metadata for structured data
- Cleanup function for old events (retention policy)
- Optional partitioning support for high volume

### 2. **EventLogger Class** (events.py, ~700 lines)

Python API for logging and querying events:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Log an event
    event_id = db.events.log_event(
        event_type='stage.started',
        severity='INFO',
        message='Started RAW to JPG conversion',
        batch_id='MD_2025-01-01',
        stage='raw_to_jpg',
        metadata={'files_count': 150}
    )
    
    # Query events
    recent = db.events.get_recent_events(hours=24)
    errors = db.events.get_errors(limit=20)
    batch_events = db.events.get_batch_events('MD_2025-01-01')
```

**Main Methods:**

1. **`log_event(event_type, severity, message, ...)`**
   - Record any event
   - Structured metadata support
   - Auto-captures hostname and user

2. **`get_events(...)`**
   - Flexible filtering
   - By type, severity, batch, stage, time range
   - Wildcard support for event types

3. **`get_recent_events(hours=24, limit=100)`**
   - Events from last N hours
   - Convenient time-based query

4. **`get_errors(batch_id=None, stage=None, hours=None, limit=100)`**
   - Find error and critical events
   - Optional filtering by batch/stage

5. **`get_batch_events(batch_id, ...)`**
   - All events for specific batch
   - Complete batch history

6. **`get_stage_events(batch_id=None, stage=None, ...)`**
   - Stage operation events
   - Track processing lifecycle

7. **`get_event_summary(hours=24)`**
   - Aggregate counts by type/severity
   - Useful for dashboards

8. **`search_events(search_text, limit=100)`**
   - Full-text search in messages
   - Find events by keyword

**Event Types:**

Events use dotted notation for categorization:

```python
# Stage events
'stage.started'
'stage.completed'
'stage.failed'
'stage.reset'

# Gap query events
'gap.batches_query'
'gap.files_query'
'gap.summary_query'

# Status events
'status.query'
'status.in_progress'
'status.failed'

# Transfer events
'transfer.started'
'transfer.completed'
'transfer.failed'

# Error events
'error.connection'
'error.query'
'error.processing'
'error.validation'

# System events
'system.startup'
'system.shutdown'
'system.maintenance'
```

**Severity Levels:**

- `DEBUG` - Detailed information for diagnosing problems
- `INFO` - Normal operations and milestones
- `WARNING` - Unusual but not necessarily wrong
- `ERROR` - Errors that need attention
- `CRITICAL` - Critical failures requiring immediate action

### 3. **Integration with AgirDB**

EventLogger is now accessible through the main facade:

```python
from agir_db import AgirDB

with AgirDB() as db:
    db.events.log_event(...)
    db.events.get_recent_events()
    db.events.get_errors()
```

## Installation

### Step 1: Install SQL Schema

```bash
# Connect to your database
source /project/dash_agir/postgres/pg_coords.env
psql

# Run the schema file
\i /path/to/events_schema.sql

# Verify table and views exist
\d processed.events
\dv processed.*
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
    "idx_events_batch" btree (batch_id, created_at DESC) WHERE batch_id IS NOT NULL
    "idx_events_created_at" btree (created_at DESC)
    "idx_events_job" btree (job_id, created_at DESC) WHERE job_id IS NOT NULL
    "idx_events_message_search" gin (to_tsvector('english'::regconfig, message))
    "idx_events_metadata" gin (metadata)
    "idx_events_severity_time" btree (severity, created_at DESC) WHERE severity = ANY (...)
    "idx_events_stage" btree (stage, created_at DESC) WHERE stage IS NOT NULL
    "idx_events_type_time" btree (event_type, created_at DESC)
```

### Step 2: Update Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

## Testing

### Unit Tests (no database required)

```bash
python test_phase4.py
```

Expected output:
```
============================================================
Phase 4 - Event Logging Tests
============================================================
Testing valid severities...
✓ Valid severities are correct

Testing event types...
✓ EVENT_TYPES defined with 23 event types

...

============================================================
✓ All Phase 4 unit tests passed!
============================================================
```

### Database Integration Tests (requires live database)

The test script automatically runs integration tests if a database is available.
These tests verify:

1. Logging INFO events
2. Logging ERROR events
3. Getting recent events
4. Filtering events by batch/stage
5. Batch event queries
6. Stage event queries
7. Error queries
8. Full-text search
9. Event summary
10. Wildcard event type queries

## Usage Examples

### Example 1: Basic Event Logging

```python
from agir_db import AgirDB, setup_logging

setup_logging(level='INFO')

with AgirDB() as db:
    # Log stage start
    db.events.log_event(
        event_type='stage.started',
        severity='INFO',
        message='Started RAW to JPG conversion for MD_2025-01-01',
        batch_id='MD_2025-01-01',
        stage='raw_to_jpg',
        job_id='slurm_12345',
        metadata={
            'files_count': 150,
            'config': {'quality': 95}
        },
        source='batch_processor'
    )
    db.commit()
```

### Example 2: Error Logging with Context

```python
from agir_db import AgirDB
import traceback

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    try:
        # Process batch...
        process_batch(batch_id)
    except Exception as e:
        # Log error with full context
        db.events.log_event(
            event_type='error.processing',
            severity='ERROR',
            message=f'Failed to process batch {batch_id}: {str(e)}',
            batch_id=batch_id,
            stage='raw_to_jpg',
            error_type=type(e).__name__,
            stack_trace=traceback.format_exc(),
            metadata={
                'error_details': str(e),
                'files_processed': 100,
                'files_failed': 50
            }
        )
        db.commit()
        raise
```

### Example 3: Query and Monitor Events

```python
from agir_db import AgirDB
from datetime import datetime, timedelta

with AgirDB() as db:
    # Get recent errors
    errors = db.events.get_errors(hours=24, limit=20)
    
    print(f"Found {len(errors)} errors in last 24 hours:")
    for error in errors:
        print(f"  [{error['created_at']}] {error['batch_id']}")
        print(f"    {error['message']}")
        print()
    
    # Get events for specific batch
    batch_events = db.events.get_batch_events('MD_2025-01-01')
    
    print(f"\nBatch MD_2025-01-01 timeline:")
    for event in batch_events:
        print(f"  [{event['created_at']}] {event['event_type']}")
        print(f"    {event['message']}")
```

### Example 4: Event Summary Dashboard

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get summary of last 24 hours
    summary = db.events.get_event_summary(hours=24)
    
    print("Event Summary (Last 24 Hours)")
    print("="*60)
    
    for row in summary:
        print(f"{row['event_type']:30s} [{row['severity']:8s}] {row['event_count']:5d} events")
        print(f"  First: {row['first_occurrence']}")
        print(f"  Last:  {row['last_occurrence']}")
        print()
```

### Example 5: Search Events

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Search for "failed" in messages
    results = db.events.search_events('failed', limit=50)
    
    print(f"Found {len(results)} events containing 'failed':")
    for event in results:
        print(f"  [{event['created_at']}] {event['event_type']}")
        print(f"    {event['message'][:100]}...")
```

### Example 6: Integration with Stage Status

Automatically log stage lifecycle events:

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
        
        # Log event
        db.events.log_event(
            event_type='stage.started',
            severity='INFO',
            message=f'Started {stage} for {batch_id}',
            batch_id=batch_id,
            stage=stage,
            job_id=job_id,
            source='StageStatus'
        )
        db.commit()
        
        # Process...
        files_processed = process_files(batch_id, stage)
        
        # Complete stage
        db.stages.complete(batch_id, stage, success=True, files_processed=files_processed)
        
        # Log completion
        db.events.log_event(
            event_type='stage.completed',
            severity='INFO',
            message=f'Completed {stage} for {batch_id}',
            batch_id=batch_id,
            stage=stage,
            job_id=job_id,
            metadata={'files_processed': files_processed},
            source='StageStatus'
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
            job_id=job_id,
            error_type=type(e).__name__,
            source='StageStatus'
        )
        db.commit()
        raise
```

### Example 7: Maintenance - Clean Old Events

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Delete events older than 90 days
    result = db._connection.fetch_one("SELECT cleanup_old_events(90);")
    deleted_count = result['cleanup_old_events']
    
    print(f"Deleted {deleted_count} old events")
    db.commit()
```

## Retention and Maintenance

### Default Retention

By default, events are kept indefinitely. For production use, implement a retention policy:

```sql
-- Delete events older than 90 days
SELECT cleanup_old_events(90);

-- Schedule this as a cron job:
-- 0 2 * * * psql -c "SELECT cleanup_old_events(90);"
```

### Partitioning (Optional)

For high-volume systems, consider partitioning by month:

```sql
-- Create partition for January 2025
SELECT create_events_partition('2025-01-01'::DATE);

-- This can be automated with a cron job
```

## Integration with Previous Phases

### With Phase 2 (Pipeline Gaps)

Log gap queries:

```python
batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)

db.events.log_event(
    event_type='gap.batches_query',
    severity='INFO',
    message=f'Found {len(batches)} batches with gaps',
    stage='raw_to_jpg',
    metadata={'batch_count': len(batches)}
)
```

### With Phase 3 (Stage Status)

Log stage operations (as shown in Example 6 above).

## Files Created

```
agir-db/
├── src/agir_db/
│   ├── events.py                        # EventLogger class (700 lines)
│   ├── api.py                           # Updated with events integration
│   └── __init__.py                      # Updated exports
│
├── sql/schemas/03_processed/
│   └── events_schema.sql                # Table, views, indexes (350 lines)
│
└── tests/
    └── test_phase4.py                   # Test suite (500 lines)

Total new code: ~1,550 lines
```

## API Reference

### EventLogger Methods

#### `log_event(event_type, severity, message, ...)`

Record an event.

**Parameters:**
- `event_type` (str): Event type in dotted notation
- `severity` (str): DEBUG, INFO, WARNING, ERROR, CRITICAL
- `message` (str): Human-readable message
- `batch_id` (str, optional): Related batch
- `stage` (str, optional): Related stage
- `job_id` (str, optional): Related job
- `metadata` (dict, optional): Structured data
- `source` (str, optional): Component that logged it
- `error_type` (str, optional): Exception type
- `stack_trace` (str, optional): Stack trace

**Returns:** int - Event ID

---

#### `get_events(...) -> list[dict]`

Query events with filtering.

**Parameters:** event_type, severity, batch_id, stage, job_id, since, until, limit, order

---

#### `get_recent_events(hours=24, limit=100) -> list[dict]`

Get recent events.

---

#### `get_errors(...) -> list[dict]`

Get error and critical events.

---

#### `get_batch_events(batch_id, ...) -> list[dict]`

Get events for specific batch.

---

#### `get_stage_events(...) -> list[dict]`

Get stage operation events.

---

#### `get_event_summary(hours=24) -> list[dict]`

Get aggregate counts by type/severity.

---

#### `search_events(search_text, limit=100) -> list[dict]`

Full-text search in messages.

## Next Steps: Phase 5 (Image Metadata & Batch Metadata)

Phase 5 will implement metadata management:

1. **SQL Tables**
   - `processed.images` - Image metadata
   - `processed.batches` - Batch metadata

2. **ImageMetadata Class**
   - Insert/update image records
   - Query images by batch, processing status
   - Track EXIF data, bounding boxes

3. **BatchMetadata Class**
   - Insert/update batch records
   - Query batches by state, date
   - Track processing statistics

## Status

**Phase 4: COMPLETE ✓**

All event logging components are implemented and tested:
- ✓ SQL schema (table, views, indexes)
- ✓ EventLogger class (8 main methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 5!**