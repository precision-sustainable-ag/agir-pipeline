# Phase 1: Foundation - Complete ✓

## Overview

Phase 1 establishes the foundation for the AgirDB API with core infrastructure for connection management, error handling, and logging.

## Components Created

### 1. **exceptions.py** (~350 lines)
Complete exception hierarchy for all AgirDB operations:

```python
from agir_db import (
    AgirDBError,           # Base exception
    ConnectionError,       # Connection failures
    QueryError,            # SQL errors
    DuplicateImageError,   # Image already exists
    ImageNotFoundError,    # Image not found
    StageAlreadyInProgressError,  # Stage running
    # ... and 20+ more
)
```

**Exception Categories:**
- Connection errors (ConnectionError, TransactionError)
- Query errors (QueryError)
- Duplicate errors (DuplicateImageError, DuplicateBatchError, etc.)
- Not found errors (ImageNotFoundError, BatchNotFoundError, etc.)
- Stage errors (StageAlreadyInProgressError, InvalidStageError, etc.)
- Transfer errors (TransferError, GlobusError, etc.)
- Migration errors (MigrationError, SQLiteConnectionError, etc.)
- Validation errors (InvalidParameterError, InvalidImageIdError, etc.)

### 2. **connection.py** (~400 lines)
`ConnectionManager` class for database operations:

```python
from agir_db import ConnectionManager

# Manual usage
conn = ConnectionManager(host='localhost', port=5432, dbname='agir')
conn.connect()
result = conn.fetch_one("SELECT * FROM processed.batch_metadata WHERE batch_id = %s", 
                        ('MD_2025-01-01',))
conn.commit()
conn.close()

# Context manager (recommended)
with ConnectionManager() as conn:
    results = conn.fetch_all("SELECT * FROM processed.developed_images LIMIT 10")
```

**Methods:**
- `connect()` - Establish connection
- `close()` - Close connection
- `commit()` - Commit transaction
- `rollback()` - Rollback transaction
- `execute(query, params)` - Execute query
- `execute_many(query, params_list)` - Bulk operations
- `fetch_one(query, params)` - Get single result
- `fetch_all(query, params)` - Get all results
- `get_cursor()` - Get raw cursor

### 3. **logging_setup.py** (~150 lines)
Centralized logging configuration:

```python
from agir_db import setup_logging, get_logger

# Setup logging (call once at application start)
setup_logging(
    log_dir='/project/dash_agir/logs',
    level='INFO',
    console=True
)

# Get logger in your module
logger = get_logger(__name__)
logger.info("Processing batch MD_2025-01-01")
logger.error("Failed to process image MD_123")
```

**Features:**
- File logging with date-stamped names: `agir_db_20250109.log`
- Optional console logging
- Configurable log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Custom format strings
- Automatic log directory creation

### 4. **api.py** (~200 lines)
`AgirDB` facade class (skeleton for now):

```python
from agir_db import AgirDB

# Context manager (auto commit/rollback)
with AgirDB() as db:
    # Domain components will be accessible here
    # db.gaps.get_batches_with_gaps('raw_to_jpg')
    # db.stages.start(batch_id, 'raw_to_jpg')
    # db.images.insert_bulk(images)
    pass

# Manual usage
db = AgirDB()
db.connect()
try:
    # Do work
    db.commit()
except Exception as e:
    db.rollback()
    raise
finally:
    db.close()
```

**Future domain components** (placeholders for now):
- `db.gaps` - Pipeline gap analysis (Phase 2)
- `db.stages` - Stage status tracking (Phase 3)
- `db.events` - Event logging (Phase 4)
- `db.images` - Image metadata (Phase 5)
- `db.batches` - Batch metadata (Phase 5)
- `db.inventory` - Inventory sync (Phase 6)
- `db.transfers` - Transfer management (Phase 7)
- `db.analytics` - Analytics/reporting (Phase 8)
- `db.migration` - SQLite import (Phase 9)

### 5. **__init__.py** (~150 lines)
Package exports and public API definition

### 6. **test_phase1.py** (~200 lines)
Test script to verify Phase 1 components

## Installation

```bash
# From agir-db repository root
cd src/agir_db
pip install -e ../../
```

Or add to your environment:
```bash
export PYTHONPATH=/path/to/agir-db/src:$PYTHONPATH
```

## Testing

Run the Phase 1 test suite:

```bash
cd /path/to/phase1/files
python test_phase1.py
```

Expected output:
```
============================================================
Phase 1 Foundation Tests
============================================================
Testing exceptions...
✓ Exceptions work correctly

Testing logging...
✓ Logging works correctly

Testing ConnectionManager...
✓ ConnectionManager initializes correctly

Testing AgirDB facade...
✓ AgirDB facade initializes correctly

Testing exception catching...
✓ Exception catching works correctly

============================================================
✓ All Phase 1 tests passed!
============================================================

Phase 1 components are working correctly.
Ready to proceed to Phase 2 (Pipeline Gaps).
```

## Usage Example

Basic setup for any AgirDB application:

```python
#!/usr/bin/env python3
"""Example AgirDB application."""

from agir_db import AgirDB, setup_logging, get_logger

# Setup logging once at startup
setup_logging(level='INFO')
logger = get_logger(__name__)

def main():
    """Main application logic."""
    logger.info("Starting application")
    
    try:
        with AgirDB() as db:
            # Your database operations here
            logger.info(f"Connected: {db.is_connected}")
            
            # Future: db.gaps, db.stages, etc.
            
    except Exception as e:
        logger.error(f"Application failed: {e}")
        raise

if __name__ == '__main__':
    main()
```

## Environment Variables

AgirDB reads database credentials from environment:

```bash
export PGHOST=ceres20-compute-45.ceres.scinet.usda.gov
export PGPORT=50412
export PGDATABASE=agir
export PGUSER=matthew.kutugata
# Password via ~/.pgpass (recommended) or PGPASSWORD
```

Or source the connection file:
```bash
source /project/dash_agir/postgres/pg_coords.env
```

## Files Created

```
agir-db/src/agir_db/
├── __init__.py              # Package exports (150 lines)
├── api.py                   # AgirDB facade (200 lines)
├── connection.py            # ConnectionManager (400 lines)
├── exceptions.py            # Exception hierarchy (350 lines)
└── utils/
    └── logging_setup.py     # Logging utilities (150 lines)

tests/
└── test_phase1.py           # Phase 1 tests (200 lines)

Total: ~1,450 lines
```

## Next Steps: Phase 2 (Pipeline Gaps)

Phase 2 will implement work discovery through pipeline gap analysis:

1. **SQL Views** (sql/schemas/06_report/)
   - `report.files_needing_raw_to_jpg`
   - `report.batches_needing_raw_to_jpg`
   - `report.batch_pipeline_status`

2. **PipelineGaps Class** (gaps.py, ~250 lines)
   - `get_batches_with_gaps(stage, limit)`
   - `get_files_with_gap(batch_id, stage)`
   - `get_batch_pipeline_summary(batch_id)`
   - `get_gap_summary(stage)`

3. **Integration**
   - Uncomment `self.gaps = PipelineGaps(self._connection)` in api.py
   - Add exports to __init__.py
   - Create test_phase2.py

## Status

**Phase 1: COMPLETE ✓**

All foundation components are implemented and tested:
- ✓ Exception hierarchy (20+ exceptions)
- ✓ Connection management (ConnectionManager)
- ✓ Logging setup (file + console)
- ✓ API facade (AgirDB skeleton)
- ✓ Package structure (__init__.py)
- ✓ Test suite (test_phase1.py)

**Ready for Phase 2!**