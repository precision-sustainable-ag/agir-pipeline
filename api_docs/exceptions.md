# Exception Handling

[← Back to Index](README.md)

AgirDB uses a hierarchical exception system for precise error handling.

---

## Exception Hierarchy

```
AgirDBError (base)
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

---

## Common Exceptions

### ConnectionError

Database connection failures.

```python
from agir_db import AgirDB, ConnectionError

try:
    with AgirDB() as db:
        # Work with database
        pass
except ConnectionError as e:
    print(f"Failed to connect: {e}")
    # Check: PGHOST, PGPORT, PGDATABASE env vars
    # Check: .pgpass credentials
    # Check: PostgreSQL is running
```

---

### StageAlreadyInProgressError

Attempt to start stage that's already running.

```python
from agir_db import StageAlreadyInProgressError

try:
    db.stages.start('B001', 'raw_to_jpg')
except StageAlreadyInProgressError:
    # Another worker has claimed this work
    print("Batch already being processed")
```

**Common scenario:** Multiple workers discovering same batch.

**Resolution:** Skip batch and move to next one.

---

### BatchNotFoundError

Referenced batch doesn't exist.

```python
from agir_db import BatchNotFoundError

try:
    batch = db.batches.get('B999')
except BatchNotFoundError:
    print("Batch B999 not found")
    # Create batch first or check batch ID
```

**Common scenario:** Typo in batch ID or batch not initialized.

**Resolution:** Verify batch ID or create batch record.

---

### ImageNotFoundError

Referenced image doesn't exist.

```python
from agir_db import ImageNotFoundError

try:
    image = db.images.get('MD_9999999999')
except ImageNotFoundError:
    print("Image not found")
    # Insert image record first
```

---

### DuplicateImageError

Attempt to insert image that already exists.

```python
from agir_db import DuplicateImageError

try:
    db.images.insert(image_data)
except DuplicateImageError:
    # Image already exists - update instead
    db.images.update(image_data['image_id'], **image_data)
```

---

### InvalidStageError

Invalid stage name provided.

```python
from agir_db import InvalidStageError

try:
    batches = db.gaps.get_batches_with_gaps('invalid_stage')
except InvalidStageError as e:
    print(f"Invalid stage: {e}")
    # Use valid stage names: raw_to_dng, dng_to_jpg, etc.
```

---

### StageNotStartedError

Attempt to complete stage that was never started.

```python
from agir_db import StageNotStartedError

try:
    db.stages.complete('B001', 'raw_to_jpg', success=True)
except StageNotStartedError:
    # Stage was never started
    print("Must call stages.start() before complete()")
```

---

### ValidationError

Invalid parameters or missing required fields.

```python
from agir_db import ValidationError

try:
    db.images.insert(image_data)
except ValidationError as e:
    print(f"Validation error: {e}")
    # Check required fields: image_id, batch_id, camera_id, etc.
```

---

## Usage Examples

### Basic Error Handling

```python
from agir_db import (
    AgirDB,
    ConnectionError,
    StageAlreadyInProgressError,
    BatchNotFoundError
)

try:
    with AgirDB() as db:
        db.stages.start('B001', 'raw_to_jpg')
        process_batch('B001')
        db.stages.complete('B001', 'raw_to_jpg', success=True)
        
except ConnectionError:
    print("Database connection failed")
    
except StageAlreadyInProgressError:
    print("Another worker processing this batch")
    
except BatchNotFoundError:
    print("Batch not found - create it first")
    
except Exception as e:
    print(f"Unexpected error: {e}")
```

---

### Specific Exception Handling

```python
from agir_db import (
    AgirDB,
    DuplicateImageError,
    ValidationError
)

with AgirDB() as db:
    try:
        db.images.insert(image_data)
    except DuplicateImageError:
        # Already exists - update instead
        db.images.update(image_data['image_id'], **image_data)
    except ValidationError as e:
        # Invalid data
        print(f"Invalid image data: {e}")
        raise
```

---

### Processing with Error Recovery

```python
from agir_db import AgirDB, StageAlreadyInProgressError

with AgirDB() as db:
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        try:
            db.stages.start(batch['batch_id'], 'raw_to_jpg')
        except StageAlreadyInProgressError:
            # Skip - another worker has it
            continue
        
        try:
            # Process batch
            result = process_batch(batch)
            db.stages.complete(
                batch['batch_id'],
                'raw_to_jpg',
                success=True,
                files_processed=result['count']
            )
        except Exception as e:
            # Mark as failed but continue
            db.stages.complete(
                batch['batch_id'],
                'raw_to_jpg',
                success=False,
                error_message=str(e)
            )
            continue
```

---

### Catching Base Exception

To catch all AgirDB exceptions:

```python
from agir_db import AgirDB, AgirDBError

try:
    with AgirDB() as db:
        # Work with database
        pass
except AgirDBError as e:
    # All AgirDB exceptions inherit from this
    print(f"AgirDB error: {e}")
except Exception as e:
    # Non-AgirDB errors
    print(f"Other error: {e}")
```

---

## Exception Import

Import exceptions from package root:

```python
from agir_db import (
    # Base
    AgirDBError,
    
    # Connection
    ConnectionError,
    TransactionError,
    
    # Query
    QueryError,
    
    # Duplicates
    DuplicateError,
    DuplicateImageError,
    DuplicateBatchError,
    
    # Not Found
    NotFoundError,
    ImageNotFoundError,
    BatchNotFoundError,
    TransferNotFoundError,
    
    # Stage
    StageError,
    StageAlreadyInProgressError,
    StageNotStartedError,
    InvalidStageError,
    
    # Transfer
    TransferError,
    TransferAlreadyInProgressError,
    GlobusError,
    
    # Migration
    MigrationError,
    SQLiteConnectionError,
    MigrationValidationError,
    
    # Validation
    ValidationError,
    InvalidParameterError,
    MissingRequiredFieldError,
)
```

---

## Best Practices

### 1. Handle Specific Exceptions First

```python
# ✓ Good
try:
    db.stages.start(batch_id, stage)
except StageAlreadyInProgressError:
    # Handle specific case
    pass
except AgirDBError:
    # Handle other AgirDB errors
    pass

# ✗ Avoid
try:
    db.stages.start(batch_id, stage)
except Exception:
    # Too broad - might catch unrelated errors
    pass
```

---

### 2. Don't Silence Errors

```python
# ✓ Good
try:
    db.images.insert(image_data)
except DuplicateImageError:
    logger.warning(f"Image {image_id} already exists")
    db.images.update(image_id, **image_data)

# ✗ Avoid
try:
    db.images.insert(image_data)
except DuplicateImageError:
    pass  # Silent failure hides problems
```

---

### 3. Log Exceptions with Context

```python
# ✓ Good
try:
    db.stages.start(batch_id, stage)
except StageAlreadyInProgressError:
    logger.info(f"Batch {batch_id} already processing - skipping")
except Exception as e:
    logger.error(f"Failed to start {batch_id}/{stage}: {e}")
    raise
```

---

### 4. Use Context Managers

```python
# ✓ Good - automatic transaction handling
try:
    with AgirDB() as db:
        db.images.insert(image_data)
except AgirDBError as e:
    # Transaction auto-rolled back
    handle_error(e)

# ✗ Avoid - manual transaction management
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

## See Also

- [Troubleshooting](troubleshooting.md) - Common issues
- [Best Practices](best-practices.md) - Error handling patterns

[← Back to Index](README.md)
