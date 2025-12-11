"""
Analytics and reporting for pipeline operations.

This module provides methods for generating reports and statistics
on processing pipeline performance, throughput, errors, and storage.
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime, timedelta

from .connection import ConnectionManager
from .exceptions import QueryError, InvalidParameterError


logger = logging.getLogger(__name__)


class Analytics:
    """
    Generate analytics and reports on pipeline operations.
    
    This class provides methods for:
    - Processing statistics and throughput
    - Error analysis
    - Storage utilization
    - Transfer performance
    - Batch summaries
    
    Parameters
    ----------
    connection : ConnectionManager
        Database connection manager
    
    Examples
    --------
    >>> from agir_db import AgirDB
    >>> 
    >>> with AgirDB() as db:
    ...     # Get processing stats
    ...     stats = db.analytics.get_processing_stats(days=7)
    ...     
    ...     # Get error rates
    ...     errors = db.analytics.get_error_rates()
    ...     
    ...     # Get throughput
    ...     throughput = db.analytics.get_throughput(days=30)
    """
    
    def __init__(self, connection: ConnectionManager):
        """Initialize with database connection."""
        self.conn = connection
        logger.debug("Analytics initialized")
    
    def get_pipeline_overview(self) -> Dict:
        """
        Get high-level pipeline statistics.
        
        Returns
        -------
        dict
            Pipeline overview with counts and totals:
            - total_batches
            - completed_batches
            - failed_batches
            - in_progress_batches
            - total_images
            - running_stages
            - active_transfers
            - total_storage_gb
            - earliest_batch_date
            - latest_batch_date
        
        Examples
        --------
        >>> overview = db.analytics.get_pipeline_overview()
        >>> print(f"Total batches: {overview['total_batches']}")
        >>> print(f"Storage: {overview['total_storage_gb']} GB")
        """
        logger.info("Getting pipeline overview")
        
        query = "SELECT * FROM processed.pipeline_overview;"
        
        try:
            result = self.conn.fetch_one(query)
            
            if not result:
                logger.warning("No pipeline overview data available")
                return {}
            
            logger.debug(f"Pipeline overview retrieved: {result['total_batches']} batches")
            return dict(result)
            
        except Exception as e:
            logger.error(f"Failed to get pipeline overview: {e}")
            raise QueryError(f"Failed to get pipeline overview: {e}") from e
    
    def get_processing_stats(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        days: Optional[int] = None
    ) -> Dict:
        """
        Get processing statistics for date range.
        
        Parameters
        ----------
        start_date : date, optional
            Start date (inclusive)
        end_date : date, optional
            End date (inclusive). Defaults to today.
        days : int, optional
            Alternative to start_date: get last N days
        
        Returns
        -------
        dict
            Processing statistics:
            - batches_processed
            - files_processed
            - total_gb_processed
            - avg_files_per_batch
        
        Examples
        --------
        >>> # Last 7 days
        >>> stats = db.analytics.get_processing_stats(days=7)
        
        >>> # Specific date range
        >>> from datetime import date
        >>> stats = db.analytics.get_processing_stats(
        ...     start_date=date(2025, 1, 1),
        ...     end_date=date(2025, 1, 31)
        ... )
        """
        # Determine date range
        if days is not None:
            end_date = date.today()
            start_date = end_date - timedelta(days=days)
        elif start_date is None:
            # Default to last 30 days
            end_date = end_date or date.today()
            start_date = end_date - timedelta(days=30)
        else:
            end_date = end_date or date.today()
        
        logger.info(f"Getting processing stats from {start_date} to {end_date}")
        
        query = "SELECT * FROM get_processing_stats(%s, %s);"
        
        try:
            results = self.conn.fetch_all(query, (start_date, end_date))
            
            # Convert to dict
            stats = {row['metric']: row['value'] for row in results}
            
            logger.debug(f"Processing stats: {stats.get('batches_processed', 0)} batches")
            return stats
            
        except Exception as e:
            logger.error(f"Failed to get processing stats: {e}")
            raise QueryError(f"Failed to get processing stats: {e}") from e
    
    def get_daily_volumes(
        self,
        days: int = 30,
        batch_state: Optional[str] = None
    ) -> List[Dict]:
        """
        Get daily processing volumes.
        
        Parameters
        ----------
        days : int, optional
            Number of days to retrieve (default: 30)
        batch_state : str, optional
            Filter by batch state (MD, TX, NC)
        
        Returns
        -------
        list of dict
            Daily volumes with:
            - processing_date
            - batch_state
            - batch_count
            - total_raw_files
            - total_jpg_files
            - total_bytes
            - completed_batches
            - failed_batches
        
        Examples
        --------
        >>> volumes = db.analytics.get_daily_volumes(days=7)
        >>> for v in volumes:
        ...     print(f"{v['processing_date']}: {v['batch_count']} batches")
        """
        logger.info(f"Getting daily volumes for last {days} days")
        
        cutoff_date = date.today() - timedelta(days=days)
        
        query = """
            SELECT *
            FROM processed.daily_batch_summary
            WHERE processing_date >= %s
        """
        params = [cutoff_date]
        
        if batch_state:
            query += " AND batch_state = %s"
            params.append(batch_state)
        
        query += " ORDER BY processing_date DESC;"
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Retrieved {len(results)} daily volume records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get daily volumes: {e}")
            raise QueryError(f"Failed to get daily volumes: {e}") from e
    
    def get_throughput(
        self,
        days: int = 30,
        stage: Optional[str] = None
    ) -> List[Dict]:
        """
        Get processing throughput metrics.
        
        Parameters
        ----------
        days : int, optional
            Number of days to retrieve (default: 30)
        stage : str, optional
            Filter by stage (raw_to_dng, dng_to_jpg, etc.)
        
        Returns
        -------
        list of dict
            Daily throughput with:
            - processing_date
            - stage
            - batches_processed
            - total_files_processed
            - avg_files_per_second
            - avg_seconds_per_batch
        
        Examples
        --------
        >>> throughput = db.analytics.get_throughput(days=7)
        >>> for t in throughput:
        ...     print(f"{t['stage']}: {t['avg_files_per_second']} files/sec")
        """
        logger.info(f"Getting throughput for last {days} days")
        
        cutoff_date = date.today() - timedelta(days=days)
        
        query = """
            SELECT *
            FROM processed.daily_throughput
            WHERE processing_date >= %s
        """
        params = [cutoff_date]
        
        if stage:
            query += " AND stage = %s"
            params.append(stage)
        
        query += " ORDER BY processing_date DESC, stage;"
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Retrieved {len(results)} throughput records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get throughput: {e}")
            raise QueryError(f"Failed to get throughput: {e}") from e
    
    def get_stage_performance(self, stage: Optional[str] = None) -> List[Dict]:
        """
        Get stage performance statistics.
        
        Parameters
        ----------
        stage : str, optional
            Filter by specific stage
        
        Returns
        -------
        list of dict
            Stage performance with:
            - stage
            - total_executions
            - completed_count
            - failed_count
            - success_rate
            - avg_duration_seconds
            - avg_files_per_second
        
        Examples
        --------
        >>> # All stages
        >>> perf = db.analytics.get_stage_performance()
        
        >>> # Specific stage
        >>> perf = db.analytics.get_stage_performance('raw_to_jpg')
        """
        logger.info(f"Getting stage performance{f' for {stage}' if stage else ''}")
        
        query = "SELECT * FROM processed.stage_performance_summary"
        params = None
        
        if stage:
            query += " WHERE stage = %s"
            params = (stage,)
        
        query += ";"
        
        try:
            results = self.conn.fetch_all(query, params)
            logger.debug(f"Retrieved {len(results)} stage performance records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get stage performance: {e}")
            raise QueryError(f"Failed to get stage performance: {e}") from e
    
    def get_error_summary(
        self,
        stage: Optional[str] = None,
        days: Optional[int] = None
    ) -> List[Dict]:
        """
        Get error summary by stage.
        
        Parameters
        ----------
        stage : str, optional
            Filter by specific stage
        days : int, optional
            Only errors from last N days
        
        Returns
        -------
        list of dict
            Error summary with:
            - stage
            - error_count
            - affected_batches
            - total_files_failed
            - last_error_time
            - sample_errors
        
        Examples
        --------
        >>> errors = db.analytics.get_error_summary(days=7)
        >>> for e in errors:
        ...     print(f"{e['stage']}: {e['error_count']} errors")
        """
        logger.info("Getting error summary")
        
        query = "SELECT * FROM processed.error_summary_by_stage"
        params = []
        
        conditions = []
        if stage:
            conditions.append("stage = %s")
            params.append(stage)
        
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            conditions.append("last_error_time >= %s")
            params.append(cutoff)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY error_count DESC;"
        
        try:
            results = self.conn.fetch_all(query, tuple(params) if params else None)
            logger.debug(f"Retrieved {len(results)} error summary records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get error summary: {e}")
            raise QueryError(f"Failed to get error summary: {e}") from e
    
    def get_recent_errors(self, limit: int = 50) -> List[Dict]:
        """
        Get recent errors across all stages.
        
        Parameters
        ----------
        limit : int, optional
            Maximum number of errors to return (default: 50)
        
        Returns
        -------
        list of dict
            Recent error records
        
        Examples
        --------
        >>> errors = db.analytics.get_recent_errors(limit=10)
        >>> for e in errors:
        ...     print(f"{e['batch_id']} {e['stage']}: {e['error_message']}")
        """
        logger.info(f"Getting {limit} recent errors")
        
        query = f"""
            SELECT *
            FROM processed.recent_errors
            LIMIT {int(limit)};
        """
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Retrieved {len(results)} recent errors")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get recent errors: {e}")
            raise QueryError(f"Failed to get recent errors: {e}") from e
    
    def get_error_rate(
        self,
        stage: Optional[str] = None,
        days: int = 30
    ) -> Dict:
        """
        Calculate error rate for stage(s).
        
        Parameters
        ----------
        stage : str, optional
            Specific stage (if None, returns overall rate)
        days : int, optional
            Calculate over last N days (default: 30)
        
        Returns
        -------
        dict
            Error rate metrics:
            - total_executions
            - failed_executions
            - error_rate (percentage)
            - stage (if filtered)
        
        Examples
        --------
        >>> rate = db.analytics.get_error_rate('raw_to_jpg', days=7)
        >>> print(f"Error rate: {rate['error_rate']:.2f}%")
        """
        logger.info(f"Calculating error rate for last {days} days")
        
        cutoff_date = datetime.now() - timedelta(days=days)
        
        query = """
            SELECT
                COUNT(*) as total_executions,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_executions,
                ROUND(
                    COUNT(*) FILTER (WHERE status = 'failed')::NUMERIC / 
                    NULLIF(COUNT(*), 0) * 100,
                    2
                ) as error_rate
            FROM processed.stage_status
            WHERE started_at >= %s
        """
        params = [cutoff_date]
        
        if stage:
            query += " AND stage = %s"
            params.append(stage)
        
        query += ";"
        
        try:
            result = self.conn.fetch_one(query, tuple(params))
            
            if not result:
                return {'total_executions': 0, 'failed_executions': 0, 'error_rate': 0.0}
            
            rate_dict = dict(result)
            if stage:
                rate_dict['stage'] = stage
            
            logger.debug(f"Error rate: {rate_dict['error_rate']}%")
            return rate_dict
            
        except Exception as e:
            logger.error(f"Failed to calculate error rate: {e}")
            raise QueryError(f"Failed to calculate error rate: {e}") from e
    
    def get_transfer_performance(
        self,
        days: int = 30,
        source_site: Optional[str] = None,
        destination_site: Optional[str] = None
    ) -> List[Dict]:
        """
        Get transfer performance metrics.
        
        Parameters
        ----------
        days : int, optional
            Number of days to retrieve (default: 30)
        source_site : str, optional
            Filter by source site
        destination_site : str, optional
            Filter by destination site
        
        Returns
        -------
        list of dict
            Transfer performance records
        
        Examples
        --------
        >>> perf = db.analytics.get_transfer_performance(days=7)
        >>> for p in perf:
        ...     print(f"{p['source_site']} → {p['destination_site']}: {p['actual_mbps']} MB/s")
        """
        logger.info(f"Getting transfer performance for last {days} days")
        
        cutoff_date = date.today() - timedelta(days=days)
        
        query = """
            SELECT *
            FROM processed.transfer_performance
            WHERE DATE(completed_at) >= %s
        """
        params = [cutoff_date]
        
        if source_site:
            query += " AND source_site = %s"
            params.append(source_site)
        
        if destination_site:
            query += " AND destination_site = %s"
            params.append(destination_site)
        
        query += " ORDER BY completed_at DESC;"
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Retrieved {len(results)} transfer performance records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get transfer performance: {e}")
            raise QueryError(f"Failed to get transfer performance: {e}") from e
    
    def get_transfer_summary_by_route(self) -> List[Dict]:
        """
        Get transfer statistics by route (source/destination pair).
        
        Returns
        -------
        list of dict
            Transfer summary by route with:
            - source_site
            - destination_site
            - total_transfers
            - completed_transfers
            - failed_transfers
            - total_gb_transferred
            - avg_duration_seconds
            - avg_transfer_rate_mbps
        
        Examples
        --------
        >>> summary = db.analytics.get_transfer_summary_by_route()
        >>> for s in summary:
        ...     route = f"{s['source_site']} → {s['destination_site']}"
        ...     print(f"{route}: {s['total_gb_transferred']} GB")
        """
        logger.info("Getting transfer summary by route")
        
        query = "SELECT * FROM processed.transfer_summary_by_route;"
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Retrieved {len(results)} route summaries")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get transfer summary: {e}")
            raise QueryError(f"Failed to get transfer summary: {e}") from e
    
    def get_storage_by_site(self) -> List[Dict]:
        """
        Get storage utilization by site.
        
        Returns
        -------
        list of dict
            Storage by site with:
            - site
            - batch_count
            - total_raw_files
            - total_jpg_files
            - total_gb
            - avg_gb_per_batch
            - earliest_batch
            - latest_batch
        
        Examples
        --------
        >>> storage = db.analytics.get_storage_by_site()
        >>> for s in storage:
        ...     print(f"{s['site']}: {s['total_gb']} GB ({s['batch_count']} batches)")
        """
        logger.info("Getting storage by site")
        
        query = "SELECT * FROM processed.storage_by_site;"
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Retrieved {len(results)} site storage records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get storage by site: {e}")
            raise QueryError(f"Failed to get storage by site: {e}") from e
    
    def get_storage_growth(
        self,
        months: int = 12,
        batch_state: Optional[str] = None
    ) -> List[Dict]:
        """
        Get storage growth over time.
        
        Parameters
        ----------
        months : int, optional
            Number of months to retrieve (default: 12)
        batch_state : str, optional
            Filter by batch state
        
        Returns
        -------
        list of dict
            Monthly storage growth with:
            - month
            - batch_state
            - batch_count
            - raw_files
            - jpg_files
            - total_gb
            - cumulative_bytes
        
        Examples
        --------
        >>> growth = db.analytics.get_storage_growth(months=6)
        >>> for g in growth:
        ...     print(f"{g['month']}: {g['total_gb']} GB")
        """
        logger.info(f"Getting storage growth for last {months} months")
        
        cutoff_date = date.today() - timedelta(days=months*30)
        
        query = """
            SELECT *
            FROM processed.storage_growth
            WHERE month >= %s
        """
        params = [cutoff_date]
        
        if batch_state:
            query += " AND batch_state = %s"
            params.append(batch_state)
        
        query += " ORDER BY month DESC, batch_state;"
        
        try:
            results = self.conn.fetch_all(query, tuple(params))
            logger.debug(f"Retrieved {len(results)} storage growth records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get storage growth: {e}")
            raise QueryError(f"Failed to get storage growth: {e}") from e
    
    def get_batch_summary(self, batch_id: str) -> Optional[Dict]:
        """
        Get comprehensive summary for a specific batch.
        
        Parameters
        ----------
        batch_id : str
            Batch identifier
        
        Returns
        -------
        dict or None
            Batch summary with:
            - batch metadata
            - stage completion status
            - transfer status
            - processing timestamps
        
        Examples
        --------
        >>> summary = db.analytics.get_batch_summary('MD_2025-01-01')
        >>> print(f"Stages completed: {summary['stages_completed']}")
        >>> print(f"Files: {summary['file_count_raw']}")
        """
        logger.info(f"Getting batch summary for {batch_id}")
        
        query = """
            SELECT *
            FROM processed.batch_completion_status
            WHERE batch_id = %s;
        """
        
        try:
            result = self.conn.fetch_one(query, (batch_id,))
            
            if not result:
                logger.warning(f"No summary found for batch {batch_id}")
                return None
            
            logger.debug(f"Retrieved batch summary for {batch_id}")
            return dict(result)
            
        except Exception as e:
            logger.error(f"Failed to get batch summary: {e}")
            raise QueryError(f"Failed to get batch summary: {e}") from e
    
    def get_camera_stats(self) -> List[Dict]:
        """
        Get camera usage statistics.
        
        Returns
        -------
        list of dict
            Camera statistics with:
            - camera_make
            - camera_model
            - image_count
            - batch_count
            - avg_width
            - avg_height
            - images_with_detections
            - avg_detections_per_image
        
        Examples
        --------
        >>> stats = db.analytics.get_camera_stats()
        >>> for s in stats:
        ...     camera = f"{s['camera_make']} {s['camera_model']}"
        ...     print(f"{camera}: {s['image_count']} images")
        """
        logger.info("Getting camera usage statistics")
        
        query = "SELECT * FROM processed.camera_usage_stats;"
        
        try:
            results = self.conn.fetch_all(query)
            logger.debug(f"Retrieved {len(results)} camera stat records")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get camera stats: {e}")
            raise QueryError(f"Failed to get camera stats: {e}") from e