/* ============================================================
 * Analytics Views
 *
 * Pre-aggregated views for reporting and dashboards.
 *
 * Design principles:
 * - Materialized views for expensive aggregations
 * - Regular views for real-time data
 * - Optimized for common reporting queries
 * ============================================================
 */

CREATE SCHEMA IF NOT EXISTS processed;

-- ============================================================
-- DAILY PROCESSING VOLUMES
-- ============================================================

-- Daily batch processing summary
CREATE OR REPLACE VIEW processed.daily_batch_summary AS
SELECT
    DATE(b.batch_date) as processing_date,
    b.batch_state,
    COUNT(DISTINCT b.batch_id) as batch_count,
    SUM(b.file_count_raw) as total_raw_files,
    SUM(b.file_count_jpg) as total_jpg_files,
    SUM(b.total_bytes) as total_bytes,
    COUNT(*) FILTER (WHERE b.processing_status = 'completed') as completed_batches,
    COUNT(*) FILTER (WHERE b.processing_status = 'failed') as failed_batches,
    COUNT(*) FILTER (WHERE b.processing_status = 'in_progress') as in_progress_batches,
    AVG(b.file_count_raw) as avg_files_per_batch,
    AVG(b.total_bytes) as avg_bytes_per_batch
FROM processed.batches b
WHERE b.batch_date IS NOT NULL
GROUP BY DATE(b.batch_date), b.batch_state
ORDER BY processing_date DESC, b.batch_state;

COMMENT ON VIEW processed.daily_batch_summary IS
'Daily processing volumes by batch state';

-- ============================================================
-- STAGE PERFORMANCE METRICS
-- ============================================================

-- Stage execution statistics
CREATE OR REPLACE VIEW processed.stage_performance AS
SELECT
    s.batch_id,
    s.stage,
    s.status,
    s.started_at,
    s.completed_at,
    s.duration_seconds,
    s.files_processed,
    s.files_failed,
    s.error_message,
    CASE 
        WHEN s.files_processed > 0 AND s.duration_seconds > 0
        THEN ROUND(s.files_processed::NUMERIC / s.duration_seconds, 2)
        ELSE NULL
    END as files_per_second,
    b.batch_state,
    b.batch_date
FROM processed.stage_status s
LEFT JOIN processed.batches b ON s.batch_id = b.batch_id
ORDER BY s.started_at DESC;

COMMENT ON VIEW processed.stage_performance IS
'Stage execution metrics with throughput calculations';

-- Stage performance summary by stage
CREATE OR REPLACE VIEW processed.stage_performance_summary AS
SELECT
    stage,
    COUNT(*) as total_executions,
    COUNT(*) FILTER (WHERE status = 'completed') as completed_count,
    COUNT(*) FILTER (WHERE status = 'failed') as failed_count,
    COUNT(*) FILTER (WHERE status = 'running') as running_count,
    ROUND(
        COUNT(*) FILTER (WHERE status = 'completed')::NUMERIC / NULLIF(COUNT(*), 0) * 100,
        2
    ) as success_rate,
    AVG(duration_seconds) FILTER (WHERE status = 'completed') as avg_duration_seconds,
    MIN(duration_seconds) FILTER (WHERE status = 'completed') as min_duration_seconds,
    MAX(duration_seconds) FILTER (WHERE status = 'completed') as max_duration_seconds,
    SUM(files_processed) as total_files_processed,
    SUM(files_failed) as total_files_failed,
    AVG(files_processed::NUMERIC / NULLIF(duration_seconds, 0)) 
        FILTER (WHERE status = 'completed' AND duration_seconds > 0) as avg_files_per_second
FROM processed.stage_status
GROUP BY stage
ORDER BY total_executions DESC;

COMMENT ON VIEW processed.stage_performance_summary IS
'Aggregate performance metrics by stage';

-- ============================================================
-- ERROR ANALYSIS
-- ============================================================

-- Recent errors across all stages
CREATE OR REPLACE VIEW processed.recent_errors AS
SELECT
    s.batch_id,
    s.stage,
    s.status,
    s.error_message,
    s.files_failed,
    s.started_at,
    s.completed_at,
    s.updated_at,
    b.batch_state,
    b.batch_date,
    b.location
FROM processed.stage_status s
LEFT JOIN processed.batches b ON s.batch_id = b.batch_id
WHERE s.status IN ('failed', 'error')
   OR s.files_failed > 0
   OR s.error_message IS NOT NULL
ORDER BY s.updated_at DESC;

COMMENT ON VIEW processed.recent_errors IS
'Recent failures and errors across all stages';

-- Error summary by stage
CREATE OR REPLACE VIEW processed.error_summary_by_stage AS
SELECT
    stage,
    COUNT(*) as error_count,
    COUNT(DISTINCT batch_id) as affected_batches,
    SUM(files_failed) as total_files_failed,
    MAX(updated_at) as last_error_time,
    array_agg(DISTINCT substring(error_message, 1, 100)) 
        FILTER (WHERE error_message IS NOT NULL) as sample_errors
FROM processed.stage_status
WHERE status IN ('failed', 'error')
   OR files_failed > 0
GROUP BY stage
ORDER BY error_count DESC;

COMMENT ON VIEW processed.error_summary_by_stage IS
'Error statistics aggregated by stage';

-- ============================================================
-- TRANSFER ANALYTICS
-- ============================================================

-- Transfer performance metrics
CREATE OR REPLACE VIEW processed.transfer_performance AS
SELECT
    t.transfer_id,
    t.batch_id,
    t.source_location,
    t.destination_location,
    t.status,
    t.file_count,
    t.bytes_total,
    t.duration_seconds,
    t.transfer_rate_mbps,
    CASE
        WHEN t.duration_seconds > 0 AND t.bytes_total > 0
        THEN ROUND((t.bytes_total::NUMERIC / 1024 / 1024) / t.duration_seconds, 2)
        ELSE NULL
    END as actual_mbps,
    t.started_at,
    t.completed_at,
    b.batch_state,
    b.batch_date
FROM processed.transfers t
LEFT JOIN processed.batches b ON t.batch_id = b.batch_id
WHERE t.status IN ('completed', 'failed')
ORDER BY t.completed_at DESC;

COMMENT ON VIEW processed.transfer_performance IS
'Transfer performance with calculated throughput';

-- Transfer summary by route
CREATE OR REPLACE VIEW processed.transfer_summary_by_route AS
SELECT
    source_location,
    destination_location,
    COUNT(*) as total_transfers,
    COUNT(*) FILTER (WHERE status = 'completed') as completed_transfers,
    COUNT(*) FILTER (WHERE status = 'failed') as failed_transfers,
    SUM(bytes_total) FILTER (WHERE status = 'completed') as total_bytes_transferred,
    ROUND(
        SUM(bytes_total) FILTER (WHERE status = 'completed')::NUMERIC / 1024 / 1024 / 1024,
        2
    ) as total_gb_transferred,
    AVG(duration_seconds) FILTER (WHERE status = 'completed') as avg_duration_seconds,
    AVG(transfer_rate_mbps) FILTER (WHERE status = 'completed') as avg_transfer_rate_mbps,
    MAX(completed_at) FILTER (WHERE status = 'completed') as last_transfer_time
FROM processed.transfers
GROUP BY source_location, destination_location
ORDER BY total_transfers DESC;

COMMENT ON VIEW processed.transfer_summary_by_route IS
'Transfer statistics by source/destination pair';

-- ============================================================
-- BATCH COMPLETION TRACKING
-- ============================================================

-- Batch completion status with stage progress
CREATE OR REPLACE VIEW processed.batch_completion_status AS
SELECT
    b.batch_id,
    b.batch_state,
    b.batch_date,
    b.location,
    b.processing_status as batch_status,
    b.file_count_raw,
    b.file_count_jpg,
    
    -- Stage completion
    COUNT(DISTINCT s.stage) FILTER (WHERE s.status = 'completed') as stages_completed,
    COUNT(DISTINCT s.stage) FILTER (WHERE s.status = 'failed') as stages_failed,
    COUNT(DISTINCT s.stage) FILTER (WHERE s.status = 'running') as stages_running,
    
    -- Pipeline completion flags
    b.raw_to_jpg_complete,
    b.jpg_to_metadata_complete,
    b.metadata_to_cutouts_complete,
    
    -- Transfer status
    COUNT(t.transfer_id) FILTER (WHERE t.status = 'completed') as transfers_completed,
    COUNT(t.transfer_id) FILTER (WHERE t.status = 'failed') as transfers_failed,
    COUNT(t.transfer_id) FILTER (WHERE t.status = 'in_progress') as transfers_in_progress,
    
    -- Timestamps
    MIN(s.started_at) as first_stage_start,
    MAX(s.completed_at) as last_stage_end,
    b.created_at,
    b.updated_at
    
FROM processed.batches b
LEFT JOIN processed.stage_status s ON b.batch_id = s.batch_id
LEFT JOIN processed.transfers t ON b.batch_id = t.batch_id
GROUP BY 
    b.batch_id, b.batch_state, b.batch_date, b.location, b.processing_status,
    b.file_count_raw, b.file_count_jpg, b.raw_to_jpg_complete,
    b.jpg_to_metadata_complete, b.metadata_to_cutouts_complete,
    b.created_at, b.updated_at
ORDER BY b.batch_date DESC;

COMMENT ON VIEW processed.batch_completion_status IS
'Comprehensive batch progress tracking';

-- ============================================================
-- THROUGHPUT METRICS
-- ============================================================

-- Daily throughput (files and bytes)
CREATE OR REPLACE VIEW processed.daily_throughput AS
SELECT
    DATE(s.started_at) as processing_date,
    s.stage,
    COUNT(DISTINCT s.batch_id) as batches_processed,
    SUM(s.files_processed) as total_files_processed,
    SUM(s.files_failed) as total_files_failed,
    SUM(s.duration_seconds) as total_processing_seconds,
    ROUND(
        SUM(s.files_processed)::NUMERIC / NULLIF(SUM(s.duration_seconds), 0),
        2
    ) as avg_files_per_second,
    ROUND(
        SUM(s.duration_seconds)::NUMERIC / NULLIF(COUNT(DISTINCT s.batch_id), 0),
        2
    ) as avg_seconds_per_batch
FROM processed.stage_status s
WHERE s.status = 'completed'
  AND s.started_at IS NOT NULL
GROUP BY DATE(s.started_at), s.stage
ORDER BY processing_date DESC, s.stage;

COMMENT ON VIEW processed.daily_throughput IS
'Daily processing throughput by stage';

-- ============================================================
-- EVENT ANALYTICS
-- ============================================================

-- Event summary by type and severity
CREATE OR REPLACE VIEW processed.event_summary AS
SELECT
    event_type,
    severity,
    COUNT(*) as event_count,
    COUNT(DISTINCT batch_id) FILTER (WHERE batch_id IS NOT NULL) as affected_batches,
    MIN(created_at) as first_occurrence,
    MAX(created_at) as last_occurrence,
    array_agg(DISTINCT substring(message, 1, 100)) as sample_messages
FROM processed.events
GROUP BY event_type, severity
ORDER BY event_count DESC;

COMMENT ON VIEW processed.event_summary IS
'Event statistics by type and severity';

-- Recent critical events
CREATE OR REPLACE VIEW processed.recent_critical_events AS
SELECT
    event_id,
    event_type,
    severity,
    message,
    batch_id,
    stage,
    job_id,
    created_at,
    metadata
FROM processed.events
WHERE severity IN ('ERROR', 'CRITICAL')
ORDER BY created_at DESC
LIMIT 100;

COMMENT ON VIEW processed.recent_critical_events IS
'Last 100 critical events';

-- ============================================================
-- STORAGE ANALYTICS
-- ============================================================

-- Storage utilization by location
CREATE OR REPLACE VIEW processed.storage_by_location AS
SELECT
    b.location,
    COUNT(DISTINCT b.batch_id) as batch_count,
    SUM(b.file_count_raw) as total_raw_files,
    SUM(b.file_count_jpg) as total_jpg_files,
    SUM(b.file_count_metadata) as total_metadata_files,
    SUM(b.file_count_cutout) as total_cutout_files,
    SUM(b.total_bytes) as total_bytes,
    ROUND(SUM(b.total_bytes)::NUMERIC / 1024 / 1024 / 1024, 2) as total_gb,
    ROUND(AVG(b.total_bytes)::NUMERIC / 1024 / 1024 / 1024, 2) as avg_gb_per_batch,
    MIN(b.batch_date) as earliest_batch,
    MAX(b.batch_date) as latest_batch
FROM processed.batches b
WHERE b.location IS NOT NULL
GROUP BY b.location
ORDER BY total_bytes DESC;

COMMENT ON VIEW processed.storage_by_location IS
'Storage utilization by location';

-- Storage growth over time
CREATE OR REPLACE VIEW processed.storage_growth AS
SELECT
    DATE_TRUNC('month', b.batch_date) as month,
    b.batch_state,
    COUNT(DISTINCT b.batch_id) as batch_count,
    SUM(b.file_count_raw) as raw_files,
    SUM(b.file_count_jpg) as jpg_files,
    SUM(b.total_bytes) as total_bytes,
    ROUND(SUM(b.total_bytes)::NUMERIC / 1024 / 1024 / 1024, 2) as total_gb,
    SUM(SUM(b.total_bytes)) OVER (
        PARTITION BY b.batch_state 
        ORDER BY DATE_TRUNC('month', b.batch_date)
    ) as cumulative_bytes
FROM processed.batches b
WHERE b.batch_date IS NOT NULL
  AND b.total_bytes IS NOT NULL
GROUP BY DATE_TRUNC('month', b.batch_date), b.batch_state
ORDER BY month DESC, b.batch_state;

COMMENT ON VIEW processed.storage_growth IS
'Monthly storage growth with cumulative totals';

-- ============================================================
-- CAMERA ANALYTICS
-- ============================================================

-- Camera usage statistics
CREATE OR REPLACE VIEW processed.camera_usage_stats AS
SELECT
    i.camera_make,
    i.camera_model,
    COUNT(*) as image_count,
    COUNT(DISTINCT i.batch_id) as batch_count,
    AVG(i.width) as avg_width,
    AVG(i.height) as avg_height,
    COUNT(*) FILTER (WHERE i.detection_count > 0) as images_with_detections,
    AVG(i.detection_count) FILTER (WHERE i.detection_count > 0) as avg_detections_per_image,
    MIN(i.capture_datetime) as earliest_capture,
    MAX(i.capture_datetime) as latest_capture
FROM processed.images i
WHERE i.camera_make IS NOT NULL
  OR i.camera_model IS NOT NULL
GROUP BY i.camera_make, i.camera_model
ORDER BY image_count DESC;

COMMENT ON VIEW processed.camera_usage_stats IS
'Image statistics by camera make/model';

-- ============================================================
-- PROCESSING PIPELINE OVERVIEW
-- ============================================================

-- High-level pipeline status
CREATE OR REPLACE VIEW processed.pipeline_overview AS
SELECT
    -- Batch counts
    (SELECT COUNT(*) FROM processed.batches) as total_batches,
    (SELECT COUNT(*) FROM processed.batches WHERE processing_status = 'completed') as completed_batches,
    (SELECT COUNT(*) FROM processed.batches WHERE processing_status = 'failed') as failed_batches,
    (SELECT COUNT(*) FROM processed.batches WHERE processing_status = 'in_progress') as in_progress_batches,
    
    -- Image counts
    (SELECT COUNT(*) FROM processed.images) as total_images,
    (SELECT COUNT(*) FROM processed.images WHERE processing_status = 'completed') as completed_images,
    (SELECT COUNT(*) FROM processed.images WHERE processing_status = 'failed') as failed_images,
    
    -- Stage executions
    (SELECT COUNT(*) FROM processed.stage_status WHERE status = 'running') as running_stages,
    (SELECT COUNT(*) FROM processed.stage_status WHERE status = 'failed') as failed_stages,
    
    -- Transfers
    (SELECT COUNT(*) FROM processed.transfers WHERE status = 'in_progress') as active_transfers,
    (SELECT COUNT(*) FROM processed.transfers WHERE status = 'pending') as pending_transfers,
    (SELECT COUNT(*) FROM processed.transfers WHERE status = 'failed') as failed_transfers,
    
    -- Storage
    (SELECT ROUND(SUM(total_bytes)::NUMERIC / 1024 / 1024 / 1024, 2) 
     FROM processed.batches) as total_storage_gb,
    
    -- Timestamps
    (SELECT MIN(batch_date) FROM processed.batches) as earliest_batch_date,
    (SELECT MAX(batch_date) FROM processed.batches) as latest_batch_date,
    (SELECT MAX(updated_at) FROM processed.batches) as last_update;

COMMENT ON VIEW processed.pipeline_overview IS
'High-level pipeline statistics';

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

-- Get processing stats for date range
CREATE OR REPLACE FUNCTION get_processing_stats(
    start_date DATE,
    end_date DATE DEFAULT CURRENT_DATE
)
RETURNS TABLE (
    metric TEXT,
    value NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    SELECT 'batches_processed'::TEXT, COUNT(DISTINCT b.batch_id)::NUMERIC
    FROM processed.batches b
    WHERE b.batch_date BETWEEN start_date AND end_date
    
    UNION ALL
    
    SELECT 'files_processed'::TEXT, SUM(s.files_processed)::NUMERIC
    FROM processed.stage_status s
    WHERE DATE(s.started_at) BETWEEN start_date AND end_date
      AND s.status = 'completed'
    
    UNION ALL
    
    SELECT 'total_gb_processed'::TEXT, 
           ROUND(SUM(b.total_bytes)::NUMERIC / 1024 / 1024 / 1024, 2)
    FROM processed.batches b
    WHERE b.batch_date BETWEEN start_date AND end_date
    
    UNION ALL
    
    SELECT 'avg_files_per_batch'::TEXT, 
           AVG(b.file_count_raw)::NUMERIC
    FROM processed.batches b
    WHERE b.batch_date BETWEEN start_date AND end_date
      AND b.file_count_raw > 0;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_processing_stats IS
'Get aggregate processing statistics for date range';