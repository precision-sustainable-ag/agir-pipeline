"""
Transfer management for Globus file transfers.

This module provides methods for tracking and managing file transfers
between storage sites (JUNO, CERES, NCSU, etc.) using Globus.
"""
import re
import logging
import socket
import getpass
import subprocess
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from psycopg2.extras import Json

from .connection import ConnectionManager
from .exceptions import (
    GlobusError,
    QueryError,
    InvalidParameterError,
    TransferNotFoundError,
    TransferAlreadyInProgressError,
    BatchNotFoundError,
    ValidationError
)


logger = logging.getLogger(__name__)


# Valid transfer statuses
VALID_TRANSFER_STATUSES = {
    'pending',
    'in_progress',
    'completed',
    'failed',
    'cancelled'
}


class TransferManager:
    """
    Manage Globus transfer operations.
    
    This class provides methods for:
    - Initiating transfers
    - Tracking transfer progress
    - Querying transfer status
    - Handling failed transfers
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Start a transfer
    ...     transfer_id = db.transfers.start_transfer(
    ...         batch_id='MD_2025-01-01',
    ...         source_site='JUNO',
    ...         destination_site='CERES',
    ...         source_path='/juno/md/MD_2025-01-01',
    ...         destination_path='/ceres/md/MD_2025-01-01'
    ...     )
    ...     
    ...     # Update with Globus task ID
    ...     db.transfers.update_globus_task(transfer_id, 'abc-123-def')
    ...     
    ...     # Mark as complete
    ...     db.transfers.complete(transfer_id, success=True, bytes_transferred=1000000)
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        self.hostname = socket.gethostname()
        self.username = getpass.getuser()
        logger.debug("TransferManager initialized")
    
    def _validate_status(self, status: str) -> None:
        """Validate that transfer status is valid."""
        if status not in VALID_TRANSFER_STATUSES:
            raise InvalidParameterError(
                f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_TRANSFER_STATUSES))}"
            )
    
    def start_transfer(
        self,
        batch_id: str,
        source_site: str,
        destination_site: str,
        source_path: Optional[str] = None,
        destination_path: Optional[str] = None,
        file_count: Optional[int] = None,
        bytes_total: Optional[int] = None,
        job_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        validate_batch: bool = True
    ) -> int:
        """
        Start a new transfer (creates pending transfer record).
        
        Parameters
        ----------
        batch_id : str
            Batch being transferred
        source_site : str
            Source site (e.g., 'JUNO', 'CERES')
        destination_site : str
            Destination site
        source_path : str, optional
            Full source path
        destination_path : str, optional
            Full destination path
        file_count : int, optional
            Number of files to transfer
        bytes_total : int, optional
            Total bytes to transfer
        job_id : str, optional
            SLURM job ID or workflow ID
        metadata : dict, optional
            Additional metadata
        validate_batch: bool, optional
            Whether to validate that the batch exists in processed.batches before starting the transfer (default: True)
        
        Returns
        -------
        int
            transfer_id of created transfer record
        
        Raises
        ------
        BatchNotFoundError
            If batch_id doesn't exist in processed.batches
        QueryError
            If insert fails
        
        Examples
        --------
        >>> transfer_id = db.transfers.start_transfer(
        ...     batch_id='MD_2025-01-01',
        ...     source_site='JUNO',
        ...     destination_site='CERES',
        ...     source_path='/juno/md/MD_2025-01-01',
        ...     destination_path='/ceres/md/MD_2025-01-01',
        ...     file_count=150,
        ...     bytes_total=3750000000
        ... )
        """
        # Verify batch exists
        batch_query = "SELECT 1 FROM processed.batches WHERE batch_id = %s;"
        if not self.conn.fetch_one(batch_query, (batch_id,)):
            raise BatchNotFoundError(f"Batch {batch_id} not found in processed.batches.\
                                      You may need to sync the batch first using db.inventory.sync_batch(batch_id)")
        
        query = """
            INSERT INTO processed.transfers (
                batch_id, source_site, destination_site,
                source_path, destination_path,
                status, file_count, bytes_total,
                job_id, requested_by, metadata
            ) VALUES (
                %s, %s, %s,
                %s, %s,
                'pending', %s, %s,
                %s, %s, %s
            )
            RETURNING transfer_id;
        """
        
        logger.info(f"Starting transfer for batch {batch_id}: {source_site} → {destination_site}")
        
        try:
            metadata_json = Json(metadata) if metadata is not None else None
            
            result = self.conn.fetch_one(
                query,
                (batch_id, source_site, destination_site,
                 source_path, destination_path,
                 file_count, bytes_total,
                 job_id, self.username, metadata_json)
            )
            
            transfer_id = result['transfer_id']
            logger.info(f"Transfer created: transfer_id={transfer_id}")
            return transfer_id
            
        except Exception as e:
            logger.error(f"Failed to start transfer: {e}")
            raise QueryError(f"Failed to start transfer for batch {batch_id}: {e}") from e
    
    def update_globus_task(
        self,
        transfer_id: int,
        globus_task_id: str
    ) -> None:
        """
        Update transfer with Globus task ID and mark as in-progress.
        
        Parameters
        ----------
        transfer_id : int
            Transfer identifier
        globus_task_id : str
            Globus task ID from globus transfer command
        
        Examples
        --------
        >>> db.transfers.update_globus_task(123, 'abc-123-def-456')
        """
        query = """
            UPDATE processed.transfers
            SET 
                globus_task_id = %s,
                status = 'in_progress',
                started_at = NOW()
            WHERE transfer_id = %s;
        """
        
        logger.info(f"Updating transfer {transfer_id} with Globus task ID: {globus_task_id}")
        
        try:
            self.conn.execute(query, (globus_task_id, transfer_id))
            logger.debug(f"Transfer {transfer_id} marked as in-progress")
        except Exception as e:
            logger.error(f"Failed to update Globus task: {e}")
            raise QueryError(f"Failed to update transfer {transfer_id}: {e}") from e
    
    def update_progress(
        self,
        transfer_id: int,
        files_transferred: Optional[int] = None,
        bytes_transferred: Optional[int] = None,
        transfer_rate_mbps: Optional[float] = None,
        globus_status: Optional[str] = None
    ) -> None:
        """
        Update transfer progress metrics.
        
        Parameters
        ----------
        transfer_id : int
            Transfer identifier
        files_transferred : int, optional
            Number of files transferred so far
        bytes_transferred : int, optional
            Bytes transferred so far
        transfer_rate_mbps : float, optional
            Current transfer rate in MB/s
        globus_status : str, optional
            Raw Globus status string
        
        Examples
        --------
        >>> db.transfers.update_progress(
        ...     123,
        ...     files_transferred=75,
        ...     bytes_transferred=1875000000,
        ...     transfer_rate_mbps=125.5
        ... )
        """
        # Build dynamic UPDATE query
        updates = []
        params = []
        
        if files_transferred is not None:
            updates.append("files_transferred = %s")
            params.append(files_transferred)
        
        if bytes_transferred is not None:
            updates.append("bytes_transferred = %s")
            params.append(bytes_transferred)
        
        if transfer_rate_mbps is not None:
            updates.append("transfer_rate_mbps = %s")
            params.append(transfer_rate_mbps)
        
        if globus_status is not None:
            updates.append("globus_status = %s")
            params.append(globus_status)
        
        if not updates:
            logger.warning("No progress updates provided")
            return
        
        params.append(transfer_id)
        
        query = f"""
            UPDATE processed.transfers
            SET {', '.join(updates)}
            WHERE transfer_id = %s;
        """
        
        logger.debug(f"Updating progress for transfer {transfer_id}")
        
        try:
            self.conn.execute(query, tuple(params))
        except Exception as e:
            logger.error(f"Failed to update progress: {e}")
            raise QueryError(f"Failed to update progress for transfer {transfer_id}: {e}") from e
    
    def complete(
        self,
        transfer_id: int,
        success: bool,
        files_transferred: Optional[int] = None,
        bytes_transferred: Optional[int] = None,
        error_message: Optional[str] = None
    ) -> None:
        """
        Mark a transfer as completed or failed.
        
        Parameters
        ----------
        transfer_id : int
            Transfer identifier
        success : bool
            Whether transfer completed successfully
        files_transferred : int, optional
            Final number of files transferred
        bytes_transferred : int, optional
            Final bytes transferred
        error_message : str, optional
            Error message if failed
        
        Examples
        --------
        >>> # Successful completion
        >>> db.transfers.complete(
        ...     123,
        ...     success=True,
        ...     files_transferred=150,
        ...     bytes_transferred=3750000000
        ... )
        
        >>> # Failed transfer
        >>> db.transfers.complete(
        ...     124,
        ...     success=False,
        ...     error_message="Connection timeout"
        ... )
        """
        status = 'completed' if success else 'failed'
        
        query = """
            UPDATE processed.transfers
            SET 
                status = %s,
                completed_at = NOW(),
                files_transferred = COALESCE(%s, files_transferred),
                bytes_transferred = COALESCE(%s, bytes_transferred),
                error_message = %s
            WHERE transfer_id = %s;
        """
        
        logger.info(f"Completing transfer {transfer_id}: success={success}")
        
        try:
            self.conn.execute(
                query,
                (status, files_transferred, bytes_transferred, error_message, transfer_id)
            )
            logger.info(f"Transfer {transfer_id} marked as {status}")
        except Exception as e:
            logger.error(f"Failed to complete transfer: {e}")
            raise QueryError(f"Failed to complete transfer {transfer_id}: {e}") from e
    
    def cancel(self, transfer_id: int, reason: Optional[str] = None) -> None:
        """
        Cancel a pending or in-progress transfer.
        
        Parameters
        ----------
        transfer_id : int
            Transfer identifier
        reason : str, optional
            Reason for cancellation
        
        Examples
        --------
        >>> db.transfers.cancel(123, reason="User requested cancellation")
        """
        query = """
            UPDATE processed.transfers
            SET 
                status = 'cancelled',
                completed_at = NOW(),
                error_message = %s
            WHERE transfer_id = %s
            AND status IN ('pending', 'in_progress');
        """
        
        logger.info(f"Cancelling transfer {transfer_id}")
        
        try:
            self.conn.execute(query, (reason, transfer_id))
            logger.debug(f"Transfer {transfer_id} cancelled")
        except Exception as e:
            logger.error(f"Failed to cancel transfer: {e}")
            raise QueryError(f"Failed to cancel transfer {transfer_id}: {e}") from e
    
    def retry(self, transfer_id: int) -> int:
        """
        Create a new transfer record for retrying a failed transfer.
        
        Parameters
        ----------
        transfer_id : int
            Original transfer identifier that failed
        
        Returns
        -------
        int
            New transfer_id for retry attempt
        
        Examples
        --------
        >>> # Original transfer failed
        >>> new_transfer_id = db.transfers.retry(123)
        >>> print(f"Retry created: {new_transfer_id}")
        """
        # Get original transfer details
        get_query = """
            SELECT 
                batch_id, source_site, destination_site,
                source_path, destination_path, file_count, bytes_total,
                job_id, metadata, retry_count
            FROM processed.transfers
            WHERE transfer_id = %s;
        """
        
        try:
            original = self.conn.fetch_one(get_query, (transfer_id,))
            
            if not original:
                raise TransferNotFoundError(f"Transfer {transfer_id} not found")
            
        except TransferNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to get original transfer: {e}")
            raise QueryError(f"Failed to get transfer {transfer_id}: {e}") from e
        
        # Create new transfer with incremented retry count
        insert_query = """
            INSERT INTO processed.transfers (
                batch_id, source_site, destination_site,
                source_path, destination_path,
                status, file_count, bytes_total,
                job_id, requested_by, metadata, retry_count
            ) VALUES (
                %s, %s, %s,
                %s, %s,
                'pending', %s, %s,
                %s, %s, %s, %s
            )
            RETURNING transfer_id;
        """
        
        logger.info(f"Creating retry for transfer {transfer_id}")
        
        try:
            new_retry_count = original['retry_count'] + 1
            
            result = self.conn.fetch_one(
                insert_query,
                (original['batch_id'], original['source_site'], original['destination_site'],
                 original['source_path'], original['destination_path'],
                 original['file_count'], original['bytes_total'],
                 original['job_id'], self.username, original['metadata'], new_retry_count)
            )
            
            new_transfer_id = result['transfer_id']
            logger.info(f"Retry created: transfer_id={new_transfer_id} (retry #{new_retry_count})")
            return new_transfer_id
            
        except Exception as e:
            logger.error(f"Failed to create retry: {e}")
            raise QueryError(f"Failed to create retry for transfer {transfer_id}: {e}") from e
    
    def get_by_id(self, transfer_id: int) -> Optional[Dict]:
        """
        Get transfer record by ID.
        
        Parameters
        ----------
        transfer_id : int
            Transfer identifier
        
        Returns
        -------
        dict or None
            Transfer record, or None if not found
        """
        query = "SELECT * FROM processed.transfers WHERE transfer_id = %s;"
        
        logger.debug(f"Getting transfer: {transfer_id}")
        
        try:
            result = self.conn.fetch_one(query, (transfer_id,))
            return result
        except Exception as e:
            logger.error(f"Failed to get transfer: {e}")
            raise QueryError(f"Failed to get transfer {transfer_id}: {e}") from e
    
    def get_by_batch(
        self,
        batch_id: str,
        status: Optional[str] = None
    ) -> List[Dict]:
        """
        Get transfers for a specific batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        status : str, optional
            Filter by status
        
        Returns
        -------
        list of dict
            Transfer records
        
        Examples
        --------
        >>> # All transfers for batch
        >>> transfers = db.transfers.get_by_batch('MD_2025-01-01')
        
        >>> # Only in-progress transfers
        >>> active = db.transfers.get_by_batch('MD_2025-01-01', status='in_progress')
        """
        if status is not None:
            self._validate_status(status)
        
        query = "SELECT * FROM processed.transfers WHERE batch_id = %s"
        params = [batch_id]
        
        if status is not None:
            query += " AND status = %s"
            params.append(status)
        
        query += " ORDER BY created_at DESC;"
        
        logger.debug(f"Getting transfers for batch {batch_id} (status={status})")
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Found {len(results)} transfer(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get transfers by batch: {e}")
            raise QueryError(f"Failed to get transfers for batch {batch_id}: {e}") from e
    
    def get_active(self, limit: Optional[int] = None) -> List[Dict]:
        """
        Get currently active (in-progress) transfers.
        
        Parameters
        ----------
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Active transfer records with progress
        
        Examples
        --------
        >>> active = db.transfers.get_active(limit=10)
        >>> for t in active:
        ...     print(f"{t['batch_id']}: {t['percent_complete']}% complete")
        """
        query = "SELECT * FROM processed.active_transfers"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug("Getting active transfers")
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Found {len(results)} active transfer(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get active transfers: {e}")
            raise QueryError(f"Failed to get active transfers: {e}") from e
    
    def get_failed(self, limit: Optional[int] = None) -> List[Dict]:
        """
        Get failed transfers that may need retry.
        
        Parameters
        ----------
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Failed transfer records
        
        Examples
        --------
        >>> failed = db.transfers.get_failed(limit=10)
        >>> for t in failed:
        ...     print(f"{t['batch_id']}: {t['error_message']}")
        """
        query = "SELECT * FROM processed.failed_transfers"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug("Getting failed transfers")
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Found {len(results)} failed transfer(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get failed transfers: {e}")
            raise QueryError(f"Failed to get failed transfers: {e}") from e
    
    def get_pending(self, limit: Optional[int] = None) -> List[Dict]:
        """
        Get pending transfers in queue.
        
        Parameters
        ----------
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Pending transfer records
        
        Examples
        --------
        >>> pending = db.transfers.get_pending(limit=5)
        >>> for t in pending:
        ...     print(f"Queued: {t['batch_id']} ({t['requested_at']})")
        """
        query = "SELECT * FROM processed.pending_transfers"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug("Getting pending transfers")
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Found {len(results)} pending transfer(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get pending transfers: {e}")
            raise QueryError(f"Failed to get pending transfers: {e}") from e
        
    def get_batches_needing_juno_transfer(
        self,
        source_site: Optional[str] = None,
        data_state: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get batches that need to be transferred to JUNO.
        
        Queries the report.missing_on_juno view to find batches where files
        exist at source sites but are missing from JUNO.
        
        Parameters
        ----------
        source_site : str, optional
            Filter by source site (e.g., 'NCSU', 'CERES')
        data_state : str, optional
            Filter by data_state (e.g., 'developed_jpg', 'upload_raw')
        limit : int, optional
            Maximum number of batches to return
        
        Returns
        -------
        List[Dict]
            List of batches needing transfer with metadata:
            - batch_id: Batch identifier
            - site: Source site
            - storge_root: LTS root on source
            - data_state: Data state (upload_raw, developed_jpg, etc.)
            - file_count: Number of files to transfer
            - total_bytes: Total size in bytes
        
        Raises
        ------
        QueryError
            If database query fails
        ValidationError
            If parameters are invalid
        
        Examples
        --------
        >>> # Get all NCSU developed_jpg batches needing transfer
        >>> batches = db.transfers.get_batches_needing_juno_transfer(
        ...     source_site='NCSU',
        ...     data_state='developed_jpg'
        ... )
        
        >>> # Get top 10 batches needing any transfer
        >>> batches = db.transfers.get_batches_needing_juno_transfer(limit=10)
        """
        logger.info(
            f"Getting batches needing JUNO transfer: "
            f"site={source_site}, data_state={data_state}, limit={limit}"
        )
        
        # Build query
        query = """
        SELECT 
            batch_id,
            site,
            storge_root,
            data_state,
            endpoint,
            COUNT(*) AS file_count,
            SUM(size_bytes) AS total_bytes
        FROM report.missing_on_juno
        WHERE 1=1
        """

        if source_site:
            query += f" AND site = '{source_site}'"
        
        if data_state:
            query += f" AND data_state = '{data_state}'"
        
        query += """
        GROUP BY batch_id, site, storage_root, data_state, endpoint
        ORDER BY batch_id
        """
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        try:
            results = self.conn.fetch_all(query)
            logger.info(f"Found {len(results)} batches needing JUNO transfer")
            return [dict(row) for row in results]
            
        except Exception as e:
            logger.error(f"Failed to query batches needing transfer: {e}")
            raise QueryError(f"Failed to get batches needing JUNO transfer: {e}")
    
    def get_files_needing_juno_transfer(
        self,
        batch_id: str,
        source_site: Optional[str] = None,
        data_state: Optional[str] = None
    ) -> List[Dict]:
        """
        Get specific files in a batch that need transfer to JUNO.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        source_site : str, optional
            Filter by source site
        data_state : str, optional
            Filter by data_state
        
        Returns
        -------
        List[Dict]
            List of files needing transfer with full metadata
        
        Raises
        ------
        QueryError
            If database query fails
        ValidationError
            If batch_id is invalid
        
        Examples
        --------
        >>> files = db.transfers.get_files_needing_juno_transfer(
        ...     batch_id='MD_2025-01-01',
        ...     source_site='NCSU',
        ...     data_state='developed_jpg'
        ... )
        """
        if not batch_id:
            raise ValidationError("batch_id is required")
        
        logger.info(f"Getting files needing transfer for batch: {batch_id}")
        
        query = f"""
        SELECT 
            file_id,
            endpoint,
            site,
            storage_root,
            rel_path,
            parent_dir,
            file_name,
            file_ext,
            size_bytes,
            checksum,
            batch_id,
            batch_state,
            batch_date,
            data_state
        FROM report.missing_on_juno
        WHERE batch_id = '{batch_id}'
        """
        
        if source_site:
            query += f" AND site = '{source_site}'"
        
        if data_state:
            query += f" AND data_state = '{data_state}'"
        
        query += " ORDER BY rel_path, file_name"
        
        try:
            results = self.conn.fetch_all(query)
            
            logger.info(f"Found {len(results)} files needing transfer")
            return [dict(row) for row in results]
            
        except Exception as e:
            logger.error(f"Failed to query files needing transfer: {e}")
            raise QueryError(f"Failed to get files needing transfer: {e}") from e

    def execute_transfer(
        self,
        batch_id: str,
        data_state: str,
        dest_endpoint: str,
        dest_root: str,
        dest_site: str,
        source_site: Optional[str] = None,
        recursive: bool = True,
        dry_run: bool = False,
        label: Optional[str] = None,
        job_id: Optional[str] = None
    ) -> int:
        """
        Execute a Globus transfer for a batch from source to destination.
        
        This method:
        1. Queries database to find source site details
        2. Builds Globus transfer command
        3. Executes the transfer via subprocess
        4. Parses the Globus task ID
        5. Creates and updates transfer record in database
        
        Parameters
        ----------
        batch_id : str
            Batch identifier to transfer
        data_state : str
            Data state to transfer (e.g., 'upload_raw', 'developed_jpg')
        dest_endpoint : str
            Globus endpoint ID for destination
        dest_root : str
            Root path on destination where batch should be copied
        dest_site : str
            Destination site identifier (e.g., 'JUNO', 'CERES', 'NCSU')
        source_site : str, optional
            Source site filter (e.g., 'NCSU', 'CERES', 'JUNO').
            If None, uses the first available source.
        recursive : bool, default=True
            Whether to use --recursive flag for directory transfer
        dry_run : bool, default=False
            If True, prints command without executing
        label : str, optional
            Custom label for Globus transfer.
            If None, generates label from batch_id and data_state.
        job_id : str, optional
            SLURM job ID or workflow identifier
        
        Returns
        -------
        int
            Transfer ID in processed.transfers table
        
        Raises
        ------
        ValidationError
            If batch_id, data_state, or required parameters are invalid
        QueryError
            If database query fails or no source found
        GlobusError
            If Globus transfer command fails
        
        Examples
        --------
        >>> # Execute transfer from NCSU to JUNO
        >>> transfer_id = db.transfers.execute_transfer(
        ...     batch_id='MD_2025-01-01',
        ...     data_state='developed_jpg',
        ...     dest_endpoint='904c2108-90cf-11e8-9672-0a6d4e044368',
        ...     dest_root='/LTS/project/dash_agir/developed_jpg',
        ...     dest_site='JUNO'
        ... )
        
        >>> # Transfer from JUNO to CERES
        >>> transfer_id = db.transfers.execute_transfer(
        ...     batch_id='MD_2025-01-01',
        ...     data_state='developed_jpg',
        ...     dest_endpoint='ceres-endpoint-id',
        ...     dest_root='/project/agir/developed_jpg',
        ...     dest_site='CERES',
        ...     source_site='JUNO'
        ... )
        
        >>> # Dry run to preview command
        >>> transfer_id = db.transfers.execute_transfer(
        ...     batch_id='MD_2025-01-01',
        ...     data_state='upload_raw',
        ...     dest_endpoint='904c2108-90cf-11e8-9672-0a6d4e044368',
        ...     dest_root='/LTS/project/dash_agir/semifield-upload',
        ...     dest_site='JUNO',
        ...     dry_run=True
        ... )
        
        >>> # Transfer from specific source site with job tracking
        >>> transfer_id = db.transfers.execute_transfer(
        ...     batch_id='MD_2025-01-01',
        ...     data_state='developed_jpg',
        ...     dest_endpoint='904c2108-90cf-11e8-9672-0a6d4e044368',
        ...     dest_root='/LTS/project/dash_agir/developed_jpg',
        ...     dest_site='JUNO',
        ...     source_site='NCSU',
        ...     job_id='slurm-12345'
        ... )
        """
        # Validate inputs
        if not batch_id:
            raise ValidationError("batch_id is required")
        if not data_state:
            raise ValidationError("data_state is required")
        if not dest_endpoint:
            raise ValidationError("dest_endpoint is required")
        if not dest_root:
            raise ValidationError("dest_root is required")
        if not dest_site:
            raise ValidationError("dest_site is required")
        
        logger.info(
            f"Executing transfer: batch={batch_id}, data_state={data_state}, "
            f"source={source_site or 'auto'} -> dest={dest_site}, dry_run={dry_run}"
        )
        
        # Query database to find source
        source = self._get_source_for_transfer(batch_id, data_state, source_site)
        
        if not source:
            raise QueryError(
                f"No source found for batch {batch_id} with data_state={data_state}"
                + (f" at site={source_site}" if source_site else "")
            )
        
        # Build paths
        source_path = f"{source['storage_root'].rstrip('/')}/{batch_id}"
        dest_path = f"{dest_root.rstrip('/')}/{batch_id}"
        
        # Generate label if not provided
        if label is None:
            label = f"agir_{data_state}_{batch_id}_{source['site']}_to_{dest_site}"
        
        # Build Globus command
        cmd = self._build_globus_command(
            source_endpoint=source['endpoint'],
            dest_endpoint=dest_endpoint,
            source_path=source_path,
            dest_path=dest_path,
            label=label,
            recursive=recursive
        )
        
        logger.info(f"Globus command: {' '.join(cmd)}")
        
        # Create pending transfer record
        transfer_id = self.start_transfer(
            batch_id=batch_id,
            source_site=source['site'],
            destination_site=dest_site,
            source_path=source_path,
            destination_path=dest_path,
            job_id=job_id,
            metadata={
                'data_state': data_state,
                'source_endpoint': source['endpoint'],
                'dest_endpoint': dest_endpoint,
                'storge_root': source.get('storge_root'),
                'command': ' '.join(cmd)
            },
            validate_batch=False
        )
        
        logger.info(f"Created transfer record: transfer_id={transfer_id}")
        
        # Execute Globus command (unless dry run)
        if dry_run:
            logger.info(f"[DRY RUN] Would execute: {' '.join(cmd)}")
            return transfer_id
        
        try:
            # Run Globus transfer command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=60
            )
            
            # Parse Globus task ID from output
            globus_task_id = self._parse_globus_task_id(result.stdout)
            
            if not globus_task_id:
                raise GlobusError(
                    f"Failed to parse Globus task ID from output: {result.stdout}"
                )
            
            logger.info(f"Globus transfer submitted: task_id={globus_task_id}")
            
            # Update transfer record with Globus task ID
            self.update_globus_task(transfer_id, globus_task_id)
            
            logger.info(f"Transfer {transfer_id} updated with Globus task {globus_task_id}")
            
            return transfer_id
            
        except subprocess.TimeoutExpired as e:
            error_msg = f"Globus transfer command timed out after 60s"
            logger.error(error_msg)
            self.complete(transfer_id, success=False, error_message=error_msg)
            raise GlobusError(error_msg) from e
            
        except subprocess.CalledProcessError as e:
            error_msg = f"Globus transfer failed: {e.stderr}"
            logger.error(error_msg)
            self.complete(transfer_id, success=False, error_message=error_msg)
            raise GlobusError(error_msg) from e
            
        except Exception as e:
            error_msg = f"Unexpected error during transfer: {str(e)}"
            logger.error(error_msg)
            self.complete(transfer_id, success=False, error_message=error_msg)
            raise GlobusError(error_msg) from e
        


    def _get_source_for_transfer(
        self,
        batch_id: str,
        data_state: str,
        source_site: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Query database to find source details for transfer.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        data_state : str
            Data state to transfer
        source_site : str, optional
            Preferred source site
        
        Returns
        -------
        dict or None
            Source details with keys: endpoint, site, storage_root, batch_id
        """
        query = """
        SELECT DISTINCT
            endpoint,
            site,
            storge_root,

            batch_id
        FROM source.globus_file_index
        WHERE batch_id = %s
        AND data_state = %s
        AND entry_type = 'file'
        """
        
        params = [batch_id, data_state]
        
        if source_site:
            query += " AND site = %s"
            params.append(source_site)
        
        query += " LIMIT 1;"
        
        try:
            result = self.conn.fetch_one(query, tuple(params))
            
            if result:
                logger.debug(
                    f"Found source: site={result['site']}, "
                    f"endpoint={result['endpoint']}"
                )
            else:
                logger.warning(
                    f"No source found for batch={batch_id}, "
                    f"data_state={data_state}, site={source_site}"
                )
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to query source: {e}")
            raise QueryError(f"Failed to get source for transfer: {e}") from e


    def _build_globus_command(
        self,
        source_endpoint: str,
        dest_endpoint: str,
        source_path: str,
        dest_path: str,
        label: str,
        recursive: bool = True
    ) -> list:
        """
        Build Globus transfer command.
        
        Parameters
        ----------
        source_endpoint : str
            Source Globus endpoint ID
        dest_endpoint : str
            Destination Globus endpoint ID
        source_path : str
            Full source path
        dest_path : str
            Full destination path
        label : str
            Transfer label
        recursive : bool, default=True
            Whether to transfer recursively
        
        Returns
        -------
        list
            Command as list of strings for subprocess
        """
        cmd = [
            'globus',
            'transfer',
            f'{source_endpoint}:{source_path}',
            f'{dest_endpoint}:{dest_path}',
            '--label', label,
            '--skip-activation-check'
        ]
        
        if recursive:
            cmd.append('--recursive')
        
        return cmd


    def _parse_globus_task_id(self, output: str) -> Optional[str]:
        """
        Parse Globus task ID from command output.
        
        Globus CLI outputs task ID in format:
        "Task ID: abc-123-def-456"
        
        Parameters
        ----------
        output : str
            stdout from globus transfer command
        
        Returns
        -------
        str or None
            Task ID if found, None otherwise
        """
        # Match pattern: "Task ID: <uuid>"
        match = re.search(r'Task ID:\s*([a-f0-9\-]+)', output, re.IGNORECASE)
        
        if match:
            task_id = match.group(1)
            logger.debug(f"Parsed task ID: {task_id}")
            return task_id
        
        logger.warning(f"Failed to parse task ID from output: {output}")
        return None


    # Also add GlobusError to exceptions if not already present
    class GlobusError(Exception):
        """Raised when Globus operations fail."""
        pass