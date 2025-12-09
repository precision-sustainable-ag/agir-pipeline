# Phase 9: Migration Tools - Complete ✓

## Overview

Phase 9 implements tools for migrating data from legacy SQLite databases to the new PostgreSQL schema. This enables smooth transition from old systems while preserving data integrity.

**Why Migration Tools?**
- **Legacy data import**: Bring existing data into new system
- **Data transformation**: Convert old formats to new schema
- **Validation**: Ensure data integrity after migration
- **Batch processing**: Handle large-scale migrations efficiently
- **Idempotent**: Safe to re-run without duplicating data

## Components Created

### 1. **Migration Class** (migration.py, ~600 lines)

Comprehensive migration capabilities:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Import from SQLite
    stats = db.migration.import_sqlite_db(
        '/path/to/legacy_batch.db',
        batch_id='MD_2024-06-01'
    )
    
    # Validate migration
    result = db.migration.validate_migration('MD_2024-06-01')
    
    # Get summary
    summary = db.migration.get_migration_summary()
```

**Main Methods (3 total):**

1. **`import_sqlite_db(sqlite_path, batch_id=None, dry_run=False, skip_existing=True)`**
   - Import batch and image data from SQLite
   - Transform legacy formats to new schema
   - Support dry run mode for testing
   - Skip existing data to avoid duplicates

2. **`validate_migration(batch_id)`**
   - Verify migrated data integrity
   - Check required fields
   - Validate relationships
   - Return detailed issues list

3. **`get_migration_summary()`**
   - Get overview of all migrated data
   - Count batches and images
   - Track metadata presence


## Installation

No SQL schema required for Phase 9 - it uses existing tables from Phase 5.

```bash
# Update Python package
cd /path/to/agir-db
pip install -e .
```

## Testing

```bash
python test_phase9.py
```

Expected output:
```
✓ All Phase 9 unit tests passed!
✓ All database integration tests passed!
✓ Phase 9 Complete!
```

## Usage Examples

### Example 1: Import Single Batch

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Import from legacy SQLite database
    stats = db.migration.import_sqlite_db(
        '/data/legacy/batch_MD_2024-06-01.db',
        batch_id='MD_2024-06-01',
        dry_run=False,
        skip_existing=True
    )
    
    db.commit()
    
    print(f"Migration Results:")
    print(f"  Batches imported: {stats['batches_imported']}")
    print(f"  Images imported: {stats['images_imported']}")
    print(f"  Batches skipped: {stats['batches_skipped']}")
    
    if stats['errors']:
        print(f"  Errors: {len(stats['errors'])}")
        for error in stats['errors']:
            print(f"    - {error}")
```

### Example 2: Dry Run (Test Before Import)

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Test import without actually modifying database
    stats = db.migration.import_sqlite_db(
        '/data/legacy/batch_TX_2024-07-15.db',
        dry_run=True
    )
    
    print(f"Dry Run Results:")
    print(f"  Would import {stats['batches_imported']} batches")
    print(f"  Would import {stats['images_imported']} images")
    print(f"  Would skip {stats['batches_skipped']} existing batches")
    
    # If results look good, run actual import
    if input("Proceed with actual import? (y/n): ").lower() == 'y':
        stats = db.migration.import_sqlite_db(
            '/data/legacy/batch_TX_2024-07-15.db',
            dry_run=False
        )
        db.commit()
        print("✓ Import complete")
```

### Example 3: Validate Migrated Data

```python
from agir_db import AgirDB

with AgirDB() as db:
    batch_id = 'MD_2024-06-01'
    
    # Validate migration
    result = db.migration.validate_migration(batch_id)
    
    print(f"Validation Results for {batch_id}:")
    print(f"  Valid: {result['valid']}")
    print(f"  Batch exists: {result['batch_exists']}")
    print(f"  Image count: {result['image_count']}")
    
    if result['missing_required_fields']:
        print(f"  Missing fields: {result['missing_required_fields']}")
    
    if result['issues']:
        print(f"\n  Issues found:")
        for issue in result['issues']:
            print(f"    - {issue}")
    else:
        print(f"  ✓ No issues found")
```

### Example 4: Bulk Migration

```python
from agir_db import AgirDB
from pathlib import Path

def migrate_directory(directory: str):
    """Migrate all SQLite databases in a directory."""
    with AgirDB() as db:
        db_dir = Path(directory)
        
        # Find all .db files
        db_files = list(db_dir.glob('*.db'))
        print(f"Found {len(db_files)} database files")
        
        total_batches = 0
        total_images = 0
        errors = []
        
        for i, db_file in enumerate(db_files, 1):
            print(f"\n[{i}/{len(db_files)}] Processing {db_file.name}...")
            
            try:
                # Extract batch_id from filename
                # e.g., "batch_MD_2024-06-01.db" -> "MD_2024-06-01"
                batch_id = db_file.stem.replace('batch_', '')
                
                # Import
                stats = db.migration.import_sqlite_db(
                    str(db_file),
                    batch_id=batch_id,
                    dry_run=False,
                    skip_existing=True
                )
                
                db.commit()
                
                total_batches += stats['batches_imported']
                total_images += stats['images_imported']
                
                print(f"  ✓ Imported: {stats['batches_imported']} batches, "
                      f"{stats['images_imported']} images")
                
                if stats['batches_skipped'] > 0:
                    print(f"  ⚠ Skipped: {stats['batches_skipped']} existing batches")
                
            except Exception as e:
                print(f"  ✗ Error: {e}")
                errors.append((db_file.name, str(e)))
                db.rollback()
                continue
        
        print(f"\n{'='*60}")
        print(f"Migration Complete:")
        print(f"  Total batches imported: {total_batches}")
        print(f"  Total images imported: {total_images}")
        print(f"  Errors: {len(errors)}")
        
        if errors:
            print(f"\n  Failed migrations:")
            for filename, error in errors:
                print(f"    {filename}: {error}")

# Use it
migrate_directory('/data/legacy/2024/')
```

### Example 5: Migration with Validation

```python
from agir_db import AgirDB

def migrate_and_validate(sqlite_path: str, batch_id: str):
    """Import and validate in one operation."""
    with AgirDB() as db:
        print(f"Migrating {batch_id}...")
        
        # Import
        stats = db.migration.import_sqlite_db(
            sqlite_path,
            batch_id=batch_id,
            dry_run=False
        )
        db.commit()
        
        print(f"✓ Imported: {stats['batches_imported']} batches, "
              f"{stats['images_imported']} images")
        
        # Validate
        print(f"\nValidating...")
        result = db.migration.validate_migration(batch_id)
        
        if result['valid']:
            print(f"✓ Validation passed")
            return True
        else:
            print(f"✗ Validation failed:")
            for issue in result['issues']:
                print(f"  - {issue}")
            return False

# Use it
success = migrate_and_validate(
    '/data/legacy/batch_MD_2024-06-01.db',
    'MD_2024-06-01'
)
```

### Example 6: Migration Progress Report

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get overall migration status
    summary = db.migration.get_migration_summary()
    
    print("Migration Status Report")
    print("="*60)
    print(f"\n📦 Batches:")
    print(f"  Total: {summary['total_batches']}")
    print(f"  With metadata: {summary['batches_with_metadata']}")
    
    print(f"\n📷 Images:")
    print(f"  Total: {summary['total_images']}")
    print(f"  With EXIF: {summary['images_with_exif']}")
    
    # Calculate percentages
    if summary['total_batches'] > 0:
        metadata_pct = (summary['batches_with_metadata'] / 
                       summary['total_batches'] * 100)
        print(f"\n  {metadata_pct:.1f}% of batches have metadata")
    
    if summary['total_images'] > 0:
        exif_pct = (summary['images_with_exif'] / 
                   summary['total_images'] * 100)
        print(f"  {exif_pct:.1f}% of images have EXIF data")
```

## Data Transformation

The migration tools automatically transform legacy data formats:

### Batch Data Transformation

**Legacy Format:**
```python
{
    'id': 'MD_2024-06-01',
    'date': '2024-06-01',
    'site': 'JUNO',
    'file_count': 150,
    'size_bytes': 3750000000
}
```

**Transformed to:**
```python
{
    'batch_id': 'MD_2024-06-01',
    'batch_state': 'MD',              # Extracted from batch_id
    'batch_date': date(2024, 6, 1),   # Converted to date object
    'location': 'JUNO',               # Mapped from 'site'
    'file_count_raw': 150,            # Mapped from 'file_count'
    'total_bytes': 3750000000,
    'processing_status': 'pending',
    'metadata': {                      # Original data preserved
        'imported_from': 'sqlite',
        'original_data': {...}
    }
}
```

### Image Data Transformation

**Legacy Format:**
```python
{
    'id': 'MD_1234',
    'file_name': 'MD_1234.raw',
    'size_bytes': 25000000,
    'camera_make': 'Canon',
    'width': 8192
}
```

**Transformed to:**
```python
{
    'image_id': 'MD_1234',
    'batch_id': 'MD_2024-06-01',
    'file_name': 'MD_1234.raw',
    'file_ext': 'raw',
    'file_size_bytes': 25000000,
    'camera_make': 'Canon',
    'camera_model': None,
    'width': 8192,
    'height': None,
    'processing_status': 'pending'
}
```

## Supported Legacy Schemas

The migration tool automatically detects and handles multiple legacy table names:

### Batch Tables
- `batch_metadata`
- `batches`
- `batch_info`

### Image Tables
- `image_metadata`
- `images`
- `image_info`

### Column Name Mapping

The tool maps common legacy column names:

| Legacy Column | New Column |
|--------------|------------|
| `id` | `batch_id` or `image_id` |
| `date` | `batch_date` |
| `site` | `location` |
| `file_count` | `file_count_raw` |
| `size_bytes` | `total_bytes` or `file_size_bytes` |
| `status` | `processing_status` |


## Integration with Other Phases

### With Phase 5 (Metadata)
Migration populates batches and images tables:
```python
# After migration
batches = db.batches.get_by_state('MD')
images = db.images.get_by_batch('MD_2024-06-01')
```

### With Phase 6 (Inventory)
Use inventory sync to complement migration:
```python
# First migrate legacy data
db.migration.import_sqlite_db('/legacy/batch.db')

# Then sync from current file index
db.inventory.sync_batch('MD_2024-06-01')
```

### With Phase 8 (Analytics)
Validate migration with analytics:
```python
# After migration
summary = db.migration.get_migration_summary()
overview = db.analytics.get_pipeline_overview()

# Compare counts
print(f"Migrated: {summary['total_batches']} batches")
print(f"Total: {overview['total_batches']} batches")
```

## Validation Rules

The `validate_migration()` method checks:

1. **Batch Exists** - Batch record in processed.batches
2. **Required Fields** - batch_state, batch_date, location
3. **Image Count** - At least some images for batch
4. **No Orphans** - All images have valid batch_id

**Validation Result:**
```python
{
    'valid': True/False,
    'issues': [],
    'batch_exists': True/False,
    'image_count': int,
    'missing_required_fields': []
}
```

## Error Handling

### Common Errors

**1. SQLite file not found:**
```python
try:
    stats = db.migration.import_sqlite_db('/nonexistent.db')
except MigrationError as e:
    print(f"Error: {e}")
    # Error: SQLite file not found: /nonexistent.db
```

**2. Batch already exists:**
```python
# Use skip_existing=True (default)
stats = db.migration.import_sqlite_db(
    '/data/batch.db',
    skip_existing=True
)
# Skips existing batches instead of erroring
```

**3. Invalid data:**
```python
# Errors are collected in stats
stats = db.migration.import_sqlite_db('/data/batch.db')
if stats['errors']:
    print(f"Errors encountered: {len(stats['errors'])}")
    for error in stats['errors']:
        print(f"  - {error}")
```

## Best Practices

### 1. Always Test First
```python
# Dry run to check what will be imported
stats = db.migration.import_sqlite_db(path, dry_run=True)
print(f"Will import: {stats['batches_imported']} batches")

# If satisfied, do actual import
stats = db.migration.import_sqlite_db(path, dry_run=False)
```

### 2. Validate After Import
```python
stats = db.migration.import_sqlite_db(path)
db.commit()

result = db.migration.validate_migration(batch_id)
if not result['valid']:
    print(f"Validation failed: {result['issues']}")
```

### 3. Handle Large Migrations in Batches
```python
# Process files one at a time with progress tracking
for i, db_file in enumerate(db_files):
    print(f"[{i+1}/{len(db_files)}] {db_file.name}")
    stats = db.migration.import_sqlite_db(str(db_file))
    db.commit()
```

### 4. Preserve Original Data
```python
# Original data is stored in metadata field
batch = db.batches.get_by_id('MD_2024-06-01')
print(batch['metadata'])
# {'imported_from': 'sqlite', 'original_data': {...}}
```

### 5. Use skip_existing for Idempotency
```python
# Safe to re-run - won't duplicate data
stats = db.migration.import_sqlite_db(
    path,
    skip_existing=True  # Default
)
```

## API Reference

### import_sqlite_db(sqlite_path, batch_id=None, dry_run=False, skip_existing=True)

Import data from SQLite database.

**Parameters:**
- `sqlite_path` (str): Path to SQLite file
- `batch_id` (str, optional): Batch ID to assign
- `dry_run` (bool): Test without modifying database
- `skip_existing` (bool): Skip batches that already exist

**Returns:** dict with keys:
- `batches_imported`: Number of batches imported
- `images_imported`: Number of images imported
- `batches_skipped`: Number of existing batches skipped
- `errors`: List of error messages

### validate_migration(batch_id)

Validate migrated batch data.

**Parameters:**
- `batch_id` (str): Batch to validate

**Returns:** dict with keys:
- `valid`: Overall validation result
- `issues`: List of validation issues
- `batch_exists`: Whether batch exists
- `image_count`: Number of images
- `missing_required_fields`: List of missing fields

### get_migration_summary()

Get summary of all migrated data.

**Returns:** dict with keys:
- `total_batches`: Total batch count
- `total_images`: Total image count
- `batches_with_metadata`: Batches with metadata
- `images_with_exif`: Images with EXIF data

## Files Created

```
agir-db/
├── src/agir_db/
│   ├── migration.py                     # Migration class (600 lines)
│   ├── api.py                           # Updated integration
│   └── __init__.py                      # Updated exports
│
└── tests/
    └── test_phase9.py                   # Test suite (400 lines)

Total new code: ~1,000 lines
```

## Status

**Phase 9: COMPLETE ✓**

All migration components are implemented and tested:
- ✓ Migration class (3 main methods)
- ✓ SQLite import with transformation
- ✓ Data validation
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 10 (Orchestration Helpers)!**