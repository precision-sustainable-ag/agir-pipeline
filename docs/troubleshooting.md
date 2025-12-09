# Troubleshooting

[← Back to Index](index.md)

Common issues and solutions when using AgirDB.

---

## Connection Issues

### Cannot Connect to Database

**Symptom:**
```
ConnectionError: could not connect to server
```

**Solutions:**

1. **Check PostgreSQL is running:**
   ```bash
   # Linux
   sudo systemctl status postgresql
   
   # macOS
   brew services list | grep postgresql
   ```

2. **Verify environment variables:**
   ```bash
   echo $PGHOST
   echo $PGPORT
   echo $PGDATABASE
   echo $PGUSER
   ```

3. **Check .pgpass file:**
   ```bash
   cat ~/.pgpass
   # Format: hostname:port:database:username:password
   
   # Verify permissions
   ls -l ~/.pgpass  # Should be 600
   chmod 600 ~/.pgpass
   ```

4. **Test connection manually:**
   ```bash
   psql -h $PGHOST -p $PGPORT -d $PGDATABASE -U $PGUSER
   ```

---

## Duplicate Key Errors

### DuplicateImageError

**Symptom:**
```
DuplicateImageError: Image 'MD_1683434234' already exists
```

**Solutions:**

1. **Update instead of insert:**
   ```python
   try:
       db.images.insert(image_data)
   except DuplicateImageError:
       db.images.update(image_data['image_id'], **image_data)
   ```

2. **Check for existing record first:**
   ```python
   try:
       image = db.images.get(image_id)
       # Update existing
       db.images.update(image_id, **updates)
   except ImageNotFoundError:
       # Insert new
       db.images.insert(image_data)
   ```

---

### DuplicateBatchError

**Symptom:**
```
DuplicateBatchError: Batch 'B001' already exists
```

**Solution:**
```python
try:
    batch = db.batches.get(batch_id)
    print(f"Batch {batch_id} already exists")
except BatchNotFoundError:
    db.batches.insert(batch_data)
```

---

## Stage Lock Issues

### StageAlreadyInProgressError

**Symptom:**
```
StageAlreadyInProgressError: Stage 'raw_to_jpg' already in progress for batch 'B001'
```

**Solutions:**

1. **Skip and move to next batch:**
   ```python
   try:
       db.stages.start(batch_id, stage)
   except StageAlreadyInProgressError:
       print(f"Skipping {batch_id} - already processing")
       continue
   ```

2. **Check if stage is stuck:**
   ```python
   status = db.stages.get_status(batch_id, stage)
   if status and status['duration_seconds'] > 7200:  # 2 hours
       # Cancel stuck stage
       db.stages.cancel(batch_id, stage, reason='Timeout')
       # Retry
       db.stages.start(batch_id, stage)
   ```

3. **Find all stuck stages:**
   ```python
   with AgirDB() as db:
       timeout = 7200
       for stage in db.stages.get_in_progress():
           if stage['duration_seconds'] > timeout:
               print(f"Stuck: {stage['batch_id']}/{stage['stage']}")
               db.stages.cancel(
                   stage['batch_id'],
                   stage['stage'],
                   reason='Cleanup'
               )
   ```

---

## Query Performance Issues

### Slow Queries

**Symptoms:**
- Long wait times for `get_batches_with_gaps()`
- Timeouts on large result sets

**Solutions:**

1. **Use pagination:**
   ```python
   limit = 100
   offset = 0
   
   while True:
       batches = db.gaps.get_batches_with_gaps(
           stage='raw_to_jpg',
           limit=limit,
           offset=offset
       )
       
       if not batches:
           break
       
       process_batches(batches)
       offset += limit
   ```

2. **Add indexes (database admin):**
   ```sql
   CREATE INDEX idx_images_batch_id ON images(batch_id);
   CREATE INDEX idx_images_jpg_path ON images(jpg_path) WHERE jpg_path IS NULL;
   CREATE INDEX idx_stage_status_batch_stage ON stage_status(batch_id, stage);
   ```

3. **Filter results:**
   ```python
   # Instead of getting all batches
   batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
   
   # Get top batches with most gaps
   batches = db.gaps.get_batches_with_gaps(
       'raw_to_jpg',
       limit=10,
       order_by='gap_count',
       order_dir='DESC'
   )
   ```

---

## Transaction Issues

### TransactionError

**Symptom:**
```
TransactionError: current transaction is aborted
```

**Cause:** Error occurred in transaction, needs rollback.

**Solution:**
```python
try:
    with AgirDB() as db:
        # Do work
        pass
except Exception as e:
    # Context manager automatically rolls back
    print(f"Error: {e}")
```

**Manual handling:**
```python
db = AgirDB()
db.connect()

try:
    db.images.insert(image_data)
    db.commit()
except Exception as e:
    db.rollback()
    raise
finally:
    db.close()
```

---

## Validation Errors

### MissingRequiredFieldError

**Symptom:**
```
MissingRequiredFieldError: Required field 'batch_id' missing
```

**Solution:**
Check all required fields are provided:
```python
# Images require: image_id, batch_id, camera_id, capture_time, raw_path
image_data = {
    'image_id': 'MD_1683434234',
    'batch_id': 'B001',  # Required
    'camera_id': 'SVS_001',  # Required
    'capture_time': '2025-01-15T10:00:00Z',  # Required
    'raw_path': '/data/raw/B001/MD_1683434234.ARW',  # Required
}
db.images.insert(image_data)
```

---

### InvalidStageError

**Symptom:**
```
InvalidStageError: Invalid stage name 'raw-to-jpg'
```

**Cause:** Incorrect stage naming format.

**Solution:**
Use underscore-separated names:
```python
# ✗ Wrong
db.gaps.get_batches_with_gaps('raw-to-jpg')  # Dashes

# ✓ Correct
db.gaps.get_batches_with_gaps('raw_to_jpg')  # Underscores
```

---

## Gap Analysis Issues

### No Batches Returned Despite Missing Files

**Symptom:**
```python
batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
# Returns []
```

**Troubleshooting:**

1. **Check image records exist:**
   ```python
   images = db.images.get_by_batch('B001')
   print(f"Found {len(images)} images")
   ```

2. **Verify paths are correct:**
   ```python
   image = db.images.get('MD_1683434234')
   print(f"RAW: {image['raw_path']}")
   print(f"JPG: {image['jpg_path']}")
   ```

3. **Check filesystem:**
   ```bash
   ls -l /data/raw/B001/MD_1683434234.ARW
   ls -l /data/jpg/B001/MD_1683434234.jpg
   ```

4. **Verify batch exists:**
   ```python
   try:
       batch = db.batches.get('B001')
       print(f"Batch exists: {batch['image_count']} images")
   except BatchNotFoundError:
       print("Batch not found - create it first")
   ```

---

## Memory Issues

### Out of Memory with Large Batches

**Symptom:**
```
MemoryError: Unable to allocate array
```

**Solutions:**

1. **Process in chunks:**
   ```python
   limit = 100
   offset = 0
   
   while True:
       images = db.images.get_by_batch('B001', limit=limit, offset=offset)
       if not images:
           break
       
       process_images(images)
       offset += limit
   ```

2. **Use generators:**
   ```python
   def image_generator(batch_id, limit=100):
       offset = 0
       while True:
           images = db.images.get_by_batch(batch_id, limit=limit, offset=offset)
           if not images:
               break
           yield from images
           offset += limit
   
   for image in image_generator('B001'):
       process_image(image)
   ```

---

## Event Log Issues

### Too Many Events

**Symptom:**
Events table growing very large, queries slowing down.

**Solutions:**

1. **Archive old events:**
   ```sql
   -- Create archive table
   CREATE TABLE events_archive AS SELECT * FROM events WHERE created_at < '2024-01-01';
   
   -- Delete archived events
   DELETE FROM events WHERE created_at < '2024-01-01';
   ```

2. **Use severity filters:**
   ```python
   # Don't log debug events in production
   if severity in ['error', 'warning', 'critical']:
       db.events.log(...)
   ```

3. **Bulk log at end of processing:**
   ```python
   events = []
   for image in images:
       events.append({...})
   
   # Single bulk insert instead of many individual inserts
   db.events.log_bulk(events)
   ```

---

## Debug Tips

### Enable Verbose Logging

```python
import logging
from agir_db import setup_logging

setup_logging(level=logging.DEBUG)
```

### Check Query Execution

```python
with AgirDB() as db:
    # Enable PostgreSQL query logging
    db._connection.cursor.execute("SET log_statement = 'all'")
```

### Inspect Database State

```python
with AgirDB() as db:
    # Check what's in progress
    in_progress = db.stages.get_in_progress()
    print(f"In progress: {len(in_progress)}")
    
    # Check gap statistics
    summary = db.gaps.get_gap_summary('raw_to_jpg')
    print(f"Gap percentage: {summary['overall_gap_percentage']:.2f}%")
    
    # Check recent errors
    errors = db.events.search(severity='error', limit=10)
    for error in errors:
        print(f"{error['created_at']}: {error['message']}")
```

---

## Getting Help

### Collect Diagnostic Information

```python
def print_diagnostics():
    with AgirDB() as db:
        print("=== AgirDB Diagnostics ===\n")
        
        # Connection status
        print(f"Connected: {db.is_connected}")
        
        # Pipeline summary
        summary = db.analytics.get_pipeline_summary()
        print(f"Total batches: {summary['total_batches']}")
        print(f"Total images: {summary['total_images']}\n")
        
        # In-progress stages
        in_progress = db.stages.get_in_progress()
        print(f"In-progress stages: {len(in_progress)}")
        
        for stage in in_progress:
            print(f"  {stage['batch_id']}/{stage['stage']}: "
                  f"{stage['duration_seconds']}s")
        
        # Recent errors
        errors = db.events.search(severity='error', limit=5)
        print(f"\nRecent errors: {len(errors)}")
        for error in errors:
            print(f"  {error['created_at']}: {error['message']}")
```

---

## See Also

- [Exception Handling](exceptions.md) - Exception reference
- [Best Practices](best-practices.md) - Recommended patterns

[← Back to Index](index.md)
