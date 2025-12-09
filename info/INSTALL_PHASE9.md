# Phase 9 Installation Guide

## Quick Install

No SQL schema required - Phase 9 uses existing tables from Phase 5.

```bash
# Update Python package
cd /path/to/agir-db
pip install -e .
```

## Run Tests

```bash
# Run Phase 9 tests
python test_phase9.py

# Should see:
# ✓ All Phase 9 unit tests passed!
# ✓ All database integration tests passed!
# ✓ Phase 9 Complete!
```

## Quick Usage Test

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Test migration summary
    summary = db.migration.get_migration_summary()
    print(f"Total batches: {summary['total_batches']}")
    print(f"Total images: {summary['total_images']}")
    
    # Test validation (for existing batch)
    result = db.migration.validate_migration('MD_2024-06-01')
    print(f"Valid: {result['valid']}")
```

## Prerequisites

Phase 9 requires:
- ✓ Phase 5 tables (processed.batches, processed.images)
- ✓ Python sqlite3 module (standard library)
- ✓ Legacy SQLite database files to migrate

## Migration Workflow

### Step 1: Locate Legacy Databases

```bash
# Find all SQLite database files
find /data/legacy -name "*.db" -type f

# Example output:
# /data/legacy/2024/batch_MD_2024-06-01.db
# /data/legacy/2024/batch_TX_2024-07-15.db
```

### Step 2: Test with Dry Run

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Test import without modifying database
    stats = db.migration.import_sqlite_db(
        '/data/legacy/2024/batch_MD_2024-06-01.db',
        dry_run=True
    )
    
    print(f"Would import:")
    print(f"  {stats['batches_imported']} batches")
    print(f"  {stats['images_imported']} images")
```

### Step 3: Import Single Batch

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Import one batch
    stats = db.migration.import_sqlite_db(
        '/data/legacy/2024/batch_MD_2024-06-01.db',
        batch_id='MD_2024-06-01',
        dry_run=False
    )
    
    db.commit()
    
    print(f"Imported:")
    print(f"  {stats['batches_imported']} batches")
    print(f"  {stats['images_imported']} images")
```

### Step 4: Validate

```python
from agir_db import AgirDB

with AgirDB() as db:
    result = db.migration.validate_migration('MD_2024-06-01')
    
    if result['valid']:
        print("✓ Validation passed")
    else:
        print("✗ Issues found:")
        for issue in result['issues']:
            print(f"  - {issue}")
```

### Step 5: Bulk Migration

```python
from agir_db import AgirDB
from pathlib import Path

with AgirDB() as db:
    db_dir = Path('/data/legacy/2024')
    
    for db_file in db_dir.glob('*.db'):
        print(f"Importing {db_file.name}...")
        
        try:
            stats = db.migration.import_sqlite_db(str(db_file))
            db.commit()
            print(f"  ✓ Imported {stats['images_imported']} images")
        except Exception as e:
            print(f"  ✗ Error: {e}")
            db.rollback()
```

## Troubleshooting

### Error: "SQLite file not found"

Check file path:
```bash
ls -lh /data/legacy/batch.db
```

Use absolute paths:
```python
from pathlib import Path
db_path = Path('/data/legacy/batch.db').resolve()
stats = db.migration.import_sqlite_db(str(db_path))
```

### Error: "Batch already exists"

Use `skip_existing=True` (default):
```python
stats = db.migration.import_sqlite_db(
    path,
    skip_existing=True  # Will skip, not error
)
print(f"Skipped: {stats['batches_skipped']}")
```

Or delete existing batch first:
```python
with AgirDB() as db:
    db._connection.execute(
        "DELETE FROM processed.images WHERE batch_id = %s",
        ('MD_2024-06-01',)
    )
    db._connection.execute(
        "DELETE FROM processed.batches WHERE batch_id = %s",
        ('MD_2024-06-01',)
    )
    db.commit()
    
    # Now import
    stats = db.migration.import_sqlite_db(path)
```

### Error: "table does not exist"

Install Phase 5 first:
```bash
psql -f metadata_schema.sql
```

### Validation Fails

Check specific issues:
```python
result = db.migration.validate_migration('MD_2024-06-01')
print(f"Issues: {result['issues']}")
print(f"Missing fields: {result['missing_required_fields']}")
```

Fix missing data:
```python
# Update batch with missing fields
db.batches.update(
    'MD_2024-06-01',
    location='JUNO',
    batch_state='MD'
)
db.commit()

# Re-validate
result = db.migration.validate_migration('MD_2024-06-01')
```

### Slow Import (Large Databases)

Images are bulk-inserted in batches of 1000 for efficiency. For very large databases (100k+ images), consider:

1. Monitor progress:
```python
import logging
logging.basicConfig(level=logging.DEBUG)

stats = db.migration.import_sqlite_db(path)
# Will log: "Imported 1000 images", "Imported 1000 images", etc.
```

2. Split into smaller batches:
```bash
# Split large SQLite database into smaller files first
sqlite3 large.db "SELECT * FROM images WHERE image_id < 'MD_50000'" > part1.db
sqlite3 large.db "SELECT * FROM images WHERE image_id >= 'MD_50000'" > part2.db
```

## Performance Tips

### 1. Use Transactions

```python
with AgirDB() as db:
    for db_file in db_files:
        stats = db.migration.import_sqlite_db(str(db_file))
        db.commit()  # Commit after each file
```

### 2. Disable Logging for Speed

```python
import logging
logging.getLogger('agir_db.migration').setLevel(logging.WARNING)
```

### 3. Check Database Performance

```sql
-- Analyze tables after bulk import
ANALYZE processed.batches;
ANALYZE processed.images;
```

## Verification Queries

After migration, verify data:

```sql
-- Check batch counts
SELECT COUNT(*) FROM processed.batches;

-- Check image counts
SELECT COUNT(*) FROM processed.images;

-- Check for orphaned images
SELECT COUNT(*) 
FROM processed.images i
WHERE NOT EXISTS (
    SELECT 1 FROM processed.batches b 
    WHERE b.batch_id = i.batch_id
);

-- Check metadata presence
SELECT 
    COUNT(*) as total,
    COUNT(*) FILTER (WHERE metadata IS NOT NULL) as with_metadata
FROM processed.batches;
```

## What Changed

Phase 9 added:
1. **Python Class**: `Migration` with 3 methods
2. **Integration**: Added `db.migration` to AgirDB facade

## Next Steps

After Phase 9:
1. Run `test_phase9.py` to verify
2. Try importing a test SQLite database
3. Validate migrated data
4. Ready to proceed to Phase 10 (Orchestration Helpers)
