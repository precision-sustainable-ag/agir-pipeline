"""
Main API facade for AgirDB.

This module provides the AgirDB class which coordinates all database operations
through two domain managers:

  - db.orchestration : lease claims, transfer tracking, run report ingestion
  - db.transfers     : Globus transfer detection and execution helpers

Usage
-----
>>> from agir_db import AgirDB
>>>
>>> # Using context manager (recommended)
>>> with AgirDB() as db:
...     transfer = db.orchestration.get_completed_windowed_input_transfer(
...         batch_id, stage, window_key
...     )
...     db.orchestration.claim_stage_lease(batch_id, stage, ...)
>>>
>>> # Manual connection management
>>> db = AgirDB()
>>> db.connect()
>>> try:
...     # do work
...     db.commit()
... except Exception as e:
...     db.rollback()
...     raise
... finally:
...     db.close()
"""

import logging
from typing import Optional

from .connection import ConnectionManager
from .exceptions import AgirDBError
from .orchestration import OrchestrationManager

# Domain class imports
from .transfers import TransferManager


logger = logging.getLogger(__name__)


class AgirDB:
    """
    Main interface for AgirDB operations.
    
    Attributes
    ----------
    orchestration : OrchestrationManager
        Lease claims, windowed transfer tracking, run report ingestion, and
        file-index queries. This is the primary interface for orchestration logic.
    transfers : TransferManager
        Globus transfer detection (gap views), command construction, submission,
        and task polling. Also used by the legacy batch-level staging loop.
    
    Parameters
    ----------
    host : str, optional
        Database host. If None, reads from PGHOST environment variable.
    port : int, optional
        Database port. If None, reads from PGPORT environment variable.
    dbname : str, optional
        Database name. If None, reads from PGDATABASE environment variable.
    user : str, optional
        Database user. If None, reads from PGUSER environment variable.
    password : str, optional
        Database password. If None, uses .pgpass file.
    
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        dbname: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None
    ):
        """Initialize AgirDB with database credentials."""
        logger.info("Initializing AgirDB")
        
        # Initialize connection manager
        self._connection = ConnectionManager(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password
        )
        
        # Initialize domain components
        self.transfers = TransferManager(self._connection)
                
        logger.info("AgirDB initialized")

        self.orchestration = OrchestrationManager(self._connection)
    
    def connect(self) -> None:
        """
        Establish database connection.
        
        Raises
        ------
        ConnectionError
            If connection fails
        """
        self._connection.connect()
    
    def close(self) -> None:
        """Close database connection."""
        self._connection.close()
    
    def commit(self) -> None:
        """
        Commit current transaction.
        
        Raises
        ------
        TransactionError
            If commit fails
        """
        self._connection.commit()
    
    def rollback(self) -> None:
        """
        Rollback current transaction.
        
        Raises
        ------
        TransactionError
            If rollback fails
        """
        self._connection.rollback()
    
    @property
    def is_connected(self) -> bool:
        """Check if database connection is active."""
        return self._connection.is_connected
    
    def __enter__(self):
        """
        Context manager entry: connect to database.
        
        Returns
        -------
        AgirDB
            Self for use in with statement
        """
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit: commit or rollback, then close.
        
        Commits transaction if no exception occurred, otherwise rolls back.
        Always closes the connection.
        
        Parameters
        ----------
        exc_type : type
            Exception type (None if no exception)
        exc_val : Exception
            Exception value (None if no exception)
        exc_tb : traceback
            Exception traceback (None if no exception)
        
        Returns
        -------
        bool
            False to propagate exceptions
        """
        if exc_type is None:
            try:
                self.commit()
                logger.info("Transaction committed")
            except Exception as e:
                logger.error(f"Failed to commit on exit: {e}")
                self.rollback()
                raise
        else:
            logger.warning(f"Rolling back due to exception: {exc_type.__name__}")
            self.rollback()
        
        self.close()
        return False  # Propagate exceptions
    
    def __repr__(self) -> str:
        """String representation of AgirDB."""
        status = "connected" if self.is_connected else "disconnected"
        return f"AgirDB({status})"