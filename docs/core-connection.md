# Core Connection Methods

[← Back to Index](index.md)

## AgirDB Class

The main interface for all database operations.

### `__init__()`

Initialize database connection with credentials.

```python
db = AgirDB(
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

**Example:**
```python
from agir_db import AgirDB

# Use environment variables
db = AgirDB()

# Or specify credentials
db = AgirDB(
    host='localhost',
    port=5432,
    dbname='agir',
    user='agir_user',
    password='secret'
)
```

---

### `connect()`

Establish database connection.

```python
db.connect()
```

**Raises:**
- `ConnectionError`: If connection fails

**Example:**
```python
db = AgirDB()
try:
    db.connect()
    print("Connected successfully")
except ConnectionError as e:
    print(f"Connection failed: {e}")
```

---

### `close()`

Close database connection.

```python
db.close()
```

**Example:**
```python
db = AgirDB()
db.connect()
# Do work
db.close()
```

---

### `commit()`

Commit current transaction.

```python
db.commit()
```

**Raises:**
- `TransactionError`: If commit fails

**Example:**
```python
db = AgirDB()
db.connect()
try:
    db.images.insert(image_data)
    db.commit()
except Exception as e:
    db.rollback()
    raise
```

---

### `rollback()`

Rollback current transaction.

```python
db.rollback()
```

**Raises:**
- `TransactionError`: If rollback fails

**Example:**
```python
try:
    db.images.insert(image_data)
    db.commit()
except Exception as e:
    db.rollback()
    print(f"Transaction rolled back: {e}")
```

---

### `is_connected`

Property to check connection status.

```python
if db.is_connected:
    print("Connected")
```

**Returns:** `bool` - True if connected, False otherwise

---

## Context Manager Usage (Recommended)

The context manager automatically handles connection, commit/rollback, and cleanup.

```python
from agir_db import AgirDB

# Automatically connects, commits on success, rolls back on error, and closes
with AgirDB() as db:
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
    for batch in batches:
        db.stages.start(batch['batch_id'], 'raw_to_jpg')
        # Process batch
        db.stages.complete(batch['batch_id'], 'raw_to_jpg', success=True)
```

**Benefits:**
- Automatic connection management
- Automatic transaction handling (commit on success, rollback on error)
- Guaranteed cleanup (connection always closed)
- Cleaner, more readable code

---

## Manual Connection Management

For cases where you need fine-grained control:

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

**When to use manual management:**
- Long-running processes that need periodic commits
- Complex error handling requirements
- Integration with existing transaction management

---

## Domain Components

The AgirDB instance provides access to all domain components:

```python
with AgirDB() as db:
    db.gaps          # Pipeline gap analysis
    db.stages        # Stage status tracking
    db.images        # Image metadata management
    db.batches       # Batch metadata management
    db.events        # Event logging
    db.inventory     # Inventory synchronization
    db.transfers     # Transfer management
    db.analytics     # Analytics and reporting
    db.migration     # SQLite migration
```

Each component is documented in its respective page:
- [Pipeline Gaps](pipeline-gaps.md)
- [Stage Status](stage-status.md)
- [Image Metadata](image-metadata.md)
- [Batch Metadata](batch-metadata.md)
- [Event Logging](event-logging.md)
- [Inventory Sync](inventory-sync.md)
- [Transfer Management](transfer-management.md)
- [Analytics](analytics.md)
- [Migration](migration.md)

---

## Connection Configuration

### Environment Variables

```bash
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agir
export PGUSER=agir_user
# Password via .pgpass file (recommended)
```

### .pgpass File

Create `~/.pgpass` with format:
```
hostname:port:database:username:password
```

Example:
```
localhost:5432:agir:agir_user:secret_password
```

Set permissions:
```bash
chmod 600 ~/.pgpass
```

---

## Error Handling

All connection methods can raise exceptions from the [exception hierarchy](exceptions.md):

```python
from agir_db import AgirDB, ConnectionError, TransactionError

try:
    with AgirDB() as db:
        # Work with database
        pass
except ConnectionError as e:
    print(f"Failed to connect: {e}")
except TransactionError as e:
    print(f"Transaction error: {e}")
```

See [Exception Handling](exceptions.md) for complete reference.

---

[← Back to Index](index.md)
