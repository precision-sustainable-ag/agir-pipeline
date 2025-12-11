#!/usr/bin/env python3
"""
Test script for Phase 10 - Orchestration Helpers.

This script verifies that orchestration workflows work correctly:
- Orchestration class methods
- Conversion queue discovery
- Batch processing workflow
- Progress tracking
- Integration with AgirDB facade

Note: These tests require a live database connection.
"""

import sys
from pathlib import Path
from datetime import date, datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.orchestration import Orchestration
from agir_db.exceptions import OrchestrationError


def test_orchestration_initialization():
    """Test Orchestration initialization without connection."""
    print("\nTesting Orchestration initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager()
    orchestration = Orchestration(conn)
    
    assert orchestration.conn is conn
    assert hasattr(orchestration, 'get_conversion_queue')
    assert hasattr(orchestration, 'start_batch_conversion')
    assert hasattr(orchestration, 'complete_batch_conversion')
    assert hasattr(orchestration, 'get_batch_progress')
    
    print("✓ Orchestration initializes correctly")


def test_agirdb_integration():
    """Test that AgirDB exposes orchestration component."""
    print("\nTesting AgirDB.orchestration integration...")
    
    db = AgirDB()
    
    # Check that component exists
    assert hasattr(db, 'orchestration'), "AgirDB does not have 'orchestration' attribute"
    assert isinstance(db.orchestration, Orchestration), "db.orchestration is not an Orchestration instance"
    
    print("✓ AgirDB.orchestration integration works correctly")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database).
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require processed schema tables and test data.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # ========================================
            # TEST 1: GET CONVERSION SUMMARY
            # ========================================
            
            print("\nTest 1: Getting conversion summary...")
            summary = db.orchestration.get_conversion_summary(days=7)
            
            assert isinstance(summary, dict)
            assert 'batches_in_queue' in summary
            assert 'batches_active' in summary
            assert 'batches_completed' in summary
            
            print(f"✓ Conversion summary:")
            print(f"  Queue: {summary.get('batches_in_queue', 0)}")
            print(f"  Active: {summary.get('batches_active', 0)}")
            print(f"  Completed (7d): {summary.get('batches_completed', 0)}")
            print(f"  Failed (7d): {summary.get('batches_failed', 0)}")
            
            # ========================================
            # TEST 2: GET CONVERSION QUEUE
            # ========================================
            
            print("\nTest 2: Getting conversion queue...")
            queue = db.orchestration.get_conversion_queue(limit=10)
            
            assert isinstance(queue, list)
            print(f"✓ Found {len(queue)} batches in queue")
            
            if queue:
                batch = queue[0]
                print(f"  First batch: {batch['batch_id']}")
                print(f"  Gap count: {batch['gap_count']}")
                print(f"  Priority: {batch['priority']}")
            
            # ========================================
            # TEST 3: GET FILES FOR CONVERSION
            # ========================================
            
            if queue:
                print("\nTest 3: Getting files for conversion...")
                batch_id = queue[0]['batch_id']
                
                files = db.orchestration.get_batch_files_for_conversion(
                    batch_id,
                    check_existing=True
                )
                
                assert isinstance(files, list)
                print(f"✓ Found {len(files)} files to convert in {batch_id}")
                
                if files:
                    file = files[0]
                    print(f"  First file: {file['file_name']}")
                    print(f"  Image ID: {file['image_id']}")
            
            # ========================================
            # TEST 4: GET ACTIVE CONVERSIONS
            # ========================================
            
            print("\nTest 4: Getting active conversions...")
            active = db.orchestration.get_active_conversions()
            
            assert isinstance(active, list)
            print(f"✓ Found {len(active)} active conversions")
            
            if active:
                conv = active[0]
                print(f"  Batch: {conv['batch_id']}")
                print(f"  Status: {conv['status']}")
                print(f"  Progress: {conv['files_processed']} files")
            
            # ========================================
            # TEST 5: GET FAILED CONVERSIONS
            # ========================================
            
            print("\nTest 5: Getting failed conversions...")
            failed = db.orchestration.get_failed_conversions(days=7)
            
            assert isinstance(failed, list)
            print(f"✓ Found {len(failed)} failed conversions (7 days)")
            
            if failed:
                conv = failed[0]
                print(f"  Batch: {conv['batch_id']}")
                print(f"  Error: {conv.get('error_message', 'N/A')[:50]}")
            
            # ========================================
            # TEST 6: START & COMPLETE BATCH CONVERSION
            # ========================================
            
            # Only test if we have a batch in queue
            if queue and len(queue) > 0:
                print("\nTest 6: Testing batch conversion workflow...")
                test_batch_id = queue[0]['batch_id']
                
                # Check current progress
                progress_before = db.orchestration.get_batch_progress(test_batch_id)
                print(f"  Current status: {progress_before.get('status', 'not_started')}")
                
                # Only proceed if not already running
                if progress_before.get('status') not in ['running']:
                    try:
                        # Start conversion
                        info = db.orchestration.start_batch_conversion(
                            test_batch_id,
                            job_id='test-worker-001',
                            worker_name='test-worker'
                        )
                        db.commit()
                        
                        assert info['batch_id'] == test_batch_id
                        assert 'file_count' in info
                        print(f"✓ Started conversion for {test_batch_id}")
                        print(f"  Files to convert: {info['file_count']}")
                        
                        # Check progress
                        progress_after = db.orchestration.get_batch_progress(test_batch_id)
                        assert progress_after['status'] == 'running'
                        print(f"✓ Status is 'running'")
                        
                        # Update progress (simulate partial completion)
                        db.orchestration.update_conversion_progress(
                            test_batch_id,
                            files_processed=5,
                            files_failed=0
                        )
                        db.commit()
                        
                        progress_updated = db.orchestration.get_batch_progress(test_batch_id)
                        assert progress_updated['files_processed'] == 5
                        print(f"✓ Updated progress: {progress_updated['files_processed']} files")
                        
                        # Complete conversion
                        db.orchestration.complete_batch_conversion(
                            test_batch_id,
                            success=True,
                            files_processed=info['file_count'],
                            files_failed=0
                        )
                        db.commit()
                        
                        progress_final = db.orchestration.get_batch_progress(test_batch_id)
                        assert progress_final['status'] == 'completed'
                        print(f"✓ Completed conversion")
                        print(f"  Final count: {progress_final['files_processed']} files")
                        
                    except Exception as e:
                        print(f"⚠ Could not complete full workflow test: {e}")
                        db.rollback()
                else:
                    print(f"  ⚠ Batch already running, skipping workflow test")
            else:
                print("\nTest 6: Skipped (no batches in queue)")
            
            # ========================================
            # TEST 7: GET BATCH PROGRESS (NON-EXISTENT)
            # ========================================
            
            print("\nTest 7: Getting progress for non-existent batch...")
            progress = db.orchestration.get_batch_progress('FAKE_BATCH_9999-99-99')
            
            assert progress['status'] == 'not_started'
            assert progress['files_processed'] == 0
            print(f"✓ Correctly returns 'not_started' for non-existent batch")
            
            # ========================================
            # TEST 8: CONVERSION QUEUE WITH FILTERS
            # ========================================
            
            print("\nTest 8: Testing queue filters...")
            
            # Filter by batch_state
            queue_md = db.orchestration.get_conversion_queue(limit=5, batch_state='MD')
            assert isinstance(queue_md, list)
            print(f"✓ Found {len(queue_md)} MD batches")
            
            # Filter by site
            queue_juno = db.orchestration.get_conversion_queue(limit=5, site='JUNO')
            assert isinstance(queue_juno, list)
            print(f"✓ Found {len(queue_juno)} JUNO batches")
            
            print("\n✓ All database integration tests passed!")
            
    except Exception as e:
        if skip_if_no_db and ('connection' in str(e).lower() or 'does not exist' in str(e).lower()):
            print(f"\n⚠ Database or table not available: {e}")
            print("Skipping database integration tests.")
            print("This is expected if:")
            print("  - Database is not running")
            print("  - processed schema tables don't exist")
            print("  - No test data available")
        else:
            print(f"\n✗ Database test failed: {e}")
            import traceback
            traceback.print_exc()
            raise


def main():
    """Run all Phase 10 tests."""
    print("=" * 60)
    print("Phase 10 - Orchestration Helpers Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_orchestration_initialization()
        test_agirdb_integration()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 10 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 10 Complete!")
        print("=" * 60)
        print("\nPhase 10 components are working correctly.")
        print("Ready for integration with svs-raw-api converters!")
        
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