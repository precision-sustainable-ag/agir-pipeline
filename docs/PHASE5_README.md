# Phase 5: Image & Batch Metadata - Complete ✓

## Overview

Phase 5 implements metadata storage and management for images and batches. This provides structured storage for EXIF data, bounding boxes, processing status, and batch-level statistics.

**Why Metadata Management?**
- **Track processing**: Know which images have been processed through each stage
- **Store EXIF data**: Camera settings, GPS coordinates, capture time
- **Object detection**: Store and query bounding boxes from CV models
- **Batch statistics**: Aggregate file counts and completion status
- **Efficient queries**: Find images by batch, camera, date, detections

## Components Created

### 1. **SQL Schema** (metadata_schema.sql, ~400 lines)

**Tables:**

**`processed.batches`** - Batch-level metadata:
```sql
CREATE TABLE processed.batches (
    batch_id TEXT PRIMARY KEY,
    batch_state TEXT NOT NULL,              -- 'MD', 'TX', 'NC'
    batch_date DATE NOT NULL,
    location TEXT,                          -- 'JUNO', 'CERES', 'NCSU'
    processing_status TEXT,                 -- pending, in_progress, completed, etc.
    file_count_raw INTEGER,
    file_count_jpg INTEGER,
    file_count_metadata INTEGER,
    file_count_cutout INTEGER,
    total_bytes BIGINT,
    raw_to_jpg_complete BOOLEAN,
    jpg_to_metadata_complete BOOLEAN,
    metadata_to_cutouts_complete BOOLEAN,
    metadata JSONB,
    ...
);
```

**`processed.images`** - Image-level metadata:
```sql
CREATE TABLE processed.images (
    image_id TEXT PRIMARY KEY,
    batch_id TEXT REFERENCES processed.batches,
    file_name TEXT NOT NULL,
    processing_status TEXT,                 -- pending, raw_to_dng, dng_to_jpg, etc.
    
    -- EXIF data
    exif_data JSONB,
    camera_make TEXT,
    camera_model TEXT,
    capture_datetime TIMESTAMPTZ,
    gps_latitude NUMERIC,
    gps_longitude NUMERIC,
    width INTEGER,
    height INTEGER,
    
    -- Bounding boxes
    bounding_boxes JSONB,
    detection_count INTEGER,
    
    -- Processing results
    jpg_path TEXT,
    metadata_path TEXT,
    cutout_paths TEXT[],
    
    metadata JSONB,
    ...
);
```

**Helper Views:**
- `batch_summary` - Comprehensive batch statistics with image counts
- `images_with_detections` - Images that have object detections
- `pending_images_by_batch` - Count of unprocessed images per batch
- `failed_images_by_batch` - Count of failed images per batch
- `camera_stats` - Usage statistics by camera make/model

**Features:**
- 20+ indexes for fast queries (batch, status, camera, GPS, detections)
- JSONB indexes for metadata and bounding boxes
- Foreign key constraints (images → batches)
- Auto-update timestamps via triggers
- Comprehensive CHECK constraints

### 2. **ImageMetadata Class** (images.py, ~450 lines)

Manage image-level metadata:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Insert image
    db.images.insert(
        image_id='MD_1234',
        batch_id='MD_2025-01-01',
        file_name='MD_1234.raw',
        file_size_bytes=25000000,
        camera_make='Canon',
        camera_model='EOS R5',
        width=8192,
        height=5464
    )
    
    # Update bounding boxes
    boxes = [
        {'x': 100, 'y': 200, 'width': 50, 'height': 50, 
         'class': 'deer', 'confidence': 0.95}
    ]
    db.images.update_bounding_boxes('MD_1234', boxes)
    
    # Query images
    images = db.images.get_by_batch('MD_2025-01-01')
    detections = db.images.get_with_detections(min_detections=5)
```

**Main Methods:**

1. **`insert(image_id, batch_id, file_name, ...)`**
   - Insert single image record
   - Store EXIF data, dimensions, camera info

2. **`insert_bulk(images)`**
   - Bulk insert multiple images
   - More efficient than individual inserts

3. **`update_status(image_id, processing_status)`**
   - Update processing status
   - Track pipeline progression

4. **`update_bounding_boxes(image_id, bounding_boxes)`**
   - Store detection results
   - Auto-updates detection_count

5. **`get_by_id(image_id)`**
   - Get single image record
   - Returns None if not found

6. **`get_by_batch(batch_id, processing_status=None, limit=None)`**
   - Get all images in batch
   - Optional status filtering

7. **`get_with_detections(batch_id=None, min_detections=1, limit=None)`**
   - Find images with object detections
   - Filter by detection count

**Valid Processing Statuses:**
- `pending` - Not yet processed
- `raw_to_dng` - RAW converted to DNG
- `dng_to_jpg` - DNG developed to JPG
- `metadata_extracted` - Metadata extracted
- `cutouts_generated` - Cutouts generated
- `completed` - All processing complete
- `failed` - Processing failed

### 3. **BatchMetadata Class** (batches.py, ~400 lines)

Manage batch-level metadata:

```python
from agir_db import AgirDB
from datetime import date

with AgirDB() as db:
    # Insert batch
    db.batches.insert(
        batch_id='MD_2025-01-01',
        batch_state='MD',
        batch_date=date(2025, 1, 1),
        location='JUNO'
    )
    
    # Update file counts
    db.batches.update_file_counts(
        'MD_2025-01-01',
        file_count_raw=150,
        file_count_jpg=150
    )
    
    # Update completion flags
    db.batches.update_completion_flags(
        'MD_2025-01-01',
        raw_to_jpg_complete=True
    )
    
    # Query batches
    batches = db.batches.get_by_state('MD', limit=10)
    summary = db.batches.get_summary(batch_id='MD_2025-01-01')
```

**Main Methods:**

1. **`insert(batch_id, batch_state, batch_date, ...)`**
   - Insert batch record
   - Set location, paths, initial status

2. **`update_status(batch_id, processing_status)`**
   - Update processing status
   - Track overall batch progress

3. **`update_file_counts(batch_id, file_count_raw, ...)`**
   - Update file counts
   - Tracks RAW, JPG, metadata, cutout counts

4. **`update_completion_flags(batch_id, raw_to_jpg_complete, ...)`**
   - Update stage completion flags
   - Boolean flags for each pipeline stage

5. **`get_by_id(batch_id)`**
   - Get single batch record

6. **`get_by_state(batch_state, limit=None)`**
   - Get batches for specific state
   - Ordered by date (newest first)

7. **`get_by_status(processing_status, limit=None)`**
   - Get batches by processing status
   - Find pending, in-progress, or failed batches

8. **`get_summary(batch_id=None, batch_state=None, limit=None)`**
   - Get comprehensive batch summary
   - Includes file counts and image statistics

**Valid Processing Statuses:**
- `pending` - Not yet processed
- `in_progress` - Currently processing
- `completed` - All stages complete
- `partial` - Some stages complete
- `failed` - Processing failed

### 4. **Integration with AgirDB**

Both classes are accessible through the main facade:

```python
from agir_db import AgirDB

with AgirDB() as db:
    db.images.insert(...)
    db.images.get_by_batch(...)
    
    db.batches.insert(...)
    db.batches.get_summary(...)
```

## Installation

### Step 1: Install SQL Schema

```bash
# Connect to your database
source /project/dash_agir/postgres/pg_coords.env
psql

# Run the schema file
\i /path/to/metadata_schema.sql

# Verify tables exist
\d processed.batches
\d processed.images
\dv processed.*
```

Expected output:
```
                Table "processed.batches"
       Column        |           Type           | Nullable
---------------------+--------------------------+----------
 batch_id            | text                     | not null
 batch_state         | text                     | not null
 batch_date          | date                     | not null
 ...

                Table "processed.images"
       Column        |           Type           | Nullable
---------------------+--------------------------+----------
 image_id            | text                     | not null
 batch_id            | text                     | not null
 file_name           | text                     | not null
 ...

 Schema    |           Name              | Type
-----------+-----------------------------+------
 processed | batch_summary               | view
 processed | camera_stats                | view
 processed | failed_images_by_batch      | view
 processed | images_with_detections      | view
 processed | pending_images_by_batch     | view
```

### Step 2: Update Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

## Testing

### Unit Tests (no database required)

```bash
python test_phase5.py
```

Expected output:
```
============================================================
Phase 5 - Image & Batch Metadata Tests
============================================================
Testing valid statuses...
✓ Valid statuses are correct

Testing ImageMetadata initialization...
✓ ImageMetadata initializes correctly

...

============================================================
✓ All Phase 5 unit tests passed!
============================================================
```

### Database Integration Tests (requires live database)

The test script automatically runs integration tests including:
1. Batch insert and query
2. Duplicate detection
3. Status updates
4. File count updates
5. Completion flag updates
6. Image insert and query
7. Bounding box updates
8. Bulk insert
9. Batch summary queries

## Usage Examples

### Example 1: Register Batch and Images

```python
from agir_db import AgirDB
from datetime import date
from pathlib import Path

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    # 1. Register batch
    db.batches.insert(
        batch_id=batch_id,
        batch_state='MD',
        batch_date=date(2025, 1, 1),
        location='JUNO',
        lts_root='lts01',
        root_path='/lts/MD_2025-01-01',
        processing_status='pending'
    )
    
    # 2. Find RAW files and register images
    raw_files = list(Path('/lts/MD_2025-01-01').glob('*.raw'))
    
    images = []
    for raw_file in raw_files:
        images.append({
            'image_id': raw_file.stem,
            'batch_id': batch_id,
            'file_name': raw_file.name,
            'file_ext': 'raw',
            'file_size_bytes': raw_file.stat().st_size,
            'processing_status': 'pending'
        })
    
    # 3. Bulk insert images
    count = db.images.insert_bulk(images)
    print(f"Registered {count} images")
    
    # 4. Update batch file counts
    db.batches.update_file_counts(
        batch_id,
        file_count_raw=len(images),
        total_bytes=sum(img['file_size_bytes'] for img in images)
    )
    
    db.commit()
```

### Example 2: Track Processing Progress

```python
from agir_db import AgirDB

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    # Start processing
    db.batches.update_status(batch_id, 'in_progress')
    
    # Get pending images
    images = db.images.get_by_batch(batch_id, processing_status='pending')
    
    for image in images:
        image_id = image['image_id']
        
        try:
            # Process image (your code here)
            process_image(image_id)
            
            # Update status
            db.images.update_status(image_id, 'completed')
            
        except Exception as e:
            # Mark as failed
            db.images.update_status(image_id, 'failed')
    
    # Check if all done
    summary = db.batches.get_summary(batch_id=batch_id)[0]
    if summary['completed_images'] == summary['registered_images']:
        db.batches.update_status(batch_id, 'completed')
        db.batches.update_completion_flags(
            batch_id,
            raw_to_jpg_complete=True
        )
    
    db.commit()
```

### Example 3: Store EXIF Data

```python
from agir_db import AgirDB
from PIL import Image
from PIL.ExifTags import TAGS
from datetime import datetime

def extract_exif(image_path):
    """Extract EXIF data from image."""
    img = Image.open(image_path)
    exif_data = {}
    
    if hasattr(img, '_getexif') and img._getexif():
        for tag_id, value in img._getexif().items():
            tag = TAGS.get(tag_id, tag_id)
            exif_data[tag] = str(value)
    
    return exif_data

with AgirDB() as db:
    image_id = 'MD_1234'
    
    # Extract EXIF
    exif = extract_exif('/path/to/MD_1234.jpg')
    
    # Update image with EXIF data
    db._connection.execute("""
        UPDATE processed.images
        SET 
            exif_data = %s,
            camera_make = %s,
            camera_model = %s,
            capture_datetime = %s,
            width = %s,
            height = %s
        WHERE image_id = %s
    """, (
        Json(exif),
        exif.get('Make'),
        exif.get('Model'),
        datetime.fromisoformat(exif.get('DateTime')) if exif.get('DateTime') else None,
        exif.get('ExifImageWidth'),
        exif.get('ExifImageHeight'),
        image_id
    ))
    
    db.commit()
```

### Example 4: Store Object Detections

```python
from agir_db import AgirDB

def run_object_detection(image_path):
    """Run object detection model (placeholder)."""
    # Your CV model here
    return [
        {'x': 100, 'y': 200, 'width': 50, 'height': 50, 
         'class': 'deer', 'confidence': 0.95},
        {'x': 300, 'y': 400, 'width': 60, 'height': 70, 
         'class': 'deer', 'confidence': 0.88}
    ]

with AgirDB() as db:
    batch_id = 'MD_2025-01-01'
    
    # Get images needing detection
    images = db.images.get_by_batch(
        batch_id,
        processing_status='metadata_extracted'
    )
    
    for image in images:
        image_id = image['image_id']
        jpg_path = image['jpg_path']
        
        # Run detection
        detections = run_object_detection(jpg_path)
        
        # Store bounding boxes
        db.images.update_bounding_boxes(image_id, detections)
        
        # Update status
        db.images.update_status(image_id, 'cutouts_generated')
    
    db.commit()
```

### Example 5: Query Statistics

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Batch summary
    summaries = db.batches.get_summary(batch_state='MD', limit=10)
    
    print("Batch Statistics:")
    for summary in summaries:
        print(f"\nBatch: {summary['batch_id']}")
        print(f"  RAW files: {summary['file_count_raw']}")
        print(f"  JPG files: {summary['file_count_jpg']}")
        print(f"  Registered images: {summary['registered_images']}")
        print(f"  Completed images: {summary['completed_images']}")
        print(f"  Failed images: {summary['failed_images']}")
        print(f"  Total detections: {summary['total_detections']}")
        print(f"  Status: {summary['processing_status']}")
    
    # Images with most detections
    top_detections = db.images.get_with_detections(
        min_detections=10,
        limit=20
    )
    
    print(f"\nTop {len(top_detections)} images by detection count:")
    for img in top_detections:
        print(f"  {img['image_id']}: {img['detection_count']} detections")
```

### Example 6: Camera Statistics

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Query camera usage
    query = """
        SELECT * FROM processed.camera_stats
        ORDER BY image_count DESC
        LIMIT 10;
    """
    
    cameras = db._connection.fetch_all(query)
    
    print("Camera Usage Statistics:")
    for cam in cameras:
        print(f"\n{cam['camera_make']} {cam['camera_model']}")
        print(f"  Images: {cam['image_count']}")
        print(f"  Batches: {cam['batch_count']}")
        print(f"  Avg detections/image: {cam['avg_detections_per_image']:.2f}")
        print(f"  Date range: {cam['first_capture']} to {cam['last_capture']}")
```

### Example 7: Integration with Pipeline Gaps

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Find work using gaps
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        batch_id = batch['batch_id']
        
        # Check if batch is registered
        batch_record = db.batches.get_by_id(batch_id)
        
        if not batch_record:
            # Register batch first
            db.batches.insert(
                batch_id=batch_id,
                batch_state=batch['batch_state'],
                batch_date=batch['batch_date'],
                processing_status='pending'
            )
        
        # Get files needing work
        files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
        
        # Check which are registered as images
        for file in files:
            image_id = file['base_name']
            image = db.images.get_by_id(image_id)
            
            if not image:
                # Register image
                db.images.insert(
                    image_id=image_id,
                    batch_id=batch_id,
                    file_name=file['file_name'],
                    file_size_bytes=file['size_bytes'],
                    processing_status='pending'
                )
    
    db.commit()
```

## Integration with Previous Phases

### With Phase 2 (Pipeline Gaps)
Register batches and images discovered through gap analysis.

### With Phase 3 (Stage Status)
Update image/batch status as stages complete:
```python
# When stage completes
db.stages.complete(batch_id, stage, success=True)
db.batches.update_completion_flags(batch_id, raw_to_jpg_complete=True)
```

### With Phase 4 (Event Logging)
Log metadata operations:
```python
db.images.insert(...)
db.events.log_event(
    event_type='metadata.image_registered',
    severity='INFO',
    message=f'Registered image {image_id}',
    batch_id=batch_id
)
```

## Files Created

```
agir-db/
├── src/agir_db/
│   ├── images.py                        # ImageMetadata class (450 lines)
│   ├── batches.py                       # BatchMetadata class (400 lines)
│   ├── api.py                           # Updated with metadata integration
│   └── __init__.py                      # Updated exports
│
├── sql/schemas/03_processed/
│   └── metadata_schema.sql              # Tables, views, indexes (400 lines)
│
└── tests/
    └── test_phase5.py                   # Test suite (600 lines)

Total new code: ~1,850 lines
```

## API Reference

### ImageMetadata Methods

#### `insert(image_id, batch_id, file_name, ...)`
Insert single image record.

#### `insert_bulk(images) -> int`
Bulk insert multiple images. Returns count.

#### `update_status(image_id, processing_status)`
Update processing status.

#### `update_bounding_boxes(image_id, bounding_boxes)`
Store detection results.

#### `get_by_id(image_id) -> dict | None`
Get single image.

#### `get_by_batch(batch_id, processing_status=None, limit=None) -> list[dict]`
Get images in batch.

#### `get_with_detections(batch_id=None, min_detections=1, limit=None) -> list[dict]`
Get images with object detections.

---

### BatchMetadata Methods

#### `insert(batch_id, batch_state, batch_date, ...)`
Insert batch record.

#### `update_status(batch_id, processing_status)`
Update processing status.

#### `update_file_counts(batch_id, file_count_raw, ...)`
Update file counts.

#### `update_completion_flags(batch_id, raw_to_jpg_complete, ...)`
Update stage completion flags.

#### `get_by_id(batch_id) -> dict | None`
Get single batch.

#### `get_by_state(batch_state, limit=None) -> list[dict]`
Get batches by state.

#### `get_by_status(processing_status, limit=None) -> list[dict]`
Get batches by status.

#### `get_summary(batch_id=None, batch_state=None, limit=None) -> list[dict]`
Get comprehensive batch summary.

## Next Steps: Phase 6 (Inventory Sync)

Phase 6 will implement synchronization from globus_file_index:

1. **InventorySync Class**
   - Sync batches from globus_file_index
   - Sync images from globus_file_index
   - Reconcile differences
   - Incremental updates

2. **Methods**
   - `sync_batch(batch_id)` - Sync one batch
   - `sync_all()` - Full sync
   - `reconcile()` - Check for differences

## Status

**Phase 5: COMPLETE ✓**

All metadata management components are implemented and tested:
- ✓ SQL schema (2 tables, 5 views, 20+ indexes)
- ✓ ImageMetadata class (7 main methods)
- ✓ BatchMetadata class (8 main methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 6!**