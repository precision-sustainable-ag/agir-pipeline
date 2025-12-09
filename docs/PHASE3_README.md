# Phase 3: Stage Status - Complete ✓

## Overview

Phase 3 implements stage execution tracking to prevent duplicate work and enable monitoring of processing jobs. This builds on Phase 2's gap detection by adding state management for in-progress stages.

**Why Stage Status?**
- **Prevents duplicate work**: Multiple workers won't process the same batch
- **Enables monitoring**: Track which jobs are running and for how long
- **Handles failures**: Record errors and retry failed stages
- **Tracks metrics**: Record files processed, duration, and other statistics

## Components Created

### 1. **SQL Schema** (stage_status_schema.sql, ~250 lines)

**Main Table: `processed.stage_status`**

Tracks execution status of pipeline stages:

```sql
CREATE TABLE processed.stage_status (
    batch_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,              -- 'in_progress', 'completed', 'failed'
    job_id TEXT,                        -- Job/worker identifier
    hostname TEXT,                      -- Processing host
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_seconds NUMERIC,           -- Auto-calculated
    success BOOLEAN,
    files_processed INTEGER,
    files_failed INTEGER,
    error_message TEXT,
    metadata JSONB,                     -- Free-form data
    PRIMARY KEY (batch_id, stage)
);
```

**Helper Views:**
- `processed.in_progress_stages` - Currently running stages with elapsed time
- `processed.failed_stages` - Failed stages needing retry
- `processed.completed_stages` - Successfully completed stages with stats

**Features:**
- Automatic `updated_at` timestamp via trigger
- Automatic duration calculation on completion
- Indexes for fast queries by status, batch, job_id
- CHECK constraints ensure data integrity

### 2. **StageStatus Class** (stages.py, ~550 lines)

Python API for managing stage lifecycle:

```python
from agir_db import AgirDB
import os

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    stage = 'raw_to_jpg'
    job_id = os.environ.get('SLURM_JOB_ID', 'local')
    
    # 1. Start processing
    db.stages.start(batch_id, stage, job_id)
    
    try:
        # 2. Do processing...
        files_processed = process_batch(batch_id)
        
        # 3. Mark as complete
        db.stages.complete(
            batch_id, stage,
            success=True,
            files_processed=files_processed
        )
    except Exception as e:
        # 4. Mark as failed
        db.stages.complete(
            batch_id, stage,
            success=False,
            error_message=str(e)
        )
        raise
```

**Main Methods:**

1. **`start(batch_id, stage, job_id=None, metadata=None)`**
   - Mark stage as in-progress
   - Prevents duplicate processing (raises `StageAlreadyInProgressError`)
   - Records job_id and hostname

2. **`complete(batch_id, stage, success, files_processed=None, ...)`**
   - Mark stage as completed (success or failure)
   - Auto-calculates duration
   - Records metrics and error messages

3. **`reset(batch_id, stage)`**
   - Clear stage status to allow retry
   - Useful for stuck jobs or failed stages

4. **`get_status(batch_id, stage)`**
   - Get current status of a stage
   - Returns None if never started

5. **`get_in_progress(stage=None)`**
   - Find all in-progress stages
   - Useful for monitoring stuck jobs

6. **`get_failed(stage=None, limit=None)`**
   - Find failed stages needing retry
   - Includes error messages

7. **`get_batch_status(batch_id)`**
   - Get all stages for a specific batch
   - Overview of batch processing progress

### 3. **Integration with AgirDB**

StageStatus is now accessible through the main facade:

```python
from agir_db import AgirDB

with AgirDB() as db:
    db.stages.start('MD_2025-01-01', 'raw_to_jpg')
    db.stages.complete('MD_2025-01-01', 'raw_to_jpg', success=True)
    db.stages.get_in_progress()
```

## Installation

### Step 1: Install SQL Schema

```bash
# Connect to your database
source /project/dash_agir/postgres/pg_coords.env
psql

# Run the schema file
\i /path/to/stage_status_schema.sql

# Verify table and views exist
\d processed.stage_status
\dv processed.*
```

Expected output:
```
                Table "processed.stage_status"
     Column      |           Type           | Nullable | Default
-----------------+--------------------------+----------+---------
 batch_id        | text                     | not null |
 stage           | text                     | not null |
 status          | text                     | not null |
 job_id          | text                     |          |
 ...

 Schema    |        Name         | Type |      Owner
-----------+---------------------+------+-----------------
 processed | completed_stages    | view | matthew.kutugata
 processed | failed_stages       | view | matthew.kutugata
 processed | in_progress_stages  | view | matthew.kutugata
```

### Step 2: Update Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

## Testing

### Unit Tests (no database required)

```bash
python test_phase3.py
```

Expected output:
```
============================================================
Phase 3 - Stage Status Tests
============================================================
Testing valid stages...
✓ Valid stages are correct

Testing valid statuses...
✓ Valid statuses are correct

...

============================================================
✓ All Phase 3 unit tests passed!
============================================================
```

### Database Integration Tests (requires live database)

The test script automatically runs integration tests if a database is available.
These tests verify the complete stage lifecycle:

1. Start a stage
2. Verify can't start twice
3. Get in-progress stages
4. Complete successfully
5. Reset and test failure path
6. Get failed stages
7. Get batch status

## Usage Examples

### Example 1: Basic Processing Pipeline

```python
from agir_db import AgirDB, setup_logging
import os

setup_logging(level='INFO')

with AgirDB() as db:
    # Find batches needing work
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        batch_id = batch['batch_id']
        stage = 'raw_to_jpg'
        job_id = os.environ.get('SLURM_JOB_ID', 'local')
        
        try:
            # Start stage (prevents duplicate work)
            db.stages.start(batch_id, stage, job_id)
            db.commit()
            
            # Get files to process
            files = db.gaps.get_files_with_gap(batch_id, stage)
            
            # Process files
            processed_count = process_files(files)
            
            # Mark as complete
            db.stages.complete(
                batch_id, stage,
                success=True,
                files_processed=processed_count
            )
            db.commit()
            
        except Exception as e:
            # Mark as failed
            db.stages.complete(
                batch_id, stage,
                success=False,
                error_message=str(e)
            )
            db.commit()
            raise
```

### Example 2: Monitoring Stuck Jobs

```python
from agir_db import AgirDB
from datetime import datetime, timedelta

with AgirDB() as db:
    # Find jobs running > 24 hours
    in_progress = db.stages.get_in_progress()
    
    for stage in in_progress:
        if stage['elapsed_hours'] > 24:
            print(f"STUCK JOB DETECTED:")
            print(f"  Batch: {stage['batch_id']}")
            print(f"  Stage: {stage['stage']}")
            print(f"  Job ID: {stage['job_id']}")
            print(f"  Host: {stage['hostname']}")
            print(f"  Running: {stage['elapsed_hours']:.1f} hours")
            print()
            
            # Optional: Reset stuck job
            # db.stages.reset(stage['batch_id'], stage['stage'])
```

### Example 3: Retry Failed Stages

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get recent failures
    failures = db.stages.get_failed(stage='raw_to_jpg', limit=20)
    
    for failure in failures:
        batch_id = failure['batch_id']
        stage = failure['stage']
        
        print(f"Batch: {batch_id}")
        print(f"  Error: {failure['error_message']}")
        print(f"  Files processed: {failure['files_processed']}")
        print(f"  Files failed: {failure['files_failed']}")
        print()
        
        # Reset to allow retry
        response = input(f"Reset {batch_id}/{stage}? (y/n): ")
        if response.lower() == 'y':
            db.stages.reset(batch_id, stage)
            db.commit()
            print(f"✓ Reset {batch_id}/{stage}")
```

### Example 4: Batch Processing Status

```python
from agir_db import AgirDB

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    # Get pipeline status
    pipeline = db.gaps.get_batch_pipeline_summary(batch_id)
    
    # Get stage execution status
    stages = db.stages.get_batch_status(batch_id)
    
    print(f"Batch: {batch_id}")
    print(f"\nPipeline Status:")
    print(f"  RAW files: {pipeline['raw_count']}")
    print(f"  JPG files: {pipeline['jpg_count']}")
    print(f"  Gaps: {pipeline['raw_to_jpg_gap']}")
    
    print(f"\nStage Execution:")
    for stage in stages:
        print(f"  {stage['stage']}: {stage['status']}")
        if stage['status'] == 'completed':
            print(f"    Duration: {stage['duration_seconds']:.1f}s")
            print(f"    Files: {stage['files_processed']}")
```

### Example 5: Using Metadata

```python
from agir_db import AgirDB

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    stage = 'raw_to_jpg'
    
    # Start with metadata
    metadata = {
        'config': {
            'quality': 95,
            'auto_exposure': True
        },
        'worker_version': '1.2.3',
        'notes': 'Test run with new settings'
    }
    
    db.stages.start(batch_id, stage, metadata=metadata)
    db.commit()
    
    # Later: retrieve and use metadata
    status = db.stages.get_status(batch_id, stage)
    config = status['metadata']['config']
    print(f"Processing with quality: {config['quality']}")
```

## Integration with Phase 2 (Pipeline Gaps)

Phases 2 and 3 work together to provide complete work discovery and tracking:

```python
from agir_db import AgirDB

with AgirDB() as db:
    stage = 'raw_to_jpg'
    
    # 1. Find batches needing work (Phase 2)
    batches = db.gaps.get_batches_with_gaps(stage, limit=10)
    
    for batch in batches:
        batch_id = batch['batch_id']
        
        # 2. Check if already being processed (Phase 3)
        status = db.stages.get_status(batch_id, stage)
        if status and status['status'] == 'in_progress':
            print(f"Skipping {batch_id} - already in progress")
            continue
        
        # 3. Process batch
        db.stages.start(batch_id, stage)
        # ... do work ...
        db.stages.complete(batch_id, stage, success=True)
```

## Files Created

```
agir-db/
├── src/agir_db/
│   ├── stages.py                        # StageStatus class (550 lines)
│   ├── api.py                           # Updated with stages integration
│   └── __init__.py                      # Updated exports
│
├── sql/schemas/03_processed/
│   └── stage_status_schema.sql          # Table, views, indexes (250 lines)
│
└── tests/
    └── test_phase3.py                   # Test suite (400 lines)

Total new code: ~1,200 lines
```

## API Reference

### StageStatus Methods

#### `start(batch_id, stage, job_id=None, metadata=None)`

Mark stage as started.

**Raises:** `StageAlreadyInProgressError` if already in progress

---

#### `complete(batch_id, stage, success, files_processed=None, ...)`

Mark stage as completed (success or failure).

**Parameters:**
- `success` (bool): True for success, False for failure
- `files_processed` (int, optional): Number of files processed
- `files_failed` (int, optional): Number of files failed
- `error_message` (str, optional): Error details if failed
- `metadata` (dict, optional): Additional metadata

**Raises:** `StageNotStartedError` if stage was never started

---

#### `reset(batch_id, stage)`

Clear stage status to allow retry.

---

#### `get_status(batch_id, stage) -> dict | None`

Get current status. Returns None if never started.

---

#### `get_in_progress(stage=None) -> list[dict]`

Get all in-progress stages.

---

#### `get_failed(stage=None, limit=None) -> list[dict]`

Get failed stages needing retry.

---

#### `get_batch_status(batch_id) -> list[dict]`

Get all stages for a specific batch.

## Next Steps: Phase 4 (Event Logging)

Phase 4 will implement event logging to track all operations:

1. **SQL Table** (`processed.events`)
   - Event log for all operations
   - Batch operations, stage changes, errors

2. **EventLogger Class** (events.py)
   - `log_event()` - Record any event
   - `get_events()` - Query event log
   - `get_batch_events()` - Events for specific batch

3. **Auto-logging**
   - Integrate with StageStatus to auto-log stage changes
   - Log gaps queries, stage starts/completions

## Status

**Phase 3: COMPLETE ✓**

All stage status components are implemented and tested:
- ✓ SQL schema (table, views, indexes, triggers)
- ✓ StageStatus class (7 main methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 4!**