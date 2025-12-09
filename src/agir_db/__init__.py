"""
AgirDB - Agricultural Image Repository Database API

A PostgreSQL database API for managing agricultural image processing pipelines,
including RAW → DNG → JPG conversion, object detection, segmentation, and
feature extraction workflows.

Basic Usage
-----------
>>> from agir_db import AgirDB
>>>
>>> with AgirDB() as db:
...     # Get batches needing processing
...     batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
...     
...     # Start processing
...     db.stages.start(batch_id, 'raw_to_jpg', job_id='12345')
...     
...     # Insert image metadata
...     db.images.insert_bulk(image_data)
...     
...     # Complete stage
...     db.stages.complete(batch_id, 'raw_to_jpg', success=True)

Components
----------
- gaps: Pipeline gap analysis (identify work to do)
- stages: Stage status tracking (prevent duplicate work)
- images: Image metadata management
- transfers: JUNO transfer operations
- events: Processing event logging
- inventory: File inventory synchronization
- analytics: Reporting and statistics
- batches: Batch metadata management
- migration: SQLite data import

See Also
--------
AgirDB : Main API class
setup_logging : Configure logging
"""

__version__ = '1.0.0'
__author__ = 'Matthew Kutugata'

# Main API
from .api import AgirDB

# Connection management
from .connection import ConnectionManager

# Domain components
from .gaps import PipelineGaps
from .stages import StageStatus
from .events import EventLogger
from .images import ImageMetadata
from .batches import BatchMetadata
from .inventory import InventorySync
from .transfers import TransferManager
from .analytics import Analytics
from .migration import Migration
from .orchestration import Orchestration

# Logging utilities
from .utils.logging_setup import setup_logging, get_logger, set_level

# Exceptions - organized by category
from .exceptions import (
    # Base
    AgirDBError,
    
    # Connection
    ConnectionError,
    TransactionError,
    
    # Query
    QueryError,
    
    # Duplicates
    DuplicateError,
    DuplicateImageError,
    DuplicateBatchError,
    DuplicateDetectionError,
    
    # Not Found
    NotFoundError,
    ImageNotFoundError,
    BatchNotFoundError,
    TransferNotFoundError,
    
    # Stage
    StageError,
    StageAlreadyInProgressError,
    StageNotStartedError,
    InvalidStageError,
    
    # Transfer
    TransferError,
    TransferAlreadyInProgressError,
    GlobusError,
    
    # Migration
    MigrationError,
    SQLiteConnectionError,
    MigrationValidationError,
    
    # Validation
    ValidationError,
    InvalidParameterError,
    MissingRequiredFieldError,
    InvalidImageIdError,
    InvalidBatchIdError,

    # Orchestration
    OrchestrationError,
)

# Public API
__all__ = [
    # Main classes
    'AgirDB',
    'ConnectionManager',
    
    # Domain components
    'PipelineGaps',
    'StageStatus',
    'EventLogger',
    'ImageMetadata',
    'BatchMetadata',
    'InventorySync',
    'TransferManager',
    'Analytics',
    'Migration',
    'Orchestration',
    
    # Logging
    'setup_logging',
    'get_logger',
    'set_level',
    
    # Exceptions
    'AgirDBError',
    'ConnectionError',
    'TransactionError',
    'QueryError',
    'DuplicateError',
    'DuplicateImageError',
    'DuplicateBatchError',
    'DuplicateDetectionError',
    'NotFoundError',
    'ImageNotFoundError',
    'BatchNotFoundError',
    'TransferNotFoundError',
    'StageError',
    'StageAlreadyInProgressError',
    'StageNotStartedError',
    'InvalidStageError',
    'TransferError',
    'TransferAlreadyInProgressError',
    'GlobusError',
    'MigrationError',
    'SQLiteConnectionError',
    'MigrationValidationError',
    'ValidationError',
    'InvalidParameterError',
    'MissingRequiredFieldError',
    'InvalidImageIdError',
    'InvalidBatchIdError',
    'OrchestrationError',
]