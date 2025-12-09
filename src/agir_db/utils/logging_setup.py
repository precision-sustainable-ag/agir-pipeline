"""
Logging configuration for AgirDB.

This module provides centralized logging setup for both file and console logging.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def setup_logging(
    log_dir: Optional[str] = None,
    log_file: Optional[str] = None,
    level: str = 'INFO',
    console: bool = True,
    format_string: Optional[str] = None
) -> logging.Logger:
    """
    Configure logging for AgirDB operations.
    
    Sets up logging to both file and console (optional). Creates log directory
    if it doesn't exist. Log files are named with date: agir_db_YYYYMMDD.log
    
    Parameters
    ----------
    log_dir : str, optional
        Directory for log files. Default: '/project/dash_agir/logs'
    log_file : str, optional
        Specific log file name. If None, uses 'agir_db_YYYYMMDD.log'
    level : str, optional
        Logging level ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
        Default: 'INFO'
    console : bool, optional
        Whether to also log to console. Default: True
    format_string : str, optional
        Custom log format string. If None, uses default format.
    
    Returns
    -------
    logging.Logger
        Configured root logger
    
    Examples
    --------
    >>> setup_logging(level='DEBUG')
    >>> logger = logging.getLogger(__name__)
    >>> logger.info("Processing batch MD_2025-01-01")
    
    >>> setup_logging(log_dir='/custom/log/path', level='WARNING', console=False)
    """
    # Default log directory
    if log_dir is None:
        log_dir = '/project/dash_agir/logs'
    
    # Create log directory if it doesn't exist
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)
    
    # Default log file name with date
    if log_file is None:
        date_str = datetime.now().strftime('%Y%m%d')
        log_file = f'agir_db_{date_str}.log'
    
    log_file_path = log_dir_path / log_file
    
    # Default format string
    if format_string is None:
        format_string = (
            '%(asctime)s - %(name)s - %(levelname)s - '
            '%(filename)s:%(lineno)d - %(message)s'
        )
    
    # Convert level string to logging level
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Create formatter
    formatter = logging.Formatter(format_string)
    
    # File handler
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Console handler (optional)
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    
    root_logger.info(f"Logging initialized: {log_file_path}")
    root_logger.info(f"Log level: {level}")
    
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a specific module.
    
    Parameters
    ----------
    name : str
        Logger name (typically __name__)
    
    Returns
    -------
    logging.Logger
        Logger instance
    
    Examples
    --------
    >>> logger = get_logger(__name__)
    >>> logger.info("Starting processing")
    """
    return logging.getLogger(name)


def set_level(level: str) -> None:
    """
    Change logging level for all handlers.
    
    Parameters
    ----------
    level : str
        New logging level ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
    
    Examples
    --------
    >>> set_level('DEBUG')  # Enable debug logging
    >>> set_level('WARNING')  # Only warnings and above
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    for handler in root_logger.handlers:
        handler.setLevel(numeric_level)
    
    root_logger.info(f"Log level changed to: {level}")