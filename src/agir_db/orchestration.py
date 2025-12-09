"""
Orchestration helpers for RAW to JPG conversion workflows.

This module provides high-level workflow orchestration for processing
batches through the raw_to_jpg pipeline, integrating AgirDB with
svs-raw-api converters.
"""

import logging
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime, timedelta

from .connection import ConnectionManager
from .exceptions import (
    QueryError,
    OrchestrationError,
    InvalidParameterError
)


logger = logging.getLogger(__name__)


class Orchestration:
    """
    High-level workflow orchestration for RAW to JPG conversion.
    
    This class provides methods for:
    - Discovering batches needing conversion
    - Running conversions with progress tracking
    - Error handling and retry logic
    - Status monitoring
    
    Designed to integrate AgirDB with svs-raw-api converters.
    
    Parameters
    ----------
    connection : ConnectionManager
        PostgreSQL database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Get conversion queue
    ...     queue = db.orchestration.get_conversion_queue(limit=10)
    ...     
    ...     # Process batch
    ...     result = db.orchestration.process_batch(
    ...         'MD_2024-06-01',
    ...         raw_dir='/data/raw',
    ...         output_dir='/data/jpg'
    ...     )
    ...     
    ...     # Monitor progress
    ...     status = db.orchestration.get_batch_progress('MD_2024-06-01')
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("Orchestration initialized")
    
    def get_conversion_queue(
        self,
        limit: int = 100,
        batch_state: Optional[str] = None,
        location: Optional[str] = None
    ) -> List[Dict]:
        """
        Get batches needing RAW to JPG conversion.
        
        Uses pipeline gaps to find batches with RAW files but missing JPGs.
        
        Parameters
        ----------
        limit : int, optional
            Maximum number of batches to return
        batch_state : str, optional
            Filter by batch state (e.g., 'MD', 'TX')
        location : str, optional
            Filter by location (e.g., 'JUNO', 'CERES')
        
        Returns
        -------
        list of dict
            Batches needing conversion, with keys:
            - batch_id
            - batch_state
            - batch_date
            - location
            - raw_count
            - jpg_count
            - gap_count
            - priority (calculated based on age)
        
        Examples
        --------
        >>> queue = db.orchestration.get_conversion_queue(limit=10)
        >>> for batch in queue:
        ...     print(f"{batch['batch_id']}: {batch['gap_count']} files")
        """
        logger.info(f"Getting conversion queue (limit={limit})")
        
        query = """
            WITH batch_gaps AS (
                SELECT
                    b.batch_id,
                    b.batch_state,
                    b.batch_date,
                    b.location,
                    b.file_count_raw as raw_count,
                    b.file_count_jpg as jpg_count,
                    (b.file_count_raw - COALESCE(b.file_count_jpg, 0)) as gap_count,
                    -- Priority: newer batches first, but also consider gap size
                    (CURRENT_DATE - b.batch_date) as age_days,
                    CASE
                        WHEN b.batch_date >= CURRENT_DATE - INTERVAL '7 days' THEN 1
                        WHEN b.batch_date >= CURRENT_DATE - INTERVAL '30 days' THEN 2
                        ELSE 3
                    END as priority_tier
                FROM processed.batches b
                WHERE b.file_count_raw > 0
                  AND (b.file_count_raw - COALESCE(b.file_count_jpg, 0)) > 0
                  AND (b.raw_to_jpg_complete IS NULL OR b.raw_to_jpg_complete = FALSE)
        """
        
        params = []
        
        if batch_state:
            query += " AND b.batch_state = %s"
            params.append(batch_state)
        
        if location:
            query += " AND b.location = %s"
            params.append(location)
        
        query += """
            )
            SELECT
                batch_id,
                batch_state,
                batch_date,
                location,
                raw_count,
                jpg_count,
                gap_count,
                age_days,
                priority_tier as priority
            FROM batch_gaps
            ORDER BY priority_tier, age_days, gap_count DESC
            LIMIT %s;
        """
        
        params.append(limit)
        
        try:
            result = self.conn.fetch_all(query, tuple(params))
            batches = [dict(row) for row in result] if result else []
            logger.info(f"Found {len(batches)} batches in conversion queue")
            return batches
        except Exception as e:
            logger.error(f"Failed to get conversion queue: {e}")
            raise QueryError(f"Failed to get conversion queue: {e}") from e
    
    def get_batch_files_for_conversion(
        self,
        batch_id: str,
        check_existing: bool = True
    ) -> List[Dict]:
        """
        Get list of RAW files that need JPG conversion.
        
        Parameters
        ----------
        batch_id : str
            Batch to process
        check_existing : bool, optional
            If True, only return files without existing JPGs
        
        Returns
        -------
        list of dict
            Files to convert, with keys:
            - image_id
            - file_name
            - file_path
            - file_ext
        
        Examples
        --------
        >>> files = db.orchestration.get_batch_files_for_conversion('MD_2024-06-01')
        >>> print(f"Need to convert {len(files)} files")
        """
        logger.info(f"Getting files for conversion: {batch_id}")
        
        if check_existing:
            # Use pipeline gaps to find missing JPGs
            query = """
                WITH base_names AS (
                    SELECT DISTINCT
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                            '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                        ) as base_name
                    FROM source.globus_file_index
                    WHERE batch_id = %s
                      AND LOWER(file_ext) IN ('raw', 'arw')
                ),
                has_jpg AS (
                    SELECT DISTINCT
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                            '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                        ) as base_name
                    FROM source.globus_file_index
                    WHERE batch_id = %s
                      AND LOWER(file_ext) IN ('jpg', 'jpeg')
                ),
                gaps AS (
                    SELECT bn.base_name
                    FROM base_names bn
                    LEFT JOIN has_jpg hj ON bn.base_name = hj.base_name
                    WHERE hj.base_name IS NULL
                )
                SELECT
                    g.base_name as image_id,
                    f.file_name,
                    f.rel_path as file_path,
                    f.file_ext
                FROM gaps g
                INNER JOIN source.globus_file_index f ON
                    REGEXP_REPLACE(
                        REGEXP_REPLACE(f.file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    ) = g.base_name
                WHERE f.batch_id = %s
                  AND LOWER(f.file_ext) IN ('raw', 'arw')
                ORDER BY f.file_name;
            """
            params = (batch_id, batch_id, batch_id)
        else:
            # Get all RAW files
            query = """
                SELECT
                    REGEXP_REPLACE(
                        REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    ) as image_id,
                    file_name,
                    full_path as file_path,
                    file_ext
                FROM source.globus_file_index
                WHERE batch_id = %s
                  AND LOWER(file_ext) IN ('raw', 'arw')
                ORDER BY file_name;
            """
            params = (batch_id,)
        
        try:
            result = self.conn.fetch_all(query, params)
            files = [dict(row) for row in result] if result else []
            logger.info(f"Found {len(files)} files to convert in {batch_id}")
            return files
        except Exception as e:
            logger.error(f"Failed to get files for conversion: {e}")
            raise QueryError(f"Failed to get files for {batch_id}: {e}") from e
    
    def start_batch_conversion(
        self,
        batch_id: str,
        job_id: str,
        worker_name: Optional[str] = None
    ) -> Dict:
        """
        Start RAW to JPG conversion for a batch.
        
        This method:
        1. Creates stage status record
        2. Logs start event
        3. Returns batch info and files to process
        
        Parameters
        ----------
        batch_id : str
            Batch to process
        job_id : str
            Job/worker identifier
        worker_name : str, optional
            Name of worker node
        
        Returns
        -------
        dict
            Contains:
            - batch_id
            - file_count
            - files (list of files to convert)
            - started_at
        
        Examples
        --------
        >>> info = db.orchestration.start_batch_conversion(
        ...     'MD_2024-06-01',
        ...     job_id='worker-001'
        ... )
        >>> print(f"Processing {info['file_count']} files")
        """
        logger.info(f"Starting batch conversion: {batch_id}")
        
        try:
            # Get files to process
            files = self.get_batch_files_for_conversion(batch_id)
            
            if not files:
                logger.warning(f"No files to convert in {batch_id}")
                return {
                    'batch_id': batch_id,
                    'file_count': 0,
                    'files': [],
                    'started_at': datetime.now()
                }
            
            # Start stage
            from .stages import StageStatus
            stages = StageStatus(self.conn)
            
            stages.start(
                batch_id=batch_id,
                stage='raw_to_jpg',
                job_id=job_id
            )
            
            # Log event
            from .events import EventLogger
            events = EventLogger(self.conn)
            
            events.log_event(
                event_type='stage.started',
                message=f'Started raw_to_jpg conversion for {batch_id}',
                severity='INFO',
                batch_id=batch_id,
                stage='raw_to_jpg',
                job_id=job_id,
                metadata={
                    'file_count': len(files),
                    'worker': worker_name
                }
            )
            
            started_at = datetime.now()
            
            logger.info(f"Started conversion for {batch_id}: {len(files)} files")
            
            return {
                'batch_id': batch_id,
                'file_count': len(files),
                'files': files,
                'started_at': started_at
            }
            
        except Exception as e:
            logger.error(f"Failed to start batch conversion: {e}")
            raise OrchestrationError(f"Failed to start {batch_id}: {e}") from e
    
    def update_conversion_progress(
        self,
        batch_id: str,
        files_processed: int,
        files_failed: int = 0
    ) -> None:
        """
        Update conversion progress for a batch.
        
        Parameters
        ----------
        batch_id : str
            Batch being processed
        files_processed : int
            Number of files successfully converted
        files_failed : int, optional
            Number of files that failed
        
        Examples
        --------
        >>> db.orchestration.update_conversion_progress(
        ...     'MD_2024-06-01',
        ...     files_processed=75,
        ...     files_failed=2
        ... )
        """
        logger.debug(f"Updating progress for {batch_id}: {files_processed} processed")
        
        try:
            from .stages import StageStatus
            stages = StageStatus(self.conn)
            
            stages.update_progress(
                batch_id=batch_id,
                stage='raw_to_jpg',
                files_processed=files_processed,
                files_failed=files_failed
            )
            
        except Exception as e:
            logger.error(f"Failed to update progress: {e}")
            raise OrchestrationError(f"Failed to update progress: {e}") from e
    
    def complete_batch_conversion(
        self,
        batch_id: str,
        success: bool,
        files_processed: int,
        files_failed: int = 0,
        error_message: Optional[str] = None
    ) -> None:
        """
        Mark batch conversion as complete.
        
        This method:
        1. Completes stage status
        2. Updates batch completion flags
        3. Logs completion event
        
        Parameters
        ----------
        batch_id : str
            Batch that finished
        success : bool
            Whether conversion succeeded
        files_processed : int
            Number of files successfully converted
        files_failed : int, optional
            Number of files that failed
        error_message : str, optional
            Error message if failed
        
        Examples
        --------
        >>> db.orchestration.complete_batch_conversion(
        ...     'MD_2024-06-01',
        ...     success=True,
        ...     files_processed=150
        ... )
        """
        logger.info(f"Completing batch conversion: {batch_id} (success={success})")
        
        try:
            from .stages import StageStatus
            from .batches import BatchMetadata
            from .events import EventLogger
            
            stages = StageStatus(self.conn)
            batches = BatchMetadata(self.conn)
            events = EventLogger(self.conn)
            
            # Complete stage
            stages.complete(
                batch_id=batch_id,
                stage='raw_to_jpg',
                success=success,
                files_processed=files_processed,
                files_failed=files_failed,
                error_message=error_message
            )
            
            # Update batch flags if successful
            if success and files_failed == 0:
                batches.update_completion_flags(
                    batch_id,
                    raw_to_jpg_complete=True
                )
            
            # Log completion
            severity = 'INFO' if success else 'ERROR'
            events.log_event(
                event_type='stage.completed' if success else 'stage.failed',
                message=f'Completed raw_to_jpg for {batch_id}: {files_processed} processed, {files_failed} failed',
                severity=severity,
                batch_id=batch_id,
                stage='raw_to_jpg',
                metadata={
                    'success': success,
                    'files_processed': files_processed,
                    'files_failed': files_failed
                }
            )
            
            logger.info(f"Completed {batch_id}: {files_processed} processed, {files_failed} failed")
            
        except Exception as e:
            logger.error(f"Failed to complete batch conversion: {e}")
            raise OrchestrationError(f"Failed to complete {batch_id}: {e}") from e
    
    def get_batch_progress(self, batch_id: str) -> Dict:
        """
        Get conversion progress for a batch.
        
        Parameters
        ----------
        batch_id : str
            Batch to check
        
        Returns
        -------
        dict
            Progress information:
            - batch_id
            - status
            - files_processed
            - files_failed
            - started_at
            - duration_seconds
            - files_per_second
        
        Examples
        --------
        >>> progress = db.orchestration.get_batch_progress('MD_2024-06-01')
        >>> print(f"Status: {progress['status']}")
        >>> print(f"Processed: {progress['files_processed']} files")
        """
        logger.debug(f"Getting progress for {batch_id}")
        
        query = """
            SELECT
                batch_id,
                status,
                files_processed,
                files_failed,
                started_at,
                completed_at,
                duration_seconds,
                CASE
                    WHEN files_processed > 0 AND duration_seconds > 0
                    THEN ROUND(files_processed::NUMERIC / duration_seconds, 2)
                    ELSE NULL
                END as files_per_second
            FROM processed.stage_status
            WHERE batch_id = %s
              AND stage = 'raw_to_jpg'
            ORDER BY started_at DESC
            LIMIT 1;
        """
        
        try:
            result = self.conn.fetch_one(query, (batch_id,))
            
            if result:
                return dict(result)
            else:
                # No stage status found
                return {
                    'batch_id': batch_id,
                    'status': 'not_started',
                    'files_processed': 0,
                    'files_failed': 0,
                    'started_at': None,
                    'completed_at': None,
                    'duration_seconds': None,
                    'files_per_second': None
                }
                
        except Exception as e:
            logger.error(f"Failed to get progress: {e}")
            raise QueryError(f"Failed to get progress for {batch_id}: {e}") from e
    
    def get_active_conversions(self) -> List[Dict]:
        """
        Get all currently running conversions.
        
        Returns
        -------
        list of dict
            Active conversions with progress info
        
        Examples
        --------
        >>> active = db.orchestration.get_active_conversions()
        >>> for conv in active:
        ...     print(f"{conv['batch_id']}: {conv['files_processed']} files")
        """
        logger.debug("Getting active conversions")
        
        query = """
            SELECT
                batch_id,
                status,
                files_processed,
                files_failed,
                started_at,
                job_id,
                ROUND(EXTRACT(EPOCH FROM (NOW() - started_at)))::INTEGER as elapsed_seconds,
                CASE
                    WHEN files_processed > 0 AND started_at IS NOT NULL
                    THEN ROUND(
                        files_processed::NUMERIC / 
                        GREATEST(EXTRACT(EPOCH FROM (NOW() - started_at)), 1),
                        2
                    )
                    ELSE NULL
                END as current_rate
            FROM processed.stage_status
            WHERE stage = 'raw_to_jpg'
              AND status = 'running'
            ORDER BY started_at DESC;
        """
        
        try:
            result = self.conn.fetch_all(query)
            return [dict(row) for row in result] if result else []
        except Exception as e:
            logger.error(f"Failed to get active conversions: {e}")
            raise QueryError(f"Failed to get active conversions: {e}") from e
    
    def get_failed_conversions(self, days: int = 7) -> List[Dict]:
        """
        Get recently failed conversions.
        
        Parameters
        ----------
        days : int, optional
            Look back this many days
        
        Returns
        -------
        list of dict
            Failed conversions with error info
        
        Examples
        --------
        >>> failed = db.orchestration.get_failed_conversions(days=7)
        >>> for conv in failed:
        ...     print(f"{conv['batch_id']}: {conv['error_message']}")
        """
        logger.debug(f"Getting failed conversions (days={days})")
        
        query = """
            SELECT
                batch_id,
                status,
                files_processed,
                files_failed,
                started_at,
                completed_at,
                duration_seconds,
                error_message,
                job_id
            FROM processed.stage_status
            WHERE stage = 'raw_to_jpg'
              AND status = 'failed'
              AND started_at >= NOW() - INTERVAL '%s days'
            ORDER BY started_at DESC;
        """
        
        try:
            result = self.conn.fetch_all(query, (days,))
            return [dict(row) for row in result] if result else []
        except Exception as e:
            logger.error(f"Failed to get failed conversions: {e}")
            raise QueryError(f"Failed to get failed conversions: {e}") from e
    
    def get_conversion_summary(self, days: int = 7) -> Dict:
        """
        Get summary of conversion activity.
        
        Parameters
        ----------
        days : int, optional
            Look back this many days
        
        Returns
        -------
        dict
            Summary statistics:
            - batches_in_queue
            - batches_active
            - batches_completed (last N days)
            - batches_failed (last N days)
            - total_files_converted (last N days)
            - avg_files_per_second
        
        Examples
        --------
        >>> summary = db.orchestration.get_conversion_summary(days=7)
        >>> print(f"Queue: {summary['batches_in_queue']}")
        >>> print(f"Active: {summary['batches_active']}")
        """
        logger.debug(f"Getting conversion summary (days={days})")
        
        query = """
            WITH queue AS (
                SELECT COUNT(*) as count
                FROM processed.batches
                WHERE file_count_raw > 0
                  AND (file_count_raw - COALESCE(file_count_jpg, 0)) > 0
                  AND (raw_to_jpg_complete IS NULL OR raw_to_jpg_complete = FALSE)
            ),
            active AS (
                SELECT COUNT(*) as count
                FROM processed.stage_status
                WHERE stage = 'raw_to_jpg'
                  AND status = 'running'
            ),
            recent AS (
                SELECT
                    COUNT(*) FILTER (WHERE status = 'completed') as completed,
                    COUNT(*) FILTER (WHERE status = 'failed') as failed,
                    SUM(files_processed) FILTER (WHERE status = 'completed') as total_files,
                    AVG(files_processed::NUMERIC / NULLIF(duration_seconds, 0)) 
                        FILTER (WHERE status = 'completed' AND duration_seconds > 0) as avg_rate
                FROM processed.stage_status
                WHERE stage = 'raw_to_jpg'
                  AND started_at >= NOW() - INTERVAL '%s days'
            )
            SELECT
                q.count as batches_in_queue,
                a.count as batches_active,
                COALESCE(r.completed, 0) as batches_completed,
                COALESCE(r.failed, 0) as batches_failed,
                COALESCE(r.total_files, 0) as total_files_converted,
                ROUND(COALESCE(r.avg_rate, 0), 2) as avg_files_per_second
            FROM queue q, active a, recent r;
        """
        
        try:
            result = self.conn.fetch_one(query, (days,))
            return dict(result) if result else {}
        except Exception as e:
            logger.error(f"Failed to get conversion summary: {e}")
            raise QueryError(f"Failed to get conversion summary: {e}") from e