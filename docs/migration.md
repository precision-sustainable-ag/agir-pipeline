# Migration Component

[← Back to Index](index.md)

The `migration` component imports data from legacy SQLite databases.

---

## Methods

### `import_from_sqlite()`

Import all data from a SQLite database.

```python
result = db.migration.import_from_sqlite(
    sqlite_path='/path/to/legacy.db',
    batch_size=1000,
    dry_run=False
)
```

**Parameters:**
- `sqlite_path` (str): Path to SQLite database
- `batch_size` (int, optional): Records per batch. Default: 1000
- `dry_run` (bool, optional): Validate only. Default: False

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
- `SQLiteConnectionError`: Can't connect
- `MigrationValidationError`: Validation fails
- `MigrationError`: Import fails

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
```

---

### `validate_sqlite()`

Validate SQLite database structure and data.

```python
validation = db.migration.validate_sqlite(
    sqlite_path='/path/to/legacy.db'
)
```

**Parameters:**
- `sqlite_path` (str): Path to SQLite database

**Returns:** `Dict` - Validation results:
```python
{
    'valid': True,
    'schema_version': '1.0',
    'tables_found': ['batches', 'images', 'events'],
    'record_counts': {
        'batches': 45,
        'images': 15000
    },
    'warnings': [],
    'errors': []
}
```

**Raises:**
- `SQLiteConnectionError`: Can't connect
- `MigrationError`: Validation fails

---

[← Back to Index](index.md)
