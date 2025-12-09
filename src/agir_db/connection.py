"""
Database connection management for AgirDB.

This module provides the ConnectionManager class which handles:
- Connection lifecycle (connect, close)
- Transaction management (commit, rollback)
- Query execution with proper error handling
- Cursor management
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from psycopg2 import sql

from .exceptions import ConnectionError, QueryError, TransactionError


logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages PostgreSQL database connections and transactions.
    
    This class handles all low-level database operations including:
    - Connection establishment and cleanup
    - Transaction management (commit/rollback)
    - Query execution with proper error handling
    - Cursor management
    
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
    
    Examples
    --------
    >>> conn = ConnectionManager(host='localhost', port=5432, dbname='agir')
    >>> conn.connect()
    >>> result = conn.fetch_one("SELECT * FROM processed.batch_metadata WHERE batch_id = %s", ('MD_2025-01-01',))
    >>> conn.commit()
    >>> conn.close()
    
    Or using as context manager:
    >>> with ConnectionManager() as conn:
    ...     result = conn.fetch_all("SELECT * FROM processed.developed_images LIMIT 10")
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        dbname: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None
    ):
        """Initialize connection manager with database credentials."""
        # Load from environment if not provided
        self.host = host or os.environ.get('PGHOST')
        self.port = port or int(os.environ.get('PGPORT', 5432))
        self.dbname = dbname or os.environ.get('PGDATABASE', 'agir')
        self.user = user or os.environ.get('PGUSER', os.environ.get('USER'))
        self.password = password  # Optional - will use .pgpass if None
        
        self._conn: Optional[psycopg2.extensions.connection] = None
        
        # Validate required parameters
        if not self.host:
            raise ConnectionError("Database host not provided and PGHOST not set")
        if not self.user:
            raise ConnectionError("Database user not provided and PGUSER not set")
        
        logger.debug(
            f"ConnectionManager initialized: {self.user}@{self.host}:{self.port}/{self.dbname}"
        )
    
    def connect(self) -> psycopg2.extensions.connection:
        """
        Establish database connection.
        
        Returns
        -------
        psycopg2.connection
            Active database connection
        
        Raises
        ------
        ConnectionError
            If connection fails
        """
        if self._conn is not None:
            logger.warning("Connection already established")
            return self._conn
        
        try:
            conn_params = {
                'host': self.host,
                'port': self.port,
                'dbname': self.dbname,
                'user': self.user,
            }
            
            # Only include password if provided (otherwise uses .pgpass)
            if self.password:
                conn_params['password'] = self.password
            
            logger.info(f"Connecting to {self.user}@{self.host}:{self.port}/{self.dbname}")
            self._conn = psycopg2.connect(**conn_params)
            self._conn.autocommit = False  # Explicit transaction control
            
            logger.info("Database connection established")
            return self._conn
            
        except psycopg2.Error as e:
            error_msg = f"Failed to connect to database: {e}"
            logger.error(error_msg)
            raise ConnectionError(error_msg) from e
    
    def close(self) -> None:
        """
        Close database connection.
        
        Safe to call multiple times. Does nothing if connection is already closed.
        """
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info("Database connection closed")
            except psycopg2.Error as e:
                logger.warning(f"Error closing connection: {e}")
            finally:
                self._conn = None
    
    def commit(self) -> None:
        """
        Commit current transaction.
        
        Raises
        ------
        TransactionError
            If commit fails
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        try:
            self._conn.commit()
            logger.debug("Transaction committed")
        except psycopg2.Error as e:
            error_msg = f"Failed to commit transaction: {e}"
            logger.error(error_msg)
            raise TransactionError(error_msg) from e
    
    def rollback(self) -> None:
        """
        Rollback current transaction.
        
        Raises
        ------
        TransactionError
            If rollback fails
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        try:
            self._conn.rollback()
            logger.debug("Transaction rolled back")
        except psycopg2.Error as e:
            error_msg = f"Failed to rollback transaction: {e}"
            logger.error(error_msg)
            raise TransactionError(error_msg) from e
    
    def get_cursor(
        self,
        cursor_factory=None
    ) -> psycopg2.extensions.cursor:
        """
        Get a database cursor.
        
        Parameters
        ----------
        cursor_factory : cursor factory, optional
            Cursor factory class (e.g., psycopg2.extras.RealDictCursor)
        
        Returns
        -------
        psycopg2.cursor
            Database cursor
        
        Raises
        ------
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        return self._conn.cursor(cursor_factory=cursor_factory)
    
    def execute(
        self,
        query: str,
        params: Optional[Tuple] = None
    ) -> psycopg2.extensions.cursor:
        """
        Execute a query and return cursor.
        
        Use this for INSERT, UPDATE, DELETE queries where you don't need results.
        
        Parameters
        ----------
        query : str
            SQL query
        params : tuple, optional
            Query parameters
        
        Returns
        -------
        psycopg2.cursor
            Cursor with query results
        
        Raises
        ------
        QueryError
            If query execution fails
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        try:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            logger.debug(f"Executed query: {query[:100]}...")
            return cursor
            
        except psycopg2.Error as e:
            error_msg = f"Query failed: {e}\nQuery: {query[:200]}"
            logger.error(error_msg)
            raise QueryError(error_msg) from e
    
    def execute_many(
        self,
        query: str,
        params_list: List[Tuple]
    ) -> None:
        """
        Execute a query multiple times with different parameters (bulk insert).
        
        More efficient than calling execute() in a loop.
        
        Parameters
        ----------
        query : str
            SQL query with placeholders
        params_list : list of tuples
            List of parameter tuples
        
        Raises
        ------
        QueryError
            If query execution fails
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        if not params_list:
            logger.warning("execute_many called with empty params_list")
            return
        
        try:
            cursor = self._conn.cursor()
            psycopg2.extras.execute_batch(cursor, query, params_list)
            logger.debug(f"Executed batch query with {len(params_list)} parameter sets")
            cursor.close()
            
        except psycopg2.Error as e:
            error_msg = f"Batch query failed: {e}\nQuery: {query[:200]}"
            logger.error(error_msg)
            raise QueryError(error_msg) from e
    
    def fetch_one(
        self,
        query: str,
        params: Optional[Tuple] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a query and fetch one result as a dictionary.
        
        Parameters
        ----------
        query : str
            SQL query
        params : tuple, optional
            Query parameters
        
        Returns
        -------
        dict or None
            Single row as dictionary, or None if no results
        
        Raises
        ------
        QueryError
            If query execution fails
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        try:
            cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(query, params)
            result = cursor.fetchone()
            cursor.close()
            
            return dict(result) if result else None
            
        except psycopg2.Error as e:
            error_msg = f"Query failed: {e}\nQuery: {query[:200]}"
            logger.error(error_msg)
            raise QueryError(error_msg) from e
    
    def fetch_all(
        self,
        query: str,
        params: Optional[Tuple] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute a query and fetch all results as a list of dictionaries.
        
        Parameters
        ----------
        query : str
            SQL query
        params : tuple, optional
            Query parameters
        
        Returns
        -------
        list of dict
            All rows as dictionaries
        
        Raises
        ------
        QueryError
            If query execution fails
        ConnectionError
            If not connected
        """
        if self._conn is None:
            raise ConnectionError("Not connected to database")
        
        try:
            cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(query, params)
            results = cursor.fetchall()
            cursor.close()
            
            return [dict(row) for row in results]
            
        except psycopg2.Error as e:
            error_msg = f"Query failed: {e}\nQuery: {query[:200]}"
            logger.error(error_msg)
            raise QueryError(error_msg) from e
    
    @property
    def is_connected(self) -> bool:
        """Check if database connection is active."""
        return self._conn is not None and not self._conn.closed
    
    def __enter__(self):
        """Context manager entry: connect to database."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: commit or rollback, then close."""
        if exc_type is None:
            try:
                self.commit()
            except Exception as e:
                logger.error(f"Failed to commit on exit: {e}")
                self.rollback()
        else:
            self.rollback()
        
        self.close()
        return False  # Don't suppress exceptions
    
    def __repr__(self) -> str:
        """String representation of connection manager."""
        status = "connected" if self.is_connected else "disconnected"
        return f"ConnectionManager({self.user}@{self.host}:{self.port}/{self.dbname}, {status})"