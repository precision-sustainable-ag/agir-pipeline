---
layout: default
title: Layout
nav_order: 4
---

# AgirDB Complete Table Reference

**Database:** `agir` (PostgreSQL)  
**Purpose:** Track agricultural image processing pipelines from RAW files through developed images, detections, and cutouts

---

## Table of Contents

1. [Schemas Overview](#schemas-overview)
2. [Source Schema](#source-schema) (Bronze Layer - File Inventory)
3. [Processed Schema](#processed-schema) (Silver Layer - Structured Data)
4. [Logs Schema](#logs-schema) (Audit Trail)
5. [Report Schema](#report-schema) (Materialized Views - Work Discovery)
6. [Core Schema](#core-schema) (Reference Data)

---

## Schemas Overview

| Schema | Purpose | Layer | Tables/Views |
|--------|---------|-------|--------------|
| **source** | File inventory from Globus | Bronze (raw) | 1 table |
| **processed** | Structured image/batch metadata | Silver (cleaned) | 4 tables, 15+ views |
| **logs** | Processing events & transfers | Audit | 2 tables |
| **report** | Pipeline gaps & status | Analytics | 8 views |
| **core** | Reference data (future) | Reference | TBD |

---

# Source Schema

## source.globus_file_index

**Purpose:** Complete inventory of all files discovered via Globus across all storage locations (JUNO, CERES, NCSU). This is the source of truth for "what files exist where."

**Use Cases:**
- Discover new batches
- Detect pipeline gaps (RAW exists but no JPG)
- Track file locations across storage systems
- Audit file counts and sizes

### Schema

```sql
CREATE TABLE source.globus_file_index (
    -- Identity
    file_id           BIGSERIAL PRIMARY KEY,
    
    -- Storage location
    endpoint          TEXT NOT NULL,        -- Globus endpoint ID
    location          TEXT NOT NULL,        -- 'JUNO', 'NCSU', 'CERES'
    lts_root          TEXT NOT NULL,        -- 'longterm_images', 'GROW_DATA', 'dash_agir'
    root_path         TEXT NOT NULL,        -- Full path to root (e.g., '/LTS/project/dash_agir/...')
    rel_path          TEXT NOT NULL,        -- Path under root_path (no leading slash)
    parent_dir        TEXT,                 -- Parent directory name
    file_name         TEXT NOT NULL,        -- File name with extension
    
    -- File properties
    entry_type        TEXT NOT NULL,        -- 'file' or 'dir'
    file_ext          TEXT,                 -- Extension without dot: 'RAW', 'jpg', 'json'
    size_bytes        BIGINT,               -- File size in bytes
    checksum          TEXT,                 -- MD5/SHA checksum (optional)
    
    -- Batch classification
    batch_id          TEXT,                 -- Parsed: 'MD_2025-01-01'
    batch_state       TEXT,                 -- Parsed: 'MD', 'TX', 'NC'
    batch_date        DATE,                 -- Parsed: 2025-01-01
    
    -- Pipeline stage
    data_state        TEXT NOT NULL,        -- 'upload_raw', 'developed_jpg', 'cutouts', etc.
    
    -- Timestamps
    mtime_iso         TIMESTAMPTZ,          -- Last modified time (from Globus)
    fname_ts_epoch    BIGINT,               -- Timestamp parsed from filename
    fname_ts_iso      TIMESTAMPTZ,          -- Timestamp parsed from filename (ISO)
    created_at_ts_iso TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- When row was inserted
);
```

### Indexes

```sql
-- Uniqueness constraint: one entry per file per location
CREATE UNIQUE INDEX ix_source_globus_unique
    ON source.globus_file_index(endpoint, data_state, root_path, rel_path);

-- Common queries
CREATE INDEX idx_globus_batch_id ON source.globus_file_index(batch_id);
CREATE INDEX idx_globus_data_state ON source.globus_file_index(data_state);
CREATE INDEX idx_globus_location ON source.globus_file_index(location);
CREATE INDEX idx_globus_file_ext ON source.globus_file_index(file_ext);
```

### Key Columns Explained

| Column | Example Value | Description |
|--------|---------------|-------------|
| `location` | `'JUNO'` | Which storage system (JUNO/CERES/NCSU) |
| `batch_id` | `'MD_2025-09-09'` | Unique batch identifier |
| `data_state` | `'upload_raw'` | Pipeline stage: raw, developed, cutouts |
| `file_ext` | `'RAW'` | File extension without dot |
| `entry_type` | `'file'` | Whether file or directory |

### Common Queries

```sql
-- Count RAW files per batch
SELECT batch_id, COUNT(*) as raw_count
FROM source.globus_file_index
WHERE file_ext = 'RAW' AND data_state = 'upload_raw'
GROUP BY batch_id;

-- Find all files for a batch
SELECT * FROM source.globus_file_index
WHERE batch_id = 'MD_2025-09-09'
ORDER BY data_state, file_name;

-- Storage usage by location
SELECT location, 
       SUM(size_bytes)/(1024^3) as size_gb,
       COUNT(*) as file_count
FROM source.globus_file_index
GROUP BY location;
```

---

# Processed Schema

## processed.batches

**Purpose:** Batch-level metadata and processing status. One row per batch with aggregate statistics and completion flags.

**Use Cases:**
- Track which batches are complete
- Store batch-level metadata (location, experiment, dates)
- Aggregate file counts across stages
- Monitor processing progress

### Schema

```sql
CREATE TABLE processed.batches (
    -- Identity
    batch_id          TEXT PRIMARY KEY,
    
    -- Classification
    batch_state       TEXT NOT NULL,          -- 'MD', 'TX', 'NC'
    batch_date        DATE NOT NULL,          -- Date of batch
    
    -- Location
    location          TEXT,                   -- 'JUNO', 'CERES', 'NCSU'
    lts_root          TEXT,                   -- LTS root identifier
    root_path         TEXT,                   -- Full path to batch root
    
    -- Processing status
    processing_status TEXT CHECK (processing_status IN (
        'pending',         -- Not yet processed
        'in_progress',     -- Currently processing
        'completed',       -- All stages complete
        'partial',         -- Some stages complete
        'failed'          -- Processing failed
    )),
    
    -- File counts (synced from globus_file_index)
    file_count_raw        INTEGER,            -- Number of RAW files
    file_count_jpg        INTEGER,            -- Number of JPG files
    file_count_metadata   INTEGER,            -- Number of metadata JSON files
    file_count_cutout     INTEGER,            -- Number of cutout files
    total_bytes           BIGINT,             -- Total size in bytes
    
    -- Pipeline completion flags
    raw_to_jpg_complete          BOOLEAN DEFAULT FALSE,
    jpg_to_metadata_complete     BOOLEAN DEFAULT FALSE,
    metadata_to_cutouts_complete BOOLEAN DEFAULT FALSE,
    
    -- Timing
    first_seen_at           TIMESTAMPTZ,      -- When batch first appeared
    processing_started_at   TIMESTAMPTZ,      -- When processing started
    processing_completed_at TIMESTAMPTZ,      -- When processing completed
    
    -- Metadata
    metadata JSONB,                           -- Free-form batch metadata
    notes    TEXT,                            -- Human-readable notes
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Indexes

```sql
CREATE INDEX idx_batches_date ON processed.batches(batch_date DESC);
CREATE INDEX idx_batches_status ON processed.batches(processing_status);
CREATE INDEX idx_batches_location ON processed.batches(location);
CREATE INDEX idx_batches_state ON processed.batches(batch_state);
CREATE INDEX idx_batches_incomplete ON processed.batches(batch_id) 
    WHERE processing_status != 'completed';
```

### Key Columns Explained

| Column | Example | Description |
|--------|---------|-------------|
| `batch_id` | `'MD_2025-09-09'` | Primary key, unique batch identifier |
| `processing_status` | `'in_progress'` | Overall batch status |
| `raw_to_jpg_complete` | `FALSE` | Whether RAW→JPG stage is done |
| `file_count_raw` | `150` | Number of RAW files (from globus_file_index) |

---

## processed.images

**Purpose:** Image-level metadata including EXIF data, bounding boxes, and processing status. One row per image.

**Use Cases:**
- Store EXIF metadata (camera, GPS, exposure)
- Track processing status per image
- Store bounding box detections
- Link images to output files (JPG, metadata, cutouts)

### Schema

```sql
CREATE TABLE processed.images (
    -- Identity
    image_id          TEXT PRIMARY KEY,      -- Base filename: 'MD_1683434234'
    batch_id          TEXT NOT NULL REFERENCES processed.batches(batch_id) ON DELETE CASCADE,
    
    -- File information
    file_name         TEXT NOT NULL,         -- Original filename: 'MD_1683434234.raw'
    file_ext          TEXT,                  -- Extension: 'RAW', 'jpg'
    file_path         TEXT,                  -- Relative path from batch root
    file_size_bytes   BIGINT,                -- File size
    
    -- Processing status
    processing_status TEXT CHECK (processing_status IN (
        'pending',             -- Not yet processed
        'raw_to_dng',         -- RAW converted to DNG
        'dng_to_jpg',         -- DNG developed to JPG
        'metadata_extracted',  -- Metadata extracted
        'cutouts_generated',  -- Cutouts generated
        'completed',          -- All processing complete
        'failed'              -- Processing failed
    )),
    
    -- EXIF data (extracted from RAW/DNG)
    exif_data         JSONB,                 -- Full EXIF JSON
    camera_make       TEXT,                  -- 'SVS'
    camera_model      TEXT,                  -- 'HR-5000'
    capture_datetime  TIMESTAMPTZ,           -- When photo was taken
    exposure_time     TEXT,                  -- '1/250'
    f_number          TEXT,                  -- 'f/5.6'
    iso_speed         INTEGER,               -- 200
    focal_length      TEXT,                  -- '16mm'
    gps_latitude      NUMERIC,               -- 38.9072
    gps_longitude     NUMERIC,               -- -77.0369
    
    -- Image dimensions
    width             INTEGER,               -- 13376
    height            INTEGER,               -- 9528
    
    -- Bounding boxes (from object detection)
    bounding_boxes    JSONB,                 -- Array of {x, y, width, height, class, confidence}
    detection_count   INTEGER,               -- Number of detections
    
    -- Output files
    jpg_path          TEXT,                  -- Path to developed JPG
    metadata_path     TEXT,                  -- Path to metadata JSON
    cutout_paths      TEXT[],                -- Array of cutout file paths
    
    -- Metadata
    metadata          JSONB,                 -- Free-form metadata
    notes             TEXT,                  -- Human-readable notes
    
    -- Timestamps
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Indexes

```sql
-- Query by batch (most common)
CREATE INDEX idx_images_batch ON processed.images (batch_id, created_at DESC);

-- Query by status
CREATE INDEX idx_images_status ON processed.images (processing_status, updated_at DESC);

-- Query by batch and status combined
CREATE INDEX idx_images_batch_status ON processed.images (batch_id, processing_status);

-- Find pending images
CREATE INDEX idx_images_pending ON processed.images (batch_id, processing_status) 
    WHERE processing_status IN ('pending', 'failed');

-- Query by camera
CREATE INDEX idx_images_camera ON processed.images (camera_make, camera_model) 
    WHERE camera_make IS NOT NULL;

-- Query by capture date
CREATE INDEX idx_images_capture_date ON processed.images (capture_datetime DESC) 
    WHERE capture_datetime IS NOT NULL;

-- Images with detections
CREATE INDEX idx_images_detections ON processed.images (batch_id, detection_count DESC) 
    WHERE detection_count > 0;

-- Spatial queries
CREATE INDEX idx_images_gps ON processed.images (gps_latitude, gps_longitude) 
    WHERE gps_latitude IS NOT NULL AND gps_longitude IS NOT NULL;

-- JSONB indexes
CREATE INDEX idx_images_exif ON processed.images USING gin(exif_data);
CREATE INDEX idx_images_bboxes ON processed.images USING gin(bounding_boxes);
CREATE INDEX idx_images_metadata ON processed.images USING gin(metadata);
```

### Key Columns Explained

| Column | Example | Description |
|--------|---------|-------------|
| `image_id` | `'MD_1683434234'` | Base filename without extension |
| `bounding_boxes` | `[{"x":100,"y":200,"class":"plant"}]` | JSON array of detections |
| `detection_count` | `5` | Number of objects detected |
| `exif_data` | `{"Make":"SVS",...}` | Full EXIF metadata as JSON |

---

## processed.stage_status

**Purpose:** Track execution status of pipeline stages to prevent duplicate work and enable monitoring.

**Use Cases:**
- Prevent multiple workers from processing same batch
- Track which worker/job is processing each stage
- Find stuck jobs
- Monitor stage completion

### Schema

```sql
CREATE TABLE processed.stage_status (
    -- Primary key
    batch_id          TEXT NOT NULL,
    stage             TEXT NOT NULL,
    
    -- Execution tracking
    status            TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    job_id            TEXT,                   -- Identifier of job/worker
    hostname          TEXT,                   -- Host where processing happened
    
    -- Timing
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    duration_seconds  NUMERIC,                -- Computed on completion
    
    -- Results
    success           BOOLEAN,                -- TRUE on success, FALSE on failure
    files_processed   INTEGER,                -- Number of files processed
    files_failed      INTEGER,                -- Number of files that failed
    error_message     TEXT,                   -- Error details if failed
    
    -- Metadata
    metadata          JSONB,                  -- Free-form data (config, parameters, etc.)
    
    -- Timestamps
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (batch_id, stage)
);
```

### Indexes

```sql
CREATE INDEX idx_stage_status_status ON processed.stage_status(status);
CREATE INDEX idx_stage_status_stage ON processed.stage_status(stage);
CREATE INDEX idx_stage_status_job ON processed.stage_status(job_id);
CREATE INDEX idx_stage_status_started ON processed.stage_status(started_at DESC);
```

### Valid Stages

- `'raw_to_jpg'` - RAW → DNG → JPG conversion
- `'jpg_to_metadata'` - Metadata extraction
- `'metadata_to_cutouts'` - Cutout generation

---

## processed.events

**Purpose:** Log all operations, queries, and errors for debugging and auditing.

**Use Cases:**
- Audit trail of all operations
- Debug processing failures
- Track query performance
- Monitor system health

### Schema

```sql
CREATE TABLE processed.events (
    -- Identity
    event_id          BIGSERIAL PRIMARY KEY,
    
    -- Classification
    event_type        TEXT NOT NULL,         -- 'stage.started', 'gap.query', 'error.connection'
    severity          TEXT NOT NULL CHECK (severity IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    
    -- Context
    batch_id          TEXT,                  -- Related batch (if applicable)
    stage             TEXT,                  -- Related stage (if applicable)
    job_id            TEXT,                  -- Related job/worker
    
    -- Content
    message           TEXT NOT NULL,         -- Human-readable message
    metadata          JSONB,                 -- Structured data
    
    -- Source
    hostname          TEXT,                  -- Host where event occurred
    username          TEXT,                  -- User who triggered event
    
    -- Timestamp
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Indexes

```sql
CREATE INDEX idx_events_type ON processed.events(event_type);
CREATE INDEX idx_events_severity ON processed.events(severity);
CREATE INDEX idx_events_batch ON processed.events(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_events_created ON processed.events(created_at DESC);
CREATE INDEX idx_events_type_created ON processed.events(event_type, created_at DESC);
```

---

# Logs Schema

## logs.image_processing_events

**Purpose:** Detailed file-level processing events (similar to processed.events but focused on individual file processing).

**Use Cases:**
- Track each file's processing history
- Debug individual file failures
- Audit trail per file
- Performance analysis per file

### Schema

```sql
CREATE TABLE logs.image_processing_events (
    event_id          BIGSERIAL PRIMARY KEY,
    batch_id          TEXT NOT NULL,
    file_name         TEXT NOT NULL,
    stage             TEXT NOT NULL,
    status            TEXT NOT NULL,         -- 'success', 'failed', 'skipped'
    
    input_path        TEXT,
    output_path       TEXT,
    processing_time_sec NUMERIC,
    error_message     TEXT,
    
    job_id            TEXT,
    node_name         TEXT,
    log_file_path     TEXT,
    
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Indexes

```sql
CREATE INDEX idx_processing_events_batch ON logs.image_processing_events(batch_id);
CREATE INDEX idx_processing_events_status ON logs.image_processing_events(status);
CREATE INDEX idx_processing_events_stage ON logs.image_processing_events(stage);
```

---

## logs.juno_transfers

**Purpose:** Track file transfer operations to JUNO long-term storage.

**Use Cases:**
- Monitor transfer status
- Track Globus task IDs
- Audit transfer history
- Debug transfer failures

### Schema

```sql
CREATE TABLE logs.juno_transfers (
    id                BIGSERIAL PRIMARY KEY,
    batch_id          TEXT NOT NULL,
    endpoint          TEXT NOT NULL,         -- Globus endpoint
    location          TEXT,                  -- Source location
    lts_root          TEXT,                  
    root_path         TEXT NOT NULL,
    data_state        TEXT NOT NULL,         -- What's being transferred
    source_dir        TEXT NOT NULL,
    destination_dir   TEXT NOT NULL,
    transfer_time     TIMESTAMPTZ DEFAULT NOW(),
    status            TEXT,                  -- 'submitted', 'dry_run', 'failed', 'completed'
    error_message     TEXT
);
```

### Indexes

```sql
CREATE INDEX idx_juno_transfers_batch ON logs.juno_transfers(batch_id);
CREATE INDEX idx_juno_transfers_status ON logs.juno_transfers(status);
```

---

# Report Schema (Views)

## Pipeline Gap Views

**Purpose:** Identify batches and files needing processing by detecting "gaps" - missing output files that should exist based on inputs.

### report.files_needing_raw_to_jpg

```sql
-- RAW files without corresponding JPG files
CREATE VIEW report.files_needing_raw_to_jpg AS
SELECT 
    raw.batch_id,
    raw.file_name,
    raw.root_path,
    raw.location,
    raw.batch_date
FROM source.globus_file_index raw
LEFT JOIN source.globus_file_index jpg
    ON raw.batch_id = jpg.batch_id
    AND REPLACE(raw.file_name, '.raw', '.jpg') = jpg.file_name
    AND jpg.data_state = 'developed_jpg'
WHERE raw.data_state = 'upload_raw'
    AND raw.file_ext = 'RAW'
    AND jpg.file_id IS NULL;  -- JPG doesn't exist
```

### report.batches_needing_raw_to_jpg

```sql
-- Batches with RAW files needing JPG conversion
CREATE VIEW report.batches_needing_raw_to_jpg AS
SELECT 
    batch_id,
    batch_date,
    location,
    COUNT(*) as files_needing_processing,
    SUM(size_bytes) as total_bytes_to_process
FROM report.files_needing_raw_to_jpg
GROUP BY batch_id, batch_date, location
ORDER BY batch_date DESC;
```

### report.batch_pipeline_status

```sql
-- Complete pipeline status for each batch
CREATE VIEW report.batch_pipeline_status AS
SELECT 
    b.batch_id,
    b.batch_date,
    
    -- File counts
    COUNT(CASE WHEN g.data_state = 'upload_raw' THEN 1 END) as raw_count,
    COUNT(CASE WHEN g.data_state = 'developed_jpg' AND g.file_ext = 'jpg' THEN 1 END) as jpg_count,
    COUNT(CASE WHEN g.data_state = 'developed_jpg' AND g.file_ext = 'json' THEN 1 END) as metadata_count,
    COUNT(CASE WHEN g.data_state = 'cutouts' THEN 1 END) as cutout_count,
    
    -- Gaps
    COUNT(CASE WHEN g.data_state = 'upload_raw' THEN 1 END) - 
    COUNT(CASE WHEN g.data_state = 'developed_jpg' AND g.file_ext = 'jpg' THEN 1 END) as raw_to_jpg_gap,
    
    -- Completion flags
    (COUNT(CASE WHEN g.data_state = 'upload_raw' THEN 1 END) = 
     COUNT(CASE WHEN g.data_state = 'developed_jpg' AND g.file_ext = 'jpg' THEN 1 END)) as raw_to_jpg_complete
     
FROM processed.batches b
LEFT JOIN source.globus_file_index g ON b.batch_id = g.batch_id
GROUP BY b.batch_id, b.batch_date;
```

### report.pipeline_gap_summary

```sql
-- Aggregate statistics across all stages
CREATE VIEW report.pipeline_gap_summary AS
SELECT 
    'raw_to_jpg' as stage,
    COUNT(DISTINCT batch_id) as batches_with_gaps,
    COUNT(*) as total_files_with_gaps,
    SUM(size_bytes)/(1024^3) as total_gb_to_process
FROM report.files_needing_raw_to_jpg
UNION ALL
SELECT 
    'jpg_to_metadata' as stage,
    COUNT(DISTINCT batch_id),
    COUNT(*),
    SUM(size_bytes)/(1024^3)
FROM report.files_needing_jpg_to_metadata
UNION ALL
SELECT 
    'metadata_to_cutouts' as stage,
    COUNT(DISTINCT batch_id),
    COUNT(*),
    SUM(size_bytes)/(1024^3)
FROM report.files_needing_metadata_to_cutouts;
```

---

## Processed Schema Views

### processed.batch_summary

```sql
-- Comprehensive batch statistics
CREATE VIEW processed.batch_summary AS
SELECT 
    b.*,
    COUNT(i.image_id) as total_images,
    COUNT(CASE WHEN i.processing_status = 'completed' THEN 1 END) as completed_images,
    COUNT(CASE WHEN i.detection_count > 0 THEN 1 END) as images_with_detections,
    SUM(i.detection_count) as total_detections
FROM processed.batches b
LEFT JOIN processed.images i ON b.batch_id = i.batch_id
GROUP BY b.batch_id;
```

### processed.images_with_detections

```sql
-- Images that have object detections
CREATE VIEW processed.images_with_detections AS
SELECT *
FROM processed.images
WHERE detection_count > 0
ORDER BY detection_count DESC;
```

### processed.pending_images_by_batch

```sql
-- Count of unprocessed images per batch
CREATE VIEW processed.pending_images_by_batch AS
SELECT 
    batch_id,
    COUNT(*) as pending_count
FROM processed.images
WHERE processing_status = 'pending'
GROUP BY batch_id;
```

### processed.failed_images_by_batch

```sql
-- Count of failed images per batch
CREATE VIEW processed.failed_images_by_batch AS
SELECT 
    batch_id,
    COUNT(*) as failed_count
FROM processed.images
WHERE processing_status = 'failed'
GROUP BY batch_id;
```

### processed.camera_stats

```sql
-- Usage statistics by camera
CREATE VIEW processed.camera_stats AS
SELECT 
    camera_make,
    camera_model,
    COUNT(*) as image_count,
    COUNT(DISTINCT batch_id) as batch_count,
    MIN(capture_datetime) as first_image,
    MAX(capture_datetime) as last_image
FROM processed.images
WHERE camera_make IS NOT NULL
GROUP BY camera_make, camera_model
ORDER BY image_count DESC;
```

---

# Core Schema (Future)

## Planned Tables

### core.species_info

```sql
-- Plant species reference data
CREATE TABLE core.species_info (
    class_id          TEXT PRIMARY KEY,
    common_name       TEXT,
    genus             TEXT,
    species           TEXT,
    plant_type        TEXT,
    notes             TEXT
);
```

### core.marker_locations

```sql
-- Field marker/path information
CREATE TABLE core.marker_locations (
    path              TEXT PRIMARY KEY,
    state             TEXT,
    season            TEXT,
    start_date        DATE,
    end_date          DATE
);
```

---

# Table Relationships

```
source.globus_file_index
        ↓ (file counts aggregated)
processed.batches ←──────┐
        ↓                │
processed.images         │
        ↓                │
processed.stage_status ──┘
        ↓
logs.image_processing_events

report.* views
        ↑
    (queries)
        ↓
source.globus_file_index + processed.batches
```

---

# Common Queries

## Work Discovery

```sql
-- Find next batch to process
SELECT * FROM report.batches_needing_raw_to_jpg
ORDER BY batch_date DESC
LIMIT 1;

-- Get files to process in a batch
SELECT * FROM report.files_needing_raw_to_jpg
WHERE batch_id = 'MD_2025-09-09';

-- Overall pipeline health
SELECT * FROM report.pipeline_gap_summary;
```

## Monitoring

```sql
-- Check in-progress jobs
SELECT * FROM processed.stage_status
WHERE status = 'in_progress';

-- Recent failures
SELECT * FROM logs.image_processing_events
WHERE status = 'failed'
ORDER BY created_at DESC
LIMIT 20;

-- Batch completion status
SELECT * FROM processed.batch_summary
WHERE raw_to_jpg_complete = TRUE;
```

## Analytics

```sql
-- Storage by location
SELECT location, 
       SUM(size_bytes)/(1024^3) as size_gb,
       COUNT(*) as file_count
FROM source.globus_file_index
GROUP BY location;

-- Processing throughput
SELECT DATE(created_at) as date,
       COUNT(*) as images_processed
FROM processed.images
WHERE processing_status = 'completed'
GROUP BY DATE(created_at)
ORDER BY date DESC;

-- Camera usage
SELECT * FROM processed.camera_stats
ORDER BY image_count DESC;
```

---

# Summary

## Total Tables: 7

| Schema | Table | Type | Rows (Est) |
|--------|-------|------|------------|
| source | globus_file_index | Base | 100,000+ |
| processed | batches | Base | 500+ |
| processed | images | Base | 75,000+ |
| processed | stage_status | Base | 1,500+ |
| processed | events | Base | 10,000+ |
| logs | image_processing_events | Base | 50,000+ |
| logs | juno_transfers | Base | 500+ |

## Total Views: 20+

- **report schema:** 8 views (pipeline gaps)
- **processed schema:** 12+ views (batch summaries, statistics)

## Key Design Principles

1. **Pipeline Gaps = Source of Truth**
   - Views detect missing files, not status flags
   - Self-correcting architecture
   
2. **Comprehensive Indexing**
   - 50+ indexes for fast queries
   - JSONB indexes for flexible metadata
   
3. **Audit Trail**
   - Every operation logged
   - File-level event tracking
   
4. **Normalized Design**
   - Clear relationships with foreign keys
   - CASCADE deletes maintain integrity
   
5. **Flexible Metadata**
   - JSONB fields for extensibility
   - No schema changes needed for new data

---

## Navigation Tips

- **Starting a new batch?** Check `report.batches_needing_raw_to_jpg`
- **Need file details?** Query `report.files_needing_raw_to_jpg`
- **Monitoring progress?** Use `processed.stage_status`
- **Debugging failures?** Check `logs.image_processing_events`
- **Analytics?** Start with `processed.batch_summary`