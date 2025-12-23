/* ============================================================
 * processed.events.sql
 *
 * Event logging system for tracking all operations.
 *
 * Design principles:
 * - Log everything: stage operations, queries, errors, etc.
 * - Structured data in JSONB metadata field
 * - Severity levels for filtering
 * - Fast queries with proper indexes
 * - Audit trail for debugging and monitoring
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS processed;

-- Drop table if exists (for clean reinstall)
DROP TABLE IF EXISTS processed.events CASCADE;

-- ============================================================
-- EVENTS TABLE
-- ============================================================

CREATE TABLE processed.events (
    -- Identity
    event_id BIGSERIAL PRIMARY KEY,
    
    -- Classification
    event_type TEXT NOT NULL,           -- e.g., 'stage.started', 'gap.query', 'error.connection'
    severity TEXT NOT NULL CHECK (severity IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    
    -- Context
    batch_id TEXT,                      -- Related batch (if applicable)
    stage TEXT,                         -- Related stage (if applicable)
    job_id TEXT,                        -- Related job/worker
    
    -- Content
    message TEXT NOT NULL,              -- Human-readable message
    metadata JSONB,                     -- Structured data (parameters, results, etc.)
    
    -- Provenance
    hostname TEXT,                      -- Where event occurred
    user_name TEXT,                     -- Who triggered it
    source TEXT,                        -- Which component logged it
    
    -- Timing
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Optional error details
    error_type TEXT,                    -- Exception type (if error)
    stack_trace TEXT                    -- Stack trace (if error)
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Time-based queries (most common)
CREATE INDEX idx_events_created_at 
ON processed.events (created_at DESC);

-- Query by event type
CREATE INDEX idx_events_type_time 
ON processed.events (event_type, created_at DESC);

-- Query by severity
CREATE INDEX idx_events_severity_time 
ON processed.events (severity, created_at DESC) 
WHERE severity IN ('ERROR', 'CRITICAL');

-- Query by batch
CREATE INDEX idx_events_batch 
ON processed.events (batch_id, created_at DESC) 
WHERE batch_id IS NOT NULL;

-- Query by stage
CREATE INDEX idx_events_stage 
ON processed.events (stage, created_at DESC) 
WHERE stage IS NOT NULL;

-- Query by job
CREATE INDEX idx_events_job 
ON processed.events (job_id, created_at DESC) 
WHERE job_id IS NOT NULL;

-- Full-text search on message
CREATE INDEX idx_events_message_search 
ON processed.events USING gin(to_tsvector('english', message));

-- JSONB metadata queries
CREATE INDEX idx_events_metadata 
ON processed.events USING gin(metadata);

-- ============================================================
-- HELPER VIEWS
-- ============================================================

-- Recent events (last 24 hours)
CREATE VIEW processed.recent_events AS
SELECT
    event_id,
    event_type,
    severity,
    batch_id,
    stage,
    message,
    created_at,
    hostname,
    user_name
FROM processed.events
WHERE created_at > NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;

-- Error events (needs attention)
CREATE VIEW processed.error_events AS
SELECT
    event_id,
    event_type,
    batch_id,
    stage,
    job_id,
    message,
    error_type,
    stack_trace,
    created_at,
    hostname,
    user_name
FROM processed.events
WHERE severity IN ('ERROR', 'CRITICAL')
ORDER BY created_at DESC;

-- Stage operation events
CREATE VIEW processed.stage_events AS
SELECT
    event_id,
    event_type,
    batch_id,
    stage,
    job_id,
    severity,
    message,
    metadata,
    created_at,
    hostname
FROM processed.events
WHERE event_type LIKE 'stage.%'
ORDER BY created_at DESC;

-- Warning events
CREATE VIEW processed.warning_events AS
SELECT
    event_id,
    event_type,
    batch_id,
    stage,
    message,
    metadata,
    created_at,
    hostname
FROM processed.events
WHERE severity = 'WARNING'
ORDER BY created_at DESC;

-- Event summary by type (last 24 hours)
CREATE VIEW processed.event_summary_24h AS
SELECT
    event_type,
    severity,
    COUNT(*) AS event_count,
    MIN(created_at) AS first_occurrence,
    MAX(created_at) AS last_occurrence
FROM processed.events
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY event_type, severity
ORDER BY event_count DESC, last_occurrence DESC;

-- Event summary by batch
CREATE VIEW processed.batch_event_summary AS
SELECT
    batch_id,
    COUNT(*) AS total_events,
    COUNT(*) FILTER (WHERE severity = 'ERROR') AS error_count,
    COUNT(*) FILTER (WHERE severity = 'WARNING') AS warning_count,
    COUNT(*) FILTER (WHERE event_type LIKE 'stage.%') AS stage_event_count,
    MIN(created_at) AS first_event,
    MAX(created_at) AS last_event
FROM processed.events
WHERE batch_id IS NOT NULL
GROUP BY batch_id
ORDER BY last_event DESC;

-- ============================================================
-- PARTITIONING FUNCTION (for future use)
-- ============================================================

-- Note: For high-volume production use, consider partitioning by month
-- This function can be used to create monthly partitions

CREATE OR REPLACE FUNCTION create_events_partition(
    partition_date DATE
) RETURNS TEXT AS $$
DECLARE
    partition_name TEXT;
    start_date DATE;
    end_date DATE;
BEGIN
    -- Calculate partition boundaries
    start_date := DATE_TRUNC('month', partition_date);
    end_date := start_date + INTERVAL '1 month';
    
    -- Generate partition name (e.g., events_2025_01)
    partition_name := 'events_' || TO_CHAR(start_date, 'YYYY_MM');
    
    -- Create partition table
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS processed.%I PARTITION OF processed.events
         FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        start_date,
        end_date
    );
    
    RETURN partition_name;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- CLEANUP FUNCTION
-- ============================================================

-- Function to delete old events (retention policy)
CREATE OR REPLACE FUNCTION cleanup_old_events(
    retention_days INTEGER DEFAULT 90
) RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM processed.events
    WHERE created_at < NOW() - (retention_days || ' days')::INTERVAL;
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON TABLE processed.events IS 
'Event log for all system operations, errors, and audit trail';

COMMENT ON COLUMN processed.events.event_type IS 
'Event type in dotted notation (e.g., stage.started, gap.query, error.connection)';

COMMENT ON COLUMN processed.events.severity IS 
'Severity level: DEBUG, INFO, WARNING, ERROR, CRITICAL';

COMMENT ON COLUMN processed.events.metadata IS 
'Structured event data in JSON format';

COMMENT ON COLUMN processed.events.source IS 
'Component that logged the event (e.g., StageStatus, PipelineGaps)';

COMMENT ON VIEW processed.recent_events IS 
'Events from the last 24 hours';

COMMENT ON VIEW processed.error_events IS 
'Error and critical events needing attention';

COMMENT ON VIEW processed.stage_events IS 
'All stage-related operation events';

COMMENT ON VIEW processed.event_summary_24h IS 
'Event counts by type and severity over last 24 hours';

COMMENT ON VIEW processed.batch_event_summary IS 
'Event summary grouped by batch';

COMMENT ON FUNCTION cleanup_old_events IS 
'Delete events older than specified retention period (default 90 days)';