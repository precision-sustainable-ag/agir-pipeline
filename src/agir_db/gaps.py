"""
Pipeline gap analysis for work discovery.

This module identifies batches and files that need processing by detecting
"gaps" in the pipeline - missing outputs that should exist based on available inputs.

The gap-based approach is self-correcting: if an output file exists, the work is done.
If it doesn't exist, the work needs to be done. No complex status tracking needed.
"""

import logging
from typing import Dict, List, Optional

from .connection import ConnectionManager
from .exceptions import QueryError, InvalidParameterError


logger = logging.getLogger(__name__)


# Valid pipeline stages
VALID_STAGES = {
    'raw_to_jpg',
    'jpg_to_metadata',
    'metadata_to_cutouts'
}


class PipelineGaps:
    """
    Identify batches and files needing processing through gap analysis.
    
    This class queries SQL views that detect missing pipeline outputs:
    - RAW files without corresponding JPG files
    - JPG files without corresponding metadata JSON files
    - Metadata files without any cutout files
    
    The "pipeline gaps" approach is more reliable than status tracking because:
    1. Files either exist or they don't (source of truth)
    2. Self-correcting (if files appear, gaps disappear)
    3. Handles edge cases (partial processing, crashes, manual fixes)
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Find batches needing RAW → JPG conversion
    ...     batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    ...     
    ...     for batch in batches:
    ...         batch_id = batch['batch_id']
    ...         
    ...         # Get specific files needing processing
    ...         files = db.gaps.get_files_with_gap(batch_id, 'raw_to_jpg')
    ...         
    ...         # Process files...
    ...         
    ...         # Check pipeline status
    ...         summary = db.gaps.get_batch_pipeline_summary(batch_id)
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("PipelineGaps initialized")
    
    def _validate_stage(self, stage: str) -> None:
        """
        Validate that stage name is valid.
        
        Parameters
        ----------
        stage : str
            Pipeline stage name
        
        Raises
        ------
        InvalidParameterError
            If stage is not valid
        """
        if stage not in VALID_STAGES:
            raise InvalidParameterError(
                f"Invalid stage '{stage}'. Must be one of: {', '.join(sorted(VALID_STAGES))}"
            )
    
    def get_batches_with_gaps(
        self,
        stage: str,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get batches that have gaps (missing outputs) for a pipeline stage.
        
        This is the main work discovery method. It returns batches that need
        processing, ordered by date (newest first).
        
        Parameters
        ----------
        stage : str
            Pipeline stage to check. One of:
            - 'raw_to_jpg': RAW files missing JPG outputs
            - 'jpg_to_metadata': JPG files missing metadata JSON
            - 'metadata_to_cutouts': Metadata files missing cutouts
        limit : int, optional
            Maximum number of batches to return. If None, returns all.
        
        Returns
        -------
        list of dict
            Batch records with gap information:
            - batch_id: Batch identifier
            - batch_state: State code (MD, TX, NC, etc.)
            - batch_date: Date of batch
            - files_needing_processing: Number of files with gaps
            - primary_location: Where files are located (JUNO, CERES, NCSU)
            - primary_lts_root: LTS root path
            - primary_root_path: Full root path
            - total_bytes: Total size of files needing processing
        
        Raises
        ------
        InvalidParameterError
            If stage is not valid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
        >>> for batch in batches:
        ...     print(f"{batch['batch_id']}: {batch['files_needing_processing']} files")
        """
        self._validate_stage(stage)
        
        view_map = {
            'raw_to_jpg': 'report.batches_needing_raw_to_jpg',
            'jpg_to_metadata': 'report.batches_needing_jpg_to_metadata',
            'metadata_to_cutouts': 'report.batches_needing_metadata_to_cutouts'
        }
        
        view_name = view_map[stage]
        
        query = f"SELECT * FROM {view_name}"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        query += ";"
        
        logger.info(f"Querying batches with gaps for stage '{stage}' (limit={limit})")
        
        try:
            results = self.conn.fetch_all(query)
            logger.info(f"Found {len(results)} batches with gaps for stage '{stage}'")
            return results
        except Exception as e:
            logger.error(f"Failed to query batches with gaps: {e}")
            raise QueryError(f"Failed to query batches with gaps for stage '{stage}': {e}") from e
    
    def get_files_with_gap(
        self,
        batch_id: str,
        stage: str
    ) -> List[Dict]:
        """
        Get specific files within a batch that have gaps (missing outputs).
        
        Use this to get file-level details for processing. Returns the actual
        input files that need to be processed.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier (e.g., 'MD_2025-01-01')
        stage : str
            Pipeline stage to check
        
        Returns
        -------
        list of dict
            File records with gap information:
            - file_id: Unique file identifier
            - batch_id: Batch identifier
            - file_name: Name of file
            - base_name: Filename without extension
            - file_ext: File extension
            - root_path: Full path to root directory
            - rel_path: Relative path under root
            - location: Storage location (JUNO, CERES, NCSU)
            - lts_root: LTS root identifier
            - size_bytes: File size
        
        Raises
        ------
        InvalidParameterError
            If stage is not valid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> files = db.gaps.get_files_with_gap('MD_2025-01-01', 'raw_to_jpg')
        >>> for file in files:
        ...     raw_path = Path(file['root_path']) / file['rel_path']
        ...     print(f"Process: {raw_path}")
        """
        self._validate_stage(stage)
        
        view_map = {
            'raw_to_jpg': 'report.files_needing_raw_to_jpg',
            'jpg_to_metadata': 'report.files_needing_jpg_to_metadata',
            'metadata_to_cutouts': 'report.files_needing_metadata_to_cutouts'
        }
        
        view_name = view_map[stage]
        
        query = f"""
            SELECT *
            FROM {view_name}
            WHERE batch_id = %s
            ORDER BY file_name;
        """
        
        logger.info(f"Querying files with gaps for batch '{batch_id}', stage '{stage}'")
        
        try:
            results = self.conn.fetch_all(query, (batch_id,))
            logger.info(f"Found {len(results)} files with gaps in batch '{batch_id}'")
            return results
        except Exception as e:
            logger.error(f"Failed to query files with gap: {e}")
            raise QueryError(
                f"Failed to query files with gap for batch '{batch_id}', stage '{stage}': {e}"
            ) from e
    
    def get_batch_pipeline_summary(self, batch_id: str) -> Optional[Dict]:
        """
        Get complete pipeline status for a single batch.
        
        Returns file counts and gap counts for all pipeline stages, providing
        a comprehensive view of batch processing status.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        
        Returns
        -------
        dict or None
            Pipeline summary with:
            - batch_id: Batch identifier
            - batch_state: State code
            - batch_date: Date of batch
            - raw_count: Number of RAW files
            - jpg_count: Number of JPG files
            - metadata_count: Number of metadata JSON files
            - cutout_count: Number of cutout files
            - raw_to_jpg_gap: Files needing RAW → JPG
            - jpg_to_metadata_gap: Files needing JPG → metadata
            - metadata_to_cutouts_gap: Files needing metadata → cutouts
            - raw_to_jpg_complete: Boolean, True if stage complete
            - jpg_to_metadata_complete: Boolean, True if stage complete
            - has_cutouts: Boolean, True if any cutouts exist
            - primary_location: Storage location
            - primary_lts_root: LTS root
            
            Returns None if batch not found.
        
        Raises
        ------
        QueryError
            If database query fails
        
        Examples
        --------
        >>> summary = db.gaps.get_batch_pipeline_summary('MD_2025-01-01')
        >>> if summary:
        ...     print(f"RAW files: {summary['raw_count']}")
        ...     print(f"JPG files: {summary['jpg_count']}")
        ...     print(f"Gaps: {summary['raw_to_jpg_gap']}")
        ...     if summary['raw_to_jpg_complete']:
        ...         print("RAW → JPG: Complete!")
        """
        query = """
            SELECT *
            FROM report.batch_pipeline_status
            WHERE batch_id = %s;
        """
        
        logger.info(f"Querying pipeline summary for batch '{batch_id}'")
        
        try:
            result = self.conn.fetch_one(query, (batch_id,))
            if result:
                logger.debug(f"Retrieved pipeline summary for batch '{batch_id}'")
            else:
                logger.warning(f"No pipeline summary found for batch '{batch_id}'")
            return result
        except Exception as e:
            logger.error(f"Failed to query batch pipeline summary: {e}")
            raise QueryError(
                f"Failed to query pipeline summary for batch '{batch_id}': {e}"
            ) from e
    
    def get_gap_summary(self, stage: Optional[str] = None) -> List[Dict]:
        """
        Get overall statistics about pipeline gaps across all batches.
        
        Returns aggregate counts of batches and files with gaps. Useful for
        monitoring and capacity planning.
        
        Parameters
        ----------
        stage : str, optional
            Specific stage to get summary for. If None, returns all stages.
        
        Returns
        -------
        list of dict
            Summary records with:
            - stage: Pipeline stage name
            - batches_with_gaps: Number of batches with gaps
            - total_files_with_gaps: Total files needing processing
            - total_bytes: Total size of files needing processing
        
        Raises
        ------
        InvalidParameterError
            If stage is not valid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> # Get summary for all stages
        >>> summaries = db.gaps.get_gap_summary()
        >>> for summary in summaries:
        ...     print(f"{summary['stage']}: {summary['batches_with_gaps']} batches")
        
        >>> # Get summary for specific stage
        >>> summary = db.gaps.get_gap_summary('raw_to_jpg')
        >>> print(f"Files needing processing: {summary[0]['total_files_with_gaps']}")
        """
        if stage is not None:
            self._validate_stage(stage)
        
        query = "SELECT * FROM report.pipeline_gap_summary"
        
        if stage is not None:
            query += " WHERE stage = %s"
            params = (stage,)
        else:
            params = None
        
        query += " ORDER BY stage;"
        
        logger.info(f"Querying gap summary (stage={stage})")
        
        try:
            results = self.conn.fetch_all(query, params)
            logger.debug(f"Retrieved gap summary with {len(results)} records")
            return results
        except Exception as e:
            logger.error(f"Failed to query gap summary: {e}")
            raise QueryError(f"Failed to query gap summary: {e}") from e