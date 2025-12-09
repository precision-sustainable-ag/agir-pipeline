# Phase 2: Pipeline Gaps - Complete ✓

## Overview

Phase 2 implements work discovery through pipeline gap analysis - identifying batches and files that need processing by detecting missing outputs.

The "pipeline gaps" approach is superior to status tracking because:
1. **Files either exist or they don't** (simple source of truth)
2. **Self-correcting** (if files appear, gaps disappear automatically)
3. **Handles edge cases** (partial processing, crashes, manual fixes)
4. **No complex state machines** to maintain

## Components Created

### 1. **SQL Views** (pipeline_gaps_schema.sql, ~350 lines)

Eight SQL views for gap detection and pipeline status:

**File-Level Gap Views:**
- `report.files_needing_raw_to_jpg` - RAW files without JPG outputs
- `report.files_needing_jpg_to_metadata` - JPG files without metadata JSON
- `report.files_needing_metadata_to_cutouts` - Metadata files without cutouts

**Batch-Level Gap Views:**
- `report.batches_needing_raw_to_jpg` - Batches needing RAW → JPG
- `report.batches_needing_jpg_to_metadata` - Batches needing JPG → metadata
- `report.batches_needing_metadata_to_cutouts` - Batches needing metadata → cutouts

**Master Status Views:**
- `report.batch_pipeline_status` - Complete pipeline status per batch
- `report.pipeline_gap_summary` - Aggregate statistics across all stages

### 2. **PipelineGaps Class** (gaps.py, ~350 lines)

Work discovery API with four main methods:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # 1. Find batches needing work
    batches = db.gaps.get_batches_with_gaps(
        stage='raw_to_jpg',
        limit=10
    )
    # Returns: [{'batch_id': 'MD_2025-01-01', 'files_needing_processing': 150, ...}]
    
    # 2. Get specific files to process
    files = db.gaps.get_files_with_gap(
        batch_id='MD_2025-01-01',
        stage='raw_to_jpg'
    )
    # Returns: [{'file_name': 'MD_123.raw', 'root_path': '/path', ...}]
    
    # 3. Get complete pipeline status for a batch
    summary = db.gaps.get_batch_pipeline_summary('MD_2025-01-01')
    # Returns: {
    #   'raw_count': 150,
    #   'jpg_count': 0,
    #   'raw_to_jpg_gap': 150,
    #   'raw_to_jpg_complete': False,
    #   ...
    # }
    
    # 4. Get overall statistics
    stats = db.gaps.get_gap_summary()  # or get_gap_summary('raw_to_jpg')
    # Returns: [{
    #   'stage': 'raw_to_jpg',
    #   'batches_with_gaps': 25,
    #   'total_files_with_gaps': 3750,
    #   ...
    # }]
```

**Valid Pipeline Stages:**
- `'raw_to_jpg'` - RAW → DNG → JPG conversion
- `'jpg_to_metadata'` - Metadata extraction (EXIF, bounding boxes)
- `'metadata_to_cutouts'` - Cutout generation from bounding boxes

### 3. **Integration with AgirDB**

PipelineGaps is now accessible through the main facade:

```python
from agir_db import AgirDB

with AgirDB() as db:
    db.gaps.get_batches_with_gaps('raw_to_jpg')
    db.gaps.get_files_with_gap('MD_2025-01-01', 'raw_to_jpg')
    db.gaps.get_batch_pipeline_summary('MD_2025-01-01')
    db.gaps.get_gap_summary()
```

## Installation

### Step 1: Install SQL Views

```bash
# Connect to your database
source /project/dash_agir/postgres/pg_coords.env
psql

# Run the schema file
\i /path/to/pipeline_gaps_schema.sql

# Verify views exist
\dv report.*
```

Expected output:
```
 Schema |              Name               | Type |      Owner
--------+---------------------------------+------+-----------------
 report | batches_needing_raw_to_jpg      | view | matthew.kutugata
 report | batch_pipeline_status           | view | matthew.kutugata
 report | files_needing_raw_to_jpg        | view | matthew.kutugata
 report | pipeline_gap_summary            | view | matthew.kutugata
 ...
```

### Step 2: Update Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

## Testing

### Unit Tests (no database required)

```bash
python test_phase2.py
```

Expected output:
```
============================================================
Phase 2 - Pipeline Gaps Tests
============================================================
Testing valid stages...
✓ Valid stages are correct

Testing PipelineGaps initialization...
✓ PipelineGaps initializes correctly

Testing stage validation...
✓ Stage validation works correctly

Testing AgirDB.gaps integration...
✓ AgirDB.gaps integration works correctly

Testing method signatures...
✓ Method signatures are correct

============================================================
✓ All Phase 2 unit tests passed!
============================================================
```

### Database Integration Tests (requires live database)

The test script will automatically try to connect and run integration tests.
If you haven't installed the SQL views yet, these tests will be skipped.

## Usage Examples

### Example 1: Basic Work Discovery

```python
from agir_db import AgirDB, setup_logging

setup_logging(level='INFO')

with AgirDB() as db:
    # Find batches needing RAW → JPG processing
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        print(f"{batch['batch_id']}: {batch['files_needing_processing']} files")
        print(f"  Location: {batch['primary_location']}")
        print(f"  Total size: {batch['total_bytes'] / 1e9:.2f} GB")
```

### Example 2: File-Level Processing

```python
from agir_db import AgirDB
from pathlib import Path

with AgirDB() as db:
    # Get batches needing work
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=5)
    
    for batch in batches:
        batch_id = batch['batch_id']
        
        # Get specific files to process
        files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
        
        for file in files:
            # Construct full path
            raw_path = Path(file['root_path']) / file['rel_path']
            
            # Process file (your code here)
            print(f"Processing: {raw_path}")
```

### Example 3: Pipeline Status Monitoring

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get complete pipeline status for a batch
    summary = db.gaps.get_batch_pipeline_summary('MD_2025-01-01')
    
    if summary:
        print(f"Batch: {summary['batch_id']}")
        print(f"RAW files: {summary['raw_count']}")
        print(f"JPG files: {summary['jpg_count']}")
        print(f"Metadata files: {summary['metadata_count']}")
        print(f"Cutout files: {summary['cutout_count']}")
        print()
        print(f"RAW → JPG gap: {summary['raw_to_jpg_gap']}")
        print(f"JPG → Metadata gap: {summary['jpg_to_metadata_gap']}")
        print(f"Metadata → Cutouts gap: {summary['metadata_to_cutouts_gap']}")
        print()
        print(f"RAW → JPG complete: {summary['raw_to_jpg_complete']}")
        print(f"JPG → Metadata complete: {summary['jpg_to_metadata_complete']}")
        print(f"Has cutouts: {summary['has_cutouts']}")
```

### Example 4: Overall Statistics

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get overall gap statistics
    stats = db.gaps.get_gap_summary()
    
    for stage_stats in stats:
        print(f"Stage: {stage_stats['stage']}")
        print(f"  Batches with gaps: {stage_stats['batches_with_gaps']}")
        print(f"  Files with gaps: {stage_stats['total_files_with_gaps']}")
        print(f"  Total size: {stage_stats['total_bytes'] / 1e9:.2f} GB")
        print()
```

## How Gap Detection Works

### Matching Logic

Gap detection uses filename matching without extensions:

```
RAW file:  "MD_1683434234.raw"  → base_name = "MD_1683434234"
JPG file:  "MD_1683434234.jpg"  → base_name = "MD_1683434234"
JSON file: "MD_1683434234.json" → base_name = "MD_1683434234"
```

Files match if:
1. Same `batch_id`
2. Same `base_name` (filename without extension)

### Example Gap Scenarios

**Scenario 1: RAW file without JPG**
```
Input:  source.globus_file_index
  - MD_2025-01-01/MD_123.raw    (upload_raw)
  
Output: report.files_needing_raw_to_jpg
  - MD_123.raw (gap detected!)
```

**Scenario 2: RAW file WITH JPG (no gap)**
```
Input:  source.globus_file_index
  - MD_2025-01-01/MD_123.raw      (upload_raw)
  - MD_2025-01-01/images/MD_123.jpg  (developed_jpg)
  
Output: report.files_needing_raw_to_jpg
  - (empty - no gap)
```

**Scenario 3: Partial batch processing**
```
Input:  source.globus_file_index
  - MD_2025-01-01/MD_123.raw      (upload_raw)
  - MD_2025-01-01/MD_456.raw      (upload_raw)
  - MD_2025-01-01/images/MD_123.jpg  (developed_jpg)
  
Output: report.files_needing_raw_to_jpg
  - MD_456.raw (gap detected for this file only)
```

## Files Created

```
agir-db/
├── src/agir_db/
│   ├── gaps.py                          # PipelineGaps class (350 lines)
│   ├── api.py                           # Updated with gaps integration
│   └── __init__.py                      # Updated exports
│
├── sql/schemas/06_report/
│   └── pipeline_gaps_schema.sql         # 8 SQL views (350 lines)
│
└── tests/
    └── test_phase2.py                   # Test suite (300 lines)

Total new code: ~1,000 lines
```

## API Reference

### PipelineGaps Methods

#### `get_batches_with_gaps(stage, limit=None)`

Find batches with missing outputs.

**Parameters:**
- `stage` (str): Pipeline stage ('raw_to_jpg', 'jpg_to_metadata', 'metadata_to_cutouts')
- `limit` (int, optional): Maximum number of batches to return

**Returns:** List of batch dictionaries with gap information

**Raises:** `InvalidParameterError` if stage is invalid

---

#### `get_files_with_gap(batch_id, stage)`

Get specific files within a batch that need processing.

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Pipeline stage

**Returns:** List of file dictionaries with full paths

**Raises:** `InvalidParameterError` if stage is invalid

---

#### `get_batch_pipeline_summary(batch_id)`

Get complete pipeline status for one batch.

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** Dictionary with counts and completion flags, or None if not found

---

#### `get_gap_summary(stage=None)`

Get aggregate statistics about pipeline gaps.

**Parameters:**
- `stage` (str, optional): Specific stage, or None for all stages

**Returns:** List of summary dictionaries (one per stage)

## Next Steps: Phase 3 (Stage Status)

Phase 3 will implement stage execution tracking to prevent duplicate work:

1. **SQL Table** (sql/schemas/03_processed/)
   - `processed.stage_status` - Track in-progress stages

2. **StageStatus Class** (stages.py, ~250 lines)
   - `start(batch_id, stage, job_id)` - Mark stage as started
   - `complete(batch_id, stage, success, error_message)` - Mark as completed
   - `reset(batch_id, stage)` - Clear failed status for retry
   - `get_status(batch_id, stage)` - Get current status
   - `get_in_progress(stage)` - Find stuck jobs

3. **Integration**
   - Uncomment `self.stages = StageStatus(self._connection)` in api.py
   - Create test_phase3.py

## Status

**Phase 2: COMPLETE ✓**

All pipeline gap components are implemented and tested:
- ✓ SQL views (8 views for gap detection)
- ✓ PipelineGaps class (4 main methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 3!**