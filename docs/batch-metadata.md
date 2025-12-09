# Batch Metadata Component

[← Back to Index](README.md)

The `batches` component manages metadata for processing batches.

---

## Methods

### `insert()`

Insert metadata for a new batch.

```python
db.batches.insert(
    batch_id='B001',
    collection_date='2025-01-15',
    location='Field_North',
    camera_id='SVS_001',
    image_count=150,
    metadata={'weather': 'sunny', 'crop': 'wheat'}
)
```

**Parameters:**
- `batch_id` (str): Unique batch identifier
- `collection_date` (str): Date images collected (YYYY-MM-DD)
- `location` (str): Collection location/field identifier
- `camera_id` (str): Camera identifier
- `image_count` (int): Expected number of images
- `metadata` (dict, optional): Free-form metadata

**Returns:** `None`

**Raises:**
- `DuplicateBatchError`: If batch_id already exists
- `ValidationError`: If required fields missing

---

### `get()`

Get metadata for a specific batch.

```python
batch = db.batches.get(batch_id='B001')
```

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** `Dict` - Batch metadata

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist

---

### `update()`

Update metadata for an existing batch.

```python
db.batches.update(
    batch_id='B001',
    image_count=148,
    metadata={'notes': 'Some images excluded'}
)
```

**Parameters:**
- `batch_id` (str): Batch identifier
- `collection_date` (str, optional): Update date
- `location` (str, optional): Update location
- `camera_id` (str, optional): Update camera
- `image_count` (int, optional): Update count
- `metadata` (dict, optional): Merge with existing

**Returns:** `None`

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist

---

### `list()`

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
- `start_date` (str, optional): Start date (YYYY-MM-DD)
- `end_date` (str, optional): End date (YYYY-MM-DD)
- `limit` (int, optional): Maximum to return. Default: 50
- `offset` (int, optional): Pagination offset. Default: 0

**Returns:** `List[Dict]` - List of batch metadata

**Raises:**
- `ValidationError`: If parameters invalid

---

### `delete()`

Delete a batch record (does not delete files).

```python
db.batches.delete(batch_id='B001')
```

**Parameters:**
- `batch_id` (str): Batch identifier

**Returns:** `None`

**Raises:**
- `BatchNotFoundError`: If batch doesn't exist

**Note:** Does not cascade delete images. Delete images first if needed.

---

## See Also

- [Image Metadata](image-metadata.md) - Individual image metadata
- [Database Schema](schema.md) - Table structure

[← Back to Index](README.md)
