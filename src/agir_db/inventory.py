"""
Inventory synchronization from globus_file_index to processed tables.

This module provides methods for synchronizing file inventory from the source
globus_file_index table into the processed.batches and processed.images tables.

This enables automated population of metadata tables from existing file inventory.
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, date, timedelta

from psycopg2.extras import Json

from .connection import ConnectionManager
from .exceptions import (
    QueryError,
    InvalidParameterError,
    BatchNotFoundError
)


logger = logging.getLogger(__name__)


class InventorySync:
    """
    Synchronize inventory from globus_file_index to processed tables.
    
    This class provides methods for:
    - Syncing individual batches
    - Full inventory synchronization
    - Incremental updates
    - Reconciliation between source and processed tables
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Sync one batch
    ...     stats = db.inventory.sync_batch('MD_2025-01-01')
    ...     print(f"Synced {stats['images_inserted']} images")
    ...     
    ...     # Sync recent batches
    ...     stats = db.inventory.sync_recent(days=7)
    ...     
    ...     # Full sync
    ...     stats = db.inventory.sync_all(limit=100)
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("InventorySync initialized")
    
    def sync_batch(
        self,
        batch_id: str,
        update_existing: bool = False
    ) -> Dict:
        """
        Synchronize a single batch from globus_file_index.
        
        This will:
        1. Check if batch exists in globus_file_index
        2. Insert/update batch record in processed.batches
        3. Insert image records in processed.images
        4. Update file counts
        
        Parameters
        ----------
        batch_id : str
            Batch identifier to sync
        update_existing : bool, optional
            If True, update existing records (default: False, skip existing)
        
        Returns
        -------
        dict
            Statistics about the sync:
            - batch_existed: bool - Whether batch already existed
            - images_inserted: int - Number of new images added
            - images_skipped: int - Number of existing images skipped
            - files_found: int - Total files in globus_file_index
        
        Raises
        ------
        BatchNotFoundError
            If batch_id not found in globus_file_index
        QueryError
            If sync fails
        
        Examples
        --------
        >>> stats = db.inventory.sync_batch('MD_2025-01-01')
        >>> print(f"Synced {stats['images_inserted']} new images")
        """
        logger.info(f"Syncing batch: {batch_id}")
        
        # Check if batch exists in globus_file_index
        batch_info_query = """
            SELECT 
                batch_id,
                batch_state,
                batch_date,
                site,
                storage_root,
                COUNT(*) as file_count,
                SUM(size_bytes) as total_bytes
            FROM source.globus_file_index
            WHERE batch_id = %s
            GROUP BY batch_id, batch_state, batch_date, site, storage_root;
        """
        
        try:
            batch_info = self.conn.fetch_one(batch_info_query, (batch_id,))
            
            if not batch_info:
                raise BatchNotFoundError(f"Batch {batch_id} not found in globus_file_index")
            
            logger.debug(f"Found {batch_info['file_count']} files for batch {batch_id}")
            
        except BatchNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to query globus_file_index: {e}")
            raise QueryError(f"Failed to query batch info: {e}") from e
        
        # Check if batch already exists in processed.batches
        batch_exists_query = "SELECT 1 FROM processed.batches WHERE batch_id = %s;"
        batch_existed = self.conn.fetch_one(batch_exists_query, (batch_id,)) is not None
        
        # Insert or update batch record
        if batch_existed and not update_existing:
            logger.debug(f"Batch {batch_id} already exists, skipping batch insert")
        else:
            batch_insert_query = """
                INSERT INTO processed.batches (
                    batch_id, batch_state, batch_date, site, storage_root,
                    file_count_raw, total_bytes, processing_status, first_seen_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, 'pending', NOW()
                )
                ON CONFLICT (batch_id) DO UPDATE SET
                    file_count_raw = EXCLUDED.file_count_raw,
                    total_bytes = EXCLUDED.total_bytes,
                    updated_at = NOW();
            """
            
            try:
                self.conn.execute(
                    batch_insert_query,
                    (batch_id, batch_info['batch_state'], batch_info['batch_date'],
                     batch_info['site'], batch_info['storage_root'],
                     batch_info['file_count'], batch_info['total_bytes'])
                )
                action = "Updated" if batch_existed else "Inserted"
                logger.info(f"{action} batch record for {batch_id}")
            except Exception as e:
                logger.error(f"Failed to insert/update batch: {e}")
                raise QueryError(f"Failed to sync batch record: {e}") from e
        
        # Get files for this batch
        files_query = """
            SELECT 
                batch_id,
                file_name,
                rel_path,
                storage_root,
                size_bytes,
                file_ext,
                -- Compute base_name by removing extensions (handles .jpg.pp3, etc.)
                -- First remove sidecar extensions (.pp3, .xmp), then remove main extension
                REGEXP_REPLACE(
                    REGEXP_REPLACE(file_name, '\\.(pp3|xmp)$', '', 'i'),
                    '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                ) as base_name,
                mtime_iso
            FROM source.globus_file_index
            WHERE batch_id = %s
            AND LOWER(file_ext) IN ('raw', 'arw', 'dng', 'jpg', 'jpeg')
            ORDER BY file_name;
        """
        
        try:
            files = self.conn.fetch_all(files_query, (batch_id,))
            logger.debug(f"Found {len(files)} processable files for batch {batch_id}")
        except Exception as e:
            logger.error(f"Failed to query files: {e}")
            raise QueryError(f"Failed to query files for batch: {e}") from e
        
        # Filter to RAW files only for image registration (case-insensitive, includes ARW)
        raw_files = [f for f in files if f['file_ext'].lower() in ('raw', 'arw')]
        
        # Prepare image records
        images_to_insert = []
        for file in raw_files:
            image_id = file['base_name']
            
            # Check if image already exists
            if not update_existing:
                exists_query = "SELECT 1 FROM processed.images WHERE image_id = %s;"
                if self.conn.fetch_one(exists_query, (image_id,)):
                    logger.debug(f"Image {image_id} already exists, skipping")
                    continue
            
            images_to_insert.append({
                'image_id': image_id,
                'batch_id': batch_id,
                'file_name': file['file_name'],
                'file_ext': file['file_ext'],
                'file_path': file['rel_path'],
                'file_size_bytes': file['size_bytes'],
                'processing_status': 'pending'
            })
        
        # Bulk insert images
        images_inserted = 0
        if images_to_insert:
            insert_query = """
                INSERT INTO processed.images (
                    image_id, batch_id, file_name, file_ext, file_path, 
                    file_size_bytes, processing_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (image_id) DO NOTHING;
            """
            
            try:
                data = [
                    (img['image_id'], img['batch_id'], img['file_name'],
                     img['file_ext'], img['file_path'], img['file_size_bytes'],
                     img['processing_status'])
                    for img in images_to_insert
                ]
                
                self.conn.execute_many(insert_query, data)
                images_inserted = len(images_to_insert)
                logger.info(f"Inserted {images_inserted} image records")
            except Exception as e:
                logger.error(f"Failed to insert images: {e}")
                raise QueryError(f"Failed to insert images: {e}") from e
        
        # Return statistics
        stats = {
            'batch_existed': batch_existed,
            'images_inserted': images_inserted,
            'images_skipped': len(raw_files) - images_inserted,
            'files_found': len(files),
            'raw_files': len(raw_files)
        }
        
        logger.info(f"Sync complete for {batch_id}: {stats}")
        return stats
    
    def sync_all(
        self,
        batch_state: Optional[str] = None,
        limit: Optional[int] = None,
        update_existing: bool = False
    ) -> Dict:
        """
        Synchronize all batches from globus_file_index.
        
        Parameters
        ----------
        batch_state : str, optional
            Filter by state (e.g., 'MD', 'TX')
        limit : int, optional
            Maximum number of batches to sync
        update_existing : bool, optional
            If True, update existing records
        
        Returns
        -------
        dict
            Overall statistics:
            - batches_synced: int
            - batches_failed: int
            - total_images_inserted: int
            - elapsed_seconds: float
        
        Examples
        --------
        >>> # Sync all MD batches
        >>> stats = db.inventory.sync_all(batch_state='MD', limit=10)
        >>> print(f"Synced {stats['batches_synced']} batches")
        """
        start_time = datetime.now()
        logger.info(f"Starting full sync (state={batch_state}, limit={limit})")
        
        # Get list of batches from globus_file_index
        query = """
            SELECT DISTINCT batch_id
            FROM source.globus_file_index
            WHERE batch_id IS NOT NULL
        """
        
        params = []
        if batch_state:
            query += " AND batch_state = %s"
            params.append(batch_state)
        
        query += " ORDER BY batch_id DESC"
        
        if limit:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        try:
            batches = self.conn.fetch_all(query, tuple(params) if params else None)
            logger.info(f"Found {len(batches)} batches to sync")
        except Exception as e:
            logger.error(f"Failed to query batches: {e}")
            raise QueryError(f"Failed to query batches: {e}") from e
        
        # Sync each batch
        batches_synced = 0
        batches_failed = 0
        total_images_inserted = 0
        
        for batch_record in batches:
            batch_id = batch_record['batch_id']
            
            try:
                stats = self.sync_batch(batch_id, update_existing=update_existing)
                batches_synced += 1
                total_images_inserted += stats['images_inserted']
                
                if batches_synced % 10 == 0:
                    logger.info(f"Progress: {batches_synced}/{len(batches)} batches synced")
                    
            except Exception as e:
                logger.error(f"Failed to sync batch {batch_id}: {e}")
                batches_failed += 1
        
        elapsed = (datetime.now() - start_time).total_seconds()
        
        overall_stats = {
            'batches_synced': batches_synced,
            'batches_failed': batches_failed,
            'total_images_inserted': total_images_inserted,
            'elapsed_seconds': elapsed
        }
        
        logger.info(f"Full sync complete: {overall_stats}")
        return overall_stats
    
    def sync_recent(
        self,
        days: int = 7,
        update_existing: bool = False
    ) -> Dict:
        """
        Synchronize batches modified in the last N days.
        
        Parameters
        ----------
        days : int, optional
            Number of days to look back (default: 7)
        update_existing : bool, optional
            If True, update existing records
        
        Returns
        -------
        dict
            Sync statistics
        
        Examples
        --------
        >>> # Sync batches from last week
        >>> stats = db.inventory.sync_recent(days=7)
        """
        logger.info(f"Syncing batches from last {days} days")
        
        cutoff_date = datetime.now() - timedelta(days=days)
        
        # Get recently modified batches
        query = """
            SELECT DISTINCT batch_id
            FROM source.globus_file_index
            WHERE batch_id IS NOT NULL
            AND mtime_iso >= %s
            ORDER BY batch_id DESC;
        """
        
        try:
            batches = self.conn.fetch_all(query, (cutoff_date,))
            logger.info(f"Found {len(batches)} batches modified in last {days} days")
        except Exception as e:
            logger.error(f"Failed to query recent batches: {e}")
            raise QueryError(f"Failed to query recent batches: {e}") from e
        
        # Sync each batch
        batches_synced = 0
        batches_failed = 0
        total_images_inserted = 0
        
        for batch_record in batches:
            batch_id = batch_record['batch_id']
            
            try:
                stats = self.sync_batch(batch_id, update_existing=update_existing)
                batches_synced += 1
                total_images_inserted += stats['images_inserted']
            except Exception as e:
                logger.error(f"Failed to sync batch {batch_id}: {e}")
                batches_failed += 1
        
        return {
            'batches_synced': batches_synced,
            'batches_failed': batches_failed,
            'total_images_inserted': total_images_inserted,
            'days': days
        }
    
    def reconcile(
        self,
        batch_id: Optional[str] = None
    ) -> Dict:
        """
        Compare source and processed tables to find differences.
        
        This helps identify:
        - Batches in globus_file_index but not in processed.batches
        - Images in globus_file_index but not in processed.images
        - Batches in processed.batches but not in globus_file_index (deleted)
        
        Parameters
        ----------
        batch_id : str, optional
            If provided, reconcile only this batch
        
        Returns
        -------
        dict
            Reconciliation results:
            - missing_batches: list - Batches in source but not processed
            - missing_images: int - Images in source but not processed
            - orphaned_batches: list - Batches in processed but not source
            - orphaned_images: int - Images in processed but not source
        
        Examples
        --------
        >>> # Reconcile all
        >>> results = db.inventory.reconcile()
        >>> print(f"Missing {len(results['missing_batches'])} batches")
        
        >>> # Reconcile one batch
        >>> results = db.inventory.reconcile(batch_id='MD_2025-01-01')
        """
        logger.info(f"Running reconciliation (batch_id={batch_id})")
        
        results = {
            'missing_batches': [],
            'missing_images': 0,
            'orphaned_batches': [],
            'orphaned_images': 0
        }
        
        # Find batches in source but not in processed
        if batch_id:
            missing_batches_query = """
                SELECT DISTINCT g.batch_id
                FROM source.globus_file_index g
                WHERE g.batch_id = %s
                AND NOT EXISTS (
                    SELECT 1 FROM processed.batches p 
                    WHERE p.batch_id = g.batch_id
                );
            """
            params = (batch_id,)
        else:
            missing_batches_query = """
                SELECT DISTINCT g.batch_id
                FROM source.globus_file_index g
                WHERE g.batch_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM processed.batches p 
                    WHERE p.batch_id = g.batch_id
                );
            """
            params = None
        
        try:
            missing = self.conn.fetch_all(missing_batches_query, params)
            results['missing_batches'] = [b['batch_id'] for b in missing]
            logger.info(f"Found {len(results['missing_batches'])} missing batches")
        except Exception as e:
            logger.error(f"Failed to find missing batches: {e}")
        
        # Find images in source but not in processed
        if batch_id:
            missing_images_query = """
                SELECT COUNT(*) as count
                FROM source.globus_file_index g
                WHERE g.batch_id = %s
                AND LOWER(g.file_ext) IN ('raw', 'arw')
                AND NOT EXISTS (
                    SELECT 1 FROM processed.images i
                    WHERE i.image_id = REGEXP_REPLACE(
                        REGEXP_REPLACE(g.file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    )
                );
            """
            params = (batch_id,)
        else:
            missing_images_query = """
                SELECT COUNT(*) as count
                FROM source.globus_file_index g
                WHERE LOWER(g.file_ext) IN ('raw', 'arw')
                AND NOT EXISTS (
                    SELECT 1 FROM processed.images i
                    WHERE i.image_id = REGEXP_REPLACE(
                        REGEXP_REPLACE(g.file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    )
                );
            """
            params = None
        
        try:
            result = self.conn.fetch_one(missing_images_query, params)
            results['missing_images'] = result['count']
            logger.info(f"Found {results['missing_images']} missing images")
        except Exception as e:
            logger.error(f"Failed to find missing images: {e}")
        
        # Find batches in processed but not in source (orphaned)
        if batch_id:
            orphaned_batches_query = """
                SELECT p.batch_id
                FROM processed.batches p
                WHERE p.batch_id = %s
                AND NOT EXISTS (
                    SELECT 1 FROM source.globus_file_index g
                    WHERE g.batch_id = p.batch_id
                );
            """
            params = (batch_id,)
        else:
            orphaned_batches_query = """
                SELECT p.batch_id
                FROM processed.batches p
                WHERE NOT EXISTS (
                    SELECT 1 FROM source.globus_file_index g
                    WHERE g.batch_id = p.batch_id
                );
            """
            params = None
        
        try:
            orphaned = self.conn.fetch_all(orphaned_batches_query, params)
            results['orphaned_batches'] = [b['batch_id'] for b in orphaned]
            logger.info(f"Found {len(results['orphaned_batches'])} orphaned batches")
        except Exception as e:
            logger.error(f"Failed to find orphaned batches: {e}")
        
        # Find images in processed but not in source (orphaned)
        if batch_id:
            orphaned_images_query = """
                SELECT COUNT(*) as count
                FROM processed.images i
                WHERE i.batch_id = %s
                AND NOT EXISTS (
                    SELECT 1 FROM source.globus_file_index g
                    WHERE REGEXP_REPLACE(
                        REGEXP_REPLACE(g.file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    ) = i.image_id
                );
            """
            params = (batch_id,)
        else:
            orphaned_images_query = """
                SELECT COUNT(*) as count
                FROM processed.images i
                WHERE NOT EXISTS (
                    SELECT 1 FROM source.globus_file_index g
                    WHERE REGEXP_REPLACE(
                        REGEXP_REPLACE(g.file_name, '\\.(pp3|xmp)$', '', 'i'),
                        '\\.(raw|arw|dng|jpg|jpeg|tif|tiff)$', '', 'i'
                    ) = i.image_id
                );
            """
            params = None
        
        try:
            result = self.conn.fetch_one(orphaned_images_query, params)
            results['orphaned_images'] = result['count']
            logger.info(f"Found {results['orphaned_images']} orphaned images")
        except Exception as e:
            logger.error(f"Failed to find orphaned images: {e}")
        
        logger.info(f"Reconciliation complete: {results}")
        return results
    
    def get_sync_status(self) -> Dict:
        """
        Get overall synchronization status.
        
        Returns
        -------
        dict
            Status information:
            - source_batches: int - Total batches in globus_file_index
            - processed_batches: int - Total batches in processed.batches
            - source_raw_files: int - RAW files in globus_file_index
            - processed_images: int - Images in processed.images
            - sync_percentage: float - Percentage of source synced
        
        Examples
        --------
        >>> status = db.inventory.get_sync_status()
        >>> print(f"Sync: {status['sync_percentage']:.1f}%")
        """
        logger.debug("Getting sync status")
        
        try:
            # Count source batches
            source_batches_query = """
                SELECT COUNT(DISTINCT batch_id) as count
                FROM source.globus_file_index
                WHERE batch_id IS NOT NULL;
            """
            source_batches = self.conn.fetch_one(source_batches_query)['count']
            
            # Count processed batches
            processed_batches_query = "SELECT COUNT(*) as count FROM processed.batches;"
            processed_batches = self.conn.fetch_one(processed_batches_query)['count']
            
            # Count source RAW files (case-insensitive, includes ARW)
            source_raw_query = """
                SELECT COUNT(*) as count
                FROM source.globus_file_index
                WHERE LOWER(file_ext) IN ('raw', 'arw');
            """
            source_raw = self.conn.fetch_one(source_raw_query)['count']
            
            # Count processed images
            processed_images_query = "SELECT COUNT(*) as count FROM processed.images;"
            processed_images = self.conn.fetch_one(processed_images_query)['count']
            
            # Calculate sync percentage
            sync_percentage = 0.0
            if source_batches > 0:
                sync_percentage = (processed_batches / source_batches) * 100
            
            status = {
                'source_batches': source_batches,
                'processed_batches': processed_batches,
                'source_raw_files': source_raw,
                'processed_images': processed_images,
                'sync_percentage': sync_percentage,
                'batches_missing': source_batches - processed_batches,
                'images_missing': source_raw - processed_images
            }
            
            logger.info(f"Sync status: {sync_percentage:.1f}% complete")
            return status
            
        except Exception as e:
            logger.error(f"Failed to get sync status: {e}")
            raise QueryError(f"Failed to get sync status: {e}") from e