"""
Migration tools for importing legacy databases.

This module provides methods for migrating data from legacy SQLite
databases to the new PostgreSQL schema, with validation and
transformation capabilities.
"""

import logging
import sqlite3
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime, date

from .connection import ConnectionManager
from .exceptions import (
    QueryError,
    MigrationError,
    ValidationError,
    InvalidParameterError
)


logger = logging.getLogger(__name__)


class Migration:
    """
    Migrate data from legacy databases.
    
    This class provides methods for:
    - Importing from SQLite databases
    - Transforming legacy data formats
    - Validating data integrity
    - Tracking migration progress
    
    Parameters
    ----------
    connection : ConnectionManager
        PostgreSQL database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Import from SQLite
    ...     stats = db.migration.import_sqlite_db(
    ...         '/path/to/legacy.db',
    ...         table_mapping={'old_table': 'new_table'}
    ...     )
    ...     
    ...     # Validate migration
    ...     issues = db.migration.validate_migration('MD_2024-06-01')
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("Migration initialized")
    
    def import_sqlite_db(
        self,
        sqlite_path: str,
        batch_id: Optional[str] = None,
        dry_run: bool = False,
        skip_existing: bool = True
    ) -> Dict:
        """
        Import data from legacy SQLite database.
        
        This method imports batch and image data from a legacy SQLite
        database, transforming it to match the new schema.
        
        Parameters
        ----------
        sqlite_path : str
            Path to SQLite database file
        batch_id : str, optional
            Batch ID to assign (if not in database)
        dry_run : bool, optional
            If True, don't actually insert data
        skip_existing : bool, optional
            If True, skip batches that already exist
        
        Returns
        -------
        dict
            Migration statistics:
            - batches_imported
            - images_imported
            - batches_skipped
            - errors
        
        Raises
        ------
        MigrationError
            If SQLite file doesn't exist or is corrupt
        
        Examples
        --------
        >>> stats = db.migration.import_sqlite_db(
        ...     '/data/legacy/batch_MD_2024-06-01.db',
        ...     batch_id='MD_2024-06-01'
        ... )
        >>> print(f"Imported {stats['images_imported']} images")
        """
        sqlite_path = Path(sqlite_path)
        
        if not sqlite_path.exists():
            raise MigrationError(f"SQLite file not found: {sqlite_path}")
        
        logger.info(f"Importing from SQLite: {sqlite_path}")
        
        stats = {
            'batches_imported': 0,
            'images_imported': 0,
            'batches_skipped': 0,
            'errors': []
        }
        
        try:
            # Connect to SQLite
            sqlite_conn = sqlite3.connect(str(sqlite_path))
            sqlite_conn.row_factory = sqlite3.Row
            sqlite_cursor = sqlite_conn.cursor()
            
            # Detect schema (check what tables exist)
            sqlite_cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' 
                ORDER BY name;
            """)
            tables = [row[0] for row in sqlite_cursor.fetchall()]
            logger.debug(f"Found tables: {tables}")
            
            # Import based on detected schema
            if 'batch_metadata' in tables or 'batches' in tables:
                # Import batch metadata
                batch_stats = self._import_batch_metadata(
                    sqlite_cursor, batch_id, dry_run, skip_existing
                )
                stats['batches_imported'] += batch_stats['imported']
                stats['batches_skipped'] += batch_stats['skipped']
            
            if 'image_metadata' in tables or 'images' in tables:
                # Import image metadata
                image_stats = self._import_image_metadata(
                    sqlite_cursor, batch_id, dry_run
                )
                stats['images_imported'] += image_stats['imported']
            
            sqlite_conn.close()
            
            logger.info(f"Import complete: {stats}")
            return stats
            
        except sqlite3.Error as e:
            logger.error(f"SQLite error: {e}")
            raise MigrationError(f"Failed to read SQLite database: {e}") from e
        except Exception as e:
            logger.error(f"Migration failed: {e}")
            raise MigrationError(f"Migration failed: {e}") from e
    
    def _import_batch_metadata(
        self,
        sqlite_cursor,
        batch_id: Optional[str],
        dry_run: bool,
        skip_existing: bool
    ) -> Dict:
        """Import batch metadata from SQLite."""
        stats = {'imported': 0, 'skipped': 0}
        
        # Try different table names
        table_name = None
        for name in ['batch_metadata', 'batches', 'batch_info']:
            sqlite_cursor.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{name}'"
            )
            if sqlite_cursor.fetchone():
                table_name = name
                break
        
        if not table_name:
            logger.warning("No batch metadata table found")
            return stats
        
        logger.debug(f"Importing from table: {table_name}")
        
        # Get batch records
        sqlite_cursor.execute(f"SELECT * FROM {table_name}")
        rows = sqlite_cursor.fetchall()
        
        for row in rows:
            row_dict = dict(row)
            
            # Determine batch_id
            bid = batch_id or row_dict.get('batch_id') or row_dict.get('id')
            
            if not bid:
                logger.warning("No batch_id found, skipping row")
                continue
            
            # Check if exists
            if skip_existing:
                check_query = "SELECT 1 FROM processed.batches WHERE batch_id = %s"
                if self.conn.fetch_one(check_query, (bid,)):
                    logger.debug(f"Batch {bid} already exists, skipping")
                    stats['skipped'] += 1
                    continue
            
            # Extract and transform data
            batch_data = self._transform_batch_data(row_dict, bid)
            
            if dry_run:
                logger.info(f"[DRY RUN] Would import batch: {bid}")
                stats['imported'] += 1
                continue
            
            # Insert batch
            try:
                insert_query = """
                    INSERT INTO processed.batches (
                        batch_id, batch_state, batch_date,
                        site, storage_root,
                        file_count_raw, total_bytes,
                        processing_status, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (batch_id) DO NOTHING;
                """
                
                from psycopg2.extras import Json
                
                self.conn.execute(
                    insert_query,
                    (
                        batch_data['batch_id'],
                        batch_data['batch_state'],
                        batch_data['batch_date'],
                        batch_data['site'],
                        batch_data['storage_root'],
                        batch_data['file_count_raw'],
                        batch_data['total_bytes'],
                        batch_data['processing_status'],
                        Json(batch_data['metadata']) if batch_data['metadata'] else None
                    )
                )
                
                stats['imported'] += 1
                logger.debug(f"Imported batch: {bid}")
                
            except Exception as e:
                logger.error(f"Failed to import batch {bid}: {e}")
                continue
        
        return stats
    
    def _import_image_metadata(
        self,
        sqlite_cursor,
        batch_id: Optional[str],
        dry_run: bool
    ) -> Dict:
        """Import image metadata from SQLite."""
        stats = {'imported': 0}
        
        # Try different table names
        table_name = None
        for name in ['image_metadata', 'images', 'image_info']:
            sqlite_cursor.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{name}'"
            )
            if sqlite_cursor.fetchone():
                table_name = name
                break
        
        if not table_name:
            logger.warning("No image metadata table found")
            return stats
        
        logger.debug(f"Importing images from table: {table_name}")
        
        # Get image records
        sqlite_cursor.execute(f"SELECT * FROM {table_name}")
        rows = sqlite_cursor.fetchall()
        
        images_to_insert = []
        
        for row in rows:
            row_dict = dict(row)
            
            # Extract and transform data
            image_data = self._transform_image_data(row_dict, batch_id)
            
            if not image_data:
                continue
            
            images_to_insert.append(image_data)
            
            # Batch insert every 1000 images
            if len(images_to_insert) >= 1000:
                if not dry_run:
                    count = self._bulk_insert_images(images_to_insert)
                    stats['imported'] += count
                else:
                    stats['imported'] += len(images_to_insert)
                
                logger.debug(f"Imported {len(images_to_insert)} images")
                images_to_insert = []
        
        # Insert remaining
        if images_to_insert:
            if not dry_run:
                count = self._bulk_insert_images(images_to_insert)
                stats['imported'] += count
            else:
                stats['imported'] += len(images_to_insert)
        
        return stats
    
    def _transform_batch_data(self, row_dict: Dict, batch_id: str) -> Dict:
        """Transform legacy batch data to new schema."""
        # Extract batch_state from batch_id (e.g., MD_2024-06-01 -> MD)
        batch_state = batch_id.split('_')[0] if '_' in batch_id else 'MD'
        
        # Extract date
        batch_date = None
        if 'batch_date' in row_dict:
            batch_date = row_dict['batch_date']
        elif 'date' in row_dict:
            batch_date = row_dict['date']
        else:
            # Try to extract from batch_id
            parts = batch_id.split('_')
            if len(parts) > 1:
                try:
                    batch_date = date.fromisoformat(parts[1])
                except:
                    batch_date = date.today()
        
        # Parse date if string
        if isinstance(batch_date, str):
            try:
                batch_date = date.fromisoformat(batch_date)
            except:
                batch_date = date.today()
        
        return {
            'batch_id': batch_id,
            'batch_state': batch_state,
            'batch_date': batch_date,
            'site': row_dict.get('site') or row_dict.get('location') or 'UNKNOWN',
            'storage_root': row_dict.get('storage_root') or row_dict.get('lts_root'),
            'file_count_raw': row_dict.get('file_count') or row_dict.get('raw_count') or 0,
            'total_bytes': row_dict.get('total_bytes') or row_dict.get('size_bytes') or 0,
            'processing_status': row_dict.get('status') or 'pending',
            'metadata': {
                'imported_from': 'sqlite',
                'original_data': {k: v for k, v in row_dict.items() if k not in [
                    'batch_id', 'id', 'batch_date', 'date', 'site', 'location'
                ]}
            }
        }
    
    def _transform_image_data(self, row_dict: Dict, batch_id: Optional[str]) -> Optional[Dict]:
        """Transform legacy image data to new schema."""
        # Get image_id
        image_id = (
            row_dict.get('image_id') or 
            row_dict.get('id') or 
            row_dict.get('file_name', '').replace('.raw', '').replace('.RAW', '')
        )
        
        if not image_id:
            return None
        
        # Get batch_id
        bid = batch_id or row_dict.get('batch_id')
        if not bid:
            return None
        
        return {
            'image_id': image_id,
            'batch_id': bid,
            'file_name': row_dict.get('file_name') or f"{image_id}.raw",
            'file_ext': row_dict.get('file_ext') or 'raw',
            'file_path': row_dict.get('file_path') or row_dict.get('path'),
            'file_size_bytes': row_dict.get('size_bytes') or row_dict.get('file_size'),
            'processing_status': row_dict.get('status') or 'pending',
            'camera_make': row_dict.get('camera_make'),
            'camera_model': row_dict.get('camera_model'),
            'width': row_dict.get('width'),
            'height': row_dict.get('height')
        }
    
    def _bulk_insert_images(self, images: List[Dict]) -> int:
        """Bulk insert images."""
        if not images:
            return 0
        
        insert_query = """
            INSERT INTO processed.images (
                image_id, batch_id, file_name, file_ext,
                file_path, file_size_bytes, processing_status,
                camera_make, camera_model, width, height
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (image_id) DO NOTHING;
        """
        
        try:
            data = [
                (
                    img['image_id'], img['batch_id'], img['file_name'], img['file_ext'],
                    img['file_path'], img['file_size_bytes'], img['processing_status'],
                    img['camera_make'], img['camera_model'], img['width'], img['height']
                )
                for img in images
            ]
            
            self.conn.execute_many(insert_query, data)
            return len(images)
            
        except Exception as e:
            logger.error(f"Bulk insert failed: {e}")
            return 0
    
    def validate_migration(self, batch_id: str) -> Dict:
        """
        Validate migrated batch data.
        
        Parameters
        ----------
        batch_id : str
            Batch to validate
        
        Returns
        -------
        dict
            Validation results:
            - valid: bool
            - issues: list of validation issues
            - batch_exists: bool
            - image_count: int
            - missing_required_fields: list
        
        Examples
        --------
        >>> result = db.migration.validate_migration('MD_2024-06-01')
        >>> if not result['valid']:
        ...     print(f"Issues: {result['issues']}")
        """
        logger.info(f"Validating batch: {batch_id}")
        
        result = {
            'valid': True,
            'issues': [],
            'batch_exists': False,
            'image_count': 0,
            'missing_required_fields': []
        }
        
        # Check batch exists
        batch_query = "SELECT * FROM processed.batches WHERE batch_id = %s"
        batch = self.conn.fetch_one(batch_query, (batch_id,))
        
        if not batch:
            result['valid'] = False
            result['issues'].append(f"Batch {batch_id} not found")
            return result
        
        result['batch_exists'] = True
        
        # Check required fields
        required_fields = ['batch_state', 'batch_date', 'site']
        for field in required_fields:
            if not batch.get(field):
                result['missing_required_fields'].append(field)
                result['issues'].append(f"Missing required field: {field}")
        
        # Check images
        image_query = "SELECT COUNT(*) as count FROM processed.images WHERE batch_id = %s"
        image_result = self.conn.fetch_one(image_query, (batch_id,))
        result['image_count'] = image_result['count'] if image_result else 0
        
        if result['image_count'] == 0:
            result['issues'].append("No images found for batch")
        
        # Check for orphaned images (batch doesn't exist in batches table)
        # Already checked above
        
        if result['issues']:
            result['valid'] = False
        
        logger.info(f"Validation result: {result}")
        return result
    
    def get_migration_summary(self) -> Dict:
        """
        Get summary of all migrated data.
        
        Returns
        -------
        dict
            Migration summary:
            - total_batches: int
            - total_images: int
            - batches_with_metadata: int
            - images_with_exif: int
        
        Examples
        --------
        >>> summary = db.migration.get_migration_summary()
        >>> print(f"Migrated: {summary['total_batches']} batches")
        """
        logger.debug("Getting migration summary")
        
        query = """
            SELECT
                (SELECT COUNT(*) FROM processed.batches) as total_batches,
                (SELECT COUNT(*) FROM processed.images) as total_images,
                (SELECT COUNT(*) FROM processed.batches WHERE metadata IS NOT NULL) as batches_with_metadata,
                (SELECT COUNT(*) FROM processed.images WHERE exif_data IS NOT NULL) as images_with_exif;
        """
        
        try:
            result = self.conn.fetch_one(query)
            return dict(result) if result else {}
        except Exception as e:
            logger.error(f"Failed to get summary: {e}")
            raise QueryError(f"Failed to get migration summary: {e}") from e