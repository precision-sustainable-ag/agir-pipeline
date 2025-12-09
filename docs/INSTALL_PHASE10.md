# Phase 10 Installation Guide

## Quick Install

No SQL schema required - Phase 10 uses existing tables from previous phases.

```bash
# Update Python package
cd /path/to/agir-db
pip install -e .
```

## Run Tests

```bash
# Run Phase 10 tests
python test_phase10.py

# Should see:
# ✓ All Phase 10 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 10 Complete!
```

## Quick Usage Test

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Test conversion summary
    summary = db.orchestration.get_conversion_summary()
    print(f"Queue: {summary['batches_in_queue']}")
    print(f"Active: {summary['batches_active']}")
    
    # Test conversion queue
    queue = db.orchestration.get_conversion_queue(limit=5)
    print(f"Found {len(queue)} batches needing conversion")
```

## Prerequisites

Phase 10 requires:
- ✓ All previous phases (1-9) installed
- ✓ Populated processed.batches table
- ✓ Populated source.globus_file_index table
- ✓ (Optional) svs-raw-api for actual conversions

## Integration with svs-raw-api

Phase 10 is designed to work with your svs-raw-api converters:

### Step 1: Verify svs-raw-api is Available

```bash
# Check if svs-raw-api is installed
python -c "from svs_raw_api import RawToDng, DngToJpg; print('✓ svs-raw-api available')"
```

### Step 2: Create Integration Script

```python
from agir_db import AgirDB
from pathlib import Path

# Import your converters
from svs_raw_api import RawToDng, DngToJpg

def convert_batch(batch_id: str):
    """Convert batch using svs-raw-api."""
    
    with AgirDB() as db:
        # Start conversion
        info = db.orchestration.start_batch_conversion(
            batch_id,
            job_id='worker-001'
        )
        db.commit()
        
        # Initialize converters
        raw_to_dng = RawToDng()
        dng_to_jpg = DngToJpg()
        
        files_processed = 0
        files_failed = 0
        
        for file in info['files']:
            try:
                raw_path = file['file_path']
                dng_path = f"/tmp/{file['image_id']}.dng"
                jpg_path = f"/data/jpg/{file['image_id']}.jpg"
                
                # Convert RAW -> DNG -> JPG
                raw_to_dng.convert(raw_path, dng_path)
                dng_to_jpg.convert(dng_path, jpg_path)
                
                files_processed += 1
                
            except Exception as e:
                print(f"Failed {file['file_name']}: {e}")
                files_failed += 1
        
        # Complete
        db.orchestration.complete_batch_conversion(
            batch_id,
            success=(files_failed == 0),
            files_processed=files_processed,
            files_failed=files_failed
        )
        db.commit()

# Test it
convert_batch('MD_2024-06-01')
```

### Step 3: Test Single Batch

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get a small batch for testing
    queue = db.orchestration.get_conversion_queue(limit=1)
    
    if queue:
        batch_id = queue[0]['batch_id']
        print(f"Testing with batch: {batch_id}")
        print(f"Files to convert: {queue[0]['gap_count']}")
        
        # Try converting it
        convert_batch(batch_id)
    else:
        print("No batches in queue")
```

## Workflow Setup

### 1. Manual Processing

```python
#!/usr/bin/env python3
"""
Manual batch processing script.
"""

from agir_db import AgirDB

def process_queue():
    with AgirDB() as db:
        queue = db.orchestration.get_conversion_queue(limit=10)
        
        for batch in queue:
            print(f"Processing {batch['batch_id']}...")
            # Your conversion code here

if __name__ == '__main__':
    process_queue()
```

Run manually:
```bash
python process_queue.py
```

### 2. Scheduled Job (Cron)

```bash
# Edit crontab
crontab -e

# Add line to run every hour
0 * * * * /usr/bin/python3 /path/to/process_queue.py >> /var/log/conversions.log 2>&1
```

### 3. Continuous Processing

```python
#!/usr/bin/env python3
"""
Continuous processing daemon.
"""

import time
from agir_db import AgirDB

def process_continuously():
    """Process queue continuously."""
    
    while True:
        try:
            with AgirDB() as db:
                queue = db.orchestration.get_conversion_queue(limit=1)
                
                if queue:
                    batch = queue[0]
                    print(f"Processing {batch['batch_id']}...")
                    # Your conversion code here
                else:
                    print("Queue empty, waiting...")
                    time.sleep(60)  # Wait 1 minute
                    
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == '__main__':
    process_continuously()
```

Run as daemon:
```bash
nohup python process_daemon.py &
```

## Troubleshooting

### Error: "No batches in queue"

Check if batches have gaps:
```sql
SELECT
    batch_id,
    file_count_raw,
    file_count_jpg,
    (file_count_raw - COALESCE(file_count_jpg, 0)) as gap_count
FROM processed.batches
WHERE file_count_raw > 0
  AND (file_count_raw - COALESCE(file_count_jpg, 0)) > 0
LIMIT 10;
```

If no gaps found:
1. Sync inventory: `db.inventory.sync_recent(days=30)`
2. Check if files exist: `ls /data/raw/`
3. Check globus_file_index: `SELECT COUNT(*) FROM source.globus_file_index`

### Error: "Stage already in progress"

Check active conversions:
```python
active = db.orchestration.get_active_conversions()
for conv in active:
    print(f"{conv['batch_id']}: {conv['status']}")
```

If stuck, complete manually:
```python
db.orchestration.complete_batch_conversion(
    'MD_2024-06-01',
    success=False,
    files_processed=0,
    error_message='Manually cleared stuck stage'
)
db.commit()
```

### Error: "Files not found"

Check file paths:
```python
files = db.orchestration.get_batch_files_for_conversion('MD_2024-06-01')
for file in files[:5]:
    path = Path(file['file_path'])
    print(f"{file['file_name']}: exists={path.exists()}")
```

Update paths if needed:
```sql
UPDATE source.globus_file_index
SET file_path = '/new/path/' || file_name
WHERE batch_id = 'MD_2024-06-01';
```

### Slow Conversions

Check conversion rate:
```python
summary = db.orchestration.get_conversion_summary()
print(f"Average rate: {summary['avg_files_per_second']:.2f} files/sec")
```

If rate is low:
1. Check CPU/memory usage
2. Check disk I/O
3. Consider parallel processing
4. Profile conversion code

### High Queue Size

```python
summary = db.orchestration.get_conversion_summary()

if summary['batches_in_queue'] > 100:
    print("Large queue - consider:")
    print("  - Adding more workers")
    print("  - Increasing worker capacity")
    print("  - Prioritizing critical batches")
```

## Performance Tips

### 1. Batch Progress Updates

Don't update too frequently:
```python
# Good: Update every 10 files
if i % 10 == 0:
    db.orchestration.update_conversion_progress(batch_id, i)
    db.commit()

# Bad: Update every file (slow)
db.orchestration.update_conversion_progress(batch_id, i)
db.commit()
```

### 2. Use Filters

Process by priority:
```python
# Process specific location first
queue = db.orchestration.get_conversion_queue(
    limit=10,
    location='JUNO'
)
```

### 3. Monitor Active Conversions

```python
active = db.orchestration.get_active_conversions()

if len(active) > 10:
    print("Too many active conversions, wait for some to finish")
else:
    # Start new conversion
    pass
```

### 4. Optimize svs-raw-api

Check svs-raw-api configuration:
- DNG compression settings
- JPG quality settings
- Thread usage
- Memory limits

## Verification Queries

After processing, verify results:

```sql
-- Check completion
SELECT
    batch_id,
    file_count_raw,
    file_count_jpg,
    raw_to_jpg_complete
FROM processed.batches
WHERE batch_id = 'MD_2024-06-01';

-- Check stage status
SELECT
    batch_id,
    stage,
    status,
    files_processed,
    files_failed,
    duration_seconds
FROM processed.stage_status
WHERE batch_id = 'MD_2024-06-01'
  AND stage = 'raw_to_jpg';

-- Check gaps
SELECT COUNT(*) as remaining_gaps
FROM source.globus_file_index
WHERE batch_id = 'MD_2024-06-01'
  AND LOWER(file_ext) IN ('raw', 'arw')
  AND NOT EXISTS (
      SELECT 1 FROM source.globus_file_index jpg
      WHERE jpg.batch_id = 'MD_2024-06-01'
        AND LOWER(jpg.file_ext) = 'jpg'
        AND REGEXP_REPLACE(jpg.file_name, '\\.(pp3|xmp|jpg|jpeg)$', '', 'i')
          = REGEXP_REPLACE(source.globus_file_index.file_name, '\\.(pp3|xmp|raw|arw)$', '', 'i')
  );
```

## Next Steps

After Phase 10:
1. Test with small batches first
2. Monitor performance and errors
3. Scale up to full queue
4. Set up monitoring dashboards
5. Configure alerts for failures
6. Document your deployment

## What Changed

Phase 10 added:
1. **Python Class**: `Orchestration` with 10 methods
2. **Integration**: Added `db.orchestration` to AgirDB facade
3. **Exception**: Added `OrchestrationError`

## Complete!

**ALL 10 PHASES ARE NOW INSTALLED! 🎉**

The AgirDB API is 100% complete and ready for production use.
