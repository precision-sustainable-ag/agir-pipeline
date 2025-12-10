---
layout: minimal
title: Requirements
nav_order: 2
nav_enabled: false
---

1. TOC
{:toc}

## **Architectural Requirements**

**1. Pipeline Gaps Methodology**
- Missing output files serve as the "source of truth" for work discovery
- More reliable than status flags that can drift from reality
- Self-correcting: system remains reliable even if status tracking fails
- Views detect gaps by comparing expected vs. actual files in storage

**2. Clean Separation of Concerns**
- Database operations completely separate from conversion logic
- Each component has a single, well-defined responsibility
- Flat architecture over nested abstractions
- Minimal coupling between components

**3. Generic Pipeline Stages**
- Support arbitrary stage names with underscore conventions (e.g., `raw_to_dng`, `dng_to_jpg`, `object_detection`)
- Extensible for future computer vision pipelines
- Not hardcoded to specific workflows

## **API Design Requirements**

**4. Production Style - Explicit Parameters**
```python
db.images.insert(
    image_id='MD_1683434234',
    batch_id='MD_2025-01-01',
    width_px=13376,
    height_px=9528,
    camera_make='SVS_VISTEK',
    # ... all parameters explicit
)
```
- No dataclasses
- Type hints for all parameters
- IDE autocomplete support
- Clear required vs optional parameters
- Plain functions returning dicts

**5. Idempotent Operations**
- Safe to re-run methods multiple times
- Handle duplicates gracefully
- Use UPSERT patterns where appropriate
- Recover from failures without corruption

**6. Comprehensive Error Handling**
- Custom exception hierarchy (not just generic errors)
- All methods raise exceptions on error (no silent failures)
- Detailed error messages with context
- Transaction-safe with proper rollback

**7. Dual Logging System**
- **File logging**: `/project/dash_agir/logs/agir_db_YYYYMMDD.log`
- **Database logging**: `processed.events` table
- Both capture: timestamp, batch_id, stage, status, error details
- Full audit trail for debugging and monitoring

## **Data Storage Requirements**

**8. Fully Queryable Metadata (No JSONB for Core Data)**
- All searchable fields as proper database columns
- No JSONB fallback for structured data
- Proper indexes for fast queries
- JSONB only for truly flexible/extensible metadata

**9. Rich Database Storage Over JSON Files**
- Prefer database tables for metadata
- String-based IDs: `"MD_1683434234"` for images, `"MD_1683434234_0"` for cutouts
- Comprehensive indexing strategy (50+ indexes planned)
- Foreign keys with CASCADE for data integrity

## **Operational Requirements**

**10. Context Manager Support**
```python
with AgirDB() as db:
    # Do work
    db.commit()  # or rollback on error
```
- Automatic connection management
- Transaction handling
- Proper cleanup on exit

**11. Bulk Operations Support**
```python
# Single insert
db.images.insert(...)

# Bulk insert (preferred for efficiency)
db.images.insert_bulk([image1, image2, ...])
```
- Both single and bulk methods
- Bulk methods for efficiency with large batches
- Maintain transactional integrity

**12. Simple Retry Logic (Manual for Now)**
```python
db.stages.reset(batch_id, 'raw_to_jpg')  # Clear failed status
db.stages.start(batch_id, 'raw_to_jpg', job_id)
```
- Manual retry capability
- Future-ready for workflow orchestration (Snakemake/Airflow)
- Track retry attempts via metadata

## **Component Organization**

**13. Domain-Specific Components**
```sh
AgirDB
├── gaps          # Work discovery
├── stages        # Status tracking
├── images        # Image metadata
├── batches       # Batch metadata
├── transfers     # JUNO transfer operations
├── events        # Event logging
├── inventory     # File inventory sync
├── analytics     # Reporting and statistics
├── migration     # SQLite import
└── orchestration # High-level workflows
```

**14. Comprehensive Method Coverage (~95+ methods across 10 classes)**
- 6-8 methods per component on average
- Each method has single responsibility
- Clear, descriptive method names
- Consistent parameter patterns

## **Quality Requirements**

**15. Production-Ready Standards**
- Comprehensive docstrings for all methods
- Test suite for each component
- Type hints throughout
- Proper exception handling
- Performance optimization (indexed queries, bulk operations)

**16. Configuration via Environment**
```bash
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agir
export PGUSER=agir_user
# Password via .pgpass file
```
- Standard PostgreSQL environment variables
- Alternative: direct parameter passing
- No hardcoded credentials


**17. Dual Logging System**
- File logging: `/project/dash_agir/logs/agir_db_YYYYMMDD.log`
- Database logging: `processed.events` table

## **Current Scope (7 Active Tables)**

Currently implemented for RAW→JPG conversion:
- `source.globus_file_index`
- `processed.batches`
- `processed.images`
- `processed.stage_status`
- `processed.events`
- `processed.transfers`
- Plus 20+ reporting views

Future stages (intentionally unimplemented until needed):
- Object detection, segmentation, cutouts, feature extraction

---

## **Future Requirements** 

### **Near term (6-12 months)**

#### **1. Computer Vision Pipeline Support (agir-db side)**

**New Database Tables:**
```sql
processed.detections      -- Bounding boxes from object detection
processed.segmentations   -- Mask file paths
processed.cutouts         -- Extracted plant images
processed.cutout_features -- Morphological/spectral features
```

**New Python Methods (in agir-db):**
```python
# Detection CRUD
db.detections.insert(image_id, bounding_boxes)
db.detections.insert_bulk(detection_results)
db.detections.get_by_image(image_id)
db.detections.get_by_class(class_name, min_confidence=0.8)
db.detections.count_by_batch(batch_id)

# Cutout CRUD
db.cutouts.insert(cutout_id, image_id, bbox, cutout_path)
db.cutouts.insert_bulk(cutout_list)
db.cutouts.get_by_image(image_id)
db.cutouts.get_primary(image_id)
db.cutouts.update_species(cutout_id, species_id)

# Segmentation CRUD
db.segmentations.insert(cutout_id, mask_path, area_px)
db.segmentations.get_by_cutout(cutout_id)

# Cutout Feature CRUD
db.features.insert(cutout_id, features_dict)
db.features.get_by_cutout(cutout_id)
db.features.search(feature_filters)
```

**Pipeline Gaps for CV:**
```python
# Work discovery for CV stages
images = db.gaps.get_files_with_gap(batch_id, 'object_detection')
images = db.gaps.get_files_with_gap(batch_id, 'segmentation')
cutouts = db.gaps.get_cutouts_needing_features(batch_id)
```

**Orchestration Helpers:**
```python
# High-level CV workflows
db.orchestration.run_detection_pipeline(
    batch_id, 
    detector_package='agir_detect',
    model='yolov8'
)

db.orchestration.run_full_cv_pipeline(
    batch_id,
    stages=['detection', 'segmentation', 'features']
)
```

---

#### **2. Globus Integration Enhancements**

**Smart Transfer Policies:**
```python
# Rule-based triggers
db.transfers.configure_auto_transfer(
    source='NCSU',
    destination='JUNO',
    trigger='batch_complete',
    stages_required=['raw_to_jpg']
)

# Space-based triggers
db.transfers.configure_space_trigger(
    source='NCSU',
    threshold_pct=80,
    destination='JUNO'
)
```

**Transfer Verification:**
```python
db.transfers.verify_transfer(transfer_id)  # Checksum validation
db.transfers.get_unverified()  # Transfers needing verification
```

---

#### **3. Enhanced Orchestration**

**Workflow Templates:**
```python
# Pre-built workflows
db.orchestration.full_raw_to_features_pipeline(
    batch_id,
    skip_existing=True,
    parallel_workers=4
)

# Custom workflow builder
workflow = db.orchestration.create_workflow()
workflow.add_stage('raw_to_jpg', package='svs_raw_api')
workflow.add_stage('object_detection', package='agir_detect')
workflow.add_stage('segmentation', package='agir_segment', depends_on='object_detection')
workflow.execute(batch_id)
```

**Progress Monitoring:**
```python
progress = db.orchestration.get_pipeline_progress(batch_id)
# Returns: {
#   'raw_to_jpg': {'status': 'completed', 'files': 150/150},
#   'object_detection': {'status': 'in_progress', 'files': 75/150},
#   'segmentation': {'status': 'pending', 'files': 0/150}
# }
```

**Automatic Retry Policies:**
```python
# Configure retry behavior
db.orchestration.set_retry_policy(
    stage='object_detection',
    max_retries=3,
    backoff='exponential',
    retry_on=['timeout', 'model_error']
)

# Retry failed batches
db.orchestration.retry_failed_stages(
    stage='object_detection',
    age_hours=24,
    max_batches=10
)
```

---

#### **4. Data Quality & Validation**

**Input Validation:**
```python
# Validation rules
db.validation.validate_batch_readiness(batch_id, stage='raw_to_jpg')
# Checks: files exist, correct format, minimum size, etc.

db.validation.validate_image_metadata(image_id)
# Checks: EXIF completeness, GPS bounds, dimension sanity

db.validation.validate_detection_results(detection_dict)
# Checks: bboxes within image bounds, valid class labels, confidence in range
```

**Quality Metrics:**
```python
# Track quality over time
db.quality.record_image_quality(image_id, blur_score, exposure_score)
db.quality.get_low_quality_images(batch_id, threshold=0.5)

# Detection quality
db.quality.get_detection_confidence_distribution(batch_id)
db.quality.flag_outliers(batch_id, stage='object_detection')
```

**Automated Alerts:**
```python
# Configure alerts
db.alerts.configure(
    name='high_error_rate',
    condition='error_rate > 0.10',
    window='1 hour',
    action='email',
    recipients=['matt@example.com']
)

db.alerts.configure(
    name='stuck_batch',
    condition='in_progress > 24 hours',
    action='slack',
    channel='#agir-alerts'
)
```

---

### **Medium term (1-2 years)**

#### **5. Horizontal Scalability**

**Multi-Worker Coordination:**
```python
# Worker registration
db.workers.register(worker_id='worker-001', hostname='node-42', capabilities=['gpu'])
db.workers.heartbeat(worker_id)  # Keep-alive
db.workers.deregister(worker_id)

# Smart work distribution
work = db.workers.claim_work(
    worker_id='worker-001',
    stage='object_detection',
    required_capabilities=['gpu']
)
# Uses row-level locking to prevent duplicate claims
```

**Distributed Processing:**
```python
# Parallel batch processing
db.orchestration.process_batches_parallel(
    stage='raw_to_jpg',
    max_workers=10,
    batches_per_worker=2
)
```

**Health Monitoring:**
```python
# Worker health
db.workers.get_health_status()
db.workers.detect_stale_workers(timeout_minutes=10)
db.workers.reassign_work(from_worker='worker-002', to_worker='worker-003')
```

---

#### **6. Database Performance Optimization**

**Table Partitioning:**
```sql
-- Partition large tables by date
CREATE TABLE processed.images_2025_01 PARTITION OF processed.images
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');

-- Automatic partition management
db.maintenance.create_monthly_partitions(
    table='processed.images',
    months_ahead=3
)
```

**Query Optimization:**
```python
# Query performance monitoring
db.analytics.get_slow_queries(min_duration_ms=1000)
db.analytics.suggest_indexes()

# Connection pooling tuning
db.configure_pool(min_connections=5, max_connections=20)
```

**Caching Layer:**
```python
# Redis integration for frequently accessed data
db.cache.enable(backend='redis', ttl=3600)
db.cache.invalidate(key='batch_summary_MD_2025-01-01')
```

---

#### **7. Data Lifecycle Management**

**Retention Policies:**
```python
# Configure retention
db.lifecycle.set_retention_policy(
    data_type='events',
    severity='INFO',
    retention_days=30
)

db.lifecycle.set_retention_policy(
    data_type='intermediate_files',
    file_pattern='*.dng',
    retention_days=90
)

# Execute cleanup
deleted = db.lifecycle.execute_cleanup()
```

**Archival:**
```python
# Archive old batches
db.lifecycle.archive_batch(
    batch_id='MD_2024-01-01',
    destination='glacier',
    keep_metadata=True
)

# Restore archived batch
db.lifecycle.restore_batch(batch_id='MD_2024-01-01')
```

**Backup & Recovery:**
```python
# Automated backups
db.backup.configure_schedule(frequency='daily', retention=7)
db.backup.create_snapshot(name='pre_migration_backup')
db.backup.restore_from_snapshot(name='pre_migration_backup')
```

---

#### **8. API & Integration Layer**

**REST API:**
```python
# FastAPI web service
from agir_api import AgirAPI

api = AgirAPI(db=db)
app = api.create_app()

# Endpoints:
# GET  /batches/{batch_id}
# POST /batches/{batch_id}/process
# GET  /batches/{batch_id}/progress
# GET  /images/{image_id}/detections
```

**CLI Tool:**
```bash
# Command-line interface
agir-db status                              # Overall system status
agir-db batch status MD_2025-01-01          # Single batch status
agir-db batch retry MD_2025-01-01 --stage raw_to_jpg
agir-db workers list                        # Active workers
agir-db gaps list --stage object_detection # Work queue
```

**Webhook Support:**
```python
# Register webhooks
db.webhooks.register(
    event='batch.completed',
    url='https://example.com/webhook',
    headers={'Authorization': 'Bearer token'}
)

# Trigger webhooks (automatic)
# Called internally when events occur
```

---

#### **9. Multi-User & Permissions**

**User Management:**
```python
# User accounts
db.users.create(username='researcher1', role='operator')
db.users.grant_permission(username='researcher1', permission='process_batches')

# Audit trail
db.audit.log_action(user='researcher1', action='started_batch', batch_id='MD_2025-01-01')
db.audit.get_user_actions(username='researcher1', days=7)
```

**Project Isolation:**
```python
# Multi-tenant support
db.projects.create(project_id='project_alpha', owner='researcher1')
db.projects.add_member(project_id='project_alpha', username='researcher2', role='viewer')

# Project-specific queries
batches = db.batches.get_by_project(project_id='project_alpha')
```

### **Long term (2+ years)**

---

#### **10. Enhanced Debugging & Observability**

**Distributed Tracing:**
```python
# OpenTelemetry integration
db.tracing.enable(backend='jaeger')

# Automatic trace propagation
with db.tracing.start_span('process_batch') as span:
    span.set_attribute('batch_id', batch_id)
    # Processing code here
```

**Advanced Profiling:**
```python
# Performance profiling
profile = db.profiling.profile_operation(
    operation='batch_processing',
    batch_id='MD_2025-01-01'
)

# Memory tracking
db.profiling.monitor_memory(alert_threshold_mb=8000)
```

---

#### **11. Data Export & Interoperability**

**ML Dataset Export:**
```python
# Export in ML-ready formats
db.export.to_coco_format(
    batch_ids=['MD_2025-01-01', 'MD_2025-01-02'],
    output_path='/datasets/training_set_v1.json'
)

db.export.to_yolo_format(
    batch_ids=batch_list,
    output_dir='/datasets/yolo_v8'
)

# Data versioning
db.export.create_dataset_version(
    name='training_set_v1',
    batch_ids=batch_list,
    tag='baseline'
)
```

---

## **Summary: Scope Boundaries**

### **What agir-db DOES (Database + Orchestration):**
- Store metadata and results
- Track processing status
- Discover work (pipeline gaps)
- Orchestrate workflows (call external packages)
- Log events and audit trail
- Generate analytics and reports
- Manage transfers
- Handle migrations
- Provide APIs for external systems

### **What agir-db DOES NOT DO (External Packages):**
- Image processing algorithms (svs-raw-api)
- Model inference code (goes in `agir-detect`)
- SAM/segmentation algorithms (goes in `agir-segment`)
- Feature extraction logic (goes in `agir-features`)
- PyTorch/TensorFlow dependencies
- Model weights/checkpoints


**Architecture Principle:** agir-db is the **control plane and data layer**. Domain-specific processing logic lives in dedicated packages.