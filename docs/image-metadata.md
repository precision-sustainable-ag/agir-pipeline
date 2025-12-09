# Image Metadata Component

[← Back to Index](index.md)

The `images` component manages metadata for individual images across all processing stages.

---

## Methods

### `insert()`

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
    metadata={'exposure': '1/1000', 'iso': 400}
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
- `metadata` (dict, optional): Free-form metadata (JSON)

**Returns:** `None`

**Raises:**
- `DuplicateImageError`: If image_id already exists
- `BatchNotFoundError`: If batch doesn't exist
- `ValidationError`: If required fields missing or invalid

---

### `insert_bulk()`

Insert metadata for multiple images efficiently.

```python
count = db.images.insert_bulk(images_data)
```

**Parameters:**
- `images_data` (List[Dict]): List of image metadata dictionaries

**Returns:** `int` - Number of images inserted

**Raises:**
- `DuplicateImageError`: If any image_id already exists
- `ValidationError`: If any record has invalid data

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
    # ... more images
]
count = db.images.insert_bulk(images)
print(f"Inserted {count} images")
```

---

### `get()`

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
    'metadata': {'iso': 400}
}
```

**Raises:**
- `ImageNotFoundError`: If image doesn't exist

---

### `update()`

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

**Example:**
```python
# Update after processing
db.images.update(
    'MD_1683434234',
    dng_path='/data/dng/B001/MD_1683434234.dng'
)
```

---

### `get_by_batch()`

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
- `limit` (int, optional): Maximum to return. Default: 1000
- `offset` (int, optional): Pagination offset. Default: 0

**Returns:** `List[Dict]` - List of image metadata

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist

---

### `search()`

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
- `start_time` (str, optional): Start of capture time range
- `end_time` (str, optional): End of capture time range
- `has_dng` (bool, optional): Filter by DNG existence
- `has_jpg` (bool, optional): Filter by JPG existence
- `limit` (int, optional): Maximum to return. Default: 100

**Returns:** `List[Dict]` - List of matching images

**Raises:**
- `ValidationError`: If search parameters invalid

**Example:**
```python
# Find images needing JPG conversion
images = db.images.search(
    batch_id='B001',
    has_dng=True,
    has_jpg=False
)
```

---

### `delete()`

Delete an image record (does not delete files).

```python
db.images.delete(image_id='MD_1683434234')
```

**Parameters:**
- `image_id` (str): Image identifier

**Returns:** `None`

**Raises:**
- `ImageNotFoundError`: If image doesn't exist

**Note:** Only removes database record. Physical files must be deleted separately.

---

## Usage Patterns

### Batch Insert After Processing

```python
with AgirDB() as db:
    # Process batch
    images_data = []
    for raw_file in scan_directory('/data/raw/B001'):
        images_data.append({
            'image_id': extract_id(raw_file),
            'batch_id': 'B001',
            'camera_id': 'SVS_001',
            'capture_time': extract_timestamp(raw_file),
            'raw_path': raw_file,
            'metadata': extract_exif(raw_file)
        })
    
    # Bulk insert
    count = db.images.insert_bulk(images_data)
    print(f"Inserted {count} images")
```

### Update Paths After Conversion

```python
with AgirDB() as db:
    images = db.gaps.get_images_with_gaps('B001', 'raw_to_jpg')
    
    for img in images:
        # Convert
        output_path = convert_to_jpg(img['input_path'])
        
        # Update metadata
        db.images.update(
            img['image_id'],
            jpg_path=output_path,
            metadata={'processed_at': datetime.now().isoformat()}
        )
```

### Query Processing Status

```python
with AgirDB() as db:
    # Find images missing JPG
    incomplete = db.images.search(
        batch_id='B001',
        has_jpg=False
    )
    print(f"{len(incomplete)} images need JPG conversion")
    
    # Find images with all outputs
    complete = db.images.search(
        batch_id='B001',
        has_dng=True,
        has_jpg=True
    )
    print(f"{len(complete)} images fully processed")
```

---

## Metadata Field

Store arbitrary JSON data in the `metadata` field:

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
            'aperture': 'f/5.6',
            'focal_length': 50
        },
        'quality_score': 0.95,
        'weather': 'sunny',
        'notes': 'Sample image'
    }
)
```

---

## See Also

- [Batch Metadata](batch-metadata.md) - Batch-level metadata
- [Pipeline Gaps](pipeline-gaps.md) - Find images needing work
- [Database Schema](schema.md) - Table structure

[← Back to Index](index.md)
