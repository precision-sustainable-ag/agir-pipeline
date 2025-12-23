"""
Batch metadata management.

This module provides methods for storing and querying batch-level metadata
including file counts, processing status, and statistics.
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, date

from psycopg2.extras import Json

from .connection import ConnectionManager
from .exceptions import (
    QueryError,
    InvalidParameterError,
    DuplicateBatchError,
    BatchNotFoundError
)


logger = logging.getLogger(__name__)


# Valid processing statuses
VALID_BATCH_STATUSES = {
    'pending',
    'in_progress',
    'completed',
    'partial',
    'failed'
}


class BatchMetadata:
    """
    Manage batch metadata records.
    
    This class provides methods for storing and querying batch-level data
    including file counts, processing status, and statistics.
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Insert batch record
    ...     db.batches.insert(
    ...         batch_id='MD_2025-01-01',
    ...         batch_state='MD',
    ...         batch_date=date(2025, 1, 1),
    ...         site='JUNO'
    ...     )
    ...     
    ...     # Update file counts
    ...     db.batches.update_file_counts(
    ...         'MD_2025-01-01',
    ...         file_count_raw=150,
    ...         file_count_jpg=150
    ...     )
    ...     
    ...     # Query batches by state
    ...     batches = db.batches.get_by_state('MD', limit=10)
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("BatchMetadata initialized")
    
    def _validate_status(self, status: str) -> None:
        """Validate that processing status is valid."""
        if status not in VALID_BATCH_STATUSES:
            raise InvalidParameterError(
                f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_BATCH_STATUSES))}"
            )
    
    def insert(
        self,
        batch_id: str,
        batch_state: str,
        batch_date: date,
        site: Optional[str] = None,
        storage_root: Optional[str] = None,
        processing_status: str = 'pending',
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Insert a new batch record.
        
        Parameters
        ----------
        batch_id : str
            Unique batch identifier (e.g., 'MD_2025-01-01')
        batch_state : str
            State code (e.g., 'MD', 'TX', 'NC')
        batch_date : date
            Date of batch
        site : str, optional
            Storage site (e.g., 'JUNO', 'CERES')
        storage_root : str, optional
        storage_roott identifier
            Full path to batch root directory
        processing_status : str, optional
            Processing status (default: 'pending')
        metadata : dict, optional
            Additional metadata
        
        Raises
        ------
        DuplicateBatchError
            If batch_id already exists
        InvalidParameterError
            If processing_status is invalid
        QueryError
            If database insert fails
        
        Examples
        --------
        >>> from datetime import date
        >>> db.batches.insert(
        ...     batch_id='MD_2025-01-01',
        ...     batch_state='MD',
        ...     batch_date=date(2025, 1, 1),
        ...     site='JUNO',
        ...     storage_root='/lts/MD_2025-01-01'
        ... )
        """
        self._validate_status(processing_status)
        
        query = """
            INSERT INTO processed.batches (
                batch_id, batch_state, batch_date,
                site, storage_root,
                processing_status, metadata
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s,
                %s
            );
        """
        
        logger.info(f"Inserting batch: {batch_id}")
        
        try:
            # Wrap metadata dict with Json() for JSONB conversion
            metadata_json = Json(metadata) if metadata is not None else None
            
            self.conn.execute(
                query,
                (batch_id, batch_state, batch_date,
                 site, storage_root,
                 processing_status, metadata_json)
            )
            logger.info(f"Batch {batch_id} inserted successfully")
        except Exception as e:
            if 'duplicate key' in str(e).lower():
                raise DuplicateBatchError(f"Batch {batch_id} already exists") from e
            logger.error(f"Failed to insert batch: {e}")
            raise QueryError(f"Failed to insert batch {batch_id}: {e}") from e
    
    def update_status(
        self,
        batch_id: str,
        processing_status: str
    ) -> None:
        """
        Update batch processing status.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        processing_status : str
            New processing status
        
        Raises
        ------
        BatchNotFoundError
            If batch doesn't exist
        InvalidParameterError
            If status is invalid
        QueryError
            If update fails
        """
        self._validate_status(processing_status)
        
        query = """
            UPDATE processed.batches
            SET processing_status = %s
            WHERE batch_id = %s;
        """
        
        logger.info(f"Updating status for {batch_id}: {processing_status}")
        
        try:
            self.conn.execute(query, (processing_status, batch_id))
            
            # Check if batch exists
            if self.conn.fetch_one("SELECT 1 FROM processed.batches WHERE batch_id = %s", (batch_id,)) is None:
                raise BatchNotFoundError(f"Batch {batch_id} not found")
            
            logger.debug(f"Status updated for {batch_id}")
        except BatchNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to update status: {e}")
            raise QueryError(f"Failed to update status for {batch_id}: {e}") from e
    
    def update_file_counts(
        self,
        batch_id: str,
        file_count_raw: Optional[int] = None,
        file_count_jpg: Optional[int] = None,
        file_count_metadata: Optional[int] = None,
        file_count_cutout: Optional[int] = None,
        total_bytes: Optional[int] = None
    ) -> None:
        """
        Update file counts for a batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        file_count_raw : int, optional
            Number of RAW files
        file_count_jpg : int, optional
            Number of JPG files
        file_count_metadata : int, optional
            Number of metadata JSON files
        file_count_cutout : int, optional
            Number of cutout files
        total_bytes : int, optional
            Total size in bytes
        
        Examples
        --------
        >>> db.batches.update_file_counts(
        ...     'MD_2025-01-01',
        ...     file_count_raw=150,
        ...     file_count_jpg=150,
        ...     total_bytes=3750000000
        ... )
        """
        # Build dynamic UPDATE query
        updates = []
        params = []
        
        if file_count_raw is not None:
            updates.append("file_count_raw = %s")
            params.append(file_count_raw)
        
        if file_count_jpg is not None:
            updates.append("file_count_jpg = %s")
            params.append(file_count_jpg)
        
        if file_count_metadata is not None:
            updates.append("file_count_metadata = %s")
            params.append(file_count_metadata)
        
        if file_count_cutout is not None:
            updates.append("file_count_cutout = %s")
            params.append(file_count_cutout)
        
        if total_bytes is not None:
            updates.append("total_bytes = %s")
            params.append(total_bytes)
        
        if not updates:
            logger.warning(f"No file counts provided for update")
            return
        
        params.append(batch_id)
        
        query = f"""
            UPDATE processed.batches
            SET {', '.join(updates)}
            WHERE batch_id = %s;
        """
        
        logger.info(f"Updating file counts for {batch_id}")
        
        try:
            self.conn.execute(query, tuple(params))
            logger.debug(f"File counts updated for {batch_id}")
        except Exception as e:
            logger.error(f"Failed to update file counts: {e}")
            raise QueryError(f"Failed to update file counts for {batch_id}: {e}") from e
    
    def update_completion_flags(
        self,
        batch_id: str,
        raw_to_jpg_complete: Optional[bool] = None,
        jpg_to_metadata_complete: Optional[bool] = None,
        metadata_to_cutouts_complete: Optional[bool] = None
    ) -> None:
        """
        Update stage completion flags for a batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        raw_to_jpg_complete : bool, optional
            RAW → JPG stage complete
        jpg_to_metadata_complete : bool, optional
            JPG → Metadata stage complete
        metadata_to_cutouts_complete : bool, optional
            Metadata → Cutouts stage complete
        
        Examples
        --------
        >>> db.batches.update_completion_flags(
        ...     'MD_2025-01-01',
        ...     raw_to_jpg_complete=True,
        ...     jpg_to_metadata_complete=True
        ... )
        """
        # Build dynamic UPDATE query
        updates = []
        params = []
        
        if raw_to_jpg_complete is not None:
            updates.append("raw_to_jpg_complete = %s")
            params.append(raw_to_jpg_complete)
        
        if jpg_to_metadata_complete is not None:
            updates.append("jpg_to_metadata_complete = %s")
            params.append(jpg_to_metadata_complete)
        
        if metadata_to_cutouts_complete is not None:
            updates.append("metadata_to_cutouts_complete = %s")
            params.append(metadata_to_cutouts_complete)
        
        if not updates:
            logger.warning(f"No completion flags provided for update")
            return
        
        params.append(batch_id)
        
        query = f"""
            UPDATE processed.batches
            SET {', '.join(updates)}
            WHERE batch_id = %s;
        """
        
        logger.info(f"Updating completion flags for {batch_id}")
        
        try:
            self.conn.execute(query, tuple(params))
            logger.debug(f"Completion flags updated for {batch_id}")
        except Exception as e:
            logger.error(f"Failed to update completion flags: {e}")
            raise QueryError(f"Failed to update completion flags for {batch_id}: {e}") from e
    
    def get_by_id(self, batch_id: str) -> Optional[Dict]:
        """
        Get batch record by ID.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        
        Returns
        -------
        dict or None
            Batch record, or None if not found
        """
        query = "SELECT * FROM processed.batches WHERE batch_id = %s;"
        
        logger.debug(f"Getting batch: {batch_id}")
        
        try:
            result = self.conn.fetch_one(query, (batch_id,))
            return result
        except Exception as e:
            logger.error(f"Failed to get batch: {e}")
            raise QueryError(f"Failed to get batch {batch_id}: {e}") from e
    
    def get_by_state(
        self,
        batch_state: str,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get batches by state.
        
        Parameters
        ----------
        batch_state : str
            State code (e.g., 'MD', 'TX')
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Batch records
        
        Examples
        --------
        >>> batches = db.batches.get_by_state('MD', limit=10)
        """
        query = """
            SELECT * FROM processed.batches
            WHERE batch_state = %s
            ORDER BY batch_date DESC, batch_id
        """
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Getting batches for state {batch_state}")
        
        try:
            results = self.conn.fetch_all(query, (batch_state,))
            logger.debug(f"Found {len(results)} batch(es)")
            return results
        except Exception as e:
            logger.error(f"Failed to get batches by state: {e}")
            raise QueryError(f"Failed to get batches for state {batch_state}: {e}") from e
    
    def get_by_status(
        self,
        processing_status: str,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get batches by processing status.
        
        Parameters
        ----------
        processing_status : str
            Processing status
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Batch records
        
        Examples
        --------
        >>> # Get pending batches
        >>> batches = db.batches.get_by_status('pending', limit=20)
        """
        self._validate_status(processing_status)
        
        query = """
            SELECT * FROM processed.batches
            WHERE processing_status = %s
            ORDER BY batch_date DESC, batch_id
        """
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Getting batches with status {processing_status}")
        
        try:
            results = self.conn.fetch_all(query, (processing_status,))
            logger.debug(f"Found {len(results)} batch(es)")
            return results
        except Exception as e:
            logger.error(f"Failed to get batches by status: {e}")
            raise QueryError(f"Failed to get batches with status {processing_status}: {e}") from e
    
    def get_summary(
        self,
        batch_id: Optional[str] = None,
        batch_state: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get batch summary with file counts and image statistics.
        
        Uses the batch_summary view which includes:
        - File counts from batches table
        - Registered images count
        - Completed/failed images count
        - Total detections
        
        Parameters
        ----------
        batch_id : str, optional
            Specific batch to get summary for
        batch_state : str, optional
            Filter by state
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Batch summary records
        
        Examples
        --------
        >>> # Summary for one batch
        >>> summary = db.batches.get_summary(batch_id='MD_2025-01-01')
        
        >>> # Summaries for all MD batches
        >>> summaries = db.batches.get_summary(batch_state='MD', limit=10)
        """
        query = "SELECT * FROM processed.batch_summary"
        
        conditions = []
        params = []
        
        if batch_id is not None:
            conditions.append("batch_id = %s")
            params.append(batch_id)
        
        if batch_state is not None:
            conditions.append("batch_state = %s")
            params.append(batch_state)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY batch_date DESC, batch_id"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Getting batch summary (batch_id={batch_id}, state={batch_state})")
        
        try:
            results = self.conn.fetch_all(query, tuple(params) if params else None)
            logger.debug(f"Found {len(results)} batch summary record(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get batch summary: {e}")
            raise QueryError(f"Failed to get batch summary: {e}") from e