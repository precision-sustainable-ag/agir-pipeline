# Stage Status Component

[← Back to Index](index.md)

The `stages` component tracks the in-progress status of pipeline stages, preventing duplicate work and enabling monitoring. It works in conjunction with [pipeline gaps](pipeline-gaps.md) to provide complete workflow orchestration.

## Key Concept

Stage status prevents race conditions where multiple workers try to process the same batch. Once a batch/stage is marked as "in_progress", other workers skip it. This pairs with gap analysis: gaps tell you *what* needs work, status tracking prevents *duplicate* work.

---

## Methods

### `start()`

Mark a stage as started for a batch.

```python
status_id = db.stages.start(
    batch_id='B001',
    stage='raw_to_jpg',
    job_id='worker-001',
    hostname='compute-01',
    metadata={'worker_type': 'gpu', 'priority': 'high'}
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name (e.g., 'raw_to_jpg', 'dng_to_jpg', 'object_detection')
- `job_id` (str, optional): Job/worker identifier
- `hostname` (str, optional): Hostname of processing machine
- `metadata` (dict, optional): Free-form metadata (stored as JSON)

**Returns:** `str` - Stage status ID

**Raises:**
- `StageAlreadyInProgressError`: If stage is already running for this batch
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database operation fails

**Example:**
```python
try:
    status_id = db.stages.start('B001', 'raw_to_jpg', job_id='worker-001')
    process_batch('B001')
    db.stages.complete('B001', 'raw_to_jpg', success=True)
except StageAlreadyInProgressError:
    print("Another worker is already processing this batch")
```

---

### `complete()`

Mark a stage as completed for a batch.

```python
db.stages.complete(
    batch_id='B001',
    stage='raw_to_jpg',
    success=True,
    error_message=None,
    files_processed=150,
    metadata={'processing_time': 342.5}
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name
- `success` (bool): Whether processing succeeded
- `error_message` (str, optional): Error description if failed
- `files_processed` (int, optional): Number of files processed
- `metadata` (dict, optional): Additional completion metadata

**Returns:** `None`

**Raises:**
- `StageNotStartedError`: If stage was never started
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database operation fails

**Example:**
```python
try:
    result = process_batch('B001')
    db.stages.complete(
        'B001',
        'raw_to_jpg',
        success=True,
        files_processed=result['count'],
        metadata={'duration': result['duration']}
    )
except Exception as e:
    db.stages.complete(
        'B001',
        'raw_to_jpg',
        success=False,
        error_message=str(e)
    )
```

---

### `get_status()`

Get current status of a stage for a batch.

```python
status = db.stages.get_status(
    batch_id='B001',
    stage='raw_to_jpg'
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name

**Returns:** `Dict` or `None` - Status information if stage started, None otherwise:
```python
{
    'status_id': 'uuid-string',
    'batch_id': 'B001',
    'stage': 'raw_to_jpg',
    'status': 'in_progress',  # or 'completed', 'failed'
    'job_id': 'worker-001',
    'hostname': 'compute-01',
    'started_at': '2025-01-15T10:30:00Z',
    'completed_at': None,
    'files_processed': None,
    'error_message': None,
    'metadata': {'worker_type': 'gpu'}
}
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database query fails

**Example:**
```python
status = db.stages.get_status('B001', 'raw_to_jpg')
if status and status['status'] == 'in_progress':
    elapsed = datetime.now() - status['started_at']
    print(f"Processing for {elapsed.total_seconds()} seconds")
```

---

### `get_in_progress()`

Get all currently in-progress stages.

```python
stages = db.stages.get_in_progress(
    stage=None,
    limit=100
)
```

**Parameters:**
- `stage` (str, optional): Filter by specific stage. If None, returns all stages.
- `limit` (int, optional): Maximum number to return. Default: 100

**Returns:** `List[Dict]` - List of in-progress stages:
```python
[
    {
        'batch_id': 'B001',
        'stage': 'raw_to_jpg',
        'job_id': 'worker-001',
        'hostname': 'compute-01',
        'started_at': '2025-01-15T10:30:00Z',
        'duration_seconds': 342,
        'metadata': {}
    },
    ...
]
```

**Raises:**
- `QueryError`: If database query fails

**Example:**
```python
# Monitor all in-progress work
in_progress = db.stages.get_in_progress()
for stage in in_progress:
    if stage['duration_seconds'] > 3600:  # 1 hour
        print(f"Warning: {stage['batch_id']}/{stage['stage']} "
              f"running for {stage['duration_seconds']}s")
```

---

### `cancel()`

Cancel an in-progress stage.

```python
db.stages.cancel(
    batch_id='B001',
    stage='raw_to_jpg',
    reason='User requested cancellation'
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name
- `reason` (str, optional): Cancellation reason

**Returns:** `None`

**Raises:**
- `StageNotStartedError`: If stage wasn't in progress
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database operation fails

**Example:**
```python
# Cancel long-running stages
for stage in db.stages.get_in_progress():
    if stage['duration_seconds'] > 7200:  # 2 hours
        db.stages.cancel(
            stage['batch_id'],
            stage['stage'],
            reason='Timeout - exceeded 2 hour limit'
        )
```

---

### `get_history()`

Get processing history for a batch across all stages.

```python
history = db.stages.get_history(
    batch_id='B001',
    limit=50
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `limit` (int, optional): Maximum number of records. Default: 50

**Returns:** `List[Dict]` - Processing history ordered by started_at:
```python
[
    {
        'stage': 'raw_to_dng',
        'status': 'completed',
        'started_at': '2025-01-15T10:00:00Z',
        'completed_at': '2025-01-15T10:15:00Z',
        'duration_seconds': 900,
        'files_processed': 150,
        'job_id': 'worker-001',
        'success': True
    },
    {
        'stage': 'dng_to_jpg',
        'status': 'completed',
        'started_at': '2025-01-15T10:16:00Z',
        'completed_at': '2025-01-15T10:30:00Z',
        'duration_seconds': 840,
        'files_processed': 150,
        'job_id': 'worker-002',
        'success': True
    }
]
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database query fails

**Example:**
```python
# Review batch processing history
history = db.stages.get_history('B001')
for stage in history:
    status_icon = '✓' if stage['success'] else '✗'
    print(f"{status_icon} {stage['stage']}: {stage['duration_seconds']}s")
```

---

## Usage Patterns

### Basic Workflow

Start, process, complete:

```python
with AgirDB() as db:
    batch_id = 'B001'
    stage = 'raw_to_jpg'
    
    try:
        # Start
        db.stages.start(batch_id, stage, job_id='worker-001')
        
        # Process
        result = process_batch(batch_id, stage)
        
        # Complete
        db.stages.complete(
            batch_id,
            stage,
            success=True,
            files_processed=result['count']
        )
    except Exception as e:
        # Mark as failed
        db.stages.complete(
            batch_id,
            stage,
            success=False,
            error_message=str(e)
        )
        raise
```

### Preventing Duplicate Work

Check status before starting:

```python
with AgirDB() as db:
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        # Check if already in progress
        status = db.stages.get_status(batch['batch_id'], 'raw_to_jpg')
        
        if status and status['status'] == 'in_progress':
            print(f"Skipping {batch['batch_id']} - already being processed")
            continue
        
        try:
            # Safe to start
            db.stages.start(batch['batch_id'], 'raw_to_jpg')
            process_batch(batch)
            db.stages.complete(batch['batch_id'], 'raw_to_jpg', success=True)
        except StageAlreadyInProgressError:
            # Race condition - another worker grabbed it
            print(f"Race condition on {batch['batch_id']} - skipping")
            continue
```

### Monitoring Active Work

Track what's currently processing:

```python
with AgirDB() as db:
    in_progress = db.stages.get_in_progress()
    
    print(f"Currently processing {len(in_progress)} batches:")
    for stage in in_progress:
        duration_min = stage['duration_seconds'] / 60
        print(f"  {stage['batch_id']}/{stage['stage']}: "
              f"{duration_min:.1f} min ({stage['job_id']})")
```

### Timeout Detection

Find and handle stuck stages:

```python
with AgirDB() as db:
    timeout_threshold = 7200  # 2 hours
    
    for stage in db.stages.get_in_progress():
        if stage['duration_seconds'] > timeout_threshold:
            print(f"Stuck: {stage['batch_id']}/{stage['stage']}")
            
            # Cancel it
            db.stages.cancel(
                stage['batch_id'],
                stage['stage'],
                reason=f'Timeout after {timeout_threshold}s'
            )
            
            # Log for investigation
            db.events.log(
                event_type='stage_timeout',
                batch_id=stage['batch_id'],
                stage=stage['stage'],
                severity='warning',
                message='Stage cancelled due to timeout',
                metadata={
                    'duration_seconds': stage['duration_seconds'],
                    'job_id': stage['job_id']
                }
            )
```

### Processing History Review

Audit batch processing:

```python
with AgirDB() as db:
    history = db.stages.get_history('B001')
    
    print("Batch B001 processing history:")
    for record in history:
        duration_min = record['duration_seconds'] / 60
        status = '✓' if record['success'] else '✗'
        
        print(f"{status} {record['stage']}")
        print(f"   Started: {record['started_at']}")
        print(f"   Duration: {duration_min:.1f} min")
        print(f"   Files: {record['files_processed']}")
        
        if not record['success']:
            print(f"   Error: {record.get('error_message', 'Unknown')}")
```

---

## Integration with Pipeline Gaps

Stage status and pipeline gaps work together:

1. **Gaps discover work**: Use `db.gaps.get_batches_with_gaps()` to find what needs processing
2. **Status prevents duplication**: Use `db.stages.start()` to claim work and prevent others from taking it
3. **Gaps verify completion**: Use `db.gaps.check_batch_complete()` before marking stage complete

```python
with AgirDB() as db:
    # 1. Discover work
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        # 2. Prevent duplication
        try:
            db.stages.start(batch['batch_id'], 'raw_to_jpg')
        except StageAlreadyInProgressError:
            continue  # Another worker has it
        
        # Process
        process_batch(batch)
        
        # 3. Verify completion
        if db.gaps.check_batch_complete(batch['batch_id'], 'raw_to_jpg'):
            db.stages.complete(batch['batch_id'], 'raw_to_jpg', success=True)
        else:
            db.stages.complete(
                batch['batch_id'],
                'raw_to_jpg',
                success=False,
                error_message='Gaps remain after processing'
            )
```

---

## Metadata Usage

The `metadata` field accepts any JSON-serializable dictionary for tracking additional context:

```python
# Track worker information
db.stages.start(
    'B001',
    'raw_to_jpg',
    metadata={
        'worker_type': 'gpu',
        'gpu_model': 'RTX 4090',
        'cuda_version': '12.1',
        'batch_size': 32
    }
)

# Track performance metrics
db.stages.complete(
    'B001',
    'raw_to_jpg',
    success=True,
    files_processed=150,
    metadata={
        'total_duration_seconds': 342.5,
        'avg_time_per_image': 2.28,
        'peak_memory_gb': 8.5,
        'cpu_percent': 45.2
    }
)
```

---

## See Also

- [Pipeline Gaps](pipeline-gaps.md) - Work discovery
- [Event Logging](event-logging.md) - Detailed audit trails
- [Orchestration Examples](orchestration.md) - Complete workflows
- [Exception Handling](exceptions.md) - Error reference

[← Back to Index](index.md)
