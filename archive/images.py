"""
Image metadata management.

This module provides methods for storing and querying image-level metadata
including EXIF data, bounding boxes, and processing status.
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime

from psycopg2.extras import Json

from .connection import ConnectionManager
from .exceptions import (
    QueryError,
    InvalidParameterError,
    DuplicateImageError,
    ImageNotFoundError
)


logger = logging.getLogger(__name__)


# Valid processing statuses
VALID_IMAGE_STATUSES = {
    'pending',
    'raw_to_dng',
    'dng_to_jpg',
    'metadata_extracted',
    'cutouts_generated',
    'completed',
    'failed'
}


class ImageMetadata:
    """
    Manage image metadata records.
    
    This class provides methods for storing and querying image-level data
    including EXIF information, bounding boxes, and processing status.
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Insert image record
    ...     db.images.insert(
    ...         image_id='MD_1234',
    ...         batch_id='MD_2025-01-01',
    ...         file_name='MD_1234.raw',
    ...         exif_data={'Make': 'Canon', 'Model': 'EOS R5'}
    ...     )
    ...     
    ...     # Query images by batch
    ...     images = db.images.get_by_batch('MD_2025-01-01')
    ...     
    ...     # Update bounding boxes
    ...     db.images.update_bounding_boxes(
    ...         'MD_1234',
    ...         [{'x': 100, 'y': 200, 'width': 50, 'height': 50, 'class': 'deer'}]
    ...     )
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("ImageMetadata initialized")
    
    def _validate_status(self, status: str) -> None:
        """Validate that processing status is valid."""
        if status not in VALID_IMAGE_STATUSES:
            raise InvalidParameterError(
                f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_IMAGE_STATUSES))}"
            )
    
    def insert(
        self,
        image_id: str,
        batch_id: str,
        file_name: str,
        file_ext: Optional[str] = None,
        file_path: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        processing_status: str = 'pending',
        exif_data: Optional[Dict] = None,
        camera_make: Optional[str] = None,
        camera_model: Optional[str] = None,
        capture_datetime: Optional[datetime] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Insert a new image record.
        
        Parameters
        ----------
        image_id : str
            Unique image identifier (typically base filename without extension)
        batch_id : str
            Batch this image belongs to
        file_name : str
            Original filename
        file_ext : str, optional
            File extension (e.g., 'raw', 'jpg')
        file_path : str, optional
            Relative path from batch root
        file_size_bytes : int, optional
            File size in bytes
        processing_status : str, optional
            Processing status (default: 'pending')
        exif_data : dict, optional
            Complete EXIF data
        camera_make : str, optional
            Camera manufacturer
        camera_model : str, optional
            Camera model
        capture_datetime : datetime, optional
            When photo was captured
        width : int, optional
            Image width in pixels
        height : int, optional
            Image height in pixels
        metadata : dict, optional
            Additional metadata
        
        Raises
        ------
        DuplicateImageError
            If image_id already exists
        InvalidParameterError
            If processing_status is invalid
        QueryError
            If database insert fails
        
        Examples
        --------
        >>> db.images.insert(
        ...     image_id='MD_1234',
        ...     batch_id='MD_2025-01-01',
        ...     file_name='MD_1234.raw',
        ...     file_size_bytes=25000000,
        ...     camera_make='Canon',
        ...     camera_model='EOS R5'
        ... )
        """
        self._validate_status(processing_status)
        
        query = """
            INSERT INTO processed.images (
                image_id, batch_id, file_name, file_ext, file_path, file_size_bytes,
                processing_status, exif_data, camera_make, camera_model,
                capture_datetime, width, height, metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            );
        """
        
        logger.info(f"Inserting image: {image_id} (batch: {batch_id})")
        
        try:
            # Wrap dicts with Json() for JSONB conversion
            exif_json = Json(exif_data) if exif_data is not None else None
            metadata_json = Json(metadata) if metadata is not None else None
            
            self.conn.execute(
                query,
                (image_id, batch_id, file_name, file_ext, file_path, file_size_bytes,
                 processing_status, exif_json, camera_make, camera_model,
                 capture_datetime, width, height, metadata_json)
            )
            logger.info(f"Image {image_id} inserted successfully")
        except Exception as e:
            if 'duplicate key' in str(e).lower():
                raise DuplicateImageError(f"Image {image_id} already exists") from e
            logger.error(f"Failed to insert image: {e}")
            raise QueryError(f"Failed to insert image {image_id}: {e}") from e
    
    def insert_bulk(self, images: List[Dict]) -> int:
        """
        Insert multiple image records in bulk.
        
        More efficient than calling insert() multiple times.
        
        Parameters
        ----------
        images : list of dict
            List of image records (same fields as insert())
        
        Returns
        -------
        int
            Number of images inserted
        
        Raises
        ------
        QueryError
            If bulk insert fails
        
        Examples
        --------
        >>> images = [
        ...     {
        ...         'image_id': 'MD_1234',
        ...         'batch_id': 'MD_2025-01-01',
        ...         'file_name': 'MD_1234.raw'
        ...     },
        ...     {
        ...         'image_id': 'MD_1235',
        ...         'batch_id': 'MD_2025-01-01',
        ...         'file_name': 'MD_1235.raw'
        ...     }
        ... ]
        >>> count = db.images.insert_bulk(images)
        >>> print(f"Inserted {count} images")
        """
        if not images:
            return 0
        
        query = """
            INSERT INTO processed.images (
                image_id, batch_id, file_name, file_ext, file_path, file_size_bytes,
                processing_status, exif_data, camera_make, camera_model,
                capture_datetime, width, height, metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (image_id) DO NOTHING;
        """
        
        logger.info(f"Bulk inserting {len(images)} images")
        
        try:
            # Prepare data tuples
            data = []
            for img in images:
                # Wrap JSONB fields
                exif_json = Json(img.get('exif_data')) if img.get('exif_data') else None
                metadata_json = Json(img.get('metadata')) if img.get('metadata') else None
                
                data.append((
                    img['image_id'],
                    img['batch_id'],
                    img['file_name'],
                    img.get('file_ext'),
                    img.get('file_path'),
                    img.get('file_size_bytes'),
                    img.get('processing_status', 'pending'),
                    exif_json,
                    img.get('camera_make'),
                    img.get('camera_model'),
                    img.get('capture_datetime'),
                    img.get('width'),
                    img.get('height'),
                    metadata_json
                ))
            
            self.conn.execute_many(query, data)
            logger.info(f"Bulk insert completed: {len(images)} images")
            return len(images)
        except Exception as e:
            logger.error(f"Failed to bulk insert images: {e}")
            raise QueryError(f"Failed to bulk insert images: {e}") from e
    
    def update_status(
        self,
        image_id: str,
        processing_status: str
    ) -> None:
        """
        Update image processing status.
        
        Parameters
        ----------
        image_id : str
            Image identifier
        processing_status : str
            New processing status
        
        Raises
        ------
        ImageNotFoundError
            If image doesn't exist
        InvalidParameterError
            If status is invalid
        QueryError
            If update fails
        """
        self._validate_status(processing_status)
        
        query = """
            UPDATE processed.images
            SET processing_status = %s
            WHERE image_id = %s;
        """
        
        logger.info(f"Updating status for {image_id}: {processing_status}")
        
        try:
            self.conn.execute(query, (processing_status, image_id))
            
            # Check if image exists
            if self.conn.fetch_one("SELECT 1 FROM processed.images WHERE image_id = %s", (image_id,)) is None:
                raise ImageNotFoundError(f"Image {image_id} not found")
            
            logger.debug(f"Status updated for {image_id}")
        except ImageNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to update status: {e}")
            raise QueryError(f"Failed to update status for {image_id}: {e}") from e
    
    def update_bounding_boxes(
        self,
        image_id: str,
        bounding_boxes: List[Dict]
    ) -> None:
        """
        Update bounding boxes for an image.
        
        Parameters
        ----------
        image_id : str
            Image identifier
        bounding_boxes : list of dict
            List of bounding boxes, each with:
            - x, y: Top-left corner
            - width, height: Box dimensions
            - class: Object class (e.g., 'deer', 'car')
            - confidence: Detection confidence (0-1)
        
        Examples
        --------
        >>> boxes = [
        ...     {'x': 100, 'y': 200, 'width': 50, 'height': 50,
        ...      'class': 'deer', 'confidence': 0.95},
        ...     {'x': 300, 'y': 150, 'width': 60, 'height': 70,
        ...      'class': 'deer', 'confidence': 0.88}
        ... ]
        >>> db.images.update_bounding_boxes('MD_1234', boxes)
        """
        query = """
            UPDATE processed.images
            SET 
                bounding_boxes = %s,
                detection_count = %s
            WHERE image_id = %s;
        """
        
        logger.info(f"Updating bounding boxes for {image_id}: {len(bounding_boxes)} detections")
        
        try:
            boxes_json = Json(bounding_boxes) if bounding_boxes else None
            self.conn.execute(query, (boxes_json, len(bounding_boxes), image_id))
            logger.debug(f"Bounding boxes updated for {image_id}")
        except Exception as e:
            logger.error(f"Failed to update bounding boxes: {e}")
            raise QueryError(f"Failed to update bounding boxes for {image_id}: {e}") from e
    
    def get_by_id(self, image_id: str) -> Optional[Dict]:
        """
        Get image record by ID.
        
        Parameters
        ----------
        image_id : str
            Image identifier
        
        Returns
        -------
        dict or None
            Image record, or None if not found
        """
        query = "SELECT * FROM processed.images WHERE image_id = %s;"
        
        logger.debug(f"Getting image: {image_id}")
        
        try:
            result = self.conn.fetch_one(query, (image_id,))
            return result
        except Exception as e:
            logger.error(f"Failed to get image: {e}")
            raise QueryError(f"Failed to get image {image_id}: {e}") from e
    
    def get_by_batch(
        self,
        batch_id: str,
        processing_status: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get images for a specific batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        processing_status : str, optional
            Filter by processing status
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Image records
        
        Examples
        --------
        >>> # All images in batch
        >>> images = db.images.get_by_batch('MD_2025-01-01')
        
        >>> # Only pending images
        >>> pending = db.images.get_by_batch('MD_2025-01-01', processing_status='pending')
        """
        if processing_status is not None:
            self._validate_status(processing_status)
        
        query = "SELECT * FROM processed.images WHERE batch_id = %s"
        params = [batch_id]
        
        if processing_status is not None:
            query += " AND processing_status = %s"
            params.append(processing_status)
        
        query += " ORDER BY created_at"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Getting images for batch {batch_id} (status={processing_status})")
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Found {len(results)} image(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to get images by batch: {e}")
            raise QueryError(f"Failed to get images for batch {batch_id}: {e}") from e
    
    def get_with_detections(
        self,
        batch_id: Optional[str] = None,
        min_detections: int = 1,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get images with object detections.
        
        Parameters
        ----------
        batch_id : str, optional
            Filter by batch
        min_detections : int, optional
            Minimum number of detections (default: 1)
        limit : int, optional
            Maximum number of results
        
        Returns
        -------
        list of dict
            Images with detections
        
        Examples
        --------
        >>> # All images with detections
        >>> images = db.images.get_with_detections()
        
        >>> # Images with 5+ detections
        >>> images = db.images.get_with_detections(min_detections=5)
        """
        query = """
            SELECT * FROM processed.images_with_detections
            WHERE detection_count >= %s
        """
        params = [min_detections]
        
        if batch_id is not None:
            query += " AND batch_id = %s"
            params.append(batch_id)
        
        query += " ORDER BY detection_count DESC"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Getting images with detections (min={min_detections})")
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Found {len(results)} image(s) with detections")
            return results
        except Exception as e:
            logger.error(f"Failed to get images with detections: {e}")
            raise QueryError(f"Failed to get images with detections: {e}") from e