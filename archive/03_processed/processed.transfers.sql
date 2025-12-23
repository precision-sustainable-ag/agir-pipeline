/* ============================================================
 * processed.transfers
 *
 * Track Globus transfers between storage sites.
 *
 * Design principles:
 * - Track transfer lifecycle (pending → in_progress → completed/failed)
 * - Store Globus task IDs for monitoring
 * - Record source/destination sites
 * - Monitor transfer progress and timing
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS processed;

-- Drop table if exists (for clean reinstall)
DROP TABLE IF EXISTS processed.transfers CASCADE;

-- ============================================================
-- TRANSFERS TABLE
-- ============================================================

CREATE TABLE processed.transfers (
    -- Identity
    transfer_id SERIAL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    
    -- Source and destination
    source_site TEXT NOT NULL,           -- 'JUNO', 'CERES', 'NCSU', etc.
    destination_site TEXT NOT NULL,      -- 'JUNO', 'CERES', 'NCSU', etc.
    source_path TEXT,                        -- Full source path
    destination_path TEXT,                   -- Full destination path
    
    -- Transfer status
    status TEXT NOT NULL CHECK (status IN (
        'pending',          -- Transfer queued but not started
        'in_progress',      -- Transfer actively running
        'completed',        -- Transfer completed successfully
        'failed',           -- Transfer failed
        'cancelled'         -- Transfer was cancelled
    )),
    
    -- Globus information
    globus_task_id TEXT UNIQUE,              -- Globus transfer task ID
    globus_status TEXT,                      -- Raw status from Globus API
    
    -- Transfer metrics
    file_count INTEGER,                      -- Number of files to transfer
    files_transferred INTEGER,               -- Number of files transferred so far
    bytes_total BIGINT,                      -- Total bytes to transfer
    bytes_transferred BIGINT,                -- Bytes transferred so far
    transfer_rate_mbps NUMERIC,              -- Transfer rate in MB/s
    
    -- Timing
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- When transfer was requested
    started_at TIMESTAMPTZ,                  -- When transfer actually started
    completed_at TIMESTAMPTZ,                -- When transfer completed/failed
    duration_seconds NUMERIC,                -- Total duration (auto-calculated)
    
    -- Job information
    job_id TEXT,                             -- SLURM job ID or workflow ID
    requested_by TEXT,                       -- Username who requested transfer
    
    -- Error handling
    error_message TEXT,                      -- Error message if failed
    retry_count INTEGER DEFAULT 0,           -- Number of retry attempts
    
    -- Metadata
    metadata JSONB,                          -- Additional transfer metadata
    notes TEXT,                              -- Human-readable notes
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Foreign key (optional - batch may not exist yet)
    FOREIGN KEY (batch_id) REFERENCES processed.batches(batch_id) ON DELETE CASCADE
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Query by batch
CREATE INDEX idx_transfers_batch 
ON processed.transfers (batch_id, created_at DESC);

-- Query by status
CREATE INDEX idx_transfers_status 
ON processed.transfers (status, updated_at DESC);

-- Query by sites
CREATE INDEX idx_transfers_sites 
ON processed.transfers (source_site, destination_site, created_at DESC);

-- Query in-progress transfers
CREATE INDEX idx_transfers_active 
ON processed.transfers (status, started_at DESC) 
WHERE status = 'in_progress';

-- Query by Globus task ID (for monitoring)
CREATE INDEX idx_transfers_globus_task 
ON processed.transfers (globus_task_id) 
WHERE globus_task_id IS NOT NULL;

-- Query failed transfers
CREATE INDEX idx_transfers_failed 
ON processed.transfers (status, updated_at DESC) 
WHERE status = 'failed';

-- Query recent transfers
CREATE INDEX idx_transfers_created 
ON processed.transfers (created_at DESC);

-- JSONB metadata queries
CREATE INDEX idx_transfers_metadata 
ON processed.transfers USING gin(metadata);

-- ============================================================
-- TRIGGERS
-- ============================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_transfers_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_transfers_updated_at
    BEFORE UPDATE ON processed.transfers
    FOR EACH ROW
    EXECUTE FUNCTION update_transfers_timestamp();

-- Auto-calculate duration when transfer completes
CREATE OR REPLACE FUNCTION calculate_transfer_duration()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.completed_at IS NOT NULL AND NEW.started_at IS NOT NULL THEN
        NEW.duration_seconds = EXTRACT(EPOCH FROM (NEW.completed_at - NEW.started_at));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_calculate_transfer_duration
    BEFORE UPDATE ON processed.transfers
    FOR EACH ROW
    WHEN (NEW.completed_at IS NOT NULL AND OLD.completed_at IS NULL)
    EXECUTE FUNCTION calculate_transfer_duration();

-- ============================================================
-- HELPER VIEWS
-- ============================================================

-- Active transfers with elapsed time
CREATE VIEW processed.active_transfers AS
SELECT
    transfer_id,
    batch_id,
    source_site,
    destination_site,
    status,
    globus_task_id,
    file_count,
    files_transferred,
    bytes_total,
    bytes_transferred,
    ROUND((bytes_transferred::NUMERIC / NULLIF(bytes_total, 0)) * 100, 2) as percent_complete,
    transfer_rate_mbps,
    started_at,
    EXTRACT(EPOCH FROM (NOW() - started_at)) as elapsed_seconds,
    job_id,
    created_at
FROM processed.transfers
WHERE status = 'in_progress'
ORDER BY started_at;

-- Failed transfers needing attention
CREATE VIEW processed.failed_transfers AS
SELECT
    transfer_id,
    batch_id,
    source_site,
    destination_site,
    globus_task_id,
    error_message,
    retry_count,
    requested_at,
    started_at,
    completed_at,
    updated_at
FROM processed.transfers
WHERE status = 'failed'
ORDER BY updated_at DESC;

-- Completed transfers with statistics
CREATE VIEW processed.completed_transfers AS
SELECT
    transfer_id,
    batch_id,
    source_site,
    destination_site,
    file_count,
    bytes_total,
    duration_seconds,
    ROUND((bytes_total::NUMERIC / 1024 / 1024) / NULLIF(duration_seconds, 0), 2) as avg_mbps,
    started_at,
    completed_at
FROM processed.transfers
WHERE status = 'completed'
ORDER BY completed_at DESC;

-- Transfer statistics by site pair
CREATE VIEW processed.transfer_stats_by_site AS
SELECT
    source_site,
    destination_site,
    COUNT(*) as total_transfers,
    COUNT(*) FILTER (WHERE status = 'completed') as completed_count,
    COUNT(*) FILTER (WHERE status = 'failed') as failed_count,
    COUNT(*) FILTER (WHERE status = 'in_progress') as in_progress_count,
    SUM(bytes_total) FILTER (WHERE status = 'completed') as total_bytes_transferred,
    AVG(duration_seconds) FILTER (WHERE status = 'completed') as avg_duration_seconds,
    MIN(completed_at) as first_transfer,
    MAX(completed_at) as last_transfer
FROM processed.transfers
GROUP BY source_site, destination_site
ORDER BY total_transfers DESC;

-- Pending transfers (queue)
CREATE VIEW processed.pending_transfers AS
SELECT
    transfer_id,
    batch_id,
    source_site,
    destination_site,
    source_path,
    destination_path,
    file_count,
    bytes_total,
    requested_at,
    requested_by,
    job_id
FROM processed.transfers
WHERE status = 'pending'
ORDER BY requested_at;

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

-- Get transfer progress percentage
CREATE OR REPLACE FUNCTION get_transfer_progress(p_transfer_id INTEGER)
RETURNS NUMERIC AS $$
DECLARE
    v_progress NUMERIC;
BEGIN
    SELECT 
        ROUND((bytes_transferred::NUMERIC / NULLIF(bytes_total, 0)) * 100, 2)
    INTO v_progress
    FROM processed.transfers
    WHERE transfer_id = p_transfer_id;
    
    RETURN COALESCE(v_progress, 0);
END;
$$ LANGUAGE plpgsql;

-- Cleanup old completed transfers (retention policy)
CREATE OR REPLACE FUNCTION cleanup_old_transfers(retention_days INTEGER DEFAULT 365)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM processed.transfers
    WHERE status IN ('completed', 'cancelled')
    AND completed_at < NOW() - INTERVAL '1 day' * retention_days;
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON TABLE processed.transfers IS 
'Track Globus transfers between storage sites';

COMMENT ON COLUMN processed.transfers.globus_task_id IS 
'Globus transfer task ID for monitoring via Globus API';

COMMENT ON COLUMN processed.transfers.status IS 
'Transfer status: pending, in_progress, completed, failed, cancelled';

COMMENT ON COLUMN processed.transfers.duration_seconds IS 
'Auto-calculated transfer duration when completed_at is set';

COMMENT ON VIEW processed.active_transfers IS 
'Currently running transfers with progress information';

COMMENT ON VIEW processed.failed_transfers IS 
'Failed transfers that may need retry or investigation';

COMMENT ON VIEW processed.transfer_stats_by_site IS 
'Aggregate statistics by source/destination site pair';