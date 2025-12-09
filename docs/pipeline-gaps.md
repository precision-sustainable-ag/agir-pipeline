# Pipeline Gaps Component

[← Back to Index](index.md)

The `gaps` component provides work discovery through pipeline gap analysis. It identifies batches where output files are missing, indicating processing needs. This "pipeline gaps" methodology serves as the source of truth for work discovery.

## Key Concept

Pipeline gaps are more reliable than status tracking alone because they're self-correcting. If files are manually deleted, added, or processing is interrupted, gap analysis automatically reflects the current reality.

---

## Methods

### `get_batches_with_gaps()`

Get batches that have missing output files for a given stage.

```python
batches = db.gaps.get_batches_with_gaps(
    stage='raw_to_jpg',
    limit=100,
    offset=0,
    order_by='batch_id',
    order_dir='ASC'
)
```

**Parameters:**
- `stage` (str): Stage name (e.g., 'raw_to_jpg', 'dng_to_jpg', 'object_detection')
- `limit` (int, optional): Maximum number of batches to return. Default: 100
- `offset` (int, optional): Pagination offset. Default: 0
- `order_by` (str, optional): Column to sort by. Default: 'batch_id'
- `order_dir` (str, optional): Sort direction ('ASC' or 'DESC'). Default: 'ASC'

**Returns:** `List[Dict]` - List of batches with gap information:
```python
[
    {
        'batch_id': 'B001',
        'input_count': 150,
        'output_count': 145,
        'gap_count': 5,
        'gap_percentage': 3.33,
        'first_gap_image': 'MD_1683434234',
        'last_gap_image': 'MD_1683434890'
    },
    ...
]
```

**Raises:**
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database query fails

**Example:**
```python
# Get top 10 batches with most gaps
batches = db.gaps.get_batches_with_gaps(
    stage='raw_to_jpg',
    limit=10,
    order_by='gap_count',
    order_dir='DESC'
)

for batch in batches:
    print(f"Batch {batch['batch_id']}: {batch['gap_count']} missing files")
```

---

### `get_images_with_gaps()`

Get specific images missing output files for a given batch and stage.

```python
images = db.gaps.get_images_with_gaps(
    batch_id='B001',
    stage='raw_to_jpg',
    limit=1000
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name
- `limit` (int, optional): Maximum number of images to return. Default: 1000

**Returns:** `List[Dict]` - List of images with missing outputs:
```python
[
    {
        'image_id': 'MD_1683434234',
        'input_path': '/data/raw/B001/MD_1683434234.ARW',
        'expected_output_path': '/data/jpg/B001/MD_1683434234.jpg',
        'input_exists': True,
        'output_exists': False
    },
    ...
]
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database query fails

**Example:**
```python
# Get images needing processing
images = db.gaps.get_images_with_gaps('B001', 'raw_to_jpg')

for img in images:
    if img['input_exists'] and not img['output_exists']:
        process_image(img['input_path'], img['expected_output_path'])
```

---

### `get_gap_summary()`

Get summary statistics of gaps across all batches for a stage.

```python
summary = db.gaps.get_gap_summary(stage='raw_to_jpg')
```

**Parameters:**
- `stage` (str): Stage name

**Returns:** `Dict` - Summary statistics:
```python
{
    'total_batches': 45,
    'batches_with_gaps': 12,
    'total_gaps': 234,
    'total_images': 15000,
    'overall_gap_percentage': 1.56,
    'avg_gaps_per_batch': 19.5,
    'max_gaps_batch': 'B023',
    'max_gaps_count': 67
}
```

**Raises:**
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database query fails

**Example:**
```python
# Check overall pipeline health
summary = db.gaps.get_gap_summary('raw_to_jpg')
if summary['overall_gap_percentage'] > 5.0:
    print("Warning: High gap percentage detected!")
    print(f"Total gaps: {summary['total_gaps']}")
    print(f"Worst batch: {summary['max_gaps_batch']} with {summary['max_gaps_count']} gaps")
```

---

### `check_batch_complete()`

Check if a batch has all expected output files for a stage.

```python
is_complete = db.gaps.check_batch_complete(
    batch_id='B001',
    stage='raw_to_jpg'
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name

**Returns:** `bool` - True if all outputs exist, False if gaps remain

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database query fails

**Example:**
```python
# Verify before marking stage complete
if db.gaps.check_batch_complete('B001', 'raw_to_jpg'):
    db.stages.complete('B001', 'raw_to_jpg', success=True)
else:
    print("Batch still has gaps - not marking complete")
```

---

### `get_stage_progress()`

Get processing progress for a specific batch and stage.

```python
progress = db.gaps.get_stage_progress(
    batch_id='B001',
    stage='raw_to_jpg'
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name

**Returns:** `Dict` - Progress information:
```python
{
    'batch_id': 'B001',
    'stage': 'raw_to_jpg',
    'total_images': 150,
    'completed_images': 145,
    'remaining_images': 5,
    'completion_percentage': 96.67,
    'is_complete': False
}
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `InvalidStageError`: If stage name is invalid
- `QueryError`: If database query fails

**Example:**
```python
# Monitor progress
progress = db.gaps.get_stage_progress('B001', 'raw_to_jpg')
print(f"Progress: {progress['completion_percentage']:.1f}%")
print(f"Remaining: {progress['remaining_images']} images")
```

---

## Usage Patterns

### Work Discovery

The primary use case - discovering what needs to be processed:

```python
with AgirDB() as db:
    # Get all batches needing work
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        print(f"Batch {batch['batch_id']} needs {batch['gap_count']} images processed")
        
        # Get specific images
        images = db.gaps.get_images_with_gaps(batch['batch_id'], 'raw_to_jpg')
        
        # Process them
        for img in images:
            process_image(img)
```

### Pipeline Health Monitoring

Check overall pipeline status:

```python
with AgirDB() as db:
    for stage in ['raw_to_dng', 'dng_to_jpg', 'object_detection']:
        summary = db.gaps.get_gap_summary(stage)
        
        print(f"\n{stage}:")
        print(f"  Completion: {100 - summary['overall_gap_percentage']:.1f}%")
        print(f"  Batches with gaps: {summary['batches_with_gaps']}")
        print(f"  Total gaps: {summary['total_gaps']}")
```

### Progress Tracking

Monitor individual batch progress:

```python
with AgirDB() as db:
    progress = db.gaps.get_stage_progress('B001', 'raw_to_jpg')
    
    print(f"Batch B001 progress:")
    print(f"  {progress['completed_images']}/{progress['total_images']} images")
    print(f"  {progress['completion_percentage']:.1f}% complete")
    
    if progress['is_complete']:
        print("  ✓ Batch complete!")
```

### Verification Before Completion

Always verify gaps before marking a stage complete:

```python
with AgirDB() as db:
    # Process batch
    process_batch('B001', 'raw_to_jpg')
    
    # Verify completion
    if db.gaps.check_batch_complete('B001', 'raw_to_jpg'):
        db.stages.complete('B001', 'raw_to_jpg', success=True)
    else:
        # Get remaining gaps
        remaining = db.gaps.get_images_with_gaps('B001', 'raw_to_jpg')
        print(f"Warning: {len(remaining)} images still need processing")
        db.stages.complete('B001', 'raw_to_jpg', success=False,
                          error_message=f'{len(remaining)} images incomplete')
```

---

## Stage Naming Conventions

AgirDB uses underscore-separated stage names for flexibility:

**Conversion Stages:**
- `raw_to_dng` - RAW to DNG conversion
- `dng_to_jpg` - DNG to JPG conversion
- `raw_to_jpg` - Direct RAW to JPG (combines above)

**Computer Vision Stages:**
- `object_detection` - Object detection pipeline
- `segmentation` - Image segmentation
- `feature_extraction` - Feature extraction
- `metadata_extraction` - EXIF/metadata extraction

**Custom Stages:**
You can use any stage name that fits your pipeline. The system is generic and doesn't hardcode stage names.

---

## Integration with Stage Status

Gap analysis works hand-in-hand with [stage status tracking](stage-status.md):

```python
with AgirDB() as db:
    # Discover work with gaps
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        # Check if already being processed
        status = db.stages.get_status(batch['batch_id'], 'raw_to_jpg')
        if status and status['status'] == 'in_progress':
            print(f"Skipping {batch['batch_id']} - already in progress")
            continue
        
        # Start processing
        db.stages.start(batch['batch_id'], 'raw_to_jpg')
        
        # Get work from gaps
        images = db.gaps.get_images_with_gaps(batch['batch_id'], 'raw_to_jpg')
        process_images(images)
        
        # Verify and complete
        if db.gaps.check_batch_complete(batch['batch_id'], 'raw_to_jpg'):
            db.stages.complete(batch['batch_id'], 'raw_to_jpg', success=True)
```

---

## See Also

- [Stage Status Tracking](stage-status.md) - Prevent duplicate work
- [Orchestration Examples](orchestration.md) - Complete workflows
- [Best Practices](best-practices.md) - Production patterns

[← Back to Index](index.md)
