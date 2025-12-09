"""
Event logging system for tracking all operations.

This module provides comprehensive logging of system operations including:
- Stage lifecycle events (start, complete, fail, reset)
- Query operations (gap queries, status checks)
- Errors and warnings
- Transfer operations
- Audit trail for debugging

Events are stored in processed.events table with structured metadata.
"""

import logging
import socket
import getpass
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from psycopg2.extras import Json

from .connection import ConnectionManager
from .exceptions import QueryError, InvalidParameterError


logger = logging.getLogger(__name__)


# Valid severity levels
VALID_SEVERITIES = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}

# Common event types (not exhaustive)
EVENT_TYPES = {
    # Stage events
    'stage.started',
    'stage.completed',
    'stage.failed',
    'stage.reset',
    
    # Gap query events
    'gap.batches_query',
    'gap.files_query',
    'gap.summary_query',
    
    # Status query events
    'status.query',
    'status.in_progress',
    'status.failed',
    
    # Transfer events
    'transfer.started',
    'transfer.completed',
    'transfer.failed',
    
    # Inventory events
    'inventory.sync',
    'inventory.update',
    
    # Error events
    'error.connection',
    'error.query',
    'error.processing',
    'error.validation',
    
    # System events
    'system.startup',
    'system.shutdown',
    'system.maintenance'
}


class EventLogger:
    """
    Log and query system events.
    
    This class provides methods for recording events and querying the event log.
    All operations (stage changes, queries, errors) can be logged for audit trail
    and debugging.
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Log an event
    ...     db.events.log_event(
    ...         event_type='stage.started',
    ...         severity='INFO',
    ...         message='Started RAW to JPG conversion',
    ...         batch_id='MD_2025-01-01',
    ...         stage='raw_to_jpg',
    ...         metadata={'files_count': 150}
    ...     )
    ...     
    ...     # Query events
    ...     recent = db.events.get_recent_events(limit=10)
    ...     errors = db.events.get_errors(limit=20)
    ...     batch_events = db.events.get_batch_events('MD_2025-01-01')
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        self.hostname = socket.gethostname()
        try:
            self.user_name = getpass.getuser()
        except:
            self.user_name = 'unknown'
        logger.debug("EventLogger initialized")
    
    def _validate_severity(self, severity: str) -> None:
        """Validate that severity level is valid."""
        if severity not in VALID_SEVERITIES:
            raise InvalidParameterError(
                f"Invalid severity '{severity}'. Must be one of: {', '.join(sorted(VALID_SEVERITIES))}"
            )
    
    def log_event(
        self,
        event_type: str,
        severity: str,
        message: str,
        batch_id: Optional[str] = None,
        stage: Optional[str] = None,
        job_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        source: Optional[str] = None,
        error_type: Optional[str] = None,
        stack_trace: Optional[str] = None
    ) -> int:
        """
        Log an event to the database.
        
        This is the main logging method. All events should flow through here.
        
        Parameters
        ----------
        event_type : str
            Event type in dotted notation (e.g., 'stage.started', 'error.connection')
        severity : str
            Severity level: 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
        message : str
            Human-readable event message
        batch_id : str, optional
            Related batch identifier
        stage : str, optional
            Related pipeline stage
        job_id : str, optional
            Related job/worker identifier
        metadata : dict, optional
            Structured event data (parameters, results, etc.)
        source : str, optional
            Component that logged the event (e.g., 'StageStatus', 'PipelineGaps')
        error_type : str, optional
            Exception type (for ERROR/CRITICAL events)
        stack_trace : str, optional
            Stack trace (for ERROR/CRITICAL events)
        
        Returns
        -------
        int
            Event ID of the logged event
        
        Raises
        ------
        InvalidParameterError
            If severity is invalid
        QueryError
            If database insert fails
        
        Examples
        --------
        >>> # Log a stage start
        >>> event_id = db.events.log_event(
        ...     event_type='stage.started',
        ...     severity='INFO',
        ...     message='Started RAW to JPG conversion for MD_2025-01-01',
        ...     batch_id='MD_2025-01-01',
        ...     stage='raw_to_jpg',
        ...     job_id='12345',
        ...     source='StageStatus'
        ... )
        
        >>> # Log an error
        >>> event_id = db.events.log_event(
        ...     event_type='error.processing',
        ...     severity='ERROR',
        ...     message='Failed to convert RAW file',
        ...     batch_id='MD_2025-01-01',
        ...     metadata={'file': 'MD_123.raw', 'error': 'Invalid format'},
        ...     error_type='ConversionError',
        ...     stack_trace='...'
        ... )
        """
        self._validate_severity(severity)
        
        query = """
            INSERT INTO processed.events (
                event_type, severity, message,
                batch_id, stage, job_id,
                metadata, hostname, user_name, source,
                error_type, stack_trace
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            )
            RETURNING event_id;
        """
        
        logger.debug(f"Logging event: {event_type} [{severity}] - {message[:50]}...")
        
        try:
            # Wrap metadata dict with Json() for JSONB conversion
            metadata_json = Json(metadata) if metadata is not None else None
            
            result = self.conn.fetch_one(
                query,
                (event_type, severity, message,
                 batch_id, stage, job_id,
                 metadata_json, self.hostname, self.user_name, source,
                 error_type, stack_trace)
            )
            event_id = result['event_id']
            logger.debug(f"Event logged: event_id={event_id}")
            return event_id
        except Exception as e:
            logger.error(f"Failed to log event: {e}")
            raise QueryError(f"Failed to log event: {e}") from e
    
    def get_events(
        self,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        batch_id: Optional[str] = None,
        stage: Optional[str] = None,
        job_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = 100,
        order: str = 'DESC'
    ) -> List[Dict]:
        """
        Query events with flexible filtering.
        
        Parameters
        ----------
        event_type : str, optional
            Filter by event type (can use % for wildcards)
        severity : str, optional
            Filter by severity level
        batch_id : str, optional
            Filter by batch
        stage : str, optional
            Filter by stage
        job_id : str, optional
            Filter by job
        since : datetime, optional
            Filter events after this time
        until : datetime, optional
            Filter events before this time
        limit : int, optional
            Maximum number of events to return (default 100)
        order : str, optional
            Sort order: 'DESC' (newest first) or 'ASC' (oldest first)
        
        Returns
        -------
        list of dict
            Event records matching filters
        
        Raises
        ------
        QueryError
            If query fails
        
        Examples
        --------
        >>> # Get all stage events
        >>> events = db.events.get_events(event_type='stage.%', limit=50)
        
        >>> # Get errors for a batch
        >>> errors = db.events.get_events(
        ...     batch_id='MD_2025-01-01',
        ...     severity='ERROR'
        ... )
        
        >>> # Get events from last hour
        >>> from datetime import datetime, timedelta
        >>> recent = db.events.get_events(
        ...     since=datetime.now() - timedelta(hours=1)
        ... )
        """
        if severity is not None:
            self._validate_severity(severity)
        
        # Build query
        conditions = []
        params = []
        
        if event_type is not None:
            if '%' in event_type:
                conditions.append("event_type LIKE %s")
            else:
                conditions.append("event_type = %s")
            params.append(event_type)
        
        if severity is not None:
            conditions.append("severity = %s")
            params.append(severity)
        
        if batch_id is not None:
            conditions.append("batch_id = %s")
            params.append(batch_id)
        
        if stage is not None:
            conditions.append("stage = %s")
            params.append(stage)
        
        if job_id is not None:
            conditions.append("job_id = %s")
            params.append(job_id)
        
        if since is not None:
            conditions.append("created_at >= %s")
            params.append(since)
        
        if until is not None:
            conditions.append("created_at <= %s")
            params.append(until)
        
        # Construct query
        query = "SELECT * FROM processed.events"
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        order_clause = "DESC" if order.upper() == "DESC" else "ASC"
        query += f" ORDER BY created_at {order_clause}"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Querying events with {len(conditions)} filter(s)")
        
        try:
            results = self.conn.fetch_all(query, tuple(params) if params else None)
            logger.debug(f"Found {len(results)} event(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to query events: {e}")
            raise QueryError(f"Failed to query events: {e}") from e
    
    def get_recent_events(
        self,
        hours: int = 24,
        limit: Optional[int] = 100
    ) -> List[Dict]:
        """
        Get recent events (last N hours).
        
        Convenience method for time-based queries.
        
        Parameters
        ----------
        hours : int, optional
            Number of hours to look back (default 24)
        limit : int, optional
            Maximum number of events (default 100)
        
        Returns
        -------
        list of dict
            Recent event records
        
        Examples
        --------
        >>> # Last 24 hours
        >>> recent = db.events.get_recent_events()
        
        >>> # Last hour
        >>> last_hour = db.events.get_recent_events(hours=1, limit=50)
        """
        since = datetime.now() - timedelta(hours=hours)
        return self.get_events(since=since, limit=limit)
    
    def get_errors(
        self,
        batch_id: Optional[str] = None,
        stage: Optional[str] = None,
        hours: Optional[int] = None,
        limit: Optional[int] = 100
    ) -> List[Dict]:
        """
        Get error and critical events.
        
        Parameters
        ----------
        batch_id : str, optional
            Filter by batch
        stage : str, optional
            Filter by stage
        hours : int, optional
            Only errors from last N hours
        limit : int, optional
            Maximum number of events (default 100)
        
        Returns
        -------
        list of dict
            Error event records
        
        Examples
        --------
        >>> # All recent errors
        >>> errors = db.events.get_errors()
        
        >>> # Errors for specific batch
        >>> batch_errors = db.events.get_errors(batch_id='MD_2025-01-01')
        
        >>> # Errors from last hour
        >>> recent_errors = db.events.get_errors(hours=1)
        """
        since = datetime.now() - timedelta(hours=hours) if hours else None
        
        query = "SELECT * FROM processed.error_events"
        
        conditions = []
        params = []
        
        if batch_id is not None:
            conditions.append("batch_id = %s")
            params.append(batch_id)
        
        if stage is not None:
            conditions.append("stage = %s")
            params.append(stage)
        
        if since is not None:
            conditions.append("created_at >= %s")
            params.append(since)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_at DESC"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Querying error events")
        
        try:
            results = self.conn.fetch_all(query, tuple(params) if params else None)
            logger.debug(f"Found {len(results)} error event(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to query error events: {e}")
            raise QueryError(f"Failed to query error events: {e}") from e
    
    def get_batch_events(
        self,
        batch_id: str,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get all events for a specific batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        event_type : str, optional
            Filter by event type (can use % for wildcards)
        severity : str, optional
            Filter by severity
        limit : int, optional
            Maximum number of events
        
        Returns
        -------
        list of dict
            Event records for this batch
        
        Examples
        --------
        >>> # All events for batch
        >>> events = db.events.get_batch_events('MD_2025-01-01')
        
        >>> # Only stage events
        >>> stage_events = db.events.get_batch_events(
        ...     'MD_2025-01-01',
        ...     event_type='stage.%'
        ... )
        
        >>> # Only errors
        >>> errors = db.events.get_batch_events(
        ...     'MD_2025-01-01',
        ...     severity='ERROR'
        ... )
        """
        return self.get_events(
            batch_id=batch_id,
            event_type=event_type,
            severity=severity,
            limit=limit
        )
    
    def get_stage_events(
        self,
        batch_id: Optional[str] = None,
        stage: Optional[str] = None,
        limit: Optional[int] = 100
    ) -> List[Dict]:
        """
        Get stage operation events.
        
        Parameters
        ----------
        batch_id : str, optional
            Filter by batch
        stage : str, optional
            Filter by stage
        limit : int, optional
            Maximum number of events (default 100)
        
        Returns
        -------
        list of dict
            Stage event records
        
        Examples
        --------
        >>> # All stage events
        >>> events = db.events.get_stage_events()
        
        >>> # Stage events for batch
        >>> events = db.events.get_stage_events(batch_id='MD_2025-01-01')
        
        >>> # Specific stage
        >>> events = db.events.get_stage_events(stage='raw_to_jpg')
        """
        query = "SELECT * FROM processed.stage_events"
        
        conditions = []
        params = []
        
        if batch_id is not None:
            conditions.append("batch_id = %s")
            params.append(batch_id)
        
        if stage is not None:
            conditions.append("stage = %s")
            params.append(stage)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_at DESC"
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Querying stage events")
        
        try:
            results = self.conn.fetch_all(query, tuple(params) if params else None)
            logger.debug(f"Found {len(results)} stage event(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to query stage events: {e}")
            raise QueryError(f"Failed to query stage events: {e}") from e
    
    def get_event_summary(self, hours: int = 24) -> List[Dict]:
        """
        Get event summary by type and severity.
        
        Parameters
        ----------
        hours : int, optional
            Time window in hours (default 24)
        
        Returns
        -------
        list of dict
            Summary records with event counts
        
        Examples
        --------
        >>> summary = db.events.get_event_summary()
        >>> for row in summary:
        ...     print(f"{row['event_type']}: {row['event_count']} events")
        """
        if hours == 24:
            # Use the view for 24-hour summary
            query = "SELECT * FROM processed.event_summary_24h;"
            params = None
        else:
            # Custom time window
            since = datetime.now() - timedelta(hours=hours)
            query = """
                SELECT
                    event_type,
                    severity,
                    COUNT(*) AS event_count,
                    MIN(created_at) AS first_occurrence,
                    MAX(created_at) AS last_occurrence
                FROM processed.events
                WHERE created_at >= %s
                GROUP BY event_type, severity
                ORDER BY event_count DESC, last_occurrence DESC;
            """
            params = (since,)
        
        logger.debug(f"Querying event summary (last {hours} hours)")
        
        try:
            results = self.conn.fetch_all(query, params)
            logger.debug(f"Retrieved {len(results)} summary record(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to query event summary: {e}")
            raise QueryError(f"Failed to query event summary: {e}") from e
    
    def search_events(
        self,
        search_text: str,
        limit: Optional[int] = 100
    ) -> List[Dict]:
        """
        Full-text search in event messages.
        
        Parameters
        ----------
        search_text : str
            Text to search for in messages
        limit : int, optional
            Maximum number of results (default 100)
        
        Returns
        -------
        list of dict
            Matching event records
        
        Examples
        --------
        >>> # Search for "failed"
        >>> events = db.events.search_events('failed')
        
        >>> # Search for multiple terms
        >>> events = db.events.search_events('conversion error')
        """
        query = """
            SELECT *
            FROM processed.events
            WHERE to_tsvector('english', message) @@ plainto_tsquery('english', %s)
            ORDER BY created_at DESC
        """
        
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        
        query += ";"
        
        logger.debug(f"Searching events for: '{search_text}'")
        
        try:
            results = self.conn.fetch_all(query, (search_text,))
            logger.debug(f"Found {len(results)} matching event(s)")
            return results
        except Exception as e:
            logger.error(f"Failed to search events: {e}")
            raise QueryError(f"Failed to search events: {e}") from e