
/* ============================================================
 * processed.images and processed.batches
 *
 * Metadata storage for images and batches.
 *
 * Design principles:
 * - Store EXIF data, bounding boxes, and processing metadata
 * - Track processing status per image
 * - Aggregate batch-level statistics
 * - Support efficient queries by batch, state, date
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS processed;

-- Drop tables if exist (for clean reinstall)
DROP TABLE IF EXISTS processed.images CASCADE;
DROP TABLE IF EXISTS processed.batches CASCADE;

-- ============================================================
-- BATCHES TABLE
-- ============================================================

CREATE TABLE processed.batches (
    -- Identity
    batch_id TEXT PRIMARY KEY,
    
    -- Classification
    batch_state TEXT NOT NULL,              -- e.g., 'MD', 'TX', 'NC'
    batch_date DATE NOT NULL,               -- Date of batch
    
    -- site
    site TEXT,                          -- e.g., 'JUNO', 'CERES', 'NCSU'
    storage_root TEXT,                          -- LTS root identifier
    storage_domain TEXT,                        -- Storage domain (e.g., 'juno#agir-data')
    namespace TEXT,                            -- Namespace within storage domain
    
    -- Processing status
    processing_status TEXT CHECK (processing_status IN (
        'pending',          -- Not yet processed
        'in_progress',      -- Currently processing
        'completed',        -- All stages complete
        'partial',          -- Some stages complete
        'failed'            -- Processing failed
    )),
    
    -- Statistics
    file_count_raw INTEGER,                 -- Number of RAW files
    file_count_jpg INTEGER,                 -- Number of JPG files
    file_count_metadata INTEGER,            -- Number of metadata JSON files
    file_count_cutout INTEGER,              -- Number of cutout files
    total_bytes BIGINT,                     -- Total size in bytes
    
    -- Processing metrics
    raw_to_jpg_complete BOOLEAN DEFAULT FALSE,
    jpg_to_metadata_complete BOOLEAN DEFAULT FALSE,
    metadata_to_cutouts_complete BOOLEAN DEFAULT FALSE,
    
    -- Timing
    first_seen_at TIMESTAMPTZ,              -- When batch first appeared
    processing_started_at TIMESTAMPTZ,      -- When processing started
    processing_completed_at TIMESTAMPTZ,    -- When processing completed
    
    -- Metadata
    metadata JSONB,                         -- Free-form batch metadata
    notes TEXT,                             -- Human-readable notes
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- IMAGES TABLE
-- ============================================================

CREATE TABLE processed.images (
    -- Identity
    image_id TEXT PRIMARY KEY,              -- Unique image identifier (base_name)
    batch_id TEXT NOT NULL REFERENCES processed.batches(batch_id) ON DELETE CASCADE,
    
    -- File information
    file_name TEXT NOT NULL,                -- Original filename (e.g., 'MD_1234.raw')
    file_ext TEXT,                          -- File extension
    file_path TEXT,                         -- Relative path from batch root
    file_size_bytes BIGINT,                 -- File size
    
    -- Processing status
    processing_status TEXT CHECK (processing_status IN (
        'pending',          -- Not yet processed
        'raw_to_dng',       -- RAW converted to DNG
        'dng_to_jpg',       -- DNG developed to JPG
        'metadata_extracted', -- Metadata extracted
        'cutouts_generated', -- Cutouts generated
        'completed',        -- All processing complete
        'failed'            -- Processing failed
    )),
    
    -- EXIF data (extracted from RAW/DNG)
    exif_data JSONB,                        -- Full EXIF data
    camera_make TEXT,                       -- Camera manufacturer
    camera_model TEXT,                      -- Camera model
    capture_datetime TIMESTAMPTZ,           -- When photo was taken
    exposure_time TEXT,                     -- Shutter speed
    f_number TEXT,                          -- Aperture
    iso_speed INTEGER,                      -- ISO setting
    focal_length TEXT,                      -- Focal length
    gps_latitude NUMERIC,                   -- GPS latitude
    gps_longitude NUMERIC,                  -- GPS longitude
    
    -- Image properties
    width INTEGER,                          -- Image width in pixels
    height INTEGER,                         -- Image height in pixels
    orientation INTEGER,                    -- EXIF orientation
    
    -- Bounding boxes (from object detection)
    bounding_boxes JSONB,                   -- Array of bounding boxes
    detection_count INTEGER,                -- Number of detections
    
    -- Processing results
    jpg_path TEXT,                          -- Path to developed JPG
    metadata_path TEXT,                     -- Path to metadata JSON
    cutout_paths TEXT[],                    -- Paths to cutout images
    
    -- Quality metrics
    quality_score NUMERIC,                  -- Image quality score (0-1)
    blur_score NUMERIC,                     -- Blur detection score
    exposure_score NUMERIC,                 -- Exposure quality score
    
    -- Timing
    processed_at TIMESTAMPTZ,               -- When processing completed
    
    -- Metadata
    metadata JSONB,                         -- Free-form image metadata
    notes TEXT,                             -- Human-readable notes
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- INDEXES - BATCHES
-- ============================================================

-- Query by state and date
CREATE INDEX idx_batches_state_date 
ON processed.batches (batch_state, batch_date DESC);

-- Query by processing status
CREATE INDEX idx_batches_status 
ON processed.batches (processing_status, updated_at DESC);

-- Query by site
CREATE INDEX idx_batches_site 
ON processed.batches (site, batch_date DESC) 
WHERE site IS NOT NULL;

-- Query by completion flags
CREATE INDEX idx_batches_completion 
ON processed.batches (raw_to_jpg_complete, jpg_to_metadata_complete, metadata_to_cutouts_complete);

-- Query recent batches
CREATE INDEX idx_batches_created 
ON processed.batches (created_at DESC);

-- JSONB metadata queries
CREATE INDEX idx_batches_metadata 
ON processed.batches USING gin(metadata);

-- ============================================================
-- INDEXES - IMAGES
-- ============================================================

-- Query by batch (most common)
CREATE INDEX idx_images_batch 
ON processed.images (batch_id, created_at DESC);

-- Query by processing status
CREATE INDEX idx_images_status 
ON processed.images (processing_status, updated_at DESC);

-- Query by batch and status (common combo)
CREATE INDEX idx_images_batch_status 
ON processed.images (batch_id, processing_status);

-- Find images needing processing
CREATE INDEX idx_images_pending 
ON processed.images (batch_id, processing_status) 
WHERE processing_status IN ('pending', 'failed');

-- Query by camera
CREATE INDEX idx_images_camera 
ON processed.images (camera_make, camera_model) 
WHERE camera_make IS NOT NULL;

-- Query by capture date
CREATE INDEX idx_images_capture_date 
ON processed.images (capture_datetime DESC) 
WHERE capture_datetime IS NOT NULL;

-- Query images with detections
CREATE INDEX idx_images_detections 
ON processed.images (batch_id, detection_count DESC) 
WHERE detection_count > 0;

-- Spatial queries (if GPS data present)
CREATE INDEX idx_images_gps 
ON processed.images (gps_latitude, gps_longitude) 
WHERE gps_latitude IS NOT NULL AND gps_longitude IS NOT NULL;

-- JSONB indexes
CREATE INDEX idx_images_exif 
ON processed.images USING gin(exif_data);

CREATE INDEX idx_images_bboxes 
ON processed.images USING gin(bounding_boxes);

CREATE INDEX idx_images_metadata 
ON processed.images USING gin(metadata);

-- ============================================================
-- TRIGGERS
-- ============================================================

-- Auto-update updated_at timestamp for batches
CREATE OR REPLACE FUNCTION update_batches_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_batches_updated_at
    BEFORE UPDATE ON processed.batches
    FOR EACH ROW
    EXECUTE FUNCTION update_batches_timestamp();

-- Auto-update updated_at timestamp for images
CREATE OR REPLACE FUNCTION update_images_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_images_updated_at
    BEFORE UPDATE ON processed.images
    FOR EACH ROW
    EXECUTE FUNCTION update_images_timestamp();

-- ============================================================
-- HELPER VIEWS
-- ============================================================

-- Batch summary with file counts
CREATE VIEW processed.batch_summary AS
SELECT
    b.batch_id,
    b.batch_state,
    b.batch_date,
    b.site,
    b.processing_status,
    b.file_count_raw,
    b.file_count_jpg,
    b.file_count_metadata,
    b.file_count_cutout,
    b.raw_to_jpg_complete,
    b.jpg_to_metadata_complete,
    b.metadata_to_cutouts_complete,
    COUNT(i.image_id) AS registered_images,
    COUNT(i.image_id) FILTER (WHERE i.processing_status = 'completed') AS completed_images,
    COUNT(i.image_id) FILTER (WHERE i.processing_status = 'failed') AS failed_images,
    SUM(COALESCE(i.detection_count, 0)) AS total_detections,
    b.created_at,
    b.updated_at
FROM processed.batches b
LEFT JOIN processed.images i ON b.batch_id = i.batch_id
GROUP BY b.batch_id, b.batch_state, b.batch_date, b.site, 
         b.processing_status, b.file_count_raw, b.file_count_jpg,
         b.file_count_metadata, b.file_count_cutout,
         b.raw_to_jpg_complete, b.jpg_to_metadata_complete,
         b.metadata_to_cutouts_complete, b.created_at, b.updated_at
ORDER BY b.batch_date DESC, b.batch_id;

-- Images with detections
CREATE VIEW processed.images_with_detections AS
SELECT
    i.image_id,
    i.batch_id,
    i.file_name,
    i.detection_count,
    i.bounding_boxes,
    i.capture_datetime,
    i.camera_make,
    i.camera_model,
    i.gps_latitude,
    i.gps_longitude,
    i.jpg_path,
    i.cutout_paths,
    i.processing_status,
    i.created_at
FROM processed.images i
WHERE i.detection_count > 0
ORDER BY i.detection_count DESC, i.created_at DESC;

-- Pending images by batch
CREATE VIEW processed.pending_images_by_batch AS
SELECT
    i.batch_id,
    b.batch_state,
    b.batch_date,
    COUNT(*) AS pending_count,
    MIN(i.created_at) AS oldest_pending,
    MAX(i.created_at) AS newest_pending
FROM processed.images i
JOIN processed.batches b ON i.batch_id = b.batch_id
WHERE i.processing_status = 'pending'
GROUP BY i.batch_id, b.batch_state, b.batch_date
ORDER BY pending_count DESC;

-- Failed images by batch
CREATE VIEW processed.failed_images_by_batch AS
SELECT
    i.batch_id,
    b.batch_state,
    b.batch_date,
    COUNT(*) AS failed_count,
    ARRAY_AGG(i.image_id ORDER BY i.updated_at DESC) AS failed_image_ids
FROM processed.images i
JOIN processed.batches b ON i.batch_id = b.batch_id
WHERE i.processing_status = 'failed'
GROUP BY i.batch_id, b.batch_state, b.batch_date
ORDER BY failed_count DESC;

-- Camera usage statistics
CREATE VIEW processed.camera_stats AS
SELECT
    camera_make,
    camera_model,
    COUNT(*) AS image_count,
    COUNT(DISTINCT batch_id) AS batch_count,
    AVG(COALESCE(detection_count, 0)) AS avg_detections_per_image,
    MIN(capture_datetime) AS first_capture,
    MAX(capture_datetime) AS last_capture
FROM processed.images
WHERE camera_make IS NOT NULL
GROUP BY camera_make, camera_model
ORDER BY image_count DESC;

-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON TABLE processed.batches IS 
'Batch-level metadata and processing status';

COMMENT ON TABLE processed.images IS 
'Image-level metadata including EXIF, bounding boxes, and processing status';

COMMENT ON COLUMN processed.images.bounding_boxes IS 
'Array of detection bounding boxes in format: [{x, y, width, height, class, confidence}, ...]';

COMMENT ON COLUMN processed.images.exif_data IS 
'Complete EXIF data extracted from RAW/DNG file';

COMMENT ON VIEW processed.batch_summary IS 
'Comprehensive batch summary including file counts and processing status';

COMMENT ON VIEW processed.images_with_detections IS 
'Images that have object detections (bounding boxes)';

COMMENT ON VIEW processed.pending_images_by_batch IS 
'Count of pending (unprocessed) images per batch';

COMMENT ON VIEW processed.failed_images_by_batch IS 
'Count of failed images per batch with image IDs';