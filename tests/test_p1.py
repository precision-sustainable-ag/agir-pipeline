#!/usr/bin/env python3
"""
Test script for Phase 1 components.

This script verifies that the foundation components work correctly:
- Exception hierarchy
- Connection management
- Logging setup
- AgirDB facade

Run this after Phase 1 to verify everything is working.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import logging

from agir_db.exceptions import (
    AgirDBError,
    ConnectionError,
    QueryError,
    DuplicateImageError,
    ImageNotFoundError,
    StageAlreadyInProgressError,
)
from agir_db.connection import ConnectionManager
from agir_db.utils.logging_setup import setup_logging
from agir_db.api import AgirDB


def test_exceptions():
    """Test exception hierarchy."""
    print("Testing exceptions...")
    
    # Test that all exceptions inherit from AgirDBError
    assert issubclass(ConnectionError, AgirDBError)
    assert issubclass(QueryError, AgirDBError)
    assert issubclass(DuplicateImageError, AgirDBError)
    assert issubclass(ImageNotFoundError, AgirDBError)
    assert issubclass(StageAlreadyInProgressError, AgirDBError)
    
    # Test exception raising
    try:
        raise DuplicateImageError("Image MD_123 already exists")
    except AgirDBError as e:
        assert "MD_123" in str(e)
    
    print("✓ Exceptions work correctly")


def test_logging():
    """Test logging setup."""
    print("\nTesting logging...")
    
    # Setup logging (use temp directory for test)
    logger = setup_logging(
        log_dir='/tmp/agir_db_test',
        level='DEBUG',
        console=False
    )
    
    # Test logging at different levels
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    
    # Check log file was created
    log_file = Path('/tmp/agir_db_test').glob('agir_db_*.log')
    assert any(log_file), "Log file not created"
    
    print("✓ Logging works correctly")


def test_connection_manager():
    """Test ConnectionManager (without actually connecting)."""
    print("\nTesting ConnectionManager...")
    
    # Test initialization
    conn = ConnectionManager()
    
    print(f"Host: {conn.host}")
    print(f"Port: {conn.port}")
    print(f"DB Name: {conn.dbname}")
    print(f"User: {conn.user}")
    
    print("✓ ConnectionManager initializes correctly")
    
    # Note: We don't actually connect since we might not have a database running
    # In real usage, you would test actual connection


def test_agirdb_facade():
    """Test AgirDB facade initialization."""
    print("\nTesting AgirDB facade...")
    
    # Test initialization
    db = AgirDB()
    
    assert not db.is_connected
    print(f"AgirDB connected: {db.is_connected}")

    print("✓ AgirDB facade initializes correctly")


def test_exception_catching():
    """Test that we can catch exceptions properly."""
    print("\nTesting exception catching...")
    
    # Test catching specific exception
    try:
        raise DuplicateImageError("Duplicate!")
    except DuplicateImageError:
        pass  # Expected
    
    # Test catching base exception
    try:
        raise ImageNotFoundError("Not found!")
    except AgirDBError:
        pass  # Expected
    
    # Test exception propagation
    caught = False
    try:
        try:
            raise QueryError("SQL error")
        except QueryError:
            caught = True
            raise  # Re-raise
    except AgirDBError:
        assert caught  # Should have caught it first
    
    print("✓ Exception catching works correctly")


def main():
    """Run all Phase 1 tests."""
    print("=" * 60)
    print("Phase 1 Foundation Tests")
    print("=" * 60)
    
    try:
        test_exceptions()
        test_logging()
        test_connection_manager()
        test_agirdb_facade()
        test_exception_catching()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 1 tests passed!")
        print("=" * 60)
        print("\nPhase 1 components are working correctly.")
        print("Ready to proceed to Phase 2 (Pipeline Gaps).")
        
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()