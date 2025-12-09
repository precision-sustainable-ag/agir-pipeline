#!/usr/bin/env python3
"""
Test script for Phase 3 - Stage Status.

This script verifies that stage execution tracking works correctly:
- SQL table and views for stage status
- StageStatus class methods
- Integration with AgirDB facade
- Stage lifecycle (start -> complete -> reset)

Note: These tests require a live database connection with the SQL schema installed.
Run stage_status_schema.sql before running these tests.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.stages import StageStatus, VALID_STAGES, VALID_STATUSES
from agir_db.exceptions import (
    InvalidStageError,
    StageAlreadyInProgressError,
    StageNotStartedError,
    QueryError
)


def test_valid_stages():
    """Test that VALID_STAGES constant is correct."""
    print("Testing valid stages...")
    
    expected = {'raw_to_jpg', 'jpg_to_metadata', 'metadata_to_cutouts'}
    assert VALID_STAGES == expected, f"VALID_STAGES mismatch: {VALID_STAGES}"
    
    print("✓ Valid stages are correct")


def test_valid_statuses():
    """Test that VALID_STATUSES constant is correct."""
    print("\nTesting valid statuses...")
    
    expected = {'in_progress', 'completed', 'failed'}
    assert VALID_STATUSES == expected, f"VALID_STATUSES mismatch: {VALID_STATUSES}"
    
    print("✓ Valid statuses are correct")


def test_stage_status_initialization():
    """Test StageStatus initialization without connection."""
    print("\nTesting StageStatus initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    stages = StageStatus(conn)
    
    assert stages.conn is conn
    assert hasattr(stages, 'start')
    assert hasattr(stages, 'complete')
    assert hasattr(stages, 'reset')
    assert hasattr(stages, 'get_status')
    assert hasattr(stages, 'get_in_progress')
    assert hasattr(stages, 'get_failed')
    assert hasattr(stages, 'get_batch_status')
    
    print("✓ StageStatus initializes correctly")


def test_stage_validation():
    """Test stage validation."""
    print("\nTesting stage validation...")
    
    from agir_db.connection import ConnectionManager
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    stages = StageStatus(conn)
    
    # Valid stages should not raise
    try:
        stages._validate_stage('raw_to_jpg')
        stages._validate_stage('jpg_to_metadata')
        stages._validate_stage('metadata_to_cutouts')
    except Exception as e:
        raise AssertionError(f"Valid stage raised exception: {e}")
    
    # Invalid stage should raise
    try:
        stages._validate_stage('invalid_stage')
        raise AssertionError("Invalid stage did not raise exception")
    except InvalidStageError as e:
        assert 'invalid_stage' in str(e).lower()
    
    print("✓ Stage validation works correctly")


def test_status_validation():
    """Test status validation."""
    print("\nTesting status validation...")
    
    from agir_db.connection import ConnectionManager
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    stages = StageStatus(conn)
    
    # Valid statuses should not raise
    try:
        stages._validate_status('in_progress')
        stages._validate_status('completed')
        stages._validate_status('failed')
    except Exception as e:
        raise AssertionError(f"Valid status raised exception: {e}")
    
    # Invalid status should raise
    try:
        stages._validate_status('invalid_status')
        raise AssertionError("Invalid status did not raise exception")
    except Exception as e:
        assert 'invalid_status' in str(e).lower()
    
    print("✓ Status validation works correctly")


def test_agirdb_stages_integration():
    """Test that AgirDB exposes stages component."""
    print("\nTesting AgirDB.stages integration...")
    
    db = AgirDB(host='localhost', port=5432, dbname='agir', user='testuser')
    
    # Check that stages component exists
    assert hasattr(db, 'stages'), "AgirDB does not have 'stages' attribute"
    assert isinstance(db.stages, StageStatus), "db.stages is not a StageStatus instance"
    
    # Check that methods are accessible
    assert hasattr(db.stages, 'start')
    assert hasattr(db.stages, 'complete')
    assert hasattr(db.stages, 'reset')
    assert hasattr(db.stages, 'get_status')
    assert hasattr(db.stages, 'get_in_progress')
    assert hasattr(db.stages, 'get_failed')
    assert hasattr(db.stages, 'get_batch_status')
    
    print("✓ AgirDB.stages integration works correctly")


def test_method_signatures():
    """Test that methods have correct signatures."""
    print("\nTesting method signatures...")
    
    from agir_db.connection import ConnectionManager
    import inspect
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    stages = StageStatus(conn)
    
    # start(batch_id, stage, job_id=None, metadata=None)
    sig = inspect.signature(stages.start)
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    assert 'job_id' in sig.parameters
    assert 'metadata' in sig.parameters
    assert sig.parameters['job_id'].default is None
    assert sig.parameters['metadata'].default is None
    
    # complete(batch_id, stage, success, files_processed=None, ...)
    sig = inspect.signature(stages.complete)
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    assert 'success' in sig.parameters
    assert 'files_processed' in sig.parameters
    
    # reset(batch_id, stage)
    sig = inspect.signature(stages.reset)
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    
    # get_status(batch_id, stage)
    sig = inspect.signature(stages.get_status)
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    
    # get_in_progress(stage=None)
    sig = inspect.signature(stages.get_in_progress)
    assert 'stage' in sig.parameters
    assert sig.parameters['stage'].default is None
    
    # get_failed(stage=None, limit=None)
    sig = inspect.signature(stages.get_failed)
    assert 'stage' in sig.parameters
    assert 'limit' in sig.parameters
    
    # get_batch_status(batch_id)
    sig = inspect.signature(stages.get_batch_status)
    assert 'batch_id' in sig.parameters
    
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
    print("If you haven't run stage_status_schema.sql yet, these will fail.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # Test batch and stage for testing
            test_batch = 'TEST_BATCH_2025-01-01'
            test_stage = 'raw_to_jpg'
            test_job_id = 'test_job_12345'
            
            # Clean up any previous test data
            print(f"\nCleaning up previous test data...")
            try:
                db.stages.reset(test_batch, test_stage)
            except:
                pass  # Ignore if doesn't exist
            db.commit()
            
            # Test 1: Start a stage
            print(f"\nTest 1: Starting stage '{test_stage}' for batch '{test_batch}'...")
            db.stages.start(test_batch, test_stage, job_id=test_job_id)
            db.commit()
            print("✓ Stage started successfully")
            
            # Verify status is in_progress
            status = db.stages.get_status(test_batch, test_stage)
            assert status is not None, "Status record not created"
            assert status['status'] == 'in_progress', f"Status is {status['status']}, not in_progress"
            assert status['job_id'] == test_job_id, f"job_id mismatch"
            print(f"✓ Status verified: {status['status']}, job_id: {status['job_id']}")
            
            # Test 2: Try to start again (should fail)
            print(f"\nTest 2: Attempting to start already-in-progress stage...")
            try:
                db.stages.start(test_batch, test_stage, job_id='another_job')
                raise AssertionError("Should have raised StageAlreadyInProgressError")
            except StageAlreadyInProgressError as e:
                print(f"✓ Correctly raised StageAlreadyInProgressError: {e}")
            
            # Test 3: Get in-progress stages
            print(f"\nTest 3: Getting in-progress stages...")
            in_progress = db.stages.get_in_progress(test_stage)
            assert len(in_progress) > 0, "No in-progress stages found"
            found = False
            for stage in in_progress:
                if stage['batch_id'] == test_batch:
                    found = True
                    print(f"✓ Found test batch in in-progress list")
                    print(f"  Elapsed: {stage['elapsed_seconds']:.2f} seconds")
                    break
            assert found, "Test batch not found in in-progress list"
            
            # Test 4: Complete the stage (success)
            print(f"\nTest 4: Completing stage successfully...")
            db.stages.complete(
                test_batch, test_stage,
                success=True,
                files_processed=150
            )
            db.commit()
            print("✓ Stage completed successfully")
            
            # Verify status is completed
            status = db.stages.get_status(test_batch, test_stage)
            assert status['status'] == 'completed', f"Status is {status['status']}, not completed"
            assert status['success'] is True, "Success flag not set"
            assert status['files_processed'] == 150, "files_processed mismatch"
            assert status['completed_at'] is not None, "completed_at not set"
            assert status['duration_seconds'] is not None, "duration_seconds not calculated"
            print(f"✓ Status verified: {status['status']}, duration: {status['duration_seconds']:.2f}s")
            
            # Test 5: Reset and test failure path
            print(f"\nTest 5: Testing failure path...")
            db.stages.reset(test_batch, test_stage)
            db.commit()
            print("✓ Stage reset")
            
            # Verify reset worked
            status = db.stages.get_status(test_batch, test_stage)
            assert status is None, "Stage was not reset"
            print("✓ Reset verified - status is None")
            
            # Start again
            db.stages.start(test_batch, test_stage, job_id=test_job_id)
            db.commit()
            
            # Complete as failed
            db.stages.complete(
                test_batch, test_stage,
                success=False,
                files_processed=100,
                files_failed=50,
                error_message="Test failure - simulated error"
            )
            db.commit()
            print("✓ Stage marked as failed")
            
            # Verify failure status
            status = db.stages.get_status(test_batch, test_stage)
            assert status['status'] == 'failed', f"Status is {status['status']}, not failed"
            assert status['success'] is False, "Success flag should be False"
            assert status['files_processed'] == 100, "files_processed mismatch"
            assert status['files_failed'] == 50, "files_failed mismatch"
            assert status['error_message'] is not None, "error_message not set"
            print(f"✓ Failure status verified: {status['error_message']}")
            
            # Test 6: Get failed stages
            print(f"\nTest 6: Getting failed stages...")
            failed = db.stages.get_failed(test_stage, limit=10)
            assert len(failed) > 0, "No failed stages found"
            found = False
            for stage in failed:
                if stage['batch_id'] == test_batch:
                    found = True
                    print(f"✓ Found test batch in failed list")
                    break
            assert found, "Test batch not found in failed list"
            
            # Test 7: Get batch status
            print(f"\nTest 7: Getting all stages for batch...")
            batch_stages = db.stages.get_batch_status(test_batch)
            assert len(batch_stages) > 0, "No stages found for batch"
            print(f"✓ Found {len(batch_stages)} stage(s) for batch '{test_batch}'")
            
            # Clean up test data
            print(f"\nCleaning up test data...")
            db.stages.reset(test_batch, test_stage)
            db.commit()
            print("✓ Test data cleaned up")
            
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
    """Run all Phase 3 tests."""
    print("=" * 60)
    print("Phase 3 - Stage Status Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_valid_stages()
        test_valid_statuses()
        test_stage_status_initialization()
        test_stage_validation()
        test_status_validation()
        test_agirdb_stages_integration()
        test_method_signatures()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 3 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 3 Complete!")
        print("=" * 60)
        print("\nPhase 3 components are working correctly.")
        print("Ready to proceed to Phase 4 (Event Logging).")
        
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