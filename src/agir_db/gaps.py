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
        limit: Optional[int] = None,
        site: Optional[str] = None,
        storage_root: Optional[str] = None
    ) -> List[Dict]:
        """
        Get batches that have gaps (missing outputs) for a pipeline stage.
        
        This is the main work discovery method. It returns batches that need
        processing, ordered by date (newest first).
        
        NOTE: This method filters at the FILE level first, then aggregates to batch level.
        This ensures accurate counts when filtering by site/storage_root.
        
        Parameters
        ----------
        stage : str
            Pipeline stage to check. One of:
            - 'raw_to_jpg': RAW files missing JPG outputs
            - 'jpg_to_metadata': JPG files missing metadata JSON
            - 'metadata_to_cutouts': Metadata files missing cutouts
        limit : int, optional
            Maximum number of batches to return. If None, returns all.
        site : str, optional
            Filter by storage site (JUNO, NCSU, CERES). If None, returns all sites.
        storage_root : str, optional
            Filter by LTS root path (longterm_images, dash_agir, GROW_DATA, etc.).
            If None, returns all LTS roots.
        
        Returns
        -------
        list of dict
            Batch records with gap information:
            - batch_id: Batch identifier
            - batch_state: State code (MD, TX, NC, etc.)
            - batch_date: Date of batch
            - files_needing_processing: Number of files with gaps (filtered by site/storage_root)
            - primary_site: Where files are located
            - primary_storage_root: LTS root path
            - total_bytes: Total size of files needing processing
        
        Raises
        ------
        InvalidParameterError
            If stage is not valid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> # Get all batches needing processing
        >>> batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
        >>> 
        >>> # Get batches from specific site
        >>> juno_batches = db.gaps.get_batches_with_gaps('raw_to_jpg', site='JUNO')
        >>> 
        >>> # Get batches from specific LTS root
        >>> grow_batches = db.gaps.get_batches_with_gaps('raw_to_jpg', storage_root='GROW_DATA')
        >>> storage_root
        >>> # Get batches with multiple filters
        >>> batches = db.gaps.get_batches_with_gaps(
        ...     stage='raw_to_jpg',
        ...     site='NCSU',
        ...     storage_root='longterm_images',
        ...     storage_root
        ... )
        """
        self._validate_stage(stage)
        
        file_view_map = {
            'raw_to_jpg': 'report.files_needing_raw_to_jpg',
            'jpg_to_metadata': 'report.files_needing_jpg_to_metadata',
            'metadata_to_cutouts': 'report.files_needing_metadata_to_cutouts'
        }
        
        file_view = file_view_map[stage]
        
        # Build WHERE clause for filters
        where_clauses = []
        if site is not None:
            where_clauses.append(f"site = '{site}'")
        if storage_root is not None:
            where_clauses.append(f"storage_root = '{storage_root}'")
        where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        
        # Aggregate files to batch level AFTER filtering
        query = f"""
            SELECT
                batch_id,
                batch_state,
                batch_date,
                COUNT(*) AS files_needing_processing,
                MIN(site) AS primary_site,
                MIN(storage_root) AS primary_storage_root,
                SUM(size_bytes) AS total_bytes
            FROM {file_view}
            {where_clause}
            GROUP BY batch_id, batch_state, batch_date
            ORDER BY batch_date DESC, batch_id
        """
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        filter_desc = []
        if site:
            filter_desc.append(f"site={site}")
        if storage_root:
            filter_desc.append(f"storage_root={storage_root}")
        filter_str = f" ({', '.join(filter_desc)})" if filter_desc else ""
        logger.info(f"Querying batches with gaps for stage '{stage}'{filter_str} (limit={limit})")
        
        try:
            results = self.conn.fetch_all(query)
            logger.info(f"Found {len(results)} batches with gaps for stage '{stage}'{filter_str}")
            return results
        except Exception as e:
            logger.error(f"Failed to query batches with gaps: {e}")
            raise QueryError(f"Failed to query batches with gaps for stage '{stage}': {e}") from e
    

    
    def get_files_with_gap(
        self,
        batch_id: str,
        stage: str,
        site: Optional[str] = None,
        storage_root: Optional[str] = None
    ) -> List[Dict]:
        """
        Get specific files within a batch that have gaps (missing outputs).
        
        This method provides file-level details for processing. Use after
        get_batches_with_gaps() to get the actual files that need work.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        stage : str
            Pipeline stage to check
        site : str, optional
            Filter by storage site (JUNO, NCSU, CERES). If None, returns all sites.
        storage_root : str, optional
            Filter by LTS root path (longterm_images, dash_agir, GROW_DATA, etc.).
        storage_root, returns all LTS roots.
        
        Returns
        -------
        list of dict
            File records with gap information:
            - image_id: Image identifier
            - file_path: Path to input file
            - expected_output_path: Where output should be
            - file_size_bytes: Size of input file
            - site: Storage site (JUNO, CERES, NCSU)
            - storage_root: LTS root path
        
        Raisesstorage_root
        ------
        InvalidParameterError
            If stage is not valid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> # Get all files needing processing in a batch
        >>> files = db.gaps.get_files_with_gap('MD_20230501', 'raw_to_jpg')
        >>> 
        >>> # Get files from specific site
        >>> files = db.gaps.get_files_with_gap('MD_20230501', 'raw_to_jpg', site='JUNO')
        >>> 
        >>> # Get files from specific LTS root
        >>> files = db.gaps.get_files_with_gap('MD_20230501', 'raw_to_jpg', storage_root='GROW_DATA')
        >>> 
        >>> # Combine filtersstorage_root
        >>> files = db.gaps.get_files_with_gap(
        ...     batch_id='MD_20230501',
        ...     stage='raw_to_jpg',
        ...     site='NCSU',
        ...     storage_root='longterm_images'
        ... )
        >>> storage_root
        >>> for file in files:
        ...     print(f"Process: {file['file_path']} → {file['expected_output_path']}")
        
        """
        self._validate_stage(stage)
        
        view_map = {
            'raw_to_jpg': 'report.files_needing_raw_to_jpg',
            'jpg_to_metadata': 'report.files_needing_jpg_to_metadata',
            'metadata_to_cutouts': 'report.files_needing_metadata_to_cutouts'
        }
        
        view_name = view_map[stage]
        
        # Build WHERE clause for filters
        where_clauses = ["batch_id = %s"]
        params = [batch_id]
        
        if site is not None:
            where_clauses.append("site = %s")
            params.append(site)
        if storage_root is not None:
            where_clauses.append("storage_root = %s")
            params.append(storage_root)
        query = f"""
            SELECT *
            FROM {view_name}
            WHERE {' AND '.join(where_clauses)}
            ORDER BY file_id;
        """
        
        filter_desc = []
        if site:
            filter_desc.append(f"site={site}")
        if storage_root:
            filter_desc.append(f"storage_root={storage_root}")
        filter_str = f" ({', '.join(filter_desc)})" if filter_desc else ""
        logger.info(f"Querying files with gaps for batch '{batch_id}', stage '{stage}'{filter_str}")
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.info(f"Found {len(results)} files with gaps for batch '{batch_id}', stage '{stage}'{filter_str}")
            return results
        except Exception as e:
            logger.error(f"Failed to query files with gaps: {e}")
            raise QueryError(f"Failed to query files with gaps for batch '{batch_id}', stage '{stage}': {e}") from e
    

    
    def get_batch_pipeline_summary(
        self,
        batch_id: str
    ) -> Dict:
        """
        Get overall pipeline status summary for a batch.
        
        This provides a high-level view of where a batch stands across
        all pipeline stages.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        
        Returns
        -------
        dict
            Summary with counts for each stage:
            - batch_id: Batch identifier
            - raw_to_jpg_gaps: Number of RAW files missing JPG
            - jpg_to_metadata_gaps: Number of JPG files missing metadata
            - metadata_to_cutouts_gaps: Number of metadata files missing cutouts
            - total_raw_files: Total RAW files in batch
            - total_jpg_files: Total JPG files in batch
            - total_metadata_files: Total metadata files in batch
        
        Raises
        ------
        QueryError
            If database query fails
        
        Examples
        --------
        >>> summary = db.gaps.get_batch_pipeline_summary('MD_20230501')
        >>> print(f"RAW→JPG: {summary['raw_to_jpg_gaps']} gaps")
        >>> print(f"JPG→Meta: {summary['jpg_to_metadata_gaps']} gaps")
        """
        query = """
            SELECT *
            FROM report.batch_pipeline_summary
            WHERE batch_id = %s;
        """
        
        logger.info(f"Querying pipeline summary for batch '{batch_id}'")
        
        try:
            results = self.conn.fetch_all(query, (batch_id,))
            if not results:
                logger.warning(f"No pipeline summary found for batch '{batch_id}'")
                return {}
            logger.info(f"Retrieved pipeline summary for batch '{batch_id}'")
            return results[0]
        except Exception as e:
            logger.error(f"Failed to query batch pipeline summary: {e}")
            raise QueryError(f"Failed to query pipeline summary for batch '{batch_id}': {e}") from e
    
    def get_gap_summary(
        self,
        stage: Optional[str] = None
    ) -> Dict:
        """
        Get overall gap statistics across all batches.
        
        This provides system-wide metrics for monitoring pipeline health.
        
        Parameters
        ----------
        stage : str, optional
            Filter to specific stage. If None, returns summary for all stages.
        
        Returns
        -------
        dict
            Summary statistics:
            - total_batches: Total number of batches
            - batches_with_gaps: Number of batches needing processing
            - total_gaps: Total number of missing outputs
            - stages: Dict of per-stage gap counts
        
        Raises
        ------
        InvalidParameterError
            If stage is not valid
        QueryError
            If database query fails
        
        Examples
        --------
        >>> # Get overall summary
        >>> summary = db.gaps.get_gap_summary()
        >>> print(f"Total batches with gaps: {summary['batches_with_gaps']}")
        >>> 
        >>> # Get stage-specific summary
        >>> summary = db.gaps.get_gap_summary('raw_to_jpg')
        >>> print(f"RAW→JPG gaps: {summary['total_gaps']}")
        """
        if stage is not None:
            self._validate_stage(stage)
        
        query = """
            SELECT *
            FROM report.pipeline_gap_summary
        """
        
        if stage is not None:
            query += f" WHERE stage = '{stage}'"
        
        query += ";"
        
        logger.info(f"Querying gap summary{' for stage ' + stage if stage else ''}")
        
        try:
            results = self.conn.fetch_all(query)
            if not results:
                logger.warning("No gap summary found")
                return {}
            logger.info("Retrieved gap summary")
            return results[0] if stage else results
        except Exception as e:
            logger.error(f"Failed to query gap summary: {e}")
            raise QueryError(f"Failed to query gap summary: {e}") from e