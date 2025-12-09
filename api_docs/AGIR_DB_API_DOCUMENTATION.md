# AgirDB API Documentation

**Version:** 1.0.0  
**Author:** Matthew Kutugata

## Overview

AgirDB is a PostgreSQL-backed API for managing agricultural image processing pipelines. It provides robust work discovery through "pipeline gaps" analysis, comprehensive status tracking, and metadata management for computer vision workflows including RAW→DNG→JPG conversion, object detection, segmentation, and feature extraction.

### Key Concepts

**Pipeline Gaps as Source of Truth**: AgirDB uses a "pipeline gaps" methodology where missing output files indicate processing needs. This approach is self-correcting and more reliable than status tracking alone, as it handles edge cases like partial failures, interrupted processing, and manual file operations gracefully.

**Generic Pipeline Stages**: Rather than hardcoding specific workflows, AgirDB supports arbitrary stage names with underscore conventions (e.g., `raw_to_dng`, `dng_to_jpg`, `object_detection`), making it extensible for future computer vision pipelines.

**Clean Separation**: The API maintains clear boundaries between conversion logic and database operations, enabling easy integration into larger processing systems.

### Architecture

```
AgirDB (Main API)
├── gaps          # Pipeline gap analysis (work discovery)
├── stages        # Stage status tracking (in-progress monitoring)
├── images        # Image metadata management
├── batches       # Batch metadata management
├── transfers     # JUNO transfer operations
├── events        # Processing event logging
├── inventory     # File inventory synchronization
├── analytics     # Reporting and statistics
└── migration     # SQLite data import
```

---

## Installation & Setup

### Requirements

- Python 3.8+
- PostgreSQL 12+
- Environment variables or .pgpass file for authentication

### Installation

```bash
pip install agir-db
```

### Configuration

AgirDB uses PostgreSQL environment variables for connection:

```bash
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agir
export PGUSER=agir_user
# Password via .pgpass file recommended
```

Alternatively, pass credentials directly:

```python
from agir_db import AgirDB

db = AgirDB(
    host='localhost',
    port=5432,
    dbname='agir',
    user='agir_user',
    password='secret'
)
```

### Logging Setup

```python
from agir_db import setup_logging, set_level
import logging

# Configure logging
setup_logging(level=logging.INFO)

# Change level at runtime
set_level(logging.DEBUG)
```

---

## Quick Start

### Basic Usage

```python
from agir_db import AgirDB

# Using context manager (recommended)
with AgirDB() as db:
    # Discover batches needing processing
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        batch_id = batch['batch_id']
        
        # Start processing
        db.stages.start(batch_id, 'raw_to_jpg', job_id='worker-001')
        
        # Process images and insert metadata
        images = process_images(batch)
        db.images.insert_bulk(images)
        
        # Mark complete
        db.stages.complete(batch_id, 'raw_to_jpg', success=True)
```

### Manual Connection Management

```python
db = AgirDB()
db.connect()

try:
    # Do work
    result = db.images.get('MD_1683434234')
    db.commit()
except Exception as e:
    db.rollback()
    raise
finally:
    db.close()
```

---

## API Reference

## Core Connection Methods

### AgirDB Class

```python
class AgirDB:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        dbname: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None
    )
```

**Parameters:**
- `host` (str, optional): Database host. Defaults to `PGHOST` env var.
- `port` (int, optional): Database port. Defaults to `PGPORT` env var.
- `dbname` (str, optional): Database name. Defaults to `PGDATABASE` env var.
- `user` (str, optional): Database user. Defaults to `PGUSER` env var.
- `password` (str, optional): Database password. Defaults to `.pgpass` file.

**Attributes:**
- `gaps`: PipelineGaps - Pipeline gap analysis
- `stages`: StageStatus - Stage status tracking
- `images`: ImageMetadata - Image metadata operations
- `batches`: BatchMetadata - Batch metadata operations
- `transfers`: TransferManager - JUNO transfer operations
- `events`: EventLogger - Event logging
- `inventory`: InventorySync - File inventory synchronization
- `analytics`: Analytics - Reporting and statistics
- `migration`: Migration - SQLite data import

#### connect()

Establish database connection.

```python
db.connect()
```

**Raises:**
- `ConnectionError`: If connection fails

#### close()

Close database connection.

```python
db.close()
```

#### commit()

Commit current transaction.

```python
db.commit()
```

**Raises:**
- `TransactionError`: If commit fails

#### rollback()

Rollback current transaction.

```python
db.rollback()
```

**Raises:**
- `TransactionError`: If rollback fails

#### is_connected

Property to check connection status.

```python
if db.is_connected:
    print("Connected")
```

**Returns:** `bool` - True if connected, False otherwise

---

## Pipeline Gaps Component

The `gaps` component provides work discovery through pipeline gap analysis. It identifies batches where output files are missing, indicating processing needs.

### gaps.get_batches_with_gaps()

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

### gaps.get_images_with_gaps()

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

### gaps.get_gap_summary()

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
```

### gaps.check_batch_complete()

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
if db.gaps.check_batch_complete('B001', 'raw_to_jpg'):
    db.stages.complete('B001', 'raw_to_jpg', success=True)
```

### gaps.get_stage_progress()

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

---

## Stage Status Component

The `stages` component tracks the in-progress status of pipeline stages, preventing duplicate work and enabling monitoring.

### stages.start()

Mark a stage as started for a batch.

```python
db.stages.start(
    batch_id='B001',
    stage='raw_to_jpg',
    job_id='worker-001',
    hostname='compute-01',
    metadata={'worker_type': 'gpu', 'priority': 'high'}
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `stage` (str): Stage name
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

### stages.complete()

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

### stages.get_status()

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

### stages.get_in_progress()

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

### stages.cancel()

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

### stages.get_history()

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

---

## Image Metadata Component

The `images` component manages metadata for individual images across all processing stages.

### images.insert()

Insert metadata for a single image.

```python
db.images.insert(
    image_id='MD_1683434234',
    batch_id='B001',
    camera_id='SVS_001',
    capture_time='2025-01-15T10:00:00Z',
    raw_path='/data/raw/B001/MD_1683434234.ARW',
    dng_path=None,
    jpg_path=None,
    metadata={
        'exposure': '1/1000',
        'iso': 400,
        'focal_length': 50
    }
)
```

**Parameters:**
- `image_id` (str): Unique image identifier
- `batch_id` (str): Batch identifier
- `camera_id` (str): Camera identifier
- `capture_time` (str): ISO 8601 timestamp
- `raw_path` (str): Path to RAW file
- `dng_path` (str, optional): Path to DNG file
- `jpg_path` (str, optional): Path to JPG file
- `metadata` (dict, optional): Free-form metadata (stored as JSON)

**Returns:** `None`

**Raises:**
- `DuplicateImageError`: If image_id already exists
- `BatchNotFoundError`: If batch doesn't exist
- `ValidationError`: If required fields missing or invalid
- `QueryError`: If database operation fails

**Example:**
```python
db.images.insert(
    image_id='MD_1683434234',
    batch_id='B001',
    camera_id='SVS_001',
    capture_time='2025-01-15T10:00:00Z',
    raw_path='/data/raw/B001/MD_1683434234.ARW',
    metadata={'lens': 'Canon 50mm f/1.8'}
)
```

### images.insert_bulk()

Insert metadata for multiple images efficiently.

```python
db.images.insert_bulk(images_data)
```

**Parameters:**
- `images_data` (List[Dict]): List of image metadata dictionaries

**Returns:** `int` - Number of images inserted

**Raises:**
- `DuplicateImageError`: If any image_id already exists
- `ValidationError`: If any record has invalid data
- `QueryError`: If database operation fails

**Example:**
```python
images = [
    {
        'image_id': 'MD_1683434234',
        'batch_id': 'B001',
        'camera_id': 'SVS_001',
        'capture_time': '2025-01-15T10:00:00Z',
        'raw_path': '/data/raw/B001/MD_1683434234.ARW',
        'metadata': {'iso': 400}
    },
    {
        'image_id': 'MD_1683434235',
        'batch_id': 'B001',
        'camera_id': 'SVS_001',
        'capture_time': '2025-01-15T10:00:01Z',
        'raw_path': '/data/raw/B001/MD_1683434235.ARW',
        'metadata': {'iso': 400}
    }
]

count = db.images.insert_bulk(images)
print(f"Inserted {count} images")
```

### images.get()

Get metadata for a specific image.

```python
image = db.images.get(image_id='MD_1683434234')
```

**Parameters:**
- `image_id` (str): Image identifier

**Returns:** `Dict` - Image metadata:
```python
{
    'image_id': 'MD_1683434234',
    'batch_id': 'B001',
    'camera_id': 'SVS_001',
    'capture_time': '2025-01-15T10:00:00Z',
    'raw_path': '/data/raw/B001/MD_1683434234.ARW',
    'dng_path': '/data/dng/B001/MD_1683434234.dng',
    'jpg_path': '/data/jpg/B001/MD_1683434234.jpg',
    'created_at': '2025-01-15T10:05:00Z',
    'updated_at': '2025-01-15T10:30:00Z',
    'metadata': {'iso': 400, 'exposure': '1/1000'}
}
```

**Raises:**
- `ImageNotFoundError`: If image doesn't exist
- `QueryError`: If database query fails

**Example:**
```python
try:
    image = db.images.get('MD_1683434234')
    print(f"RAW: {image['raw_path']}")
    print(f"JPG: {image['jpg_path']}")
except ImageNotFoundError:
    print("Image not found in database")
```

### images.update()

Update metadata for an existing image.

```python
db.images.update(
    image_id='MD_1683434234',
    dng_path='/data/dng/B001/MD_1683434234.dng',
    jpg_path='/data/jpg/B001/MD_1683434234.jpg',
    metadata={'processed': True, 'quality': 95}
)
```

**Parameters:**
- `image_id` (str): Image identifier
- `dng_path` (str, optional): Update DNG path
- `jpg_path` (str, optional): Update JPG path
- `metadata` (dict, optional): Merge with existing metadata

**Returns:** `None`

**Raises:**
- `ImageNotFoundError`: If image doesn't exist
- `QueryError`: If database operation fails

**Example:**
```python
# Update after processing
db.images.update(
    'MD_1683434234',
    dng_path='/data/dng/B001/MD_1683434234.dng'
)
```

### images.get_by_batch()

Get all images for a specific batch.

```python
images = db.images.get_by_batch(
    batch_id='B001',
    limit=1000,
    offset=0
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `limit` (int, optional): Maximum number to return. Default: 1000
- `offset` (int, optional): Pagination offset. Default: 0

**Returns:** `List[Dict]` - List of image metadata

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database query fails

**Example:**
```python
# Get all images in a batch
images = db.images.get_by_batch('B001')
for img in images:
    print(f"{img['image_id']}: {img['raw_path']}")
```

### images.search()

Search images by various criteria.

```python
images = db.images.search(
    batch_id='B001',
    camera_id='SVS_001',
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z',
    has_dng=True,
    has_jpg=False,
    limit=100
)
```

**Parameters:**
- `batch_id` (str, optional): Filter by batch
- `camera_id` (str, optional): Filter by camera
- `start_time` (str, optional): Start of capture time range (ISO 8601)
- `end_time` (str, optional): End of capture time range (ISO 8601)
- `has_dng` (bool, optional): Filter by DNG existence
- `has_jpg` (bool, optional): Filter by JPG existence
- `limit` (int, optional): Maximum number to return. Default: 100

**Returns:** `List[Dict]` - List of matching images

**Raises:**
- `ValidationError`: If search parameters are invalid
- `QueryError`: If database query fails

**Example:**
```python
# Find images needing JPG conversion
images = db.images.search(
    batch_id='B001',
    has_dng=True,
    has_jpg=False
)
```

### images.delete()

Delete an image record (does not delete files).

```python
db.images.delete(image_id='MD_1683434234')
```

**Parameters:**
- `image_id` (str): Image identifier

**Returns:** `None`

**Raises:**
- `ImageNotFoundError`: If image doesn't exist
- `QueryError`: If database operation fails

**Note:** This only removes the database record. Physical files must be deleted separately.

---

## Batch Metadata Component

The `batches` component manages metadata for processing batches.

### batches.insert()

Insert metadata for a new batch.

```python
db.batches.insert(
    batch_id='B001',
    collection_date='2025-01-15',
    location='Field_North',
    camera_id='SVS_001',
    image_count=150,
    metadata={
        'weather': 'sunny',
        'crop': 'wheat',
        'growth_stage': 'heading'
    }
)
```

**Parameters:**
- `batch_id` (str): Unique batch identifier
- `collection_date` (str): Date images were collected (YYYY-MM-DD)
- `location` (str): Collection location/field identifier
- `camera_id` (str): Camera identifier
- `image_count` (int): Expected number of images
- `metadata` (dict, optional): Free-form metadata

**Returns:** `None`

**Raises:**
- `DuplicateBatchError`: If batch_id already exists
- `ValidationError`: If required fields missing or invalid
- `QueryError`: If database operation fails

### batches.get()

Get metadata for a specific batch.

```python
batch = db.batches.get(batch_id='B001')
```

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** `Dict` - Batch metadata:
```python
{
    'batch_id': 'B001',
    'collection_date': '2025-01-15',
    'location': 'Field_North',
    'camera_id': 'SVS_001',
    'image_count': 150,
    'created_at': '2025-01-15T08:00:00Z',
    'updated_at': '2025-01-15T10:30:00Z',
    'metadata': {'weather': 'sunny', 'crop': 'wheat'}
}
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database query fails

### batches.update()

Update metadata for an existing batch.

```python
db.batches.update(
    batch_id='B001',
    image_count=148,
    metadata={'notes': 'Some images excluded due to quality issues'}
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `collection_date` (str, optional): Update collection date
- `location` (str, optional): Update location
- `camera_id` (str, optional): Update camera ID
- `image_count` (int, optional): Update image count
- `metadata` (dict, optional): Merge with existing metadata

**Returns:** `None`

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database operation fails

### batches.list()

List all batches with optional filtering.

```python
batches = db.batches.list(
    location='Field_North',
    camera_id='SVS_001',
    start_date='2025-01-01',
    end_date='2025-01-31',
    limit=50,
    offset=0
)
```

**Parameters:**
- `location` (str, optional): Filter by location
- `camera_id` (str, optional): Filter by camera
- `start_date` (str, optional): Start of date range (YYYY-MM-DD)
- `end_date` (str, optional): End of date range (YYYY-MM-DD)
- `limit` (int, optional): Maximum number to return. Default: 50
- `offset` (int, optional): Pagination offset. Default: 0

**Returns:** `List[Dict]` - List of batch metadata

**Raises:**
- `ValidationError`: If search parameters are invalid
- `QueryError`: If database query fails

### batches.delete()

Delete a batch record (does not delete files or images).

```python
db.batches.delete(batch_id='B001')
```

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** `None`

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database operation fails

**Note:** This does not cascade delete associated images. Delete images first if needed.

---

## Event Logging Component

The `events` component logs processing events for auditing and debugging.

### events.log()

Log a single processing event.

```python
db.events.log(
    event_type='processing_started',
    batch_id='B001',
    stage='raw_to_jpg',
    severity='info',
    message='Started RAW to JPG conversion',
    metadata={
        'worker_id': 'worker-001',
        'hostname': 'compute-01'
    }
)
```

**Parameters:**
- `event_type` (str): Event type category
- `batch_id` (str, optional): Associated batch
- `stage` (str, optional): Associated stage
- `image_id` (str, optional): Associated image
- `severity` (str): Severity level ('debug', 'info', 'warning', 'error', 'critical')
- `message` (str): Event description
- `metadata` (dict, optional): Additional event data

**Returns:** `str` - Event ID

**Raises:**
- `ValidationError`: If required fields missing
- `QueryError`: If database operation fails

**Example:**
```python
event_id = db.events.log(
    event_type='conversion_complete',
    batch_id='B001',
    stage='raw_to_jpg',
    severity='info',
    message='Converted 150 images successfully',
    metadata={'duration': 342.5, 'success_rate': 100.0}
)
```

### events.log_bulk()

Log multiple events efficiently.

```python
db.events.log_bulk(events_data)
```

**Parameters:**
- `events_data` (List[Dict]): List of event dictionaries

**Returns:** `int` - Number of events logged

**Raises:**
- `ValidationError`: If any event has invalid data
- `QueryError`: If database operation fails

**Example:**
```python
events = [
    {
        'event_type': 'image_processed',
        'batch_id': 'B001',
        'image_id': 'MD_1683434234',
        'stage': 'raw_to_jpg',
        'severity': 'info',
        'message': 'Image converted successfully'
    },
    {
        'event_type': 'image_processed',
        'batch_id': 'B001',
        'image_id': 'MD_1683434235',
        'stage': 'raw_to_jpg',
        'severity': 'info',
        'message': 'Image converted successfully'
    }
]

count = db.events.log_bulk(events)
```

### events.get()

Get a specific event by ID.

```python
event = db.events.get(event_id='uuid-string')
```

**Parameters:**
- `event_id` (str): Event identifier

**Returns:** `Dict` - Event details

**Raises:**
- `NotFoundError`: If event doesn't exist
- `QueryError`: If database query fails

### events.search()

Search events by various criteria.

```python
events = db.events.search(
    event_type='conversion_error',
    batch_id='B001',
    stage='raw_to_jpg',
    severity='error',
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z',
    limit=100
)
```

**Parameters:**
- `event_type` (str, optional): Filter by event type
- `batch_id` (str, optional): Filter by batch
- `stage` (str, optional): Filter by stage
- `image_id` (str, optional): Filter by image
- `severity` (str, optional): Filter by severity
- `start_time` (str, optional): Start of time range (ISO 8601)
- `end_time` (str, optional): End of time range (ISO 8601)
- `limit` (int, optional): Maximum number to return. Default: 100

**Returns:** `List[Dict]` - List of matching events ordered by timestamp

**Raises:**
- `ValidationError`: If search parameters are invalid
- `QueryError`: If database query fails

**Example:**
```python
# Find all errors in last 24 hours
import datetime
yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()

errors = db.events.search(
    severity='error',
    start_time=yesterday,
    limit=100
)

for event in errors:
    print(f"{event['event_type']}: {event['message']}")
```

### events.get_recent()

Get most recent events.

```python
events = db.events.get_recent(limit=50)
```

**Parameters:**
- `limit` (int, optional): Maximum number to return. Default: 50

**Returns:** `List[Dict]` - Recent events ordered by timestamp descending

**Raises:**
- `QueryError`: If database query fails

---

## Inventory Synchronization Component

The `inventory` component synchronizes file system state with database records.

### inventory.scan_directory()

Scan a directory and sync file inventory with database.

```python
result = db.inventory.scan_directory(
    directory='/data/raw/B001',
    batch_id='B001',
    file_type='raw',
    pattern='*.ARW',
    recursive=False
)
```

**Parameters:**
- `directory` (str): Directory path to scan
- `batch_id` (str): Associated batch
- `file_type` (str): File type ('raw', 'dng', 'jpg', etc.)
- `pattern` (str, optional): Glob pattern for files. Default: '*'
- `recursive` (bool, optional): Scan subdirectories. Default: False

**Returns:** `Dict` - Scan results:
```python
{
    'files_found': 150,
    'files_added': 5,
    'files_updated': 10,
    'files_missing': 2,
    'scan_duration': 3.45
}
```

**Raises:**
- `ValidationError`: If directory doesn't exist
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database operation fails

**Example:**
```python
# Sync RAW files
result = db.inventory.scan_directory(
    directory='/data/raw/B001',
    batch_id='B001',
    file_type='raw',
    pattern='*.ARW'
)

print(f"Found {result['files_found']} files, "
      f"added {result['files_added']}, "
      f"missing {result['files_missing']}")
```

### inventory.verify_batch()

Verify file existence for all images in a batch.

```python
result = db.inventory.verify_batch(
    batch_id='B001',
    file_types=['raw', 'dng', 'jpg']
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `file_types` (List[str], optional): Types to verify. Default: all types

**Returns:** `Dict` - Verification results:
```python
{
    'batch_id': 'B001',
    'total_images': 150,
    'raw': {'exist': 150, 'missing': 0},
    'dng': {'exist': 148, 'missing': 2},
    'jpg': {'exist': 145, 'missing': 5},
    'missing_images': ['MD_1683434234', 'MD_1683434567']
}
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database query fails

**Example:**
```python
# Verify all file types exist
result = db.inventory.verify_batch('B001')
if result['jpg']['missing'] > 0:
    print(f"Missing {result['jpg']['missing']} JPG files")
```

### inventory.mark_missing()

Mark files as missing in database.

```python
db.inventory.mark_missing(
    image_ids=['MD_1683434234', 'MD_1683434567'],
    file_type='jpg'
)
```

**Parameters:**
- `image_ids` (List[str]): Image identifiers
- `file_type` (str): File type that's missing

**Returns:** `int` - Number of images marked

**Raises:**
- `ValidationError`: If parameters invalid
- `QueryError`: If database operation fails

### inventory.get_missing()

Get list of images with missing files.

```python
missing = db.inventory.get_missing(
    batch_id='B001',
    file_type='jpg'
)
```

**Parameters:**
- `batch_id` (str, optional): Filter by batch
- `file_type` (str, optional): Filter by file type

**Returns:** `List[Dict]` - Images with missing files:
```python
[
    {
        'image_id': 'MD_1683434234',
        'batch_id': 'B001',
        'file_type': 'jpg',
        'expected_path': '/data/jpg/B001/MD_1683434234.jpg',
        'marked_missing_at': '2025-01-15T10:30:00Z'
    },
    ...
]
```

**Raises:**
- `QueryError`: If database query fails

---

## Transfer Management Component

The `transfers` component manages JUNO transfer operations.

### transfers.create()

Create a new transfer request.

```python
transfer_id = db.transfers.create(
    batch_id='B001',
    source_path='/local/data/raw/B001',
    destination_path='/juno/archive/raw/B001',
    transfer_type='upload',
    priority='high',
    metadata={'size_gb': 15.5}
)
```

**Parameters:**
- `batch_id` (str): Associated batch
- `source_path` (str): Source path
- `destination_path` (str): Destination path
- `transfer_type` (str): Transfer type ('upload', 'download', 'move')
- `priority` (str, optional): Priority level ('low', 'normal', 'high'). Default: 'normal'
- `metadata` (dict, optional): Additional transfer metadata

**Returns:** `str` - Transfer ID

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `ValidationError`: If parameters invalid
- `QueryError`: If database operation fails

### transfers.start()

Mark a transfer as started.

```python
db.transfers.start(
    transfer_id='uuid-string',
    globus_task_id='globus-task-123',
    estimated_duration=3600
)
```

**Parameters:**
- `transfer_id` (str): Transfer identifier
- `globus_task_id` (str, optional): Globus task identifier
- `estimated_duration` (int, optional): Estimated seconds

**Returns:** `None`

**Raises:**
- `TransferNotFoundError`: If transfer doesn't exist
- `TransferAlreadyInProgressError`: If already started
- `QueryError`: If database operation fails

### transfers.complete()

Mark a transfer as completed.

```python
db.transfers.complete(
    transfer_id='uuid-string',
    success=True,
    files_transferred=150,
    bytes_transferred=15500000000,
    error_message=None
)
```

**Parameters:**
- `transfer_id` (str): Transfer identifier
- `success` (bool): Whether transfer succeeded
- `files_transferred` (int, optional): Number of files transferred
- `bytes_transferred` (int, optional): Total bytes transferred
- `error_message` (str, optional): Error if failed

**Returns:** `None`

**Raises:**
- `TransferNotFoundError`: If transfer doesn't exist
- `QueryError`: If database operation fails

### transfers.get_status()

Get status of a transfer.

```python
status = db.transfers.get_status(transfer_id='uuid-string')
```

**Parameters:**
- `transfer_id` (str): Transfer identifier

**Returns:** `Dict` - Transfer status:
```python
{
    'transfer_id': 'uuid-string',
    'batch_id': 'B001',
    'status': 'in_progress',  # or 'pending', 'completed', 'failed'
    'source_path': '/local/data/raw/B001',
    'destination_path': '/juno/archive/raw/B001',
    'transfer_type': 'upload',
    'globus_task_id': 'globus-task-123',
    'created_at': '2025-01-15T10:00:00Z',
    'started_at': '2025-01-15T10:05:00Z',
    'completed_at': None,
    'files_transferred': None,
    'bytes_transferred': None
}
```

**Raises:**
- `TransferNotFoundError`: If transfer doesn't exist
- `QueryError`: If database query fails

### transfers.list_pending()

Get all pending transfers.

```python
transfers = db.transfers.list_pending(
    priority='high',
    limit=20
)
```

**Parameters:**
- `priority` (str, optional): Filter by priority
- `limit` (int, optional): Maximum number to return. Default: 20

**Returns:** `List[Dict]` - Pending transfers ordered by priority and creation time

**Raises:**
- `QueryError`: If database query fails

### transfers.cancel()

Cancel a pending or in-progress transfer.

```python
db.transfers.cancel(
    transfer_id='uuid-string',
    reason='User requested cancellation'
)
```

**Parameters:**
- `transfer_id` (str): Transfer identifier
- `reason` (str, optional): Cancellation reason

**Returns:** `None`

**Raises:**
- `TransferNotFoundError`: If transfer doesn't exist
- `QueryError`: If database operation fails

---

## Analytics Component

The `analytics` component provides reporting and statistics.

### analytics.get_pipeline_summary()

Get summary statistics for entire pipeline.

```python
summary = db.analytics.get_pipeline_summary()
```

**Returns:** `Dict` - Pipeline-wide statistics:
```python
{
    'total_batches': 45,
    'total_images': 15000,
    'stages': {
        'raw_to_dng': {
            'complete': 44,
            'in_progress': 1,
            'pending': 0,
            'completion_rate': 97.8
        },
        'dng_to_jpg': {
            'complete': 43,
            'in_progress': 2,
            'pending': 0,
            'completion_rate': 95.6
        }
    },
    'last_updated': '2025-01-15T10:30:00Z'
}
```

**Raises:**
- `QueryError`: If database query fails

### analytics.get_batch_statistics()

Get detailed statistics for a specific batch.

```python
stats = db.analytics.get_batch_statistics(batch_id='B001')
```

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** `Dict` - Batch statistics:
```python
{
    'batch_id': 'B001',
    'total_images': 150,
    'stages': {
        'raw_to_dng': {
            'status': 'completed',
            'files_complete': 150,
            'duration_seconds': 900,
            'avg_time_per_file': 6.0
        },
        'dng_to_jpg': {
            'status': 'in_progress',
            'files_complete': 145,
            'duration_seconds': 342,
            'avg_time_per_file': 2.3
        }
    },
    'disk_usage': {
        'raw_gb': 12.5,
        'dng_gb': 18.7,
        'jpg_gb': 3.2
    }
}
```

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist
- `QueryError`: If database query fails

### analytics.get_processing_rates()

Get processing rates and throughput metrics.

```python
rates = db.analytics.get_processing_rates(
    stage='raw_to_jpg',
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z'
)
```

**Parameters:**
- `stage` (str, optional): Filter by stage
- `start_time` (str, optional): Start of time range (ISO 8601)
- `end_time` (str, optional): End of time range (ISO 8601)

**Returns:** `Dict` - Processing rate metrics:
```python
{
    'stage': 'raw_to_jpg',
    'time_period': {
        'start': '2025-01-15T00:00:00Z',
        'end': '2025-01-15T23:59:59Z'
    },
    'total_images_processed': 4500,
    'total_batches_processed': 30,
    'avg_images_per_hour': 187.5,
    'avg_batch_duration_seconds': 540,
    'avg_image_processing_time': 3.6
}
```

**Raises:**
- `ValidationError`: If parameters invalid
- `QueryError`: If database query fails

### analytics.get_error_summary()

Get summary of errors and failures.

```python
errors = db.analytics.get_error_summary(
    start_time='2025-01-15T00:00:00Z',
    end_time='2025-01-15T23:59:59Z'
)
```

**Parameters:**
- `start_time` (str, optional): Start of time range (ISO 8601)
- `end_time` (str, optional): End of time range (ISO 8601)

**Returns:** `Dict` - Error summary:
```python
{
    'total_errors': 15,
    'by_stage': {
        'raw_to_dng': 3,
        'dng_to_jpg': 12
    },
    'by_type': {
        'conversion_failed': 10,
        'file_not_found': 3,
        'timeout': 2
    },
    'affected_batches': ['B001', 'B023', 'B045']
}
```

**Raises:**
- `ValidationError`: If parameters invalid
- `QueryError`: If database query fails

### analytics.export_report()

Export analytics data to CSV or JSON.

```python
filepath = db.analytics.export_report(
    report_type='pipeline_summary',
    format='csv',
    output_path='/reports/pipeline_2025-01-15.csv',
    start_time='2025-01-01T00:00:00Z',
    end_time='2025-01-31T23:59:59Z'
)
```

**Parameters:**
- `report_type` (str): Report type ('pipeline_summary', 'batch_details', 'errors')
- `format` (str): Output format ('csv' or 'json')
- `output_path` (str): Where to save report
- `start_time` (str, optional): Start of time range
- `end_time` (str, optional): End of time range

**Returns:** `str` - Path to generated report file

**Raises:**
- `ValidationError`: If parameters invalid
- `QueryError`: If database query fails

---

## Migration Component

The `migration` component imports data from legacy SQLite databases.

### migration.import_from_sqlite()

Import all data from a SQLite database.

```python
result = db.migration.import_from_sqlite(
    sqlite_path='/path/to/legacy.db',
    batch_size=1000,
    dry_run=False
)
```

**Parameters:**
- `sqlite_path` (str): Path to SQLite database file
- `batch_size` (int, optional): Number of records per batch. Default: 1000
- `dry_run` (bool, optional): If True, validate only without importing. Default: False

**Returns:** `Dict` - Import results:
```python
{
    'dry_run': False,
    'batches_imported': 45,
    'images_imported': 15000,
    'events_imported': 3500,
    'duration_seconds': 125.3,
    'errors': []
}
```

**Raises:**
- `SQLiteConnectionError`: If can't connect to SQLite database
- `MigrationValidationError`: If data validation fails
- `MigrationError`: If import fails

**Example:**
```python
# Validate first
result = db.migration.import_from_sqlite(
    '/path/to/legacy.db',
    dry_run=True
)

if not result['errors']:
    # Actually import
    result = db.migration.import_from_sqlite('/path/to/legacy.db')
    print(f"Imported {result['images_imported']} images")
```

### migration.validate_sqlite()

Validate SQLite database structure and data.

```python
validation = db.migration.validate_sqlite(sqlite_path='/path/to/legacy.db')
```

**Parameters:**
- `sqlite_path` (str): Path to SQLite database file

**Returns:** `Dict` - Validation results:
```python
{
    'valid': True,
    'schema_version': '1.0',
    'tables_found': ['batches', 'images', 'events'],
    'record_counts': {
        'batches': 45,
        'images': 15000,
        'events': 3500
    },
    'warnings': [],
    'errors': []
}
```

**Raises:**
- `SQLiteConnectionError`: If can't connect to database
- `MigrationError`: If validation fails

---

## Exception Handling

AgirDB uses a hierarchical exception system for precise error handling.

### Exception Hierarchy

```
AgirDBError (base exception)
├── ConnectionError
│   └── TransactionError
├── QueryError
├── DuplicateError
│   ├── DuplicateImageError
│   ├── DuplicateBatchError
│   └── DuplicateDetectionError
├── NotFoundError
│   ├── ImageNotFoundError
│   ├── BatchNotFoundError
│   └── TransferNotFoundError
├── StageError
│   ├── StageAlreadyInProgressError
│   ├── StageNotStartedError
│   └── InvalidStageError
├── TransferError
│   ├── TransferAlreadyInProgressError
│   └── GlobusError
├── MigrationError
│   ├── SQLiteConnectionError
│   └── MigrationValidationError
└── ValidationError
    ├── InvalidParameterError
    ├── MissingRequiredFieldError
    ├── InvalidImageIdError
    └── InvalidBatchIdError
```

### Exception Usage Examples

```python
from agir_db import (
    AgirDB,
    StageAlreadyInProgressError,
    BatchNotFoundError,
    ConnectionError
)

with AgirDB() as db:
    try:
        # Start processing
        db.stages.start('B001', 'raw_to_jpg', job_id='worker-001')
        process_batch('B001')
        db.stages.complete('B001', 'raw_to_jpg', success=True)
        
    except StageAlreadyInProgressError:
        print("Another worker is processing this batch")
        
    except BatchNotFoundError:
        print("Batch not found in database")
        
    except ConnectionError as e:
        print(f"Database connection error: {e}")
        
    except Exception as e:
        print(f"Unexpected error: {e}")
        db.stages.complete('B001', 'raw_to_jpg', success=False, 
                          error_message=str(e))
```

---

## Complete Usage Examples

### Example 1: Basic RAW→JPG Processing Pipeline

```python
from agir_db import AgirDB, StageAlreadyInProgressError
import logging

# Configure logging
from agir_db import setup_logging
setup_logging(level=logging.INFO)

def process_batch_workflow():
    """Complete workflow for processing a batch."""
    
    with AgirDB() as db:
        # Discover work
        batches = db.gaps.get_batches_with_gaps(
            stage='raw_to_jpg',
            limit=1,
            order_by='gap_count',
            order_dir='DESC'
        )
        
        if not batches:
            print("No batches need processing")
            return
        
        batch = batches[0]
        batch_id = batch['batch_id']
        print(f"Processing {batch_id}: {batch['gap_count']} images need conversion")
        
        try:
            # Start stage
            db.stages.start(batch_id, 'raw_to_jpg', job_id='worker-001')
            
            # Get images needing processing
            images = db.gaps.get_images_with_gaps(batch_id, 'raw_to_jpg')
            
            # Process each image
            for img in images:
                try:
                    # Convert RAW→JPG
                    output_path = convert_to_jpg(
                        img['input_path'],
                        img['expected_output_path']
                    )
                    
                    # Update metadata
                    db.images.update(
                        img['image_id'],
                        jpg_path=output_path
                    )
                    
                    # Log success
                    db.events.log(
                        event_type='image_converted',
                        batch_id=batch_id,
                        image_id=img['image_id'],
                        stage='raw_to_jpg',
                        severity='info',
                        message='Successfully converted to JPG'
                    )
                    
                except Exception as e:
                    # Log error but continue
                    db.events.log(
                        event_type='conversion_error',
                        batch_id=batch_id,
                        image_id=img['image_id'],
                        stage='raw_to_jpg',
                        severity='error',
                        message=f'Conversion failed: {str(e)}'
                    )
            
            # Mark complete
            db.stages.complete(
                batch_id,
                'raw_to_jpg',
                success=True,
                files_processed=len(images)
            )
            
            print(f"Completed {batch_id}: processed {len(images)} images")
            
        except StageAlreadyInProgressError:
            print(f"Batch {batch_id} already being processed")

if __name__ == '__main__':
    process_batch_workflow()
```

### Example 2: Multi-Stage Pipeline with Gap Analysis

```python
from agir_db import AgirDB

def multi_stage_pipeline():
    """Process through RAW→DNG→JPG pipeline."""
    
    stages = ['raw_to_dng', 'dng_to_jpg']
    
    with AgirDB() as db:
        for stage in stages:
            print(f"\n=== Processing stage: {stage} ===")
            
            # Get pipeline health
            summary = db.gaps.get_gap_summary(stage)
            print(f"Overall completion: {100 - summary['overall_gap_percentage']:.1f}%")
            print(f"Batches with gaps: {summary['batches_with_gaps']}")
            
            # Process batches with gaps
            batches = db.gaps.get_batches_with_gaps(stage, limit=10)
            
            for batch in batches:
                batch_id = batch['batch_id']
                
                # Check if already in progress
                status = db.stages.get_status(batch_id, stage)
                if status and status['status'] == 'in_progress':
                    print(f"Skipping {batch_id} - already in progress")
                    continue
                
                # Process batch
                print(f"Processing {batch_id}: {batch['gap_count']} gaps")
                
                db.stages.start(batch_id, stage)
                
                # Get specific images needing work
                images = db.gaps.get_images_with_gaps(batch_id, stage)
                
                # Process images...
                process_images(images, stage)
                
                # Verify completion
                if db.gaps.check_batch_complete(batch_id, stage):
                    db.stages.complete(batch_id, stage, success=True)
                    print(f"✓ {batch_id} complete")
                else:
                    db.stages.complete(batch_id, stage, success=False,
                                     error_message='Some files still missing')
                    print(f"✗ {batch_id} incomplete")

if __name__ == '__main__':
    multi_stage_pipeline()
```

### Example 3: Monitoring and Analytics

```python
from agir_db import AgirDB
import datetime

def generate_daily_report():
    """Generate daily processing report."""
    
    with AgirDB() as db:
        # Get pipeline summary
        summary = db.analytics.get_pipeline_summary()
        
        print("=== Daily Pipeline Report ===\n")
        print(f"Total batches: {summary['total_batches']}")
        print(f"Total images: {summary['total_images']}\n")
        
        # Stage-by-stage summary
        for stage, stats in summary['stages'].items():
            print(f"{stage}:")
            print(f"  Complete: {stats['complete']}")
            print(f"  In Progress: {stats['in_progress']}")
            print(f"  Completion Rate: {stats['completion_rate']:.1f}%\n")
        
        # Processing rates (last 24 hours)
        yesterday = (datetime.datetime.now() - 
                    datetime.timedelta(days=1)).isoformat()
        
        for stage in ['raw_to_dng', 'dng_to_jpg']:
            rates = db.analytics.get_processing_rates(
                stage=stage,
                start_time=yesterday
            )
            
            print(f"\n{stage} throughput (24h):")
            print(f"  Images processed: {rates['total_images_processed']}")
            print(f"  Avg images/hour: {rates['avg_images_per_hour']:.1f}")
            print(f"  Avg time/image: {rates['avg_image_processing_time']:.2f}s")
        
        # Error summary
        errors = db.analytics.get_error_summary(start_time=yesterday)
        
        if errors['total_errors'] > 0:
            print(f"\n⚠ Errors in last 24h: {errors['total_errors']}")
            for error_type, count in errors['by_type'].items():
                print(f"  {error_type}: {count}")
        
        # Export detailed report
        report_path = db.analytics.export_report(
            report_type='pipeline_summary',
            format='csv',
            output_path=f'/reports/daily_{datetime.date.today()}.csv',
            start_time=yesterday
        )
        
        print(f"\nDetailed report saved to: {report_path}")

if __name__ == '__main__':
    generate_daily_report()
```

### Example 4: Batch Initialization

```python
from agir_db import AgirDB
import os
from datetime import datetime

def initialize_new_batch(batch_directory: str):
    """Initialize a new batch from filesystem."""
    
    batch_id = os.path.basename(batch_directory)
    
    with AgirDB() as db:
        # Create batch record
        db.batches.insert(
            batch_id=batch_id,
            collection_date=datetime.now().strftime('%Y-%m-%d'),
            location='Field_North',
            camera_id='SVS_001',
            image_count=0,  # Will update after scan
            metadata={
                'source_directory': batch_directory,
                'initialized_by': 'script',
                'initialized_at': datetime.now().isoformat()
            }
        )
        
        # Scan RAW files
        result = db.inventory.scan_directory(
            directory=batch_directory,
            batch_id=batch_id,
            file_type='raw',
            pattern='*.ARW'
        )
        
        print(f"Initialized batch {batch_id}:")
        print(f"  Files found: {result['files_found']}")
        print(f"  Files added: {result['files_added']}")
        
        # Update batch with actual count
        db.batches.update(
            batch_id=batch_id,
            image_count=result['files_found']
        )
        
        # Log initialization
        db.events.log(
            event_type='batch_initialized',
            batch_id=batch_id,
            severity='info',
            message=f'Initialized with {result["files_found"]} images',
            metadata=result
        )
        
        return batch_id

if __name__ == '__main__':
    batch_id = initialize_new_batch('/data/raw/B123')
    print(f"Batch {batch_id} ready for processing")
```

### Example 5: Error Recovery

```python
from agir_db import AgirDB
import datetime

def recover_stuck_stages():
    """Find and recover stages stuck in 'in_progress' state."""
    
    with AgirDB() as db:
        # Find stages in progress for more than 2 hours
        in_progress = db.stages.get_in_progress()
        timeout_threshold = 7200  # 2 hours in seconds
        
        for stage in in_progress:
            if stage['duration_seconds'] > timeout_threshold:
                batch_id = stage['batch_id']
                stage_name = stage['stage']
                
                print(f"Found stuck stage: {batch_id}/{stage_name}")
                print(f"  Duration: {stage['duration_seconds']}s")
                print(f"  Job ID: {stage['job_id']}")
                
                # Cancel the stuck stage
                db.stages.cancel(
                    batch_id,
                    stage_name,
                    reason=f'Timeout - exceeded {timeout_threshold}s limit'
                )
                
                # Log the recovery
                db.events.log(
                    event_type='stage_recovered',
                    batch_id=batch_id,
                    stage=stage_name,
                    severity='warning',
                    message='Stage cancelled due to timeout',
                    metadata={
                        'duration': stage['duration_seconds'],
                        'original_job_id': stage['job_id']
                    }
                )
                
                # Check if any files were actually processed
                progress = db.gaps.get_stage_progress(batch_id, stage_name)
                
                if progress['completion_percentage'] > 0:
                    print(f"  Partial completion: {progress['completion_percentage']:.1f}%")
                    print(f"  Remaining: {progress['remaining_images']} images")
                else:
                    print("  No progress made - full reprocessing needed")

if __name__ == '__main__':
    recover_stuck_stages()
```

---

## Best Practices

### 1. Always Use Context Managers

```python
# ✓ Good - automatic transaction handling
with AgirDB() as db:
    db.images.insert(image_data)

# ✗ Avoid - manual transaction management
db = AgirDB()
db.connect()
db.images.insert(image_data)
db.commit()
db.close()
```

### 2. Use Pipeline Gaps as Source of Truth

```python
# ✓ Good - use gaps to discover work
batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
for batch in batches:
    process_batch(batch)

# ✗ Avoid - relying solely on status without verifying gaps
batches = db.batches.list()  # Might include already-complete batches
```

### 3. Handle Errors Gracefully

```python
# ✓ Good - specific exception handling
try:
    db.stages.start(batch_id, stage)
except StageAlreadyInProgressError:
    logger.info("Batch already being processed")
except BatchNotFoundError:
    logger.error(f"Batch {batch_id} not found")

# ✗ Avoid - catching all exceptions
try:
    db.stages.start(batch_id, stage)
except Exception:
    pass  # Silent failures hide problems
```

### 4. Log Important Events

```python
# ✓ Good - comprehensive logging
db.events.log(
    event_type='processing_started',
    batch_id=batch_id,
    stage=stage,
    severity='info',
    message=f'Started processing {image_count} images',
    metadata={'worker_id': worker_id, 'hostname': hostname}
)

# ✗ Avoid - minimal logging
db.stages.start(batch_id, stage)  # No audit trail
```

### 5. Verify Before Marking Complete

```python
# ✓ Good - verify completion before marking
if db.gaps.check_batch_complete(batch_id, stage):
    db.stages.complete(batch_id, stage, success=True)
else:
    db.stages.complete(batch_id, stage, success=False,
                      error_message='Gaps remain')

# ✗ Avoid - assuming success
db.stages.complete(batch_id, stage, success=True)  # Hope for the best
```

### 6. Use Bulk Operations for Efficiency

```python
# ✓ Good - bulk insert
db.images.insert_bulk(image_list)  # Single transaction

# ✗ Avoid - individual inserts
for image in image_list:
    db.images.insert(image)  # Many transactions
```

### 7. Implement Monitoring

```python
# ✓ Good - regular health checks
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

### 8. Use Metadata Fields for Extensibility

```python
# ✓ Good - store additional context in metadata
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

---

## Performance Considerations

### Connection Pooling

For high-throughput applications, consider implementing connection pooling:

```python
# Use multiple connections for parallel processing
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

### Batch Operations

Always prefer bulk operations when processing multiple records:

```python
# Fast - single transaction
db.images.insert_bulk(images)
db.events.log_bulk(events)

# Slow - multiple transactions
for image in images:
    db.images.insert(image)
```

### Query Optimization

Use pagination for large result sets:

```python
# Process in chunks
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

---

## Troubleshooting

### Connection Issues

```python
from agir_db import AgirDB, ConnectionError

try:
    db = AgirDB(host='localhost', port=5432, dbname='agir')
    db.connect()
except ConnectionError as e:
    print(f"Connection failed: {e}")
    print("Check: PGHOST, PGPORT, PGDATABASE environment variables")
    print("Check: .pgpass file for credentials")
    print("Check: PostgreSQL is running and accessible")
```

### Duplicate Key Errors

```python
from agir_db import DuplicateImageError

try:
    db.images.insert(image_data)
except DuplicateImageError:
    # Image already exists - update instead
    db.images.update(image_data['image_id'], **image_data)
```

### Stage Lock Conflicts

```python
from agir_db import StageAlreadyInProgressError

try:
    db.stages.start(batch_id, stage)
except StageAlreadyInProgressError:
    # Check if it's genuinely in progress or stuck
    status = db.stages.get_status(batch_id, stage)
    duration = status['duration_seconds']
    
    if duration > 7200:  # 2 hours
        # Cancel stuck stage
        db.stages.cancel(batch_id, stage, reason='Stuck - timeout')
        # Retry
        db.stages.start(batch_id, stage)
    else:
        # Genuinely in progress
        print(f"Stage in progress for {duration}s")
```

---

## Database Schema Reference

### Key Tables

**batches**
- `batch_id` (text, PK): Unique batch identifier
- `collection_date` (date): When images were collected
- `location` (text): Collection location
- `camera_id` (text): Camera identifier
- `image_count` (int): Expected number of images
- `metadata` (jsonb): Free-form metadata
- `created_at` (timestamp): Record creation time
- `updated_at` (timestamp): Last update time

**images**
- `image_id` (text, PK): Unique image identifier
- `batch_id` (text, FK): Associated batch
- `camera_id` (text): Camera identifier
- `capture_time` (timestamp): Image capture time
- `raw_path` (text): Path to RAW file
- `dng_path` (text): Path to DNG file (nullable)
- `jpg_path` (text): Path to JPG file (nullable)
- `metadata` (jsonb): Free-form metadata
- `created_at` (timestamp): Record creation time
- `updated_at` (timestamp): Last update time

**stage_status**
- `status_id` (uuid, PK): Unique status identifier
- `batch_id` (text, FK): Associated batch
- `stage` (text): Stage name (e.g., 'raw_to_jpg')
- `status` (text): Status ('in_progress', 'completed', 'failed', 'cancelled')
- `job_id` (text): Job/worker identifier
- `hostname` (text): Processing hostname
- `started_at` (timestamp): Stage start time
- `completed_at` (timestamp): Stage completion time (nullable)
- `files_processed` (int): Number of files processed (nullable)
- `error_message` (text): Error description if failed (nullable)
- `metadata` (jsonb): Free-form metadata

**events**
- `event_id` (uuid, PK): Unique event identifier
- `event_type` (text): Event type category
- `batch_id` (text, FK): Associated batch (nullable)
- `stage` (text): Associated stage (nullable)
- `image_id` (text, FK): Associated image (nullable)
- `severity` (text): Severity level
- `message` (text): Event description
- `metadata` (jsonb): Additional event data
- `created_at` (timestamp): Event timestamp

**transfers**
- `transfer_id` (uuid, PK): Unique transfer identifier
- `batch_id` (text, FK): Associated batch
- `source_path` (text): Source path
- `destination_path` (text): Destination path
- `transfer_type` (text): Transfer type
- `status` (text): Transfer status
- `priority` (text): Priority level
- `globus_task_id` (text): Globus task ID (nullable)
- `files_transferred` (int): Files transferred (nullable)
- `bytes_transferred` (bigint): Bytes transferred (nullable)
- `created_at` (timestamp): Transfer creation time
- `started_at` (timestamp): Transfer start time (nullable)
- `completed_at` (timestamp): Transfer completion time (nullable)
- `metadata` (jsonb): Additional transfer data

---

## Version History

### 1.0.0 (2025-01-15)
- Initial release
- Pipeline gaps component
- Stage status tracking
- Image and batch metadata management
- Event logging
- Inventory synchronization
- Transfer management
- Analytics and reporting
- SQLite migration support

---

## Support & Contributing

### Getting Help

- Documentation: This file
- Issues: Report on GitHub repository
- Email: Contact package maintainer

### Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

### License

Copyright © 2025 Matthew Kutugata. All rights reserved.

---

## Appendix: Common Workflows

### A. Daily Batch Processing

```python
#!/usr/bin/env python3
"""Daily batch processing script."""

from agir_db import AgirDB
import sys

def daily_processing():
    with AgirDB() as db:
        for stage in ['raw_to_dng', 'dng_to_jpg']:
            batches = db.gaps.get_batches_with_gaps(stage, limit=10)
            
            for batch in batches:
                try:
                    db.stages.start(batch['batch_id'], stage)
                    process_batch(batch, stage)
                    db.stages.complete(batch['batch_id'], stage, success=True)
                except Exception as e:
                    db.stages.complete(
                        batch['batch_id'],
                        stage,
                        success=False,
                        error_message=str(e)
                    )
                    continue

if __name__ == '__main__':
    sys.exit(0 if daily_processing() else 1)
```

### B. Health Check Script

```python
#!/usr/bin/env python3
"""Pipeline health check script."""

from agir_db import AgirDB
import sys

def health_check():
    issues = []
    
    with AgirDB() as db:
        # Check for stuck stages
        stuck = [s for s in db.stages.get_in_progress()
                if s['duration_seconds'] > 7200]
        if stuck:
            issues.append(f"Found {len(stuck)} stuck stages")
        
        # Check gap percentages
        for stage in ['raw_to_dng', 'dng_to_jpg']:
            summary = db.gaps.get_gap_summary(stage)
            if summary['overall_gap_percentage'] > 10:
                issues.append(
                    f"{stage}: {summary['overall_gap_percentage']:.1f}% gaps"
                )
        
        # Check recent errors
        errors = db.analytics.get_error_summary()
        if errors['total_errors'] > 50:
            issues.append(f"High error count: {errors['total_errors']}")
    
    if issues:
        print("⚠ Pipeline Issues:")
        for issue in issues:
            print(f"  - {issue}")
        return False
    else:
        print("✓ Pipeline healthy")
        return True

if __name__ == '__main__':
    sys.exit(0 if health_check() else 1)
```

---

*End of AgirDB API Documentation*
