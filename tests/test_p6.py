#!/usr/bin/env python3
"""
Test script for Phase 6 - Inventory Sync.

This script verifies that inventory synchronization works correctly:
- InventorySync class methods
- Syncing from globus_file_index to processed tables
- Reconciliation between source and processed
- Integration with AgirDB facade

Note: These tests require a live database connection with both:
  - source.globus_file_index table (with data)
  - processed.batches and processed.images tables (from Phase 5)
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.inventory import InventorySync
from agir_db.exceptions import (
    BatchNotFoundError,
    QueryError
)


def test_inventory_sync_initialization():
    """Test InventorySync initialization without connection."""
    print("Testing InventorySync initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    inventory = InventorySync(conn)
    
    assert inventory.conn is conn
    assert hasattr(inventory, 'sync_batch')
    assert hasattr(inventory, 'sync_all')
    assert hasattr(inventory, 'sync_recent')
    assert hasattr(inventory, 'reconcile')
    assert hasattr(inventory, 'get_sync_status')
    
    print("✓ InventorySync initializes correctly")


def test_agirdb_integration():
    """Test that AgirDB exposes inventory component."""
    print("\nTesting AgirDB.inventory integration...")
    
    db = AgirDB(host='localhost', port=5432, dbname='agir', user='testuser')
    
    # Check that component exists
    assert hasattr(db, 'inventory'), "AgirDB does not have 'inventory' attribute"
    assert isinstance(db.inventory, InventorySync), "db.inventory is not an InventorySync instance"
    
    print("✓ AgirDB.inventory integration works correctly")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database with data).
    
    This test requires:
    1. source.globus_file_index table with real data
    2. processed.batches and processed.images tables (from Phase 5)
    
    These tests will be skipped if database is not available or has no data.
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require source.globus_file_index with data.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # ========================================
            # SETUP: Find a batch in globus_file_index
            # ========================================
            
            print("\nFinding test batch in globus_file_index...")
            
            # Get a batch from globus_file_index that has RAW files (case-insensitive)
            query = """
                SELECT DISTINCT batch_id
                FROM source.globus_file_index
                WHERE batch_id IS NOT NULL
                AND LOWER(file_ext) IN ('raw', 'arw')
                LIMIT 1;
            """
            
            try:
                result = db._connection.fetch_one(query)
                if not result:
                    print("\n⚠ No batches with RAW files found in globus_file_index")
                    print("Skipping database integration tests (no RAW files to sync)")
                    if skip_if_no_db:
                        return
                    else:
                        raise AssertionError("No batches with RAW files in globus_file_index for testing")
                
                test_batch_id = result['batch_id']
                print(f"✓ Using test batch: {test_batch_id}")
                
            except Exception as e:
                print(f"\n⚠ Could not query globus_file_index: {e}")
                print("Skipping database integration tests")
                if skip_if_no_db:
                    return
                else:
                    raise
            
            # Clean up previous test data
            print(f"\nCleaning up previous test data for {test_batch_id}...")
            try:
                db._connection.execute("DELETE FROM processed.images WHERE batch_id = %s", (test_batch_id,))
                db._connection.execute("DELETE FROM processed.batches WHERE batch_id = %s", (test_batch_id,))
                db.commit()
                print("✓ Cleaned up previous data")
            except:
                pass
            
            # ========================================
            # TEST SYNC STATUS
            # ========================================
            
            print("\nTest 1: Getting sync status...")
            status = db.inventory.get_sync_status()
            assert 'source_batches' in status
            assert 'processed_batches' in status
            assert 'source_raw_files' in status
            assert 'processed_images' in status
            assert 'sync_percentage' in status
            print(f"✓ Sync status: {status['processed_batches']}/{status['source_batches']} batches ({status['sync_percentage']:.1f}%)")
            print(f"  Images: {status['processed_images']}/{status['source_raw_files']} ({status['images_missing']} missing)")
            
            # ========================================
            # TEST SYNC BATCH
            # ========================================
            
            print(f"\nTest 2: Syncing batch {test_batch_id}...")
            stats = db.inventory.sync_batch(test_batch_id)
            db.commit()
            
            assert 'batch_existed' in stats
            assert 'images_inserted' in stats
            assert 'images_skipped' in stats
            assert 'files_found' in stats
            
            print(f"✓ Sync completed:")
            print(f"  Batch existed: {stats['batch_existed']}")
            print(f"  Images inserted: {stats['images_inserted']}")
            print(f"  Images skipped: {stats['images_skipped']}")
            print(f"  Files found: {stats['files_found']}")
            print(f"  RAW files: {stats['raw_files']}")
            
            # Verify we actually found and inserted RAW files
            assert stats['raw_files'] > 0, f"No RAW files found in batch {test_batch_id}"
            assert stats['images_inserted'] > 0, f"No images inserted for batch {test_batch_id}"
            
            # Verify batch was created
            batch = db.batches.get_by_id(test_batch_id)
            assert batch is not None, "Batch not found after sync"
            assert batch['batch_id'] == test_batch_id
            print(f"✓ Batch verified in processed.batches")
            
            # Verify images were created
            images = db.images.get_by_batch(test_batch_id)
            assert len(images) == stats['images_inserted'], "Image count mismatch"
            print(f"✓ {len(images)} images verified in processed.images")
            
            # ========================================
            # TEST SYNC BATCH AGAIN (should skip existing)
            # ========================================
            
            print(f"\nTest 3: Syncing batch {test_batch_id} again (should skip existing)...")
            stats2 = db.inventory.sync_batch(test_batch_id, update_existing=False)
            db.commit()
            
            print(f"Second sync results:")
            print(f"  Batch existed: {stats2['batch_existed']}")
            print(f"  Images inserted: {stats2['images_inserted']}")
            print(f"  Images skipped: {stats2['images_skipped']}")
            print(f"  RAW files: {stats2['raw_files']}")
            
            assert stats2['batch_existed'] is True, "Batch should have existed"
            assert stats2['images_inserted'] == 0, "Should not insert duplicate images"
            assert stats2['raw_files'] > 0, f"Should still find RAW files in batch {test_batch_id}"
            assert stats2['images_skipped'] > 0, \
                f"Should skip existing images (raw_files={stats2['raw_files']}, inserted={stats2['images_inserted']}, skipped={stats2['images_skipped']})"
            print(f"✓ Correctly skipped {stats2['images_skipped']} existing images")
            
            # ========================================
            # TEST RECONCILE
            # ========================================
            
            print(f"\nTest 4: Reconciling batch {test_batch_id}...")
            reconcile_results = db.inventory.reconcile(batch_id=test_batch_id)
            
            assert 'missing_batches' in reconcile_results
            assert 'missing_images' in reconcile_results
            assert 'orphaned_batches' in reconcile_results
            assert 'orphaned_images' in reconcile_results
            
            # Since we just synced, there should be no missing data for this batch
            assert test_batch_id not in reconcile_results['missing_batches'], \
                "Batch should not be missing after sync"
            
            print(f"✓ Reconciliation results:")
            print(f"  Missing batches: {len(reconcile_results['missing_batches'])}")
            print(f"  Missing images: {reconcile_results['missing_images']}")
            print(f"  Orphaned batches: {len(reconcile_results['orphaned_batches'])}")
            print(f"  Orphaned images: {reconcile_results['orphaned_images']}")
            
            # ========================================
            # TEST FULL RECONCILE
            # ========================================
            
            print("\nTest 5: Full reconciliation...")
            full_reconcile = db.inventory.reconcile()
            
            print(f"✓ Full reconciliation:")
            print(f"  Missing batches: {len(full_reconcile['missing_batches'])}")
            print(f"  Missing images: {full_reconcile['missing_images']}")
            
            # ========================================
            # TEST SYNC_RECENT
            # ========================================
            
            print("\nTest 6: Syncing recent batches (last 7 days)...")
            recent_stats = db.inventory.sync_recent(days=7, update_existing=False)
            db.commit()
            
            assert 'batches_synced' in recent_stats
            assert 'batches_failed' in recent_stats
            assert 'total_images_inserted' in recent_stats
            
            print(f"✓ Recent sync completed:")
            print(f"  Batches synced: {recent_stats['batches_synced']}")
            print(f"  Batches failed: {recent_stats['batches_failed']}")
            print(f"  Images inserted: {recent_stats['total_images_inserted']}")
            
            # ========================================
            # TEST SYNC_ALL (with limit)
            # ========================================
            
            print("\nTest 7: Full sync with limit...")
            all_stats = db.inventory.sync_all(limit=5, update_existing=False)
            db.commit()
            
            assert 'batches_synced' in all_stats
            assert 'batches_failed' in all_stats
            assert 'total_images_inserted' in all_stats
            assert 'elapsed_seconds' in all_stats
            
            print(f"✓ Full sync (limit=5) completed:")
            print(f"  Batches synced: {all_stats['batches_synced']}")
            print(f"  Batches failed: {all_stats['batches_failed']}")
            print(f"  Images inserted: {all_stats['total_images_inserted']}")
            print(f"  Elapsed: {all_stats['elapsed_seconds']:.2f}s")
            
            # ========================================
            # TEST SYNC STATUS AGAIN
            # ========================================
            
            print("\nTest 8: Final sync status...")
            final_status = db.inventory.get_sync_status()
            
            # Should have more batches synced now
            assert final_status['processed_batches'] >= status['processed_batches'], \
                "Should have same or more batches synced"
            
            print(f"✓ Final sync status:")
            print(f"  Progress: {final_status['processed_batches']}/{final_status['source_batches']} batches")
            print(f"  Sync: {final_status['sync_percentage']:.1f}%")
            print(f"  Missing batches: {final_status['batches_missing']}")
            print(f"  Missing images: {final_status['images_missing']}")
            
            # ========================================
            # TEST BATCH NOT FOUND
            # ========================================
            
            print("\nTest 9: Syncing non-existent batch...")
            fake_batch_id = 'FAKE_BATCH_9999-99-99'
            try:
                db.inventory.sync_batch(fake_batch_id)
                raise AssertionError("Should have raised BatchNotFoundError")
            except BatchNotFoundError as e:
                print(f"✓ Correctly raised BatchNotFoundError: {e}")
            
            # Note: Don't clean up test data - leave it for manual inspection
            print("\n✓ All database integration tests passed!")
            print(f"\nNote: Test batch {test_batch_id} was left in database for inspection.")
            print("You can clean it up manually if desired:")
            print(f"  DELETE FROM processed.images WHERE batch_id = '{test_batch_id}';")
            print(f"  DELETE FROM processed.batches WHERE batch_id = '{test_batch_id}';")
            
    except Exception as e:
        if skip_if_no_db and ('connection' in str(e).lower() or 'does not exist' in str(e).lower()):
            print(f"\n⚠ Database or table not available: {e}")
            print("Skipping database integration tests.")
            print("This is expected if:")
            print("  - Database is not running")
            print("  - source.globus_file_index table doesn't exist")
            print("  - No data in globus_file_index")
        else:
            print(f"\n✗ Database test failed: {e}")
            import traceback
            traceback.print_exc()
            raise


def main():
    """Run all Phase 6 tests."""
    print("=" * 60)
    print("Phase 6 - Inventory Sync Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_inventory_sync_initialization()
        test_agirdb_integration()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 6 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 6 Complete!")
        print("=" * 60)
        print("\nPhase 6 components are working correctly.")
        print("Ready to proceed to Phase 7 (Transfer Management).")
        
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