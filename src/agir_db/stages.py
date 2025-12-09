"""
Stage execution tracking for pipeline processing.

This module manages the lifecycle of pipeline stage processing:
- Starting stages (marking as in-progress)
- Completing stages (success or failure)
- Resetting failed stages for retry
- Querying stage status

This prevents duplicate work and enables monitoring of processing jobs.
"""

import logging
import socket
from typing import Dict, List, Optional
from datetime import datetime

from psycopg2.extras import Json

from .connection import ConnectionManager
from .exceptions import (
    QueryError,
    InvalidParameterError,
    StageAlreadyInProgressError,
    StageNotStartedError,
    InvalidStageError
)


logger = logging.getLogger(__name__)


# Valid pipeline stages (must match gaps.py)
VALID_STAGES = {
    'raw_to_jpg',
    'jpg_to_metadata',
    'metadata_to_cutouts'
}

# Valid status values
VALID_STATUSES = {'in_progress', 'completed', 'failed'}


class StageStatus:
    """
    Manage execution status of pipeline stages.
    
    This class tracks which batches are being processed, who's processing them,
    and whether processing succeeded or failed. This prevents multiple workers
    from processing the same batch simultaneously.
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> import os
    >>> 
    >>> with AgirDB() as db:
    ...     batch_id = 'MD_2025-01-01'
    ...     stage = 'raw_to_jpg'
    ...     job_id = os.environ.get('SLURM_JOB_ID', 'local')
    ...     
    ...     # Start processing
    ...     db.stages.start(batch_id, stage, job_id)
    ...     
    ...     try:
    ...         # Do processing...
    ...         files_processed = 150
    ...         
    ...         # Mark as complete
    ...         db.stages.complete(
    ...             batch_id, stage,
    ...             success=True,
    ...             files_processed=files_processed
    ...         )
    ...     except Exception as e:
    ...         # Mark as failed
    ...         db.stages.complete(
    ...             batch_id, stage,
    ...             success=False,
    ...             error_message=str(e)
    ...         )
    ...         raise
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("StageStatus initialized")
    
    def _validate_stage(self, stage: str) -> None:
        """Validate that stage name is valid."""
        if stage not in VALID_STAGES:
            raise InvalidStageError(
                f"Invalid stage '{stage}'. Must be one of: {', '.join(sorted(VALID_STAGES))}"
            )
    
    def _validate_status(self, status: str) -> None:
        """Validate that status value is valid."""
        if status not in VALID_STATUSES:
            raise InvalidParameterError(
                f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}"
            )
    
    def start(
        self,
        batch_id: str,
        stage: str,
        job_id: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Mark a stage as started (in-progress).
        
        This creates or updates a stage status record to indicate that processing
        has begun. If the stage is already in-progress, raises an exception to
        prevent duplicate work.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        stage : str
            Pipeline stage name
        job_id : str, optional
            Job identifier (e.g., SLURM job ID, process ID)
        metadata : dict, optional
            Additional metadata to store (config, parameters, etc.)
        
        Raises
        ------
        InvalidStageError
            If stage name is invalid
        StageAlreadyInProgressError
            If stage is already being processed
        QueryError
            If database operation fails
        
        Examples
        --------
        >>> import os
        >>> job_id = os.environ.get('SLURM_JOB_ID', 'local')
        >>> db.stages.start('MD_2025-01-01', 'raw_to_jpg', job_id)
        """
        self._validate_stage(stage)
        
        # Check if already in progress
        current = self.get_status(batch_id, stage)
        if current and current['status'] == 'in_progress':
            raise StageAlreadyInProgressError(
                f"Stage '{stage}' for batch '{batch_id}' is already in progress "
                f"(job_id: {current['job_id']}, started: {current['started_at']})"
            )
        
        hostname = socket.gethostname()
        
        query = """
            INSERT INTO processed.stage_status (
                batch_id, stage, status, job_id, hostname, started_at, metadata
            ) VALUES (
                %s, %s, 'in_progress', %s, %s, NOW(), %s
            )
            ON CONFLICT (batch_id, stage) DO UPDATE SET
                status = 'in_progress',
                job_id = EXCLUDED.job_id,
                hostname = EXCLUDED.hostname,
                started_at = NOW(),
                completed_at = NULL,
                duration_seconds = NULL,
                success = NULL,
                files_processed = NULL,
                files_failed = NULL,
                error_message = NULL,
                metadata = EXCLUDED.metadata;
        """
        
        logger.info(f"Starting stage '{stage}' for batch '{batch_id}' (job_id={job_id})")
        
        try:
            # Wrap metadata dict with Json() for JSONB conversion
            metadata_json = Json(metadata) if metadata is not None else None
            
            self.conn.execute(query, (batch_id, stage, job_id, hostname, metadata_json))
            logger.info(f"Stage '{stage}' marked as in-progress for batch '{batch_id}'")
        except Exception as e:
            logger.error(f"Failed to start stage: {e}")
            raise QueryError(f"Failed to start stage '{stage}' for batch '{batch_id}': {e}") from e
    
    def complete(
        self,
        batch_id: str,
        stage: str,
        success: bool,
        files_processed: Optional[int] = None,
        files_failed: Optional[int] = None,
        error_message: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Mark a stage as completed (success or failure).
        
        This updates the stage status to indicate processing has finished.
        Duration is automatically calculated from start time.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        stage : str
            Pipeline stage name
        success : bool
            True if processing succeeded, False if failed
        files_processed : int, optional
            Number of files successfully processed
        files_failed : int, optional
            Number of files that failed
        error_message : str, optional
            Error details if failed (required if success=False)
        metadata : dict, optional
            Additional metadata to store
        
        Raises
        ------
        InvalidStageError
            If stage name is invalid
        StageNotStartedError
            If stage was never started
        QueryError
            If database operation fails
        
        Examples
        --------
        >>> # Success
        >>> db.stages.complete(
        ...     'MD_2025-01-01', 'raw_to_jpg',
        ...     success=True,
        ...     files_processed=150
        ... )
        
        >>> # Failure
        >>> db.stages.complete(
        ...     'MD_2025-01-01', 'raw_to_jpg',
        ...     success=False,
        ...     files_processed=100,
        ...     files_failed=50,
        ...     error_message="Conversion failed for 50 files"
        ... )
        """
        self._validate_stage(stage)
        
        # Check if stage was started
        current = self.get_status(batch_id, stage)
        if not current:
            raise StageNotStartedError(
                f"Stage '{stage}' for batch '{batch_id}' was never started"
            )
        
        status = 'completed' if success else 'failed'
        
        query = """
            UPDATE processed.stage_status
            SET
                status = %s,
                completed_at = NOW(),
                success = %s,
                files_processed = %s,
                files_failed = %s,
                error_message = %s,
                metadata = COALESCE(%s, metadata)
            WHERE batch_id = %s AND stage = %s;
        """
        
        logger.info(f"Completing stage '{stage}' for batch '{batch_id}' "
                   f"(success={success}, files_processed={files_processed})")
        
        try:
            # Wrap metadata dict with Json() for JSONB conversion
            metadata_json = Json(metadata) if metadata is not None else None
            
            self.conn.execute(
                query,
                (status, success, files_processed, files_failed, error_message,
                 metadata_json, batch_id, stage)
            )
            logger.info(f"Stage '{stage}' marked as {status} for batch '{batch_id}'")
        except Exception as e:
            logger.error(f"Failed to complete stage: {e}")
            raise QueryError(f"Failed to complete stage '{stage}' for batch '{batch_id}': {e}") from e
    
    def reset(self, batch_id: str, stage: str) -> None:
        """
        Reset a stage status to allow retry.
        
        This deletes the stage status record, clearing any failed or stuck
        status and allowing the stage to be reprocessed.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        stage : str
            Pipeline stage name
        
        Raises
        ------
        InvalidStageError
            If stage name is invalid
        QueryError
            If database operation fails
        
        Examples
        --------
        >>> # Reset a failed stage
        >>> db.stages.reset('MD_2025-01-01', 'raw_to_jpg')
        """
        self._validate_stage(stage)
        
        query = """
            DELETE FROM processed.stage_status
            WHERE batch_id = %s AND stage = %s;
        """
        
        logger.info(f"Resetting stage '{stage}' for batch '{batch_id}'")
        
        try:
            self.conn.execute(query, (batch_id, stage))
            logger.info(f"Stage '{stage}' reset for batch '{batch_id}'")
        except Exception as e:
            logger.error(f"Failed to reset stage: {e}")
            raise QueryError(f"Failed to reset stage '{stage}' for batch '{batch_id}': {e}") from e
    
    def get_status(self, batch_id: str, stage: str) -> Optional[Dict]:
        """
        Get current status of a stage.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        stage : str
            Pipeline stage name
        
        Returns
        -------
        dict or None
            Stage status record with all fields, or None if not found:
            - batch_id: Batch identifier
            - stage: Stage name
            - status: 'in_progress', 'completed', or 'failed'
            - job_id: Job identifier
            - hostname: Processing host
            - started_at: Start timestamp
            - completed_at: Completion timestamp (if completed)
            - duration_seconds: Processing duration (if completed)
            - success: Boolean success flag (if completed)
            - files_processed: Number of files processed
            - files_failed: Number of files failed
            - error_message: Error details (if failed)
            - metadata: Additional metadata
        
        Raises
        ------
        InvalidStageError
            If stage name is invalid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> status = db.stages.get_status('MD_2025-01-01', 'raw_to_jpg')
        >>> if status:
        ...     print(f"Status: {status['status']}")
        ...     print(f"Started: {status['started_at']}")
        """
        self._validate_stage(stage)
        
        query = """
            SELECT *
            FROM processed.stage_status
            WHERE batch_id = %s AND stage = %s;
        """
        
        logger.debug(f"Getting status for batch '{batch_id}', stage '{stage}'")
        
        try:
            result = self.conn.fetch_one(query, (batch_id, stage))
            return result
        except Exception as e:
            logger.error(f"Failed to get stage status: {e}")
            raise QueryError(
                f"Failed to get status for batch '{batch_id}', stage '{stage}': {e}"
            ) from e
    
    def get_in_progress(self, stage: Optional[str] = None) -> List[Dict]:
        """
        Get all in-progress stages, optionally filtered by stage type.
        
        Useful for monitoring which jobs are currently running and detecting
        stuck jobs that have been running too long.
        
        Parameters
        ----------
        stage : str, optional
            Filter to specific stage, or None for all stages
        
        Returns
        -------
        list of dict
            In-progress stage records with elapsed time:
            - batch_id: Batch identifier
            - stage: Stage name
            - job_id: Job identifier
            - hostname: Processing host
            - started_at: Start timestamp
            - elapsed_seconds: Time elapsed since start
            - elapsed_hours: Time elapsed in hours
            - metadata: Additional metadata
        
        Raises
        ------
        InvalidStageError
            If stage name is invalid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> # Get all in-progress stages
        >>> in_progress = db.stages.get_in_progress()
        >>> for stage in in_progress:
        ...     if stage['elapsed_hours'] > 24:
        ...         print(f"Stuck job: {stage['batch_id']} / {stage['stage']}")
        
        >>> # Get in-progress for specific stage
        >>> raw_jobs = db.stages.get_in_progress('raw_to_jpg')
        """
        if stage is not None:
            self._validate_stage(stage)
        
        query = "SELECT * FROM processed.in_progress_stages"
        
        if stage is not None:
            query += " WHERE stage = %s"
            params = (stage,)
        else:
            params = None
        
        query += " ORDER BY started_at;"
        
        logger.info(f"Getting in-progress stages (stage={stage})")
        
        try:
            results = self.conn.fetch_all(query, params)
            logger.debug(f"Found {len(results)} in-progress stage(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get in-progress stages: {e}")
            raise QueryError(f"Failed to get in-progress stages: {e}") from e
    
    def get_failed(self, stage: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        """
        Get failed stages that may need retry.
        
        Parameters
        ----------
        stage : str, optional
            Filter to specific stage, or None for all stages
        limit : int, optional
            Maximum number of results to return
        
        Returns
        -------
        list of dict
            Failed stage records ordered by completion time (newest first)
        
        Raises
        ------
        InvalidStageError
            If stage name is invalid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> # Get recent failures
        >>> failures = db.stages.get_failed(limit=10)
        >>> for failure in failures:
        ...     print(f"{failure['batch_id']}: {failure['error_message']}")
        """
        if stage is not None:
            self._validate_stage(stage)
        
        query = "SELECT * FROM processed.failed_stages"
        
        if stage is not None:
            query += " WHERE stage = %s"
            params = (stage,)
        else:
            params = None
        
        query += " ORDER BY completed_at DESC"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.info(f"Getting failed stages (stage={stage}, limit={limit})")
        
        try:
            results = self.conn.fetch_all(query, params)
            logger.debug(f"Found {len(results)} failed stage(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get failed stages: {e}")
            raise QueryError(f"Failed to get failed stages: {e}") from e
    
    def get_batch_status(self, batch_id: str) -> List[Dict]:
        """
        Get status of all stages for a specific batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        
        Returns
        -------
        list of dict
            All stage status records for this batch
        
        Raises
        ------
        QueryError
            If database query fails
        
        Examples
        --------
        >>> stages = db.stages.get_batch_status('MD_2025-01-01')
        >>> for stage in stages:
        ...     print(f"{stage['stage']}: {stage['status']}")
        """
        query = """
            SELECT *
            FROM processed.stage_status
            WHERE batch_id = %s
            ORDER BY started_at;
        """
        
        logger.info(f"Getting all stage status for batch '{batch_id}'")
        
        try:
            results = self.conn.fetch_all(query, (batch_id,))
            logger.debug(f"Found {len(results)} stage(s) for batch '{batch_id}'")
            return results
        except Exception as e:
            logger.error(f"Failed to get batch status: {e}")
            raise QueryError(f"Failed to get batch status for '{batch_id}': {e}") from e