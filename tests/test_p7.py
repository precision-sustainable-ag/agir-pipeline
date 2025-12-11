#!/usr/bin/env python3
"""
Test script for Phase 7 - Transfer Management.

This script verifies that transfer tracking works correctly:
- TransferManager class methods
- SQL table and views
- Transfer lifecycle (pending → in_progress → completed/failed)
- Integration with AgirDB facade

Note: These tests require a live database connection with:
  - processed.transfers table (from Phase 7)
  - processed.batches table (from Phase 5)
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.transfers import TransferManager, VALID_TRANSFER_STATUSES
from agir_db.exceptions import (
    InvalidParameterError,
    TransferNotFoundError,
    BatchNotFoundError,
    QueryError
)


def test_valid_statuses():
    """Test that status constants are correct."""
    print("Testing valid statuses...")
    
    expected = {'pending', 'in_progress', 'completed', 'failed', 'cancelled'}
    assert VALID_TRANSFER_STATUSES == expected, f"VALID_TRANSFER_STATUSES mismatch"
    
    print("✓ Valid statuses are correct")


def test_transfer_manager_initialization():
    """Test TransferManager initialization without connection."""
    print("\nTesting TransferManager initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    transfers = TransferManager(conn)
    
    assert transfers.conn is conn
    assert hasattr(transfers, 'start_transfer')
    assert hasattr(transfers, 'update_globus_task')
    assert hasattr(transfers, 'update_progress')
    assert hasattr(transfers, 'complete')
    assert hasattr(transfers, 'cancel')
    assert hasattr(transfers, 'retry')
    assert hasattr(transfers, 'get_by_id')
    assert hasattr(transfers, 'get_by_batch')
    assert hasattr(transfers, 'get_active')
    assert hasattr(transfers, 'get_failed')
    assert hasattr(transfers, 'get_pending')
    
    print("✓ TransferManager initializes correctly")


def test_agirdb_integration():
    """Test that AgirDB exposes transfers component."""
    print("\nTesting AgirDB.transfers integration...")
    
    db = AgirDB(host='localhost', port=5432, dbname='agir', user='testuser')
    
    # Check that component exists
    assert hasattr(db, 'transfers'), "AgirDB does not have 'transfers' attribute"
    assert isinstance(db.transfers, TransferManager), "db.transfers is not a TransferManager instance"
    
    print("✓ AgirDB.transfers integration works correctly")


def test_status_validation():
    """Test status validation."""
    print("\nTesting status validation...")
    
    from agir_db.connection import ConnectionManager
    conn = ConnectionManager()
    
    transfers = TransferManager(conn)
    
    # Valid statuses should not raise
    try:
        for status in VALID_TRANSFER_STATUSES:
            transfers._validate_status(status)
    except Exception as e:
        raise AssertionError(f"Valid status raised exception: {e}")
    
    # Invalid status should raise
    try:
        transfers._validate_status('invalid_status')
        raise AssertionError("Invalid status did not raise exception")
    except InvalidParameterError:
        pass
    
    print("✓ Status validation works correctly")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database).
    
    This test requires processed.transfers and processed.batches tables.
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require processed.transfers table.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # ========================================
            # SETUP: Find or create a test batch
            # ========================================
            
            print("\nSetting up test batch...")
            test_batch_id = 'TEST_TRANSFER_2025-01-01'
            
            # Check if batch exists, create if not
            batch = db.batches.get_by_id(test_batch_id)
            if not batch:
                from datetime import date
                db.batches.insert(
                    batch_id=test_batch_id,
                    batch_state='MD',
                    batch_date=date(2025, 1, 1),
                    site='JUNO'
                )
                db.commit()
                print(f"✓ Created test batch: {test_batch_id}")
            else:
                print(f"✓ Using existing batch: {test_batch_id}")
            
            # Clean up any previous test transfers
            print(f"\nCleaning up previous test transfers...")
            try:
                db._connection.execute(
                    "DELETE FROM processed.transfers WHERE batch_id = %s",
                    (test_batch_id,)
                )
                db.commit()
            except:
                pass
            
            # ========================================
            # TEST START_TRANSFER
            # ========================================
            
            print("\nTest 1: Starting a transfer...")
            transfer_id = db.transfers.start_transfer(
                batch_id=test_batch_id,
                source_site='JUNO',
                destination_site='CERES',
                source_path='/juno/test',
                destination_path='/ceres/test',
                file_count=100,
                bytes_total=2500000000,
                job_id='test_job_123'
            )
            db.commit()
            
            assert transfer_id is not None
            assert isinstance(transfer_id, int)
            print(f"✓ Transfer started: transfer_id={transfer_id}")
            
            # Verify transfer was created
            transfer = db.transfers.get_by_id(transfer_id)
            assert transfer is not None
            assert transfer['batch_id'] == test_batch_id
            assert transfer['status'] == 'pending'
            assert transfer['source_site'] == 'JUNO'
            assert transfer['destination_site'] == 'CERES'
            print(f"✓ Transfer verified: status={transfer['status']}")
            
            # ========================================
            # TEST BATCH NOT FOUND
            # ========================================
            
            print("\nTest 2: Attempting transfer for non-existent batch...")
            try:
                db.transfers.start_transfer(
                    batch_id='FAKE_BATCH_9999-99-99',
                    source_site='JUNO',
                    destination_site='CERES'
                )
                db.commit()
                raise AssertionError("Should have raised BatchNotFoundError")
            except BatchNotFoundError as e:
                print(f"✓ Correctly raised BatchNotFoundError: {e}")
                db.rollback()
            
            # ========================================
            # TEST UPDATE_GLOBUS_TASK
            # ========================================
            
            print("\nTest 3: Updating with Globus task ID...")
            globus_task_id = 'test-abc-123-def-456'
            db.transfers.update_globus_task(transfer_id, globus_task_id)
            db.commit()
            
            transfer = db.transfers.get_by_id(transfer_id)
            assert transfer['globus_task_id'] == globus_task_id
            assert transfer['status'] == 'in_progress'
            assert transfer['started_at'] is not None
            print(f"✓ Updated Globus task ID: {globus_task_id}")
            print(f"  Status changed to: {transfer['status']}")
            
            # ========================================
            # TEST UPDATE_PROGRESS
            # ========================================
            
            print("\nTest 4: Updating transfer progress...")
            db.transfers.update_progress(
                transfer_id,
                files_transferred=50,
                bytes_transferred=1250000000,
                transfer_rate_mbps=125.5,
                globus_status='ACTIVE'
            )
            db.commit()
            
            transfer = db.transfers.get_by_id(transfer_id)
            assert transfer['files_transferred'] == 50
            assert transfer['bytes_transferred'] == 1250000000
            assert float(transfer['transfer_rate_mbps']) == 125.5
            assert transfer['globus_status'] == 'ACTIVE'
            print(f"✓ Progress updated: 50/100 files, 1.25GB/2.5GB")
            
            # ========================================
            # TEST GET_ACTIVE
            # ========================================
            
            print("\nTest 5: Getting active transfers...")
            active = db.transfers.get_active()
            assert isinstance(active, list)
            found = any(t['transfer_id'] == transfer_id for t in active)
            assert found, "Test transfer not found in active transfers"
            print(f"✓ Found {len(active)} active transfer(s)")
            
            # ========================================
            # TEST COMPLETE (SUCCESS)
            # ========================================
            
            print("\nTest 6: Completing transfer successfully...")
            db.transfers.complete(
                transfer_id,
                success=True,
                files_transferred=100,
                bytes_transferred=2500000000
            )
            db.commit()
            
            transfer = db.transfers.get_by_id(transfer_id)
            assert transfer['status'] == 'completed'
            assert transfer['files_transferred'] == 100
            assert transfer['bytes_transferred'] == 2500000000
            assert transfer['completed_at'] is not None
            assert transfer['duration_seconds'] is not None
            print(f"✓ Transfer completed successfully")
            print(f"  Duration: {transfer['duration_seconds']:.2f}s")
            
            # ========================================
            # TEST GET_BY_BATCH
            # ========================================
            
            print("\nTest 7: Getting transfers by batch...")
            batch_transfers = db.transfers.get_by_batch(test_batch_id)
            assert len(batch_transfers) >= 1
            found = any(t['transfer_id'] == transfer_id for t in batch_transfers)
            assert found, "Test transfer not found in batch transfers"
            print(f"✓ Found {len(batch_transfers)} transfer(s) for batch")
            
            # ========================================
            # TEST FAILED TRANSFER
            # ========================================
            
            print("\nTest 8: Creating a failed transfer...")
            failed_transfer_id = db.transfers.start_transfer(
                batch_id=test_batch_id,
                source_site='JUNO',
                destination_site='CERES',
                file_count=50,
                bytes_total=1250000000
            )
            db.commit()
            
            db.transfers.update_globus_task(failed_transfer_id, 'test-failed-task')
            db.commit()
            
            db.transfers.complete(
                failed_transfer_id,
                success=False,
                error_message="Connection timeout"
            )
            db.commit()
            
            failed_transfer = db.transfers.get_by_id(failed_transfer_id)
            assert failed_transfer['status'] == 'failed'
            assert failed_transfer['error_message'] == "Connection timeout"
            print(f"✓ Failed transfer created: {failed_transfer_id}")
            
            # ========================================
            # TEST GET_FAILED
            # ========================================
            
            print("\nTest 9: Getting failed transfers...")
            failed = db.transfers.get_failed()
            assert isinstance(failed, list)
            found = any(t['transfer_id'] == failed_transfer_id for t in failed)
            assert found, "Failed transfer not found in failed list"
            print(f"✓ Found {len(failed)} failed transfer(s)")
            
            # ========================================
            # TEST RETRY
            # ========================================
            
            print("\nTest 10: Retrying failed transfer...")
            retry_transfer_id = db.transfers.retry(failed_transfer_id)
            db.commit()
            
            assert retry_transfer_id != failed_transfer_id
            retry_transfer = db.transfers.get_by_id(retry_transfer_id)
            assert retry_transfer['status'] == 'pending'
            assert retry_transfer['retry_count'] == 1
            assert retry_transfer['batch_id'] == test_batch_id
            print(f"✓ Retry created: transfer_id={retry_transfer_id} (retry #1)")
            
            # ========================================
            # TEST CANCEL
            # ========================================
            
            print("\nTest 11: Cancelling pending transfer...")
            db.transfers.cancel(retry_transfer_id, reason="Test cancellation")
            db.commit()
            
            cancelled_transfer = db.transfers.get_by_id(retry_transfer_id)
            assert cancelled_transfer['status'] == 'cancelled'
            assert cancelled_transfer['error_message'] == "Test cancellation"
            print(f"✓ Transfer cancelled")
            
            # ========================================
            # TEST GET_PENDING
            # ========================================
            
            print("\nTest 12: Creating and getting pending transfer...")
            pending_transfer_id = db.transfers.start_transfer(
                batch_id=test_batch_id,
                source_site='CERES',
                destination_site='NCSU',
                file_count=25
            )
            db.commit()
            
            pending = db.transfers.get_pending()
            assert isinstance(pending, list)
            found = any(t['transfer_id'] == pending_transfer_id for t in pending)
            assert found, "Pending transfer not found in pending list"
            print(f"✓ Found {len(pending)} pending transfer(s)")
            
            # Clean up test data
            print("\nCleaning up test data...")
            db._connection.execute(
                "DELETE FROM processed.transfers WHERE batch_id = %s",
                (test_batch_id,)
            )
            db._connection.execute(
                "DELETE FROM processed.batches WHERE batch_id = %s",
                (test_batch_id,)
            )
            db.commit()
            print("✓ Test data cleaned up")
            
            print("\n✓ All database integration tests passed!")
            
    except Exception as e:
        if skip_if_no_db and ('connection' in str(e).lower() or 'does not exist' in str(e).lower()):
            print(f"\n⚠ Database or table not available: {e}")
            print("Skipping database integration tests.")
            print("This is expected if:")
            print("  - Database is not running")
            print("  - processed.transfers table doesn't exist")
            print("  - processed.batches table doesn't exist")
        else:
            print(f"\n✗ Database test failed: {e}")
            import traceback
            traceback.print_exc()
            raise


def main():
    """Run all Phase 7 tests."""
    print("=" * 60)
    print("Phase 7 - Transfer Management Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_valid_statuses()
        test_transfer_manager_initialization()
        test_agirdb_integration()
        test_status_validation()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 7 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 7 Complete!")
        print("=" * 60)
        print("\nPhase 7 components are working correctly.")
        print("Ready to proceed to Phase 8 (Analytics).")
        
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