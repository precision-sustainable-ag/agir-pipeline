# Inventory Synchronization Component

[← Back to Index](README.md)

The `inventory` component synchronizes file system state with database records.

---

## Methods

### `scan_directory()`

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
- `pattern` (str, optional): Glob pattern. Default: '*'
- `recursive` (bool, optional): Scan subdirs. Default: False

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

**Example:**
```python
result = db.inventory.scan_directory(
    directory='/data/raw/B001',
    batch_id='B001',
    file_type='raw',
    pattern='*.ARW'
)
print(f"Found {result['files_found']} files")
```

---

### `verify_batch()`

Verify file existence for all images in a batch.

```python
result = db.inventory.verify_batch(
    batch_id='B001',
    file_types=['raw', 'dng', 'jpg']
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `file_types` (List[str], optional): Types to verify. Default: all

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

**Example:**
```python
result = db.inventory.verify_batch('B001')
if result['jpg']['missing'] > 0:
    print(f"Missing {result['jpg']['missing']} JPG files")
```

---

### `mark_missing()`

Mark files as missing in database.

```python
count = db.inventory.mark_missing(
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

---

### `get_missing()`

Get list of images with missing files.

```python
missing = db.inventory.get_missing(
    batch_id='B001',
    file_type='jpg'
)
```

**Parameters:**
- `batch_id` (str, optional): Filter by batch
- `file_type` (str, optional): Filter by type

**Returns:** `List[Dict]` - Images with missing files:
```python
[
    {
        'image_id': 'MD_1683434234',
        'batch_id': 'B001',
        'file_type': 'jpg',
        'expected_path': '/data/jpg/B001/MD_1683434234.jpg',
        'marked_missing_at': '2025-01-15T10:30:00Z'
    }
]
```

---

## Usage Patterns

### Initialize Batch from Filesystem

```python
with AgirDB() as db:
    # Scan directory
    result = db.inventory.scan_directory(
        directory='/data/raw/B001',
        batch_id='B001',
        file_type='raw',
        pattern='*.ARW'
    )
    
    # Update batch count
    db.batches.update(
        batch_id='B001',
        image_count=result['files_found']
    )
```

### Verify Processing Results

```python
with AgirDB() as db:
    # After processing, verify files exist
    result = db.inventory.verify_batch('B001')
    
    if result['jpg']['missing'] > 0:
        print(f"Warning: {result['jpg']['missing']} JPG files missing")
        print(f"Missing images: {result['missing_images']}")
```

---

## See Also

- [Pipeline Gaps](pipeline-gaps.md) - Gap-based work discovery
- [Image Metadata](image-metadata.md) - Image records

[← Back to Index](README.md)
