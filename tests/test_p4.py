#!/usr/bin/env python3
"""
Test script for Phase 4 - Event Logging.

This script verifies that event logging works correctly:
- SQL table and views for events
- EventLogger class methods
- Integration with AgirDB facade
- Event logging, querying, and search

Note: These tests require a live database connection with the SQL schema installed.
Run events_schema.sql before running these tests.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.events import EventLogger, VALID_SEVERITIES, EVENT_TYPES
from agir_db.exceptions import InvalidParameterError, QueryError


def test_valid_severities():
    """Test that VALID_SEVERITIES constant is correct."""
    print("Testing valid severities...")
    
    expected = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
    assert VALID_SEVERITIES == expected, f"VALID_SEVERITIES mismatch: {VALID_SEVERITIES}"
    
    print("✓ Valid severities are correct")


def test_event_types():
    """Test that EVENT_TYPES is defined."""
    print("\nTesting event types...")
    
    assert isinstance(EVENT_TYPES, set), "EVENT_TYPES should be a set"
    assert len(EVENT_TYPES) > 0, "EVENT_TYPES should not be empty"
    assert 'stage.started' in EVENT_TYPES
    assert 'error.connection' in EVENT_TYPES
    
    print(f"✓ EVENT_TYPES defined with {len(EVENT_TYPES)} event types")


def test_event_logger_initialization():
    """Test EventLogger initialization without connection."""
    print("\nTesting EventLogger initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    events = EventLogger(conn)
    
    assert events.conn is conn
    assert hasattr(events, 'hostname')
    assert hasattr(events, 'user_name')
    assert hasattr(events, 'log_event')
    assert hasattr(events, 'get_events')
    assert hasattr(events, 'get_recent_events')
    assert hasattr(events, 'get_errors')
    assert hasattr(events, 'get_batch_events')
    assert hasattr(events, 'get_stage_events')
    assert hasattr(events, 'get_event_summary')
    assert hasattr(events, 'search_events')
    
    print("✓ EventLogger initializes correctly")


def test_severity_validation():
    """Test severity validation."""
    print("\nTesting severity validation...")
    
    from agir_db.connection import ConnectionManager
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    events = EventLogger(conn)
    
    # Valid severities should not raise
    try:
        events._validate_severity('DEBUG')
        events._validate_severity('INFO')
        events._validate_severity('WARNING')
        events._validate_severity('ERROR')
        events._validate_severity('CRITICAL')
    except Exception as e:
        raise AssertionError(f"Valid severity raised exception: {e}")
    
    # Invalid severity should raise
    try:
        events._validate_severity('INVALID')
        raise AssertionError("Invalid severity did not raise exception")
    except InvalidParameterError as e:
        assert 'invalid' in str(e).lower()
    
    print("✓ Severity validation works correctly")


def test_agirdb_events_integration():
    """Test that AgirDB exposes events component."""
    print("\nTesting AgirDB.events integration...")
    
    db = AgirDB()
    
    # Check that events component exists
    assert hasattr(db, 'events'), "AgirDB does not have 'events' attribute"
    assert isinstance(db.events, EventLogger), "db.events is not an EventLogger instance"
    
    # Check that methods are accessible
    assert hasattr(db.events, 'log_event')
    assert hasattr(db.events, 'get_events')
    assert hasattr(db.events, 'get_recent_events')
    assert hasattr(db.events, 'get_errors')
    
    print("✓ AgirDB.events integration works correctly")


def test_method_signatures():
    """Test that methods have correct signatures."""
    print("\nTesting method signatures...")
    
    from agir_db.connection import ConnectionManager
    import inspect
    
    conn = ConnectionManager()
    events = EventLogger(conn)
    
    # log_event(event_type, severity, message, ...)
    sig = inspect.signature(events.log_event)
    assert 'event_type' in sig.parameters
    assert 'severity' in sig.parameters
    assert 'message' in sig.parameters
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    
    # get_events(...)
    sig = inspect.signature(events.get_events)
    assert 'event_type' in sig.parameters
    assert 'severity' in sig.parameters
    assert 'batch_id' in sig.parameters
    assert 'limit' in sig.parameters
    
    # get_recent_events(hours=24, limit=100)
    sig = inspect.signature(events.get_recent_events)
    assert 'hours' in sig.parameters
    assert 'limit' in sig.parameters
    
    # get_errors(...)
    sig = inspect.signature(events.get_errors)
    assert 'batch_id' in sig.parameters
    assert 'hours' in sig.parameters
    
    # get_batch_events(batch_id, ...)
    sig = inspect.signature(events.get_batch_events)
    assert 'batch_id' in sig.parameters
    
    # get_stage_events(...)
    sig = inspect.signature(events.get_stage_events)
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    
    # search_events(search_text, ...)
    sig = inspect.signature(events.search_events)
    assert 'search_text' in sig.parameters
    
    print("✓ Method signatures are correct")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database).
    
    This test is optional and will be skipped if database is not available.
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require a live database with schema installed.")
    print("If you haven't run events_schema.sql yet, these will fail.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # Test data
            test_batch = 'TEST_BATCH_2025-01-01'
            test_stage = 'raw_to_jpg'
            
            # Test 1: Log an INFO event
            print("\nTest 1: Logging INFO event...")
            event_id = db.events.log_event(
                event_type='stage.started',
                severity='INFO',
                message=f'Test event: Started {test_stage} for {test_batch}',
                batch_id=test_batch,
                stage=test_stage,
                metadata={'test': True, 'files_count': 150},
                source='test_phase4'
            )
            db.commit()
            assert isinstance(event_id, int), f"event_id should be int, got {type(event_id)}"
            print(f"✓ Event logged successfully (event_id={event_id})")
            
            # Test 2: Log an ERROR event
            print("\nTest 2: Logging ERROR event...")
            error_id = db.events.log_event(
                event_type='error.processing',
                severity='ERROR',
                message='Test error: Simulated processing failure',
                batch_id=test_batch,
                stage=test_stage,
                error_type='TestError',
                stack_trace='Test stack trace',
                metadata={'error_code': 123},
                source='test_phase4'
            )
            db.commit()
            print(f"✓ Error event logged successfully (event_id={error_id})")
            
            # Test 3: Get recent events
            print("\nTest 3: Getting recent events...")
            recent = db.events.get_recent_events(hours=1, limit=10)
            assert isinstance(recent, list), "get_recent_events should return list"
            print(f"✓ Found {len(recent)} recent event(s)")
            
            # Test 4: Get events with filters
            print("\nTest 4: Getting events with filters...")
            test_events = db.events.get_events(
                batch_id=test_batch,
                limit=10
            )
            assert len(test_events) >= 2, f"Should have at least 2 test events, found {len(test_events)}"
            print(f"✓ Found {len(test_events)} event(s) for test batch")
            
            # Verify event content
            found_info = False
            found_error = False
            for event in test_events:
                if event['event_id'] == event_id:
                    assert event['severity'] == 'INFO'
                    assert event['event_type'] == 'stage.started'
                    assert event['batch_id'] == test_batch
                    assert event['stage'] == test_stage
                    assert event['metadata']['test'] is True
                    found_info = True
                elif event['event_id'] == error_id:
                    assert event['severity'] == 'ERROR'
                    assert event['event_type'] == 'error.processing'
                    assert event['error_type'] == 'TestError'
                    found_error = True
            
            assert found_info, "INFO event not found in results"
            assert found_error, "ERROR event not found in results"
            print("✓ Event content verified")
            
            # Test 5: Get batch events
            print("\nTest 5: Getting batch events...")
            batch_events = db.events.get_batch_events(test_batch)
            assert len(batch_events) >= 2, "Should have at least 2 batch events"
            print(f"✓ Found {len(batch_events)} event(s) for batch")
            
            # Test 6: Get stage events
            print("\nTest 6: Getting stage events...")
            stage_events = db.events.get_stage_events(
                batch_id=test_batch,
                stage=test_stage
            )
            assert len(stage_events) >= 1, "Should have at least 1 stage event"
            print(f"✓ Found {len(stage_events)} stage event(s)")
            
            # Test 7: Get errors
            print("\nTest 7: Getting error events...")
            errors = db.events.get_errors(batch_id=test_batch)
            assert len(errors) >= 1, "Should have at least 1 error"
            found_test_error = False
            for error in errors:
                if error['event_id'] == error_id:
                    found_test_error = True
                    break
            assert found_test_error, "Test error not found in error events"
            print(f"✓ Found {len(errors)} error event(s)")
            
            # Test 8: Search events
            print("\nTest 8: Searching events...")
            search_results = db.events.search_events('test event', limit=10)
            assert isinstance(search_results, list), "search_events should return list"
            print(f"✓ Search found {len(search_results)} result(s)")
            
            # Test 9: Get event summary
            print("\nTest 9: Getting event summary...")
            summary = db.events.get_event_summary(hours=1)
            assert isinstance(summary, list), "get_event_summary should return list"
            print(f"✓ Summary has {len(summary)} record(s)")
            
            # Test 10: Query with wildcard event type
            print("\nTest 10: Query with wildcard event type...")
            stage_type_events = db.events.get_events(
                event_type='stage.%',
                batch_id=test_batch,
                limit=10
            )
            assert len(stage_type_events) >= 1, "Should find stage.* events"
            print(f"✓ Found {len(stage_type_events)} event(s) matching 'stage.%'")
            
            # Clean up test data
            print("\nCleaning up test events...")
            cleanup_query = "DELETE FROM processed.events WHERE batch_id = %s AND source = 'test_phase4';"
            db._connection.execute(cleanup_query, (test_batch,))
            db.commit()
            print("✓ Test events cleaned up")
            
            print("\n✓ All database integration tests passed!")
            
    except Exception as e:
        if skip_if_no_db and 'connection' in str(e).lower():
            print(f"\n⚠ Database not available: {e}")
            print("Skipping database integration tests.")
            print("This is expected if you don't have a database running.")
        else:
            print(f"\n✗ Database test failed: {e}")
            import traceback
            traceback.print_exc()
            raise


def main():
    """Run all Phase 4 tests."""
    print("=" * 60)
    print("Phase 4 - Event Logging Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_valid_severities()
        test_event_types()
        test_event_logger_initialization()
        test_severity_validation()
        test_agirdb_events_integration()
        test_method_signatures()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 4 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 4 Complete!")
        print("=" * 60)
        print("\nPhase 4 components are working correctly.")
        print("Ready to proceed to Phase 5 (Image Metadata).")
        
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