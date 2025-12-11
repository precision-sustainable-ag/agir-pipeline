"""
Orchestration helpers for RAW to JPG conversion workflows.

This module provides high-level workflow orchestration for processing
batches through the raw_to_jpg pipeline, integrating AgirDB with
svs-raw-api converters.

KEY DESIGN: Handles multi-site, multi-storage_root storage with deduplication.
Storage hierarchy:
  site (NCSU, JUNO, CERES)
    └── storage_root (GROW_DATA, dash_agir, longterm_images, etc.)
         └── batch files (potentially with duplicates)

All methods support filtering by site + storage_root and include deduplication
to handle rare cases where the same file appears multiple times.
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import OrderedDict

from .connection import ConnectionManager
from .gaps import PipelineGaps
from .exceptions import (
    QueryError,
    OrchestrationError,
    InvalidParameterError
)


logger = logging.getLogger(__name__)


class Orchestration:
    """
    High-level workflow orchestration for RAW to JPG conversion.
    
    This class coordinates batch processing workflows with support for
    multi-site, multi-storage_root file storage with deduplication.
    
    Storage Hierarchy:
    ------------------
    site → storage_root → batch → files
    
    Example:
    NCSU → GROW_DATA → MD_2024-06-01 → MD_1683434234.raw
    NCSU → dash_agir → MD_2024-06-01 → MD_1683434234.raw (duplicate!)
    JUNO → longterm_images → MD_2024-06-01 → MD_1683434234.raw
    
    To avoid processing duplicates:
    1. Filter by site + storage_root
    2. Deduplicate by base_name (in case of rare duplicates)
    
    Parameters
    ----------
    connection : ConnectionManager
        PostgreSQL database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Get batches at specific site + storage_root
    ...     queue = db.orchestration.get_conversion_queue(
    ...         limit=10,
    ...         site='NCSU',
    ...         storage_root='GROW_DATA'
    ...     )
    ...     
    ...     # Get files (deduplicated automatically)
    ...     files = db.orchestration.get_batch_files_for_conversion(
    ...         'MD_2024-06-01',
    ...         site='NCSU',
    ...         storage_root='GROW_DATA'
    ...     )
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        self.gaps = PipelineGaps(connection)
        logger.debug("Orchestration initialized")
    
    def get_conversion_queue(
        self,
        limit: int = 100,
        batch_state: Optional[str] = None,
        site: Optional[str] = None,
        storage_root: Optional[str] = None
    ) -> List[Dict]:
        """
        Get batches needing RAW to JPG conversion.
        
        Uses data_state to distinguish upload_raw (input) from developed_jpg (output).
        Supports site and storage_root filtering to handle batch duplicates.
        
        A batch may appear multiple times if it exists at different site/storage_root
        combinations with different completion status.
        
        Parameters
        ----------
        limit : int, optional
            Maximum number of batches to return
        batch_state : str, optional
            Filter by batch state (e.g., 'MD', 'TX')
        site : str, optional
            Filter by site (e.g., 'JUNO', 'CERES', 'NCSU')
        storage_root : str, optional
            Filter by LTS root (e.g., 'longterm_images', 'dash_agir')
        
        Returns
        -------
        list of dict
            Batches needing conversion, with keys:
            - batch_id
            - batch_state
            - batch_date
            - site
            - storage_root
            - raw_count (files at data_state='upload_raw')
            - jpg_count (files at data_state='developed_jpg')
            - gap_count (raw_count - jpg_count)
            - priority (calculated based on age)
        
        Examples
        --------
        >>> # Get batches at JUNO/longterm_images only
        >>> queue = db.orchestration.get_conversion_queue(
        ...     limit=10,
        ...     site='JUNO',
        ...     storage_root='longterm_images'
        ... )
        >>> 
        >>> for batch in queue:
        ...     print(f"{batch['batch_id']}: {batch['gap_count']} files at "
        ...           f"{batch['site']}/{batch['storage_root']}")
        """
        logger.info(f"Getting conversion queue (limit={limit})" +
                   (f", batch_state={batch_state}" if batch_state else "") +
                   (f", site={site}" if site else "") +
                   (f", storage_root={storage_root}" if storage_root else ""))
        
        query = """
            WITH batch_gaps AS (
                SELECT
                    g.batch_id,
                    g.batch_state,
                    g.batch_date,
                    g.site,
                    g.storage_root,
                    -- Count RAW files at upload_raw state
                    COUNT(*) FILTER (
                        WHERE g.data_state = 'upload_raw' 
                          AND LOWER(g.file_ext) IN ('raw', 'arw')
                          AND g.entry_type = 'file'
                    ) as raw_count,
                    -- Count JPG files at developed_jpg state
                    COUNT(*) FILTER (
                        WHERE g.data_state = 'developed_jpg' 
                          AND LOWER(g.file_ext) IN ('jpg', 'jpeg')
                          AND g.entry_type = 'file'
                    ) as jpg_count,
                    -- Calculate gap
                    (
                        COUNT(*) FILTER (
                            WHERE g.data_state = 'upload_raw' 
                              AND LOWER(g.file_ext) IN ('raw', 'arw')
                              AND g.entry_type = 'file'
                        ) - 
                        COUNT(*) FILTER (
                            WHERE g.data_state = 'developed_jpg' 
                              AND LOWER(g.file_ext) IN ('jpg', 'jpeg')
                              AND g.entry_type = 'file'
                        )
                    ) as gap_count,
                    -- Priority based on age
                    (CURRENT_DATE - g.batch_date) as age_days,
                    CASE
                        WHEN g.batch_date >= CURRENT_DATE - INTERVAL '7 days' THEN 1
                        WHEN g.batch_date >= CURRENT_DATE - INTERVAL '30 days' THEN 2
                        ELSE 3
                    END as priority_tier
                FROM source.globus_file_index g
                WHERE g.batch_id IS NOT NULL
        """
        
        params = []
        
        if batch_state:
            query += " AND g.batch_state = %s"
            params.append(batch_state)
        
        if site:
            query += " AND g.site = %s"
            params.append(site)
        
        if storage_root:
            query += " AND g.storage_root = %s"
            params.append(storage_root)
        
        query += """
                GROUP BY g.batch_id, g.batch_state, g.batch_date, g.site, g.storage_root
                HAVING 
                    -- Only include batches with RAW files and gaps
                    COUNT(*) FILTER (
                        WHERE g.data_state = 'upload_raw' 
                          AND LOWER(g.file_ext) IN ('raw', 'arw')
                          AND g.entry_type = 'file'
                    ) > 0
                    AND (
                        COUNT(*) FILTER (
                            WHERE g.data_state = 'upload_raw' 
                              AND LOWER(g.file_ext) IN ('raw', 'arw')
                              AND g.entry_type = 'file'
                        ) - 
                        COUNT(*) FILTER (
                            WHERE g.data_state = 'developed_jpg' 
                              AND LOWER(g.file_ext) IN ('jpg', 'jpeg')
                              AND g.entry_type = 'file'
                        )
                    ) > 0
            )
            SELECT
                batch_id,
                batch_state,
                batch_date,
                site,
                storage_root,
                raw_count,
                jpg_count,
                gap_count,
                age_days,
                priority_tier as priority
            FROM batch_gaps
            WHERE gap_count > 0
            ORDER BY priority_tier, age_days, gap_count DESC
            LIMIT %s;
        """
        
        params.append(limit)
        
        try:
            result = self.conn.fetch_all(query, tuple(params))
            batches = [dict(row) for row in result] if result else []
            logger.info(f"Found {len(batches)} batch/site combinations in queue")
            
            # Log duplicates if found
            batch_ids = [b['batch_id'] for b in batches]
            duplicates = [bid for bid in set(batch_ids) if batch_ids.count(bid) > 1]
            if duplicates:
                logger.info(f"Found {len(duplicates)} batches with multiple sites: "
                          f"{', '.join(duplicates[:5])}{'...' if len(duplicates) > 5 else ''}")
            
            return batches
        except Exception as e:
            logger.error(f"Failed to get conversion queue: {e}")
            raise QueryError(f"Failed to get conversion queue: {e}") from e
    
    def get_batch_files_for_conversion(
        self,
        batch_id: str,
        check_existing: bool = True,
        site: Optional[str] = None,
        storage_root: Optional[str] = None
    ) -> List[Dict]:
        """
        Get list of RAW files that need JPG conversion.
        
        Looks for RAW files at data_state='upload_raw' that don't have
        corresponding JPG files at data_state='developed_jpg'. Filters
        by site and storage_root to ensure comparison at same storage site.
        
        Parameters
        ----------
        batch_id : str
            Batch to process
        check_existing : bool, optional
            If True, only return files without existing JPGs.
            If False, return all RAW files.
        site : str, optional
            Filter by storage site (e.g., 'JUNO', 'NCSU', 'CERES')
            If None, returns files from all sites
        storage_root : str, optional
            Filter by LTS root (e.g., 'longterm_images', 'dash_agir')
            If None, returns files from all storage_roots
        
        Returns
        -------
        list of dict
            Files to convert, with keys:
            - image_id: Base filename without extension
            - file_name: Full filename with extension
            - file_path: Relative path (rel_path)
            - file_ext: File extension
            - site: Storage site
            - storage_root: LTS root identifier
            - data_state: Always 'upload_raw' for files needing conversion
            - size_bytes: File size
        
        Examples
        --------
        >>> # Get files from specific site/storage_root
        >>> files = db.orchestration.get_batch_files_for_conversion(
        ...     'MD_2024-06-01',
        ...     site='JUNO',
        ...     storage_root='longterm_images'
        ... )
        >>> 
        >>> # Integration with queue
        >>> queue = db.orchestration.get_conversion_queue(
        ...     site='JUNO', storage_root='longterm_images'
        ... )
        >>> for batch in queue:
        ...     files = db.orchestration.get_batch_files_for_conversion(
        ...         batch['batch_id'],
        ...         site=batch['site'],
        ...         storage_root=batch['storage_root']
        ...     )
        """
        logger.info(f"Getting files for conversion: {batch_id}" + 
                    (f" (site={site})" if site else "") +
                    (f" (storage_root={storage_root})" if storage_root else ""))
        
        if check_existing:
            # Build filter strings for site and storage_root
            site_filter = " AND site = %s" if site else ""
            storage_root_filter = " AND storage_root = %s" if storage_root else ""
            combined_filters = site_filter + storage_root_filter
            
            # Use pipeline gaps to find missing JPGs
            query = """
                WITH base_names AS (
                    -- Get base names of RAW files at upload_raw state
                    SELECT DISTINCT
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                            '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                        ) as base_name
                    FROM source.globus_file_index
                    WHERE batch_id = %s
                      AND data_state = 'upload_raw'
                      AND LOWER(file_ext) IN ('raw', 'arw')
                      AND entry_type = 'file'
            """
            
            # Add site/storage_root filters to RAW files
            query += combined_filters
            
            query += """
                ),
                has_jpg AS (
                    -- Get base names of JPG files at developed_jpg state
                    SELECT DISTINCT
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                            '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                        ) as base_name
                    FROM source.globus_file_index
                    WHERE batch_id = %s
                      AND data_state = 'developed_jpg'
                      AND LOWER(file_ext) IN ('jpg', 'jpeg')
                      AND entry_type = 'file'
            """
            
            # CRITICAL: Add SAME filters to JPG files to compare at same site
            query += combined_filters
            
            query += """
                ),
                gaps AS (
                    -- Find RAW files without corresponding JPG
                    SELECT bn.base_name
                    FROM base_names bn
                    LEFT JOIN has_jpg hj ON bn.base_name = hj.base_name
                    WHERE hj.base_name IS NULL
                )
                SELECT
                    g.base_name as image_id,
                    f.file_name,
                    f.rel_path as file_path,
                    f.file_ext,
                    f.site,
                    f.storage_root,
                    f.parent_dir,
                    f.data_state,
                    f.size_bytes
                FROM gaps g
                INNER JOIN source.globus_file_index f ON
                    REGEXP_REPLACE(
                        REGEXP_REPLACE(f.file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    ) = g.base_name
                WHERE f.batch_id = %s
                  AND f.data_state = 'upload_raw'
                  AND LOWER(f.file_ext) IN ('raw', 'arw')
                  AND f.entry_type = 'file'
            """
            
            # Add site/storage_root filters to final query
            query += combined_filters
            query += " ORDER BY f.file_name;"
            
            # Build params tuple
            # Each CTE needs: batch_id [, site] [, storage_root]
            base_params = [batch_id]
            if site:
                base_params.append(site)
            if storage_root:
                base_params.append(storage_root)
            
            # We use the same filters 3 times (base_names, has_jpg, final)
            params = tuple(base_params * 3)
            
        else:
            # Get all RAW files at upload_raw state
            query = """
                SELECT
                    REGEXP_REPLACE(
                        REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    ) as image_id,
                    file_name,
                    rel_path as file_path,
                    file_ext,
                    site,
                    storage_root,
                    parent_dir,
                    data_state,
                    size_bytes
                FROM source.globus_file_index
                WHERE batch_id = %s
                  AND data_state = 'upload_raw'
                  AND LOWER(file_ext) IN ('raw', 'arw')
                  AND entry_type = 'file'
            """
            
            params = [batch_id]
            if site:
                query += " AND site = %s"
                params.append(site)
            if storage_root:
                query += " AND storage_root = %s"
                params.append(storage_root)
            
            query += " ORDER BY file_name;"
            params = tuple(params)
        
        try:
            result = self.conn.fetch_all(query, params)
            files = [dict(row) for row in result] if result else []
            logger.info(f"Found {len(files)} files to convert in {batch_id}" +
                       (f" at {site}" if site else "") +
                       (f" in {storage_root}" if storage_root else ""))
            
            # Verify all files are at upload_raw state
            if files and check_existing:
                for file in files:
                    assert file['data_state'] == 'upload_raw', \
                        f"File {file['file_name']} has wrong data_state: {file['data_state']}"
            
            return files
        except Exception as e:
            logger.error(f"Failed to get files for conversion: {e}")
            raise QueryError(f"Failed to get files for {batch_id}: {e}") from e

    
    def get_batch_storage_sites(
        self,
        batch_id: str,
        data_state: str = 'upload_raw'
    ) -> List[Dict]:
        """
        Get all storage sites (site + storage_root combinations) where a batch has RAW files.
        
        Use this to discover all storage sites for a batch, then process
        each site separately to avoid duplicates.
        
        Parameters
        ----------
        batch_id : str
            Batch to check
        data_state : str, optional
            Data state to check (default: 'upload_raw')
        
        Returns
        -------
        list of dict
            Storage sites where batch exists, each with:
            - site: Storage site (NCSU, JUNO, etc.)
            - storage_root: LTS root (GROW_DATA, dash_agir, etc.)
            - file_count: Number of RAW files (before deduplication)
            - unique_file_count: Number of unique base_names
            - has_duplicates: True if file_count > unique_file_count
            - total_bytes: Total size of files
        
        Examples
        --------
        >>> # Check where a batch exists
        >>> sites = db.orchestration.get_batch_storage_sites('MD_2024-06-01')
        >>> for loc in sites:
        ...     print(f"{loc['site']}/{loc['storage_root']}: "
        ...           f"{loc['unique_file_count']} unique files")
        ...     if loc['has_duplicates']:
        ...         print(f"  ⚠️  Has {loc['file_count'] - loc['unique_file_count']} duplicate(s)")
        >>> 
        >>> # Process each site separately
        >>> for loc in sites:
        ...     files = db.orchestration.get_batch_files_for_conversion(
        ...         'MD_2024-06-01',
        ...         site=loc['site'],
        ...         storage_root=loc['storage_root']
        ...     )
        ...     # process files...
        """
        logger.info(f"Getting storage sites for batch {batch_id}")
        
        try:
            query = """
                WITH file_counts AS (
                    SELECT
                        site,
                        storage_root,
                        file_name,
                        regexp_replace(file_name, '\\.[^.]+$', '') AS base_name,
                        size_bytes
                    FROM source.globus_file_index
                    WHERE batch_id = %s
                      AND data_state = %s
                      AND entry_type = 'file'
                      AND LOWER(file_ext) IN ('raw', 'arw')
                )
                SELECT
                    site,
                    storage_root,
                    COUNT(*) as file_count,
                    COUNT(DISTINCT base_name) as unique_file_count,
                    (COUNT(*) > COUNT(DISTINCT base_name)) as has_duplicates,
                    SUM(size_bytes) as total_bytes
                FROM file_counts
                GROUP BY site, storage_root
                ORDER BY site, storage_root;
            """
            
            results = self.conn.fetch_all(query, (batch_id, data_state))
            sites = [dict(row) for row in results] if results else []
            
            # Log warnings for sites with duplicates
            for loc in sites:
                if loc['has_duplicates']:
                    dup_count = loc['file_count'] - loc['unique_file_count']
                    logger.warning(f"⚠️  {batch_id} at {loc['site']}/{loc['storage_root']} "
                                 f"has {dup_count} duplicate(s)")
            
            logger.info(f"Found {len(sites)} storage site(s) for {batch_id}")
            return sites
            
        except Exception as e:
            logger.error(f"Failed to get batch storage sites: {e}")
            raise QueryError(f"Failed to get storage sites for {batch_id}: {e}") from e
    
    def get_conversion_summary(
        self,
        days: int = 7,
        site: Optional[str] = None,
        storage_root: Optional[str] = None
    ) -> Dict:
        """
        Get summary statistics for RAW to JPG conversion pipeline.
        
        Parameters
        ----------
        days : int, optional
            Look back period for completed/failed stats (default: 7)
        site : str, optional
            Filter by storage site
        storage_root : str, optional
            Filter by LTS root
        
        Returns
        -------
        dict
            Summary with keys:
            - batches_in_queue: Number of batch/site/storage_root combinations needing conversion
            - files_in_queue: Total files needing conversion
            - bytes_in_queue: Total bytes to process (estimated)
            - batches_active: Number of currently processing batches
            - batches_completed: Number completed in last N days
            - batches_failed: Number failed in last N days
        
        Examples
        --------
        >>> # Overall summary
        >>> summary = db.orchestration.get_conversion_summary()
        >>> 
        >>> # NCSU/GROW_DATA-specific summary
        >>> summary = db.orchestration.get_conversion_summary(
        ...     site='NCSU',
        ...     storage_root='GROW_DATA'
        ... )
        """
        logger.info(f"Getting conversion summary (days={days}, "
                   f"site={site}, storage_root={storage_root})")
        
        try:
            # Get queue from our site-aware method
            queue = self.get_conversion_queue(
                limit=None,
                site=site,
                storage_root=storage_root
            )
            
            batches_in_queue = len(queue)
            files_in_queue = sum(b['files_needing_processing'] for b in queue)
            bytes_in_queue = sum(b['total_bytes'] for b in queue)
            
            # Get active conversions
            try:
                from .stages import StageStatus
                stages = StageStatus(self.conn)
                
                active_query = """
                    SELECT COUNT(*) as count
                    FROM processed.stage_status
                    WHERE stage = 'raw_to_jpg'
                      AND status = 'in_progress';
                """
                result = self.conn.fetch_one(active_query)
                batches_active = result['count'] if result else 0
            except Exception as e:
                logger.warning(f"Could not get active conversions: {e}")
                batches_active = 0
            
            # Get recently completed/failed
            try:
                completed_query = """
                    SELECT COUNT(*) as count
                    FROM processed.stage_status
                    WHERE stage = 'raw_to_jpg'
                      AND status = 'completed'
                      AND completed_at >= NOW() - INTERVAL '%s days';
                """
                result = self.conn.fetch_one(completed_query, (days,))
                batches_completed = result['count'] if result else 0
                
                failed_query = """
                    SELECT COUNT(*) as count
                    FROM processed.stage_status
                    WHERE stage = 'raw_to_jpg'
                      AND status = 'failed'
                      AND completed_at >= NOW() - INTERVAL '%s days';
                """
                result = self.conn.fetch_one(failed_query, (days,))
                batches_failed = result['count'] if result else 0
            except Exception as e:
                logger.warning(f"Could not get completion stats: {e}")
                batches_completed = 0
                batches_failed = 0
            
            summary = {
                'batches_in_queue': batches_in_queue,
                'files_in_queue': files_in_queue,
                'bytes_in_queue': bytes_in_queue,
                'batches_active': batches_active,
                'batches_completed': batches_completed,
                'batches_failed': batches_failed
            }
            
            if site:
                summary['site'] = site
            if storage_root:
                summary['storage_root'] = storage_root
            
            logger.info(f"Summary: {batches_in_queue} in queue, {batches_active} active")
            return summary
            
        except Exception as e:
            logger.error(f"Failed to get conversion summary: {e}")
            raise QueryError(f"Failed to get conversion summary: {e}") from e
    
    # ========================================
    # WORKFLOW COORDINATION METHODS
    # (Unchanged from previous version)
    # ========================================
    
    def start_batch_conversion(
        self,
        batch_id: str,
        job_id: str,
        site: Optional[str] = None,
        storage_root: Optional[str] = None,
        worker_name: Optional[str] = None
    ) -> Dict:
        """Start batch conversion workflow."""
        logger.info(f"Starting batch conversion: {batch_id} at "
                   f"{site or 'ALL'}/{storage_root or 'ALL'}")
        
        try:
            # Get files to process (site + storage_root filtered, deduplicated)
            files = self.get_batch_files_for_conversion(
                batch_id,
                site=site,
                storage_root=storage_root
            )
            
            if not files:
                logger.warning(f"No files to convert in {batch_id}")
                return {
                    'batch_id': batch_id,
                    'site': site,
                    'storage_root': storage_root,
                    'file_count': 0,
                    'files': [],
                    'started_at': datetime.now()
                }
            
            # Start stage (via stages component)
            try:
                from .stages import StageStatus
                stages = StageStatus(self.conn)
                
                stages.start(
                    batch_id=batch_id,
                    stage='raw_to_jpg',
                    job_id=job_id
                )
            except Exception as e:
                logger.warning(f"Could not create stage status: {e}")
            
            # Log event (via events component)
            try:
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
                        'site': site,
                        'storage_root': storage_root,
                        'worker': worker_name
                    }
                )
            except Exception as e:
                logger.warning(f"Could not log start event: {e}")
            
            started_at = datetime.now()
            
            logger.info(f"Started conversion for {batch_id}: {len(files)} files")
            
            return {
                'batch_id': batch_id,
                'site': site,
                'storage_root': storage_root,
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
        """Update conversion progress for a batch."""
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
        """Mark batch conversion as complete."""
        logger.info(f"Completing batch conversion: {batch_id} (success={success})")
        
        try:
            # Complete stage (via stages)
            try:
                from .stages import StageStatus
                stages = StageStatus(self.conn)
                
                stages.complete(
                    batch_id=batch_id,
                    stage='raw_to_jpg',
                    success=success,
                    files_processed=files_processed,
                    files_failed=files_failed,
                    error_message=error_message
                )
            except Exception as e:
                logger.warning(f"Could not complete stage status: {e}")
            
            # Update batch flags if successful (via batches)
            if success and files_failed == 0:
                try:
                    from .batches import BatchMetadata
                    batches = BatchMetadata(self.conn)
                    
                    batches.update_completion_flags(
                        batch_id,
                        raw_to_jpg_complete=True
                    )
                except Exception as e:
                    logger.warning(f"Could not update batch flags: {e}")
            
            # Log completion (via events)
            try:
                from .events import EventLogger
                events = EventLogger(self.conn)
                
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
            except Exception as e:
                logger.warning(f"Could not log completion event: {e}")
            
            logger.info(f"Completed {batch_id}: {files_processed} processed, {files_failed} failed")
            
        except Exception as e:
            logger.error(f"Failed to complete batch conversion: {e}")
            raise OrchestrationError(f"Failed to complete {batch_id}: {e}") from e
    
    def get_batch_progress(self, batch_id: str) -> Dict:
        """Get conversion progress for a batch."""
        logger.debug(f"Getting progress for {batch_id}")
        
        try:
            from .stages import StageStatus
            stages = StageStatus(self.conn)
            
            stage_info = stages.get_status(batch_id, 'raw_to_jpg')
            
            if not stage_info:
                return {
                    'batch_id': batch_id,
                    'status': 'not_started',
                    'files_processed': 0,
                    'files_failed': 0,
                    'started_at': None,
                    'completed_at': None,
                    'job_id': None
                }
            
            return {
                'batch_id': batch_id,
                'status': stage_info['status'],
                'files_processed': stage_info.get('files_processed', 0),
                'files_failed': stage_info.get('files_failed', 0),
                'started_at': stage_info.get('started_at'),
                'completed_at': stage_info.get('completed_at'),
                'job_id': stage_info.get('job_id')
            }
            
        except Exception as e:
            logger.error(f"Failed to get batch progress: {e}")
            raise QueryError(f"Failed to get progress for {batch_id}: {e}") from e
    
    def get_active_conversions(self) -> List[Dict]:
        """Get list of currently active conversions."""
        logger.debug("Getting active conversions")
        
        try:
            from .stages import StageStatus
            stages = StageStatus(self.conn)
            
            query = """
                SELECT *
                FROM processed.stage_status
                WHERE stage = 'raw_to_jpg'
                  AND status = 'in_progress'
                ORDER BY started_at DESC;
            """
            
            results = self.conn.fetch_all(query)
            return [dict(row) for row in results] if results else []
            
        except Exception as e:
            logger.error(f"Failed to get active conversions: {e}")
            raise QueryError(f"Failed to get active conversions: {e}") from e
    
    def get_failed_conversions(self, days: int = 7) -> List[Dict]:
        """Get list of recently failed conversions."""
        logger.debug(f"Getting failed conversions (days={days})")
        
        try:
            query = """
                SELECT *
                FROM processed.stage_status
                WHERE stage = 'raw_to_jpg'
                  AND status = 'failed'
                  AND completed_at >= NOW() - INTERVAL '%s days'
                ORDER BY completed_at DESC;
            """
            
            results = self.conn.fetch_all(query, (days,))
            return [dict(row) for row in results] if results else []
            
        except Exception as e:
            logger.error(f"Failed to get failed conversions: {e}")
            raise QueryError(f"Failed to get failed conversions: {e}") from e

def choose_primary_source(connection: ConnectionManager, batch_id: str) -> tuple:
    """
    Choose the best source for a batch when duplicates exist.
    
    Priority:
    1. site: JUNO > NCSU > CERES
    2. LTS root: longterm_images > dash_agir > GROW_DATA
    3. Fewest gaps (most complete)
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection
    batch_id : str
        Batch identifier
    
    Returns
    -------
    tuple
        (site, storage_root) or (None, None) if batch not found
    
    Example
    -------
    >>> site, storage_root = choose_primary_source(db._connection, 'MD_2024-06-01')
    >>> if site:
    ...     files = db.orchestration.get_batch_files_for_conversion(
    ...         'MD_2024-06-01',
    ...         site=site,
    ...         storage_root=storage_root
    ...     )
    """
    query = """
        SELECT 
            site,
            storage_root,
            COUNT(*) FILTER (
                WHERE data_state = 'upload_raw' 
                  AND LOWER(file_ext) IN ('raw', 'arw')
                  AND entry_type = 'file'
            ) as raw_count,
            COUNT(*) FILTER (
                WHERE data_state = 'developed_jpg' 
                  AND LOWER(file_ext) IN ('jpg', 'jpeg')
                  AND entry_type = 'file'
            ) as jpg_count
        FROM source.globus_file_index
        WHERE batch_id = %s
        GROUP BY site, storage_root
        HAVING COUNT(*) FILTER (
            WHERE data_state = 'upload_raw' 
              AND LOWER(file_ext) IN ('raw', 'arw')
              AND entry_type = 'file'
        ) > 0
    """
    
    sources = connection.fetch_all(query, (batch_id,))
    
    if not sources:
        return None, None
    
    # Define priorities (lower is better)
    site_priority = {'JUNO': 0, 'NCSU': 1, 'CERES': 2}
    storage_root_priority = {'longterm_images': 0, 'dash_agir': 1, 'GROW_DATA': 2}
    
    def score_source(src):
        loc_score = site_priority.get(src['site'], 999)
        root_score = storage_root_priority.get(src['storage_root'], 999)
        gap_count = src['raw_count'] - src['jpg_count']
        # Lower is better: (site priority, root priority, gaps)
        return (loc_score, root_score, gap_count)
    
    best_source = min(sources, key=score_source)
    
    logger.info(f"Chose primary source for {batch_id}: "
               f"{best_source['site']}/{best_source['storage_root']} "
               f"({best_source['raw_count'] - best_source['jpg_count']} gaps)")
    
    return best_source['site'], best_source['storage_root']


def get_all_batch_sources(connection: ConnectionManager, batch_id: str) -> List[Dict]:
    """
    Get all site/storage_root combinations where batch exists.
    
    Useful for understanding the full scope of duplicates.
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection
    batch_id : str
        Batch identifier
    
    Returns
    -------
    list of dict
        List of sources with keys: site, storage_root, raw_count, jpg_count, gap_count
    
    Example
    -------
    >>> sources = get_all_batch_sources(db._connection, 'MD_2024-06-01')
    >>> for src in sources:
    ...     print(f"{src['site']}/{src['storage_root']}: {src['gap_count']} gaps")
    """
    query = """
        SELECT 
            site,
            storage_root,
            COUNT(*) FILTER (
                WHERE data_state = 'upload_raw' 
                  AND LOWER(file_ext) IN ('raw', 'arw')
                  AND entry_type = 'file'
            ) as raw_count,
            COUNT(*) FILTER (
                WHERE data_state = 'developed_jpg' 
                  AND LOWER(file_ext) IN ('jpg', 'jpeg')
                  AND entry_type = 'file'
            ) as jpg_count,
            (
                COUNT(*) FILTER (
                    WHERE data_state = 'upload_raw' 
                      AND LOWER(file_ext) IN ('raw', 'arw')
                      AND entry_type = 'file'
                ) - 
                COUNT(*) FILTER (
                    WHERE data_state = 'developed_jpg' 
                      AND LOWER(file_ext) IN ('jpg', 'jpeg')
                      AND entry_type = 'file'
                )
            ) as gap_count
        FROM source.globus_file_index
        WHERE batch_id = %s
          AND entry_type = 'file'
        GROUP BY site, storage_root
        ORDER BY site, storage_root
    """
    
    sources = connection.fetch_all(query, (batch_id,))
    return [dict(row) for row in sources]