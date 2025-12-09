# Database Schema

[← Back to Index](README.md)

PostgreSQL database schema reference for AgirDB.

---

## Overview

AgirDB uses PostgreSQL 12+ with the following key tables:
- `batches` - Batch metadata
- `images` - Image metadata
- `stage_status` - Processing stage tracking
- `events` - Event logging
- `transfers` - JUNO transfer operations

---

## Tables

### batches

Stores batch-level metadata.

```sql
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    collection_date DATE NOT NULL,
    location TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    image_count INTEGER NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_batches_collection_date ON batches(collection_date);
CREATE INDEX idx_batches_location ON batches(location);
CREATE INDEX idx_batches_camera_id ON batches(camera_id);
```

**Columns:**
- `batch_id` (text, PK): Unique batch identifier
- `collection_date` (date): When images were collected
- `location` (text): Collection location/field identifier
- `camera_id` (text): Camera identifier
- `image_count` (int): Expected number of images
- `metadata` (jsonb): Free-form metadata
- `created_at` (timestamp): Record creation time
- `updated_at` (timestamp): Last update time

---

### images

Stores individual image metadata.

```sql
CREATE TABLE images (
    image_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    camera_id TEXT NOT NULL,
    capture_time TIMESTAMP NOT NULL,
    raw_path TEXT NOT NULL,
    dng_path TEXT,
    jpg_path TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_images_batch_id ON images(batch_id);
CREATE INDEX idx_images_camera_id ON images(camera_id);
CREATE INDEX idx_images_capture_time ON images(capture_time);
CREATE INDEX idx_images_dng_path ON images(dng_path) WHERE dng_path IS NULL;
CREATE INDEX idx_images_jpg_path ON images(jpg_path) WHERE jpg_path IS NULL;
```

**Columns:**
- `image_id` (text, PK): Unique image identifier
- `batch_id` (text, FK): Associated batch
- `camera_id` (text): Camera identifier
- `capture_time` (timestamp): Image capture time
- `raw_path` (text): Path to RAW file
- `dng_path` (text, nullable): Path to DNG file
- `jpg_path` (text, nullable): Path to JPG file
- `metadata` (jsonb): Free-form metadata
- `created_at` (timestamp): Record creation time
- `updated_at` (timestamp): Last update time

---

### stage_status

Tracks pipeline stage processing status.

```sql
CREATE TABLE stage_status (
    status_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed', 'cancelled')),
    job_id TEXT,
    hostname TEXT,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    files_processed INTEGER,
    error_message TEXT,
    metadata JSONB DEFAULT '{}',
    UNIQUE (batch_id, stage, status) WHERE status = 'in_progress'
);

CREATE INDEX idx_stage_status_batch_stage ON stage_status(batch_id, stage);
CREATE INDEX idx_stage_status_status ON stage_status(status);
CREATE INDEX idx_stage_status_started_at ON stage_status(started_at);
```

**Columns:**
- `status_id` (uuid, PK): Unique status identifier
- `batch_id` (text, FK): Associated batch
- `stage` (text): Stage name (e.g., 'raw_to_jpg')
- `status` (text): Status enum
- `job_id` (text): Job/worker identifier
- `hostname` (text): Processing hostname
- `started_at` (timestamp): Stage start time
- `completed_at` (timestamp, nullable): Stage completion time
- `files_processed` (int, nullable): Number of files processed
- `error_message` (text, nullable): Error description if failed
- `metadata` (jsonb): Free-form metadata

**Constraints:**
- UNIQUE constraint on (batch_id, stage, status) WHERE status = 'in_progress'
  - Prevents multiple workers from processing same batch/stage

---

### events

Logs processing events for auditing.

```sql
CREATE TABLE events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    batch_id TEXT REFERENCES batches(batch_id),
    stage TEXT,
    image_id TEXT REFERENCES images(image_id),
    severity TEXT NOT NULL CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_events_event_type ON events(event_type);
CREATE INDEX idx_events_batch_id ON events(batch_id);
CREATE INDEX idx_events_stage ON events(stage);
CREATE INDEX idx_events_severity ON events(severity);
CREATE INDEX idx_events_created_at ON events(created_at);
```

**Columns:**
- `event_id` (uuid, PK): Unique event identifier
- `event_type` (text): Event type category
- `batch_id` (text, FK, nullable): Associated batch
- `stage` (text, nullable): Associated stage
- `image_id` (text, FK, nullable): Associated image
- `severity` (text): Severity level enum
- `message` (text): Event description
- `metadata` (jsonb): Additional event data
- `created_at` (timestamp): Event timestamp

---

### transfers

Manages JUNO transfer operations.

```sql
CREATE TABLE transfers (
    transfer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    transfer_type TEXT NOT NULL CHECK (transfer_type IN ('upload', 'download', 'move')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'cancelled')),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    globus_task_id TEXT,
    files_transferred INTEGER,
    bytes_transferred BIGINT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX idx_transfers_batch_id ON transfers(batch_id);
CREATE INDEX idx_transfers_status ON transfers(status);
CREATE INDEX idx_transfers_priority ON transfers(priority);
CREATE INDEX idx_transfers_created_at ON transfers(created_at);
```

**Columns:**
- `transfer_id` (uuid, PK): Unique transfer identifier
- `batch_id` (text, FK): Associated batch
- `source_path` (text): Source path
- `destination_path` (text): Destination path
- `transfer_type` (text): Transfer type enum
- `status` (text): Transfer status enum
- `priority` (text): Priority level enum
- `globus_task_id` (text, nullable): Globus task ID
- `files_transferred` (int, nullable): Files count
- `bytes_transferred` (bigint, nullable): Bytes count
- `created_at` (timestamp): Transfer creation time
- `started_at` (timestamp, nullable): Transfer start time
- `completed_at` (timestamp, nullable): Transfer completion time
- `metadata` (jsonb): Additional transfer data

---

## Relationships

```
batches (1) ←→ (N) images
batches (1) ←→ (N) stage_status
batches (1) ←→ (N) events
batches (1) ←→ (N) transfers
images (1) ←→ (N) events
```

**Foreign Keys:**
- `images.batch_id` → `batches.batch_id`
- `stage_status.batch_id` → `batches.batch_id`
- `events.batch_id` → `batches.batch_id` (nullable)
- `events.image_id` → `images.image_id` (nullable)
- `transfers.batch_id` → `batches.batch_id`

---

## Triggers

### updated_at Triggers

Auto-update `updated_at` timestamp on record modification:

```sql
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_batches_updated_at
    BEFORE UPDATE ON batches
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_images_updated_at
    BEFORE UPDATE ON images
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
```

---

## Common Queries

### Find Batches with Gaps

```sql
-- Find batches missing JPG files
SELECT 
    i.batch_id,
    COUNT(*) as total_images,
    COUNT(i.jpg_path) as with_jpg,
    COUNT(*) - COUNT(i.jpg_path) as missing_jpg
FROM images i
GROUP BY i.batch_id
HAVING COUNT(*) > COUNT(i.jpg_path)
ORDER BY missing_jpg DESC;
```

### Find Images Missing Outputs

```sql
-- Find images missing JPG for a batch
SELECT 
    image_id,
    raw_path,
    dng_path,
    jpg_path
FROM images
WHERE batch_id = 'B001'
  AND jpg_path IS NULL;
```

### Check Stage Status

```sql
-- Find in-progress stages
SELECT 
    batch_id,
    stage,
    job_id,
    started_at,
    EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) as duration_seconds
FROM stage_status
WHERE status = 'in_progress'
ORDER BY started_at;
```

### Get Processing History

```sql
-- Get processing history for a batch
SELECT 
    stage,
    status,
    started_at,
    completed_at,
    EXTRACT(EPOCH FROM (completed_at - started_at)) as duration_seconds,
    files_processed
FROM stage_status
WHERE batch_id = 'B001'
ORDER BY started_at;
```

---

## Performance Tuning

### Recommended Indexes

Already included in table definitions above. Key indexes:
- Batch and image lookups by ID
- Stage status by (batch_id, stage)
- Partial indexes on NULL paths for gap analysis
- Event lookups by type, severity, and timestamp

### ANALYZE After Bulk Loads

```sql
-- After bulk inserting images
ANALYZE images;

-- After bulk logging events
ANALYZE events;
```

### Vacuum Strategy

```sql
-- Regular maintenance
VACUUM ANALYZE;

-- For heavily updated tables
VACUUM FULL stage_status;
```

---

## Partitioning (For Large Deployments)

For very large event tables, consider partitioning by timestamp:

```sql
CREATE TABLE events_2025_01 PARTITION OF events
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');

CREATE TABLE events_2025_02 PARTITION OF events
    FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');
```

---

## See Also

- [Image Metadata](image-metadata.md) - Using the images table
- [Batch Metadata](batch-metadata.md) - Using the batches table
- [Stage Status](stage-status.md) - Using the stage_status table

[← Back to Index](README.md)
