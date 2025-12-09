/* ============================================================
 * processed.stage_status.sql
 *
 * Track execution status of pipeline stages to prevent duplicate work
 * and enable monitoring of in-progress jobs.
 *
 * Design principles:
 * - One row per (batch_id, stage) combination
 * - Tracks who's processing, when started, and success/failure
 * - Uses UPSERT for idempotent operations
 * - Includes job_id to identify which worker is processing
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS processed;

-- Drop table if exists (for clean reinstall)
DROP TABLE IF EXISTS processed.stage_status CASCADE;

-- ============================================================
-- STAGE STATUS TABLE
-- ============================================================

CREATE TABLE processed.stage_status (
    -- Primary key
    batch_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    
    -- Execution tracking
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    job_id TEXT,                    -- Identifier of job/worker processing this
    hostname TEXT,                  -- Host where processing is happening
    
    -- Timing
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_seconds NUMERIC,       -- Computed on completion
    
    -- Results
    success BOOLEAN,                -- TRUE on success, FALSE on failure
    files_processed INTEGER,        -- Number of files successfully processed
    files_failed INTEGER,           -- Number of files that failed
    error_message TEXT,             -- Error details if failed
    
    -- Metadata
    metadata JSONB,                 -- Free-form data (config, parameters, etc.)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Constraints
    PRIMARY KEY (batch_id, stage),
    
    -- Ensure completed_at is set when status is completed/failed
    CHECK (
        (status = 'in_progress' AND completed_at IS NULL) OR
        (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
    ),
    
    -- Ensure success is set when status is completed/failed
    CHECK (
        (status = 'in_progress' AND success IS NULL) OR
        (status IN ('completed', 'failed') AND success IS NOT NULL)
    )
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Find all in-progress stages (for monitoring stuck jobs)
CREATE INDEX idx_stage_status_in_progress 
ON processed.stage_status (stage, status) 
WHERE status = 'in_progress';

-- Find stages by status
CREATE INDEX idx_stage_status_status 
ON processed.stage_status (status, started_at DESC);

-- Find stages by batch
CREATE INDEX idx_stage_status_batch 
ON processed.stage_status (batch_id, updated_at DESC);

-- Find stages by job_id (for worker-specific queries)
CREATE INDEX idx_stage_status_job 
ON processed.stage_status (job_id, started_at DESC) 
WHERE job_id IS NOT NULL;

-- ============================================================
-- TRIGGER FOR updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION update_stage_status_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    
    -- Auto-compute duration on completion
    IF NEW.status IN ('completed', 'failed') AND NEW.completed_at IS NOT NULL THEN
        NEW.duration_seconds = EXTRACT(EPOCH FROM (NEW.completed_at - NEW.started_at));
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_stage_status_updated_at
    BEFORE UPDATE ON processed.stage_status
    FOR EACH ROW
    EXECUTE FUNCTION update_stage_status_timestamp();

-- ============================================================
-- HELPER VIEWS
-- ============================================================

-- View of currently in-progress stages with duration
CREATE VIEW processed.in_progress_stages AS
SELECT
    batch_id,
    stage,
    job_id,
    hostname,
    started_at,
    EXTRACT(EPOCH FROM (NOW() - started_at)) AS elapsed_seconds,
    EXTRACT(EPOCH FROM (NOW() - started_at)) / 3600.0 AS elapsed_hours,
    metadata
FROM processed.stage_status
WHERE status = 'in_progress'
ORDER BY started_at;

-- View of failed stages needing retry
CREATE VIEW processed.failed_stages AS
SELECT
    batch_id,
    stage,
    job_id,
    hostname,
    started_at,
    completed_at,
    duration_seconds,
    files_processed,
    files_failed,
    error_message,
    metadata
FROM processed.stage_status
WHERE status = 'failed'
ORDER BY completed_at DESC;

-- View of completed stages with stats
CREATE VIEW processed.completed_stages AS
SELECT
    batch_id,
    stage,
    job_id,
    hostname,
    started_at,
    completed_at,
    duration_seconds,
    duration_seconds / 3600.0 AS duration_hours,
    files_processed,
    files_failed,
    metadata
FROM processed.stage_status
WHERE status = 'completed'
ORDER BY completed_at DESC;

-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON TABLE processed.stage_status IS 
'Track execution status of pipeline stages to prevent duplicate work';

COMMENT ON COLUMN processed.stage_status.batch_id IS 
'Batch identifier (e.g., MD_2025-01-01)';

COMMENT ON COLUMN processed.stage_status.stage IS 
'Pipeline stage name (e.g., raw_to_jpg, jpg_to_metadata)';

COMMENT ON COLUMN processed.stage_status.status IS 
'Current status: in_progress, completed, or failed';

COMMENT ON COLUMN processed.stage_status.job_id IS 
'Identifier of job/worker processing this stage (e.g., slurm job ID)';

COMMENT ON COLUMN processed.stage_status.hostname IS 
'Hostname where processing is happening';

COMMENT ON COLUMN processed.stage_status.metadata IS 
'Free-form JSON data for configuration, parameters, stats, etc.';

COMMENT ON VIEW processed.in_progress_stages IS 
'Currently running stages with elapsed time';

COMMENT ON VIEW processed.failed_stages IS 
'Failed stages that may need retry';

COMMENT ON VIEW processed.completed_stages IS 
'Successfully completed stages with timing stats';