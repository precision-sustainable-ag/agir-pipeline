# **FINAL COMPREHENSIVE PLANNING DOCUMENT**

## **✅ ALL DECISIONS CONFIRMED**

---

## **1. API DESIGN DECISIONS**

### **API Style: Production (Explicit Parameters)**
```python
# ✅ USE THIS STYLE
db.insert_developed_image(
    image_id='MD_1683434234',
    batch_id='MD_2025-01-01',
    width_px=13376,
    height_px=9528,
    camera_make='SVS_VISTEK',
    camera_model='shr661CXGE',
    camera_serial='119885',
    lens_model='Linos Inspect XL 60mm',
    focal_length=60.0,
    f_number=13.0,
    iso_speed=100,
    exposure_time=0.001,
    capture_timestamp=datetime(...),
    raw_path='/path/to/raw',
    jpg_path_processing='/path/to/jpg',
    software_version='2.0.0',
    color_profile_used='MD_calibration_v1'
)
```

**Characteristics:**
- Explicit parameters (IDE autocomplete)
- Type hints for all parameters
- No dataclasses (plain functions returning dicts)
- Clear required vs optional parameters

### **Metadata Storage: Fully Queryable (No JSONB)**
- All fields as proper columns
- Indexed for fast queries
- No JSONB fallback fields
- Clean, structured schema

### **Error Handling: Exceptions + Logging**
```python
import logging

# All methods raise exceptions on error
try:
    db.insert_developed_image(...)
except DuplicateImageError as e:
    logging.error(f"Image already exists: {e}")
    # Handle accordingly
except DatabaseError as e:
    logging.error(f"Database error: {e}")
    raise
```

**Logging strategy:**
- File logging: `/project/dash_agir/logs/agir_db_YYYYMMDD.log`
- Database logging: `logs.processing_events` table
- Both capture: timestamp, batch_id, stage, status, error details

### **Bulk Operations: Yes**
```python
# Single insert
db.insert_developed_image(...)

# Bulk insert (preferred for efficiency)
db.insert_developed_images([
    {'image_id': 'MD_001', 'batch_id': 'MD_2025-01-01', ...},
    {'image_id': 'MD_002', 'batch_id': 'MD_2025-01-01', ...},
    # ... hundreds of images
])
```

### **Retry Logic: Simple Manual (Future-Ready)**
```python
# Current: Manual retry
db.reset_stage(batch_id, 'raw_to_jpg')  # Clear failed status
db.start_stage(batch_id, 'raw_to_jpg', job_id)

# Future: Can be wrapped in Snakemake/Airflow/Prefect
# - Workflow engines handle retry logic
# - API just tracks attempts via retry_count
```

### **Transfer Management**
- **Trigger**: Manual + Scheduled
  - Manual: `python -m agir_db.transfer_batch --batch-id MD_2025-01-01`
  - Scheduled: Cron job runs daily, processes pending batches
- **Monitoring**: Synchronous (block until complete)
- **Error handling**: Alert on failure (manual intervention)

---

## **2. DATABASE SCHEMA (FINAL)**

### **NEW TABLES**

#### **processed.batch_metadata**
```sql
CREATE TABLE processed.batch_metadata (
    batch_id          TEXT PRIMARY KEY,
    batch_state       TEXT NOT NULL,  -- 'MD', 'TX', 'NC'
    batch_date        DATE NOT NULL,
    
    -- Location/Experiment context
    location_code     TEXT,  -- 'Field_A', 'Greenhouse_2'
    experiment_id     TEXT,
    season            TEXT,  -- '2025_Spring'
    plot_id           TEXT,
    
    -- Processing tracking
    image_count       INTEGER DEFAULT 0,
    processing_started TIMESTAMPTZ,
    processing_completed TIMESTAMPTZ,
    
    -- Notes
    notes             TEXT,
    
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_batch_metadata_date ON processed.batch_metadata(batch_date);
CREATE INDEX idx_batch_metadata_experiment ON processed.batch_metadata(experiment_id);
CREATE INDEX idx_batch_metadata_location ON processed.batch_metadata(location_code);
```

---

#### **processed.developed_images**
```sql
CREATE TABLE processed.developed_images (
    image_id          TEXT PRIMARY KEY,  -- "MD_1683434234"
    batch_id          TEXT NOT NULL REFERENCES processed.batch_metadata(batch_id),
    
    -- Image properties
    width_px          INTEGER NOT NULL,
    height_px         INTEGER NOT NULL,
    bit_depth         INTEGER,
    file_size_bytes   BIGINT,
    
    -- Timestamps
    capture_timestamp TIMESTAMPTZ,
    processed_timestamp TIMESTAMPTZ DEFAULT now(),
    
    -- Camera EXIF
    camera_make       TEXT,
    camera_model      TEXT,
    camera_serial     TEXT,
    lens_model        TEXT,
    focal_length      REAL,  -- mm
    f_number          REAL,
    iso_speed         INTEGER,
    exposure_time     REAL,  -- seconds
    shutter_speed     TEXT,
    
    -- File paths (traceability)
    raw_path          TEXT,
    jpg_path_processing TEXT NOT NULL,
    jpg_path_juno     TEXT,  -- NULL until transferred
    
    -- Processing info
    software_version  TEXT,
    color_profile_used TEXT,
    processing_node   TEXT,
    
    -- QC (future)
    qc_status         TEXT,  -- 'pending', 'pass', 'fail', 'review'
    qc_score          REAL,  -- 0.0 - 1.0
    qc_notes          TEXT,
    
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now(),
    
    UNIQUE(batch_id, image_id)
);

CREATE INDEX idx_developed_images_batch ON processed.developed_images(batch_id);
CREATE INDEX idx_developed_images_qc ON processed.developed_images(qc_status);
CREATE INDEX idx_developed_images_timestamp ON processed.developed_images(capture_timestamp);
CREATE INDEX idx_developed_images_camera ON processed.developed_images(camera_model);
```

---

#### **processed.detections** (Basic schema - future implementation)
```sql
CREATE TABLE processed.detections (
    cutout_id         TEXT PRIMARY KEY,  -- "MD_1683434234_0" (same as cutout_id)
    image_id          TEXT NOT NULL REFERENCES processed.developed_images(image_id),
    
    -- Bounding box (pixel coordinates)
    x_min             REAL NOT NULL,
    y_min             REAL NOT NULL,
    x_max             REAL NOT NULL,
    y_max             REAL NOT NULL,
    area_px           REAL,
    
    -- Classification
    class_id          TEXT NOT NULL,  -- 'plant', 'colorchecker', 'weed', 'marker'
    confidence        REAL,
    
    -- Model info
    model_name        TEXT,
    model_version     TEXT,
    
    -- Overlapping detections (for orthorectification)
    overlapping_cutout_ids TEXT[],  -- Array of other cutout_ids
    
    -- Instance tracking (future)
    instance_id       TEXT,  -- Global plant ID across images
    
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_detections_image ON processed.detections(image_id);
CREATE INDEX idx_detections_class ON processed.detections(class_id);
CREATE INDEX idx_detections_instance ON processed.detections(instance_id);
```

---

#### **processed.segmentations** (Basic schema - future implementation)
```sql
CREATE TABLE processed.segmentations (
    segmentation_id   BIGSERIAL PRIMARY KEY,
    cutout_id         TEXT NOT NULL REFERENCES processed.detections(cutout_id),
    image_id          TEXT NOT NULL REFERENCES processed.developed_images(image_id),
    
    -- Mask storage (file path only)
    mask_path         TEXT NOT NULL,
    mask_type         TEXT,  -- 'instance', 'semantic', 'panoptic'
    
    -- Mask properties
    area_px           REAL,
    
    -- Model info
    model_name        TEXT,
    model_version     TEXT,
    
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_segmentations_cutout ON processed.segmentations(cutout_id);
CREATE INDEX idx_segmentations_image ON processed.segmentations(image_id);
```

---

#### **processed.cutouts**
```sql
CREATE TABLE processed.cutouts (
    cutout_id         TEXT PRIMARY KEY,  -- "MD_1683434234_0" (1:1 with detection)
    image_id          TEXT NOT NULL REFERENCES processed.developed_images(image_id),
    
    -- Cutout file
    cutout_path       TEXT NOT NULL,
    width_px          INTEGER,
    height_px         INTEGER,
    file_size_bytes   BIGINT,
    
    -- Plant identification
    species_id        TEXT,
    variety           TEXT,
    is_primary        BOOLEAN DEFAULT false,
    
    -- Position/context
    row_position      INTEGER,  -- Row in potting area
    col_position      INTEGER,  -- Column in potting area
    touches_border    BOOLEAN,
    
    created_at        TIMESTAMPTZ DEFAULT now(),
    
    -- 1:1 relationship with detections
    CONSTRAINT fk_detection FOREIGN KEY (cutout_id) 
        REFERENCES processed.detections(cutout_id) ON DELETE CASCADE
);

CREATE INDEX idx_cutouts_image ON processed.cutouts(image_id);
CREATE INDEX idx_cutouts_species ON processed.cutouts(species_id);
CREATE INDEX idx_cutouts_primary ON processed.cutouts(is_primary);
```

---

#### **processed.cutout_features** (Basic schema - future implementation)
```sql
CREATE TABLE processed.cutout_features (
    feature_id        BIGSERIAL PRIMARY KEY,
    cutout_id         TEXT NOT NULL REFERENCES processed.cutouts(cutout_id),
    
    -- Feature type
    feature_type      TEXT NOT NULL,  -- 'morphological', 'spectral', 'texture'
    
    -- Morphological features
    area_px           REAL,
    perimeter_px      REAL,
    aspect_ratio      REAL,
    solidity          REAL,
    convex_hull_area  REAL,
    
    -- Spectral features (RGB)
    rgb_mean_r        REAL,
    rgb_mean_g        REAL,
    rgb_mean_b        REAL,
    rgb_std_r         REAL,
    rgb_std_g         REAL,
    rgb_std_b         REAL,
    
    -- Method info
    extractor_name    TEXT,
    extractor_version TEXT,
    
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_features_cutout ON processed.cutout_features(cutout_id);
CREATE INDEX idx_features_type ON processed.cutout_features(feature_type);
```

---

### **UPDATED EXISTING TABLES**

#### **logs.processing_events** (Add log file path)
```sql
-- Add column for log file reference
ALTER TABLE logs.processing_events 
ADD COLUMN log_file_path TEXT;
```

---

## **3. EARLY RELEASE PRIORITIES (9 PHASES)**

### **Phase 1: Connection Management**
```python
class AgirDB:
    def __init__(self, host=None, port=None, dbname=None, user=None)
    def __enter__(self)
    def __exit__(self, exc_type, exc_val, exc_tb)
    def connect(self)
    def close(self)
    def commit(self)
    def rollback(self)
```

---

### **Phase 2: Pipeline Gaps**
```python
# Identify batches needing raw2jpg
get_batches_with_gaps(stage='raw_to_jpg', limit=10, order_by='batch_date')
get_files_with_gap(batch_id, stage='raw_to_jpg')
get_batch_pipeline_summary(batch_id)
```

**SQL View:**
```sql
-- Already defined in planning
CREATE VIEW report.batches_needing_raw_to_jpg AS ...
```

---

### **Phase 3: Stage Status**
```python
# Get/update stage status
get_stage_status(batch_id, stage)
get_all_stage_statuses(batch_id)
start_stage(batch_id, stage, job_id=None)
complete_stage(batch_id, stage, success=True, error_message=None)
reset_stage(batch_id, stage)
```

---

### **Phase 4: Transfer Management**
```python
# JUNO transfers
get_batches_needing_juno_transfer(data_state='developed_jpg', limit=10)
start_juno_transfer(batch_id, data_state, source_info, dest_info, job_id=None)
complete_juno_transfer(transfer_id, success=True, error_message=None)
get_transfer_status(batch_id=None, data_state=None)
```

---

### **Phase 5: Stage Execution (raw_to_jpg)**
```python
# Image metadata insertion
insert_developed_image(
    image_id, batch_id, width_px, height_px,
    capture_timestamp=None,
    camera_make=None, camera_model=None, camera_serial=None,
    lens_model=None, focal_length=None, f_number=None,
    iso_speed=None, exposure_time=None,
    raw_path=None, jpg_path_processing=None,
    software_version=None, color_profile_used=None,
    processing_node=None
)

insert_developed_images(images: List[Dict])  # Bulk

get_developed_image(image_id)
get_developed_images(batch_id)
update_developed_image(image_id, **kwargs)
```

---

### **Phase 6: Logging**
```python
# File + database logging
setup_logging(log_dir='/project/dash_agir/logs', level='INFO')

# Database event logging
log_event(
    batch_id, file_name, stage, status,
    input_path=None, output_path=None,
    processing_time_sec=None,
    error_message=None,
    job_id=None, node_name=None,
    log_file_path=None
)

log_events(events: List[Dict])  # Bulk

get_events(batch_id=None, stage=None, status=None, limit=100)
get_failed_events(batch_id=None, stage=None, limit=100)
```

**Log file format:**
```
/project/dash_agir/logs/
    agir_db_20250109.log
    agir_db_20250110.log
```

---

### **Phase 7: Inventory Sync**
```python
# Sync file counts from globus_file_index
sync_batch_inventory(batch_id)

# Transfer operations (move to JUNO)
transfer_upload_to_juno(batch_id)        # upload_raw → JUNO
transfer_developed_to_juno(batch_id)     # developed_jpg/images → JUNO
transfer_metadata_to_juno(batch_id)      # developed_jpg/metadata → JUNO
transfer_cutouts_to_juno(batch_id)       # cutouts → JUNO
```

**Note:** These are convenience wrappers around the generic transfer methods.

---

### **Phase 8: Path Updates**
```python
# Update JUNO paths after transfer
update_image_juno_path(image_id, juno_path)
update_image_paths_after_transfer(batch_id, juno_base_path)
```

---

### **Phase 9: Migration**
```python
# Import from old SQLite DB
import_batch_from_sqlite(batch_id, sqlite_db_path)
import_images_from_sqlite(batch_id, sqlite_db_path)
validate_migration(batch_id)  # Check data integrity
get_migration_summary()       # Statistics
```

**Migration strategy:**
- Old SQLite: Single monolithic table
- New PostgreSQL: Split into proper normalized schema
- Map old columns → new tables (batch_metadata, developed_images, detections, cutouts)

---

## **4. COMPLETE API METHOD LIST (~48 methods)**

### **Connection Management (7)**
```
__init__, __enter__, __exit__
connect, close, commit, rollback
```

### **Pipeline Gaps (4)**
```
get_batches_with_gaps
get_files_with_gap
get_batch_pipeline_summary
get_gap_summary
```

### **Stage Status (7)**
```
start_stage
complete_stage
reset_stage
get_stage_status
get_all_stage_statuses
get_in_progress_batches
get_stuck_batches
```

### **File Queries (7)**
```
get_files_by_data_state
get_raw_files
get_jpg_files
get_metadata_files
get_cutout_files
get_file_count
file_exists
```

### **Event Logging (4)**
```
log_event
log_events (bulk)
get_events
get_failed_events
```

### **Inventory Sync (2)**
```
sync_batch_inventory
sync_all_inventories
```

### **Analytics (4)**
```
get_processing_summary
get_stage_summary
get_error_summary
get_throughput_stats
```

### **Batch Metadata (5)**
```
insert_batch_metadata
update_batch_metadata
get_batch_metadata
get_all_batches
delete_batch_metadata
```

### **Image Metadata (6)**
```
insert_developed_image
insert_developed_images (bulk)
get_developed_image
get_developed_images
update_developed_image
image_exists
```

### **Transfer Management (6)**
```
get_batches_needing_juno_transfer
start_juno_transfer
complete_juno_transfer
get_transfer_status
transfer_batch_to_juno (convenience wrapper)
poll_transfer_status (check Globus task)
```

### **Path Updates (2)**
```
update_image_juno_path
update_image_paths_after_transfer
```

### **Detection/Cutout Data (Future - 6)**
```
insert_detection (or insert_detections bulk)
get_detections
insert_cutout (or insert_cutouts bulk)
get_cutouts
insert_features
get_features
```

### **Migration (4)**
```
import_batch_from_sqlite
import_images_from_sqlite
validate_migration
get_migration_summary
```

---

## **5. PROCESSING WORKFLOW (Batch-Based)**

### **Typical workflow:**
```python
import logging
from agir_db import AgirDB

# Setup logging
logging.basicConfig(
    filename=f'/project/dash_agir/logs/agir_db_{datetime.now():%Y%m%d}.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

with AgirDB() as db:
    # 1. Get batches needing processing
    batches = db.get_batches_with_gaps(stage='raw_to_jpg', limit=10)
    
    for batch in batches:
        batch_id = batch['batch_id']
        
        # 2. Mark stage as started
        db.start_stage(batch_id, 'raw_to_jpg', job_id=slurm_job_id)
        
        try:
            # 3. Get all RAW files for this batch
            raw_files = db.get_raw_files(batch_id)
            
            # 4. Process ALL images in batch
            processed_images = []
            events = []
            
            for raw_file in raw_files:
                # Convert RAW → JPG (using svs-raw-api)
                result = process_raw_to_jpg(raw_file)
                processed_images.append(result['metadata'])
                events.append(result['event'])
            
            # 5. Bulk insert metadata
            db.insert_developed_images(processed_images)
            
            # 6. Bulk log events
            db.log_events(events)
            
            # 7. Mark stage complete
            db.complete_stage(batch_id, 'raw_to_jpg', success=True)
            
            # 8. Sync inventory
            db.sync_batch_inventory(batch_id)
            
            db.commit()
            
        except Exception as e:
            logging.error(f"Batch {batch_id} failed: {e}")
            db.complete_stage(batch_id, 'raw_to_jpg', 
                            success=False, error_message=str(e))
            db.rollback()
            continue
```

---

## **6. IMPLEMENTATION STRUCTURE**

```
agir-db/
├── src/agir_db/
│   ├── __init__.py          # Export AgirDB class
│   ├── api.py               # Main AgirDB class (~1500 lines)
│   ├── exceptions.py        # Custom exception classes
│   ├── utils/
│   │   ├── db.py           # Connection helper (from existing)
│   │   └── logging.py      # Logging setup
│   └── workers/
│       ├── process_batches.py     # Phase 5: raw_to_jpg worker
│       ├── transfer_batches.py    # Phase 7: JUNO transfer worker
│       └── migrate_sqlite.py      # Phase 9: Migration script
├── sql/
│   ├── schemas/
│   │   ├── 03_processed/
│   │   │   ├── processed.batch_metadata.sql
│   │   │   ├── processed.developed_images.sql
│   │   │   ├── processed.detections.sql
│   │   │   ├── processed.segmentations.sql
│   │   │   ├── processed.cutouts.sql
│   │   │   └── processed.cutout_features.sql
│   │   └── 06_report/
│   │       └── report.pipeline_gaps.sql (update existing)
│   └── migrations/
│       └── 001_add_new_tables.sql
└── tests/
    ├── test_api.py
    ├── test_gaps.py
    ├── test_transfers.py
    └── test_migration.py
```

---

## **7. NEXT STEPS (READY FOR IMPLEMENTATION)**

### **Phase-by-phase implementation order:**

1. ✅ **Planning complete** - All decisions made
2. **Phase 1**: Connection management + basic API skeleton
3. **Phase 2**: Pipeline gaps queries (SQL views + Python methods)
4. **Phase 3**: Stage status tracking
5. **Phase 6**: Logging setup (do this early)
6. **Phase 5**: Image metadata + raw_to_jpg integration
7. **Phase 4**: Transfer management
8. **Phase 7**: Inventory sync
9. **Phase 8**: Path updates
10. **Phase 9**: Migration tools

### **Estimated LOC:**
- `api.py`: ~1500 lines
- SQL schemas: ~500 lines
- Workers: ~1000 lines
- Tests: ~800 lines
- **Total: ~3800 lines**

---

## **🎯 READY TO IMPLEMENT?**

All planning complete. All questions answered. Architecture finalized.

**Shall we proceed with implementation, starting with Phase 1 (Connection Management)?**