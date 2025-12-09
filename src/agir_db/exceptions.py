"""
Custom exceptions for AgirDB.

This module defines the exception hierarchy for all AgirDB operations.
All exceptions inherit from AgirDBError for easy catching.
"""


class AgirDBError(Exception):
    """
    Base exception for all AgirDB errors.
    
    All custom exceptions in this module inherit from this base class,
    allowing users to catch all AgirDB-related errors with a single except clause.
    """
    pass


# ==============================================================================
# CONNECTION ERRORS
# ==============================================================================

class ConnectionError(AgirDBError):
    """
    Database connection failed.
    
    Raised when unable to establish connection to PostgreSQL database.
    Common causes:
    - Database server not running
    - Invalid credentials
    - Network issues
    - Incorrect host/port
    """
    pass


class TransactionError(AgirDBError):
    """
    Transaction operation failed.
    
    Raised when commit or rollback operations fail.
    """
    pass


# ==============================================================================
# QUERY ERRORS
# ==============================================================================

class QueryError(AgirDBError):
    """
    Database query failed.
    
    Raised when SQL query execution fails.
    Common causes:
    - Syntax errors in SQL
    - Missing tables/columns
    - Permission issues
    - Data type mismatches
    """
    pass


# ==============================================================================
# DUPLICATE ERRORS
# ==============================================================================

class DuplicateError(AgirDBError):
    """
    Record already exists.
    
    Base class for all duplicate record errors.
    """
    pass


class DuplicateImageError(DuplicateError):
    """
    Image already exists in database.
    
    Raised when attempting to insert an image with an image_id that already exists.
    """
    pass


class DuplicateBatchError(DuplicateError):
    """
    Batch already exists in database.
    
    Raised when attempting to insert a batch with a batch_id that already exists.
    """
    pass


class DuplicateDetectionError(DuplicateError):
    """
    Detection already exists in database.
    
    Raised when attempting to insert a detection with a cutout_id that already exists.
    """
    pass


# ==============================================================================
# NOT FOUND ERRORS
# ==============================================================================

class NotFoundError(AgirDBError):
    """
    Record not found.
    
    Base class for all record not found errors.
    """
    pass


class ImageNotFoundError(NotFoundError):
    """
    Image not found in database.
    
    Raised when querying for an image_id that doesn't exist.
    """
    pass


class BatchNotFoundError(NotFoundError):
    """
    Batch not found in database.
    
    Raised when querying for a batch_id that doesn't exist.
    """
    pass


class TransferNotFoundError(NotFoundError):
    """
    Transfer record not found in database.
    
    Raised when querying for a transfer_id that doesn't exist.
    """
    pass


# ==============================================================================
# STAGE ERRORS
# ==============================================================================

class StageError(AgirDBError):
    """
    Stage operation failed.
    
    Base class for all stage-related errors.
    """
    pass


class StageAlreadyInProgressError(StageError):
    """
    Stage is already in progress for this batch.
    
    Raised when attempting to start a stage that's already running.
    This prevents duplicate processing of the same batch.
    """
    pass


class StageNotStartedError(StageError):
    """
    Stage has not been started.
    
    Raised when attempting to complete a stage that was never started.
    """
    pass


class InvalidStageError(StageError):
    """
    Invalid stage name.
    
    Raised when referencing a stage name that doesn't exist in the pipeline.
    """
    pass


# ==============================================================================
# TRANSFER ERRORS
# ==============================================================================

class TransferError(AgirDBError):
    """
    Transfer operation failed.
    
    Base class for all transfer-related errors.
    """
    pass


class TransferAlreadyInProgressError(TransferError):
    """
    Transfer is already in progress for this batch.
    
    Raised when attempting to start a transfer that's already running.
    """
    pass


class GlobusError(TransferError):
    """
    Globus operation failed.
    
    Raised when Globus CLI commands fail or Globus API returns errors.
    """
    pass


# ==============================================================================
# MIGRATION ERRORS
# ==============================================================================

class MigrationError(AgirDBError):
    """
    Migration operation failed.
    
    Base class for all migration-related errors.
    """
    pass


class SQLiteConnectionError(MigrationError):
    """
    Failed to connect to SQLite database.
    
    Raised when unable to open or read the source SQLite database.
    """
    pass


class MigrationValidationError(MigrationError):
    """
    Migration validation failed.
    
    Raised when post-migration validation detects data inconsistencies.
    """
    pass


# ==============================================================================
# ORCHESTRATION ERRORS
# ==============================================================================

class OrchestrationError(AgirDBError):
    """
    Orchestration operation failed.
    
    Raised when high-level workflow orchestration encounters errors.
    Common causes:
    - Missing batch data
    - Invalid workflow state
    - Conversion pipeline failures
    """
    pass


# ==============================================================================
# VALIDATION ERRORS
# ==============================================================================

class ValidationError(AgirDBError):
    """
    Data validation failed.
    
    Base class for all validation errors.
    """
    pass


class InvalidParameterError(ValidationError):
    """
    Invalid parameter value.
    
    Raised when a method receives an invalid parameter value.
    """
    pass


class MissingRequiredFieldError(ValidationError):
    """
    Required field is missing.
    
    Raised when a required field is not provided.
    """
    pass


class InvalidImageIdError(ValidationError):
    """
    Invalid image_id format.
    
    Raised when image_id doesn't match expected format (STATE_EPOCH).
    """
    pass


class InvalidBatchIdError(ValidationError):
    """
    Invalid batch_id format.
    
    Raised when batch_id doesn't match expected format (STATE_YYYY-MM-DD).
    """
    pass