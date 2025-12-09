# Best Practices

[← Back to Index](README.md)

Production-ready patterns and recommendations for using AgirDB.

---

## 1. Always Use Context Managers

**✓ Good** - Automatic transaction handling:
```python
with AgirDB() as db:
    db.images.insert(image_data)
```

**✗ Avoid** - Manual transaction management:
```python
db = AgirDB()
db.connect()
db.images.insert(image_data)
db.commit()
db.close()
```

**Why:** Context managers automatically handle commits, rollbacks, and connection cleanup.

---

## 2. Use Pipeline Gaps as Source of Truth

**✓ Good** - Use gaps to discover work:
```python
batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
for batch in batches:
    process_batch(batch)
```

**✗ Avoid** - Relying solely on status without verifying gaps:
```python
batches = db.batches.list()  # Might include already-complete batches
```

**Why:** Pipeline gaps reflect actual filesystem state and are self-correcting.

---

## 3. Handle Errors Gracefully

**✓ Good** - Specific exception handling:
```python
try:
    db.stages.start(batch_id, stage)
except StageAlreadyInProgressError:
    logger.info("Batch already being processed")
except BatchNotFoundError:
    logger.error(f"Batch {batch_id} not found")
```

**✗ Avoid** - Catching all exceptions:
```python
try:
    db.stages.start(batch_id, stage)
except Exception:
    pass  # Silent failures hide problems
```

**Why:** Specific exceptions provide actionable information. Silent failures hide bugs.

---

## 4. Log Important Events

**✓ Good** - Comprehensive logging:
```python
db.events.log(
    event_type='processing_started',
    batch_id=batch_id,
    stage=stage,
    severity='info',
    message=f'Started processing {image_count} images',
    metadata={'worker_id': worker_id, 'hostname': hostname}
)
```

**✗ Avoid** - Minimal logging:
```python
db.stages.start(batch_id, stage)  # No audit trail
```

**Why:** Event logs provide audit trails, debugging context, and analytics data.

---

## 5. Verify Before Marking Complete

**✓ Good** - Verify completion before marking:
```python
if db.gaps.check_batch_complete(batch_id, stage):
    db.stages.complete(batch_id, stage, success=True)
else:
    db.stages.complete(batch_id, stage, success=False,
                      error_message='Gaps remain')
```

**✗ Avoid** - Assuming success:
```python
db.stages.complete(batch_id, stage, success=True)  # Hope for the best
```

**Why:** Verification prevents false completion and maintains data integrity.

---

## 6. Use Bulk Operations for Efficiency

**✓ Good** - Bulk insert:
```python
db.images.insert_bulk(image_list)  # Single transaction
```

**✗ Avoid** - Individual inserts:
```python
for image in image_list:
    db.images.insert(image)  # Many transactions
```

**Why:** Bulk operations are orders of magnitude faster for multiple records.

---

## 7. Implement Monitoring

**✓ Good** - Regular health checks:
```python
def monitor_pipeline():
    with AgirDB() as db:
        summary = db.gaps.get_gap_summary('raw_to_jpg')
        if summary['overall_gap_percentage'] > 10:
            alert_team(f"High gap percentage: {summary['overall_gap_percentage']}")
        
        stuck = [s for s in db.stages.get_in_progress()
                if s['duration_seconds'] > 7200]
        if stuck:
            alert_team(f"Found {len(stuck)} stuck stages")
```

**Why:** Proactive monitoring catches issues before they become critical.

---

## 8. Use Metadata Fields for Extensibility

**✓ Good** - Store additional context:
```python
db.images.insert(
    image_id='MD_1683434234',
    batch_id='B001',
    camera_id='SVS_001',
    capture_time='2025-01-15T10:00:00Z',
    raw_path='/data/raw/B001/MD_1683434234.ARW',
    metadata={
        'camera_settings': {
            'exposure': '1/1000',
            'iso': 400,
            'aperture': 'f/5.6'
        },
        'quality_score': 0.95,
        'weather_conditions': 'sunny'
    }
)
```

**Why:** Metadata fields provide flexibility without schema changes.

---

## 9. Check Status Before Starting

**✓ Good** - Prevent duplicate work:
```python
status = db.stages.get_status(batch_id, stage)
if status and status['status'] == 'in_progress':
    print(f"Skipping {batch_id} - already processing")
    continue

try:
    db.stages.start(batch_id, stage)
except StageAlreadyInProgressError:
    continue  # Race condition
```

**Why:** Reduces unnecessary attempts and handles race conditions.

---

## 10. Clean Up Stuck Stages

**✓ Good** - Regular cleanup:
```python
def cleanup_stuck_stages():
    with AgirDB() as db:
        timeout = 7200  # 2 hours
        for stage in db.stages.get_in_progress():
            if stage['duration_seconds'] > timeout:
                db.stages.cancel(
                    stage['batch_id'],
                    stage['stage'],
                    reason='Timeout'
                )
```

**Why:** Prevents abandoned stages from blocking work indefinitely.

---

## Performance Tips

### Use Pagination for Large Queries

```python
offset = 0
limit = 1000

while True:
    batches = db.gaps.get_batches_with_gaps(
        stage='raw_to_jpg',
        limit=limit,
        offset=offset
    )
    
    if not batches:
        break
    
    for batch in batches:
        process_batch(batch)
    
    offset += limit
```

### Parallel Processing

```python
from concurrent.futures import ThreadPoolExecutor

def process_batch_parallel(batch_id):
    # Each worker gets its own connection
    with AgirDB() as db:
        images = db.gaps.get_images_with_gaps(batch_id, 'raw_to_jpg')
        # Process images...

with ThreadPoolExecutor(max_workers=4) as executor:
    batches = ['B001', 'B002', 'B003', 'B004']
    executor.map(process_batch_parallel, batches)
```

### Batch Event Logging

```python
# Accumulate events during processing
events = []
for image in images:
    events.append({
        'event_type': 'image_processed',
        'batch_id': batch_id,
        'image_id': image['image_id'],
        'severity': 'info',
        'message': 'Processed successfully'
    })

# Bulk log at end
db.events.log_bulk(events)
```

---

## Configuration Management

### Use Environment Variables

```bash
# .env file
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agir
export PGUSER=agir_user
```

```python
# Code - no hardcoded credentials
with AgirDB() as db:
    # Uses environment variables
    pass
```

### Use Configuration Files

```yaml
# config.yaml
database:
  host: localhost
  port: 5432
  dbname: agir
  user: agir_user

processing:
  batch_size: 100
  timeout_seconds: 7200
  max_workers: 4
```

```python
import yaml

with open('config.yaml') as f:
    config = yaml.safe_load(f)

db = AgirDB(**config['database'])
```

---

## Error Handling Patterns

### Retry Logic

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10)
)
def process_with_retry(batch_id, stage):
    with AgirDB() as db:
        db.stages.start(batch_id, stage)
        process_batch(batch_id)
        db.stages.complete(batch_id, stage, success=True)
```

### Graceful Degradation

```python
with AgirDB() as db:
    for batch in batches:
        try:
            process_batch(batch)
        except Exception as e:
            # Log and continue
            db.events.log(
                event_type='batch_error',
                batch_id=batch['batch_id'],
                severity='error',
                message=str(e)
            )
            continue  # Don't let one failure stop all work
```

---

## Testing Recommendations

### Use Transactions for Tests

```python
import pytest

@pytest.fixture
def db():
    db = AgirDB()
    db.connect()
    yield db
    db.rollback()  # Undo all changes
    db.close()

def test_insert_image(db):
    db.images.insert(test_image_data)
    image = db.images.get(test_image_id)
    assert image['batch_id'] == 'TEST_BATCH'
```

### Mock External Dependencies

```python
from unittest.mock import patch

def test_processing_workflow():
    with patch('mymodule.convert_to_jpg') as mock_convert:
        mock_convert.return_value = '/path/to/output.jpg'
        
        with AgirDB() as db:
            # Test workflow without actual conversion
            process_batch('B001')
```

---

## Security Best Practices

### Never Hardcode Credentials

```python
# ✗ Bad
db = AgirDB(
    host='localhost',
    user='admin',
    password='secret123'  # Don't do this!
)

# ✓ Good
db = AgirDB()  # Uses environment variables or .pgpass
```

### Use Read-Only Connections for Analytics

```python
# Create separate read-only user in PostgreSQL
# GRANT SELECT ON ALL TABLES IN SCHEMA public TO agir_readonly;

analytics_db = AgirDB(user='agir_readonly')
```

### Validate User Input

```python
def process_batch_id(batch_id: str):
    # Validate format
    if not batch_id.startswith('B') or not batch_id[1:].isdigit():
        raise ValueError(f"Invalid batch ID format: {batch_id}")
    
    with AgirDB() as db:
        batch = db.batches.get(batch_id)
```

---

## See Also

- [Orchestration](orchestration.md) - Complete workflow examples
- [Exception Handling](exceptions.md) - Error handling reference
- [Troubleshooting](troubleshooting.md) - Common issues

[← Back to Index](README.md)
