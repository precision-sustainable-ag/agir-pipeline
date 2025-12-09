-- ============================================================
-- processed.batch_processing_status
-- Tracks the processing state of each batch through the pipeline
-- ============================================================

CREATE SCHEMA IF NOT EXISTS processed;

CREATE TABLE IF NOT EXISTS processed.batch_processing_status (
    batch_id              TEXT PRIMARY KEY,
    batch_state           TEXT,              -- 'MD','TX','NC'
    batch_date            DATE,
    
    -- File inventory from source.globus_file_index
    raw_file_count        INTEGER DEFAULT 0,
    dng_file_count        INTEGER DEFAULT 0,
    jpg_file_count        INTEGER DEFAULT 0,
    json_file_count       INTEGER DEFAULT 0,
    
    -- Processing pipeline status
    raw_to_dng_status     TEXT DEFAULT 'pending',  -- pending, in_progress, completed, failed
    dng_to_jpg_status     TEXT DEFAULT 'pending',
    metadata_status       TEXT DEFAULT 'pending',
    
    -- Processing metadata
    raw_to_dng_started    TIMESTAMPTZ,
    raw_to_dng_completed  TIMESTAMPTZ,
    raw_to_dng_job_id     TEXT,
    
    dng_to_jpg_started    TIMESTAMPTZ,
    dng_to_jpg_completed  TIMESTAMPTZ,
    dng_to_jpg_job_id     TEXT,
    
    -- Error tracking
    last_error            TEXT,
    retry_count           INTEGER DEFAULT 0,
    
    -- Storage locations
    primary_location      TEXT,              -- 'JUNO','NCSU','CERES'
    primary_lts_root      TEXT,
    
    -- Timestamps
    created_at            TIMESTAMPTZ DEFAULT now(),
    updated_at            TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_batch_processing_raw_to_dng 
    ON processed.batch_processing_status(raw_to_dng_status);
    
CREATE INDEX IF NOT EXISTS idx_batch_processing_dng_to_jpg 
    ON processed.batch_processing_status(dng_to_jpg_status);

-- ============================================================
-- logs.image_processing_events
-- Detailed per-image processing logs
-- ============================================================

CREATE SCHEMA IF NOT EXISTS logs;

CREATE TABLE IF NOT EXISTS logs.image_processing_events (
    event_id              BIGSERIAL PRIMARY KEY,
    batch_id              TEXT NOT NULL,
    file_name             TEXT NOT NULL,
    
    pipeline_stage        TEXT NOT NULL,     -- 'raw_to_dng', 'dng_to_jpg'
    status                TEXT NOT NULL,     -- 'success', 'failed', 'skipped'
    
    input_path            TEXT,
    output_path           TEXT,
    
    processing_time_sec   NUMERIC,
    error_message         TEXT,
    
    job_id                TEXT,
    node_name             TEXT,
    
    created_at            TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_processing_events_batch 
    ON logs.image_processing_events(batch_id);
    
CREATE INDEX IF NOT EXISTS idx_processing_events_status 
    ON logs.image_processing_events(status);

-- ============================================================
-- Views for reporting
-- ============================================================

-- Batches ready for RAW→DNG processing
CREATE OR REPLACE VIEW processed.v_batches_ready_for_dng AS
SELECT 
    b.batch_id,
    b.batch_state,
    b.batch_date,
    b.raw_file_count,
    b.primary_location,
    b.primary_lts_root
FROM processed.batch_processing_status b
WHERE 
    b.raw_file_count > 0
    AND b.raw_to_dng_status IN ('pending', 'failed')
    AND b.retry_count < 3
ORDER BY b.batch_date DESC;

-- Batches ready for DNG→JPG processing
CREATE OR REPLACE VIEW processed.v_batches_ready_for_jpg AS
SELECT 
    b.batch_id,
    b.batch_state,
    b.batch_date,
    b.dng_file_count,
    b.primary_location,
    b.primary_lts_root
FROM processed.batch_processing_status b
WHERE 
    b.dng_file_count > 0
    AND b.raw_to_dng_status = 'completed'
    AND b.dng_to_jpg_status IN ('pending', 'failed')
    AND b.retry_count < 3
ORDER BY b.batch_date DESC;

-- Processing summary
CREATE OR REPLACE VIEW processed.v_batch_processing_summary AS
SELECT 
    COUNT(*) as total_batches,
    SUM(raw_file_count) as total_raw_files,
    SUM(dng_file_count) as total_dng_files,
    SUM(jpg_file_count) as total_jpg_files,
    SUM(CASE WHEN raw_to_dng_status = 'completed' THEN 1 ELSE 0 END) as dng_completed,
    SUM(CASE WHEN dng_to_jpg_status = 'completed' THEN 1 ELSE 0 END) as jpg_completed,
    SUM(CASE WHEN raw_to_dng_status = 'failed' THEN 1 ELSE 0 END) as dng_failed,
    SUM(CASE WHEN dng_to_jpg_status = 'failed' THEN 1 ELSE 0 END) as jpg_failed
FROM processed.batch_processing_status;
