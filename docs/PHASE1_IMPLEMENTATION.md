# **FINAL IMPLEMENTATION PLAN - Option 2 (Separate Domain Classes)**

---

## **1. FILE STRUCTURE**

```
agir-db/
├── src/agir_db/
│   ├── __init__.py                 # Export AgirDB + exceptions
│   ├── api.py                      # AgirDB facade (~150 lines)
│   ├── connection.py               # ConnectionManager (~150 lines)
│   ├── gaps.py                     # PipelineGaps (~250 lines)
│   ├── stages.py                   # StageStatus (~250 lines)
│   ├── images.py                   # ImageMetadata (~350 lines)
│   ├── transfers.py                # TransferManager (~300 lines)
│   ├── events.py                   # EventLogger (~250 lines)
│   ├── inventory.py                # InventorySync (~150 lines)
│   ├── analytics.py                # Analytics (~200 lines)
│   ├── migration.py                # Migration (~250 lines)
│   ├── batches.py                  # BatchMetadata (~200 lines)
│   ├── exceptions.py               # Custom exceptions (~100 lines)
│   └── utils/
│       ├── __init__.py
│       ├── db.py                   # Existing - keep as-is
│       └── logging_setup.py        # Logging configuration (~100 lines)
│
├── sql/
│   ├── schemas/
│   │   ├── 00_init/
│   │   │   └── 00_create_schemas.sql (existing)
│   │   ├── 02_source/
│   │   │   └── source.globus_file_index.sql (existing)
│   │   ├── 03_processed/
│   │   │   ├── processed.batch_metadata.sql (NEW)
│   │   │   ├── processed.batch_stage_status.sql (existing)
│   │   │   ├── processed.developed_images.sql (NEW)
│   │   │   ├── processed.detections.sql (NEW)
│   │   │   ├── processed.segmentations.sql (NEW)
│   │   │   ├── processed.cutouts.sql (NEW)
│   │   │   └── processed.cutout_features.sql (NEW)
│   │   ├── 05_logs/
│   │   │   ├── logs.processing_events.sql (update existing)
│   │   │   └── logs.juno_transfers.sql (existing)
│   │   └── 06_report/
│   │       ├── report.pipeline_gaps.sql (update existing)
│   │       ├── report.missing_on_juno.sql (existing)
│   │       └── report.batch_pipeline_status.sql (NEW)
│   └── migrations/
│       └── 001_add_processing_tables.sql
│
├── scripts/
│   ├── apply_schemas.py            # Apply all SQL schemas
│   ├── process_batches.py          # Worker: raw_to_jpg
│   ├── transfer_batches.py         # Worker: JUNO transfers
│   └── migrate_from_sqlite.py      # Migration tool
│
├── tests/
│   ├── test_connection.py
│   ├── test_gaps.py
│   ├── test_stages.py
│   ├── test_images.py
│   ├── test_transfers.py
│   ├── test_events.py
│   └── test_integration.py
│
├── pyproject.toml
└── README.md
```

---

## **2. CLASS RESPONSIBILITIES**

### **ConnectionManager** (`connection.py`)
**Responsibility:** Database connection lifecycle, transactions, resource management

**Methods:**
- `__init__(host, port, dbname, user, password)`
- `connect() -> psycopg2.connection`
- `close()`
- `commit()`
- `rollback()`
- `get_cursor(cursor_factory=None) -> cursor`
- `execute(sql, params) -> cursor`
- `execute_many(sql, params_list)`
- `fetch_one(sql, params) -> dict`
- `fetch_all(sql, params) -> List[dict]`

**State:**
- `_conn: psycopg2.connection`
- `_config: DBConfig`

---

### **PipelineGaps** (`gaps.py`)
**Responsibility:** Identify batches/files needing processing (source of truth for work discovery)

**Methods:**
- `__init__(connection: ConnectionManager)`
- `get_batches_with_gaps(stage=None, limit=10, order_by='batch_date', order_direction='DESC') -> List[dict]`
- `get_files_with_gap(batch_id: str, stage: str) -> List[dict]`
- `get_batch_pipeline_summary(batch_id: str) -> dict`
- `get_gap_summary(stage=None) -> dict`

**SQL Views Used:**
- `report.files_needing_raw_to_jpg`
- `report.files_needing_jpg_to_metadata`
- `report.batches_needing_raw_to_jpg`
- `report.batch_pipeline_status`

---

### **StageStatus** (`stages.py`)
**Responsibility:** Track pipeline stage execution (prevent duplicate work, monitor progress)

**Methods:**
- `__init__(connection: ConnectionManager)`
- `start(batch_id: str, stage: str, job_id=None, node_name=None) -> None`
- `complete(batch_id: str, stage: str, success=True, error_message=None) -> None`
- `reset(batch_id: str, stage: str) -> None`
- `get_status(batch_id: str, stage: str) -> dict`
- `get_all_statuses(batch_id: str) -> dict`
- `get_in_progress(stage=None) -> List[dict]`
- `get_stuck(stage=None, hours=4) -> List[dict]`

**Table:** `processed.batch_stage_status`

---

### **ImageMetadata** (`images.py`)
**Responsibility:** Manage developed image metadata (silver layer)

**Methods:**
- `__init__(connection: ConnectionManager)`
- `insert(image_id: str, batch_id: str, width_px: int, height_px: int, **kwargs) -> str`
- `insert_bulk(images: List[dict]) -> List[str]`
- `get(image_id: str) -> dict`
- `get_many(batch_id=None, image_ids=None, filters=None) -> List[dict]`
- `update(image_id: str, **kwargs) -> None`
- `update_juno_path(image_id: str, juno_path: str) -> None`
- `update_juno_paths_bulk(batch_id: str, juno_base_path: str) -> int`
- `exists(image_id: str) -> bool`
- `get_by_qc_status(qc_status: str, batch_id=None) -> List[dict]`
- `delete(image_id: str) -> None`

**Table:** `processed.developed_images`

---

### **TransferManager** (`transfers.py`)
**Responsibility:** Manage file transfers to JUNO LTS

**Methods:**
- `__init__(connection: ConnectionManager)`
- `get_batches_needing_transfer(data_state=None, limit=10) -> List[dict]`
- `start(batch_id: str, data_state: str, source_endpoint: str, source_dir: str, dest_endpoint: str, dest_dir: str, job_id=None, **kwargs) -> int`
- `update(transfer_id: int, task_id=None, status=None, error_message=None) -> None`
- `complete(transfer_id: int, success=True, error_message=None, file_count=None, total_bytes=None) -> None`
- `get_status(batch_id=None, data_state=None, status=None) -> List[dict]`
- `get_in_progress() -> List[dict]`
- `poll_globus_status(transfer_id: int) -> dict`
- `transfer_batch(batch_id: str, data_state: str, dest_base_path: str, job_id=None) -> int`

**Table:** `logs.juno_transfers`

**Views:** `report.batches_needing_juno_transfer`

---

### **EventLogger** (`events.py`)
**Responsibility:** Log file-level processing events (audit trail)

**Methods:**
- `__init__(connection: ConnectionManager)`
- `log(batch_id: str, file_name: str, stage: str, status: str, input_path=None, output_path=None, processing_time_sec=None, error_message=None, job_id=None, node_name=None, log_file_path=None) -> None`
- `log_bulk(events: List[dict]) -> None`
- `get_events(batch_id=None, stage=None, status=None, limit=100, order_by='created_at', order_direction='DESC') -> List[dict]`
- `get_failed_events(batch_id=None, stage=None, limit=100) -> List[dict]`
- `get_event_count(batch_id: str, stage: str, status=None) -> int`

**Table:** `logs.processing_events`

---

### **InventorySync** (`inventory.py`)
**Responsibility:** Sync file counts from globus_file_index

**Methods:**
- `__init__(connection: ConnectionManager)`
- `sync_batch(batch_id: str) -> dict`
- `sync_all(limit=None) -> int`
- `get_file_counts(batch_id: str) -> dict`

**Updates:** `processed.batch_metadata` (file counts)

---

### **Analytics** (`analytics.py`)
**Responsibility:** Reporting and statistics

**Methods:**
- `__init__(connection: ConnectionManager)`
- `get_processing_summary() -> dict`
- `get_stage_summary(stage: str) -> dict`
- `get_error_summary(stage=None, days=7) -> List[dict]`
- `get_throughput_stats(stage: str, days=7) -> dict`
- `get_batch_summary(batch_id: str) -> dict`

**Queries:** Aggregate stats across multiple tables

---

### **BatchMetadata** (`batches.py`)
**Responsibility:** Manage batch-level metadata

**Methods:**
- `__init__(connection: ConnectionManager)`
- `insert(batch_id: str, batch_state: str, batch_date, location_code=None, experiment_id=None, season=None, **kwargs) -> str`
- `update(batch_id: str, **kwargs) -> None`
- `get(batch_id: str) -> dict`
- `get_all(filters=None, limit=None, order_by='batch_date', order_direction='DESC') -> List[dict]`
- `delete(batch_id: str) -> None`
- `exists(batch_id: str) -> bool`

**Table:** `processed.batch_metadata`

---

### **Migration** (`migration.py`)
**Responsibility:** Import data from old SQLite database

**Methods:**
- `__init__(connection: ConnectionManager)`
- `import_batch(batch_id: str, sqlite_path: str) -> dict`
- `import_images(batch_id: str, sqlite_path: str) -> int`
- `import_detections(batch_id: str, sqlite_path: str) -> int`
- `import_cutouts(batch_id: str, sqlite_path: str) -> int`
- `validate_migration(batch_id: str) -> dict`
- `get_migration_summary() -> dict`

**Reads:** Old SQLite DB
**Writes:** Multiple tables in processed schema

---

### **AgirDB** (`api.py`) - Main Facade
**Responsibility:** Coordinate all components, provide unified interface, manage context

**Structure:**
```python
class AgirDB:
    def __init__(self, host=None, port=None, dbname=None, user=None):
        self._connection = ConnectionManager(host, port, dbname, user)
        
        # Initialize all domain classes
        self.gaps = PipelineGaps(self._connection)
        self.stages = StageStatus(self._connection)
        self.images = ImageMetadata(self._connection)
        self.transfers = TransferManager(self._connection)
        self.events = EventLogger(self._connection)
        self.inventory = InventorySync(self._connection)
        self.analytics = Analytics(self._connection)
        self.batches = BatchMetadata(self._connection)
        self.migration = Migration(self._connection)
    
    def __enter__(self):
        self._connection.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._connection.close()
    
    # Direct access to connection methods
    def commit(self): ...
    def rollback(self): ...
```

---

## **3. EXCEPTION HIERARCHY** (`exceptions.py`)

```python
class AgirDBError(Exception):
    """Base exception for all AgirDB errors"""

class ConnectionError(AgirDBError):
    """Database connection failed"""

class QueryError(AgirDBError):
    """Database query failed"""

class DuplicateError(AgirDBError):
    """Record already exists"""
    
class DuplicateImageError(DuplicateError):
    """Image already exists in database"""

class DuplicateBatchError(DuplicateError):
    """Batch already exists in database"""

class NotFoundError(AgirDBError):
    """Record not found"""
    
class ImageNotFoundError(NotFoundError):
    """Image not found in database"""
    
class BatchNotFoundError(NotFoundError):
    """Batch not found in database"""

class StageError(AgirDBError):
    """Stage operation failed"""
    
class StageAlreadyInProgressError(StageError):
    """Stage is already in progress for this batch"""

class TransferError(AgirDBError):
    """Transfer operation failed"""

class MigrationError(AgirDBError):
    """Migration operation failed"""

class ValidationError(AgirDBError):
    """Data validation failed"""
```

---

## **4. USAGE PATTERNS**

### **Simple workflow:**
```python
from agir_db import AgirDB
import logging

# Setup logging
logging.basicConfig(
    filename='/project/dash_agir/logs/agir_db_20250109.log',
    level=logging.INFO
)

with AgirDB() as db:
    # Get work
    batches = db.gaps.get_batches_with_gaps(stage='raw_to_jpg', limit=10)
    
    for batch in batches:
        batch_id = batch['batch_id']
        
        # Mark started
        db.stages.start(batch_id, 'raw_to_jpg', job_id='12345')
        
        try:
            # Get files
            raw_files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
            
            # Process (using svs-raw-api)
            results = process_all_raws(raw_files)
            
            # Insert metadata
            db.images.insert_bulk(results['images'])
            
            # Log events
            db.events.log_bulk(results['events'])
            
            # Complete
            db.stages.complete(batch_id, 'raw_to_jpg', success=True)
            
            # Sync
            db.inventory.sync_batch(batch_id)
            
        except Exception as e:
            logging.error(f"Failed: {e}")
            db.stages.complete(batch_id, 'raw_to_jpg', 
                             success=False, error_message=str(e))
            db.rollback()
            continue
```

### **Complex workflow with transfers:**
```python
with AgirDB() as db:
    # Get batches ready for transfer
    batches = db.transfers.get_batches_needing_transfer(data_state='developed_jpg')
    
    for batch in batches:
        # Start transfer
        transfer_id = db.transfers.start(
            batch_id=batch['batch_id'],
            data_state='developed_jpg',
            source_endpoint='ceres_ep',
            source_dir='/90daydata/...',
            dest_endpoint='juno_ep',
            dest_dir='/LTS/project/...',
            job_id='67890'
        )
        
        # Submit to Globus
        task_id = submit_globus_transfer(...)
        
        # Update with task ID
        db.transfers.update(transfer_id, task_id=task_id, status='submitted')
        
        # Wait for completion (synchronous)
        wait_for_globus_task(task_id)
        
        # Complete
        db.transfers.complete(transfer_id, success=True)
        
        # Update image paths
        db.images.update_juno_paths_bulk(
            batch_id=batch['batch_id'],
            juno_base_path='/LTS/project/dash_agir/semifield-developed-images'
        )
```

### **Analytics workflow:**
```python
with AgirDB() as db:
    # Overall summary
    summary = db.analytics.get_processing_summary()
    print(f"Total batches: {summary['total_batches']}")
    print(f"Total images: {summary['total_images']}")
    
    # Stage-specific
    stage_stats = db.analytics.get_stage_summary('raw_to_jpg')
    print(f"Completed: {stage_stats['completed']}")
    print(f"Failed: {stage_stats['failed']}")
    
    # Error analysis
    errors = db.analytics.get_error_summary(stage='raw_to_jpg', days=7)
    for error in errors[:10]:
        print(f"{error['count']}x: {error['error_message']}")
```

---

## **5. IMPLEMENTATION PHASES**

### **Phase 1: Foundation** (Day 1-2)
Files to implement:
- `exceptions.py` - All exception classes
- `connection.py` - ConnectionManager class
- `api.py` - AgirDB facade (basic structure)
- `utils/logging_setup.py` - Logging configuration
- SQL schemas for new tables

**Deliverable:** Can create AgirDB instance, connect, commit, rollback

---

### **Phase 2: Pipeline Gaps** (Day 2-3)
Files to implement:
- `gaps.py` - PipelineGaps class
- SQL views in `report.pipeline_gaps.sql`
- SQL view `report.batch_pipeline_status.sql`

**Deliverable:** Can identify batches needing processing

---

### **Phase 3: Stage Tracking** (Day 3-4)
Files to implement:
- `stages.py` - StageStatus class

**Deliverable:** Can start/complete/reset stages, prevent duplicate work

---

### **Phase 4: Event Logging** (Day 4-5)
Files to implement:
- `events.py` - EventLogger class
- Update `logs.processing_events.sql`

**Deliverable:** Can log processing events, query failures

---

### **Phase 5: Image Metadata** (Day 5-7)
Files to implement:
- `images.py` - ImageMetadata class
- `batches.py` - BatchMetadata class
- SQL: `processed.developed_images.sql`, `processed.batch_metadata.sql`

**Deliverable:** Can insert/query image metadata, batch metadata

---

### **Phase 6: Inventory Sync** (Day 7-8)
Files to implement:
- `inventory.py` - InventorySync class

**Deliverable:** Can sync file counts from globus_file_index

---

### **Phase 7: Transfer Management** (Day 8-10)
Files to implement:
- `transfers.py` - TransferManager class
- SQL view: `report.batches_needing_juno_transfer.sql`

**Deliverable:** Can manage JUNO transfers, update paths

---

### **Phase 8: Analytics** (Day 10-11)
Files to implement:
- `analytics.py` - Analytics class

**Deliverable:** Can generate reports, statistics

---

### **Phase 9: Migration** (Day 11-12)
Files to implement:
- `migration.py` - Migration class
- `scripts/migrate_from_sqlite.py` - Migration script

**Deliverable:** Can import old SQLite data

---

### **Phase 10: Workers & Integration** (Day 12-14)
Files to implement:
- `scripts/process_batches.py` - raw_to_jpg worker
- `scripts/transfer_batches.py` - Transfer worker
- Integration tests

**Deliverable:** Complete end-to-end pipeline

---

## **6. TESTING STRATEGY**

Each class gets its own test file:
- `test_connection.py` - Connection lifecycle, transactions
- `test_gaps.py` - Pipeline gap queries
- `test_stages.py` - Stage tracking logic
- `test_images.py` - Image metadata CRUD
- `test_transfers.py` - Transfer operations
- `test_events.py` - Event logging
- `test_integration.py` - End-to-end workflows

**Test database:** Separate test DB with sample data

---

## **7. ESTIMATED LOC BY FILE**

```
connection.py        150 lines
gaps.py              250 lines
stages.py            250 lines
images.py            350 lines
transfers.py         300 lines
events.py            250 lines
inventory.py         150 lines
analytics.py         200 lines
batches.py           200 lines
migration.py         250 lines
api.py               150 lines
exceptions.py        100 lines
utils/logging_setup.py 100 lines
--------------------------------
TOTAL:              2,700 lines

SQL schemas:         500 lines
Workers:            1,000 lines
Tests:              1,200 lines
--------------------------------
GRAND TOTAL:        5,400 lines
```

---

## **✅ READY TO START IMPLEMENTATION**

**Shall we begin with Phase 1 (Foundation)?**

This includes:
1. `exceptions.py` - Exception hierarchy
2. `connection.py` - ConnectionManager class
3. `api.py` - AgirDB facade skeleton
4. `utils/logging_setup.py` - Logging configuration

Once Phase 1 is complete, we'll have the foundation to build everything else on top of.

**Ready to write code?**