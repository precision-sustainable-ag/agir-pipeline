#!/usr/bin/env python3
"""
Test script for Phase 5 - Image & Batch Metadata.

This script verifies that metadata management works correctly:
- SQL tables for images and batches
- ImageMetadata class methods
- BatchMetadata class methods
- Integration with AgirDB facade

Note: These tests require a live database connection with the SQL schema installed.
Run metadata_schema.sql before running these tests.
"""

import sys
from pathlib import Path
from datetime import date, datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.images import ImageMetadata, VALID_IMAGE_STATUSES
from agir_db.batches import BatchMetadata, VALID_BATCH_STATUSES
from agir_db.exceptions import (
    InvalidParameterError,
    DuplicateImageError,
    DuplicateBatchError,
    ImageNotFoundError,
    BatchNotFoundError,
    QueryError
)


def test_valid_statuses():
    """Test that status constants are correct."""
    print("Testing valid statuses...")
    
    expected_image = {'pending', 'raw_to_dng', 'dng_to_jpg', 'metadata_extracted', 
                      'cutouts_generated', 'completed', 'failed'}
    assert VALID_IMAGE_STATUSES == expected_image, f"VALID_IMAGE_STATUSES mismatch"
    
    expected_batch = {'pending', 'in_progress', 'completed', 'partial', 'failed'}
    assert VALID_BATCH_STATUSES == expected_batch, f"VALID_BATCH_STATUSES mismatch"
    
    print("✓ Valid statuses are correct")


def test_image_metadata_initialization():
    """Test ImageMetadata initialization without connection."""
    print("\nTesting ImageMetadata initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    images = ImageMetadata(conn)
    
    assert images.conn is conn
    assert hasattr(images, 'insert')
    assert hasattr(images, 'insert_bulk')
    assert hasattr(images, 'update_status')
    assert hasattr(images, 'update_bounding_boxes')
    assert hasattr(images, 'get_by_id')
    assert hasattr(images, 'get_by_batch')
    assert hasattr(images, 'get_with_detections')
    
    print("✓ ImageMetadata initializes correctly")


def test_batch_metadata_initialization():
    """Test BatchMetadata initialization without connection."""
    print("\nTesting BatchMetadata initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    batches = BatchMetadata(conn)
    
    assert batches.conn is conn
    assert hasattr(batches, 'insert')
    assert hasattr(batches, 'update_status')
    assert hasattr(batches, 'update_file_counts')
    assert hasattr(batches, 'update_completion_flags')
    assert hasattr(batches, 'get_by_id')
    assert hasattr(batches, 'get_by_state')
    assert hasattr(batches, 'get_by_status')
    assert hasattr(batches, 'get_summary')
    
    print("✓ BatchMetadata initializes correctly")


def test_agirdb_integration():
    """Test that AgirDB exposes images and batches components."""
    print("\nTesting AgirDB.images and AgirDB.batches integration...")
    
    db = AgirDB(host='localhost', port=5432, dbname='agir', user='testuser')
    
    # Check that components exist
    assert hasattr(db, 'images'), "AgirDB does not have 'images' attribute"
    assert hasattr(db, 'batches'), "AgirDB does not have 'batches' attribute"
    assert isinstance(db.images, ImageMetadata), "db.images is not an ImageMetadata instance"
    assert isinstance(db.batches, BatchMetadata), "db.batches is not a BatchMetadata instance"
    
    print("✓ AgirDB.images and AgirDB.batches integration works correctly")


def test_status_validation():
    """Test status validation."""
    print("\nTesting status validation...")
    
    from agir_db.connection import ConnectionManager
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    
    images = ImageMetadata(conn)
    batches = BatchMetadata(conn)
    
    # Valid statuses should not raise
    try:
        for status in VALID_IMAGE_STATUSES:
            images._validate_status(status)
        for status in VALID_BATCH_STATUSES:
            batches._validate_status(status)
    except Exception as e:
        raise AssertionError(f"Valid status raised exception: {e}")
    
    # Invalid status should raise
    try:
        images._validate_status('invalid_status')
        raise AssertionError("Invalid image status did not raise exception")
    except InvalidParameterError:
        pass
    
    try:
        batches._validate_status('invalid_status')
        raise AssertionError("Invalid batch status did not raise exception")
    except InvalidParameterError:
        pass
    
    print("✓ Status validation works correctly")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database).
    
    This test is optional and will be skipped if database is not available.
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require a live database with schema installed.")
    print("If you haven't run metadata_schema.sql yet, these will fail.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # Test data
            test_batch_id = 'TEST_BATCH_2025-01-01'
            test_image_id = 'TEST_IMG_001'
            test_batch_date = date(2025, 1, 1)
            
            # Clean up any previous test data
            print(f"\nCleaning up previous test data...")
            try:
                db._connection.execute("DELETE FROM processed.images WHERE batch_id = %s", (test_batch_id,))
                db._connection.execute("DELETE FROM processed.batches WHERE batch_id = %s", (test_batch_id,))
                db.commit()
            except:
                pass
            
            # ========================================
            # BATCH TESTS
            # ========================================
            
            # Test 1: Insert batch
            print("\nTest 1: Inserting batch...")
            db.batches.insert(
                batch_id=test_batch_id,
                batch_state='MD',
                batch_date=test_batch_date,
                location='JUNO',
                lts_root='test_lts',
                root_path='/test/path',
                processing_status='pending'
            )
            db.commit()
            print("✓ Batch inserted successfully")
            
            # Verify batch exists
            batch = db.batches.get_by_id(test_batch_id)
            assert batch is not None, "Batch not found after insert"
            assert batch['batch_id'] == test_batch_id
            assert batch['batch_state'] == 'MD'
            assert batch['processing_status'] == 'pending'
            print(f"✓ Batch verified: {batch['batch_id']}")
            
            # Test 2: Try to insert duplicate batch (should fail)
            print("\nTest 2: Attempting to insert duplicate batch...")
            try:
                db.batches.insert(
                    batch_id=test_batch_id,
                    batch_state='MD',
                    batch_date=test_batch_date
                )
                raise AssertionError("Should have raised DuplicateBatchError")
            except DuplicateBatchError as e:
                print(f"✓ Correctly raised DuplicateBatchError: {e}")
                # Rollback to reset transaction state
                db.rollback()
            
            # Test 3: Update batch status
            print("\nTest 3: Updating batch status...")
            db.batches.update_status(test_batch_id, 'in_progress')
            db.commit()
            batch = db.batches.get_by_id(test_batch_id)
            assert batch['processing_status'] == 'in_progress'
            print("✓ Batch status updated")
            
            # Test 4: Update file counts
            print("\nTest 4: Updating file counts...")
            db.batches.update_file_counts(
                test_batch_id,
                file_count_raw=100,
                file_count_jpg=50,
                total_bytes=2500000000
            )
            db.commit()
            batch = db.batches.get_by_id(test_batch_id)
            assert batch['file_count_raw'] == 100
            assert batch['file_count_jpg'] == 50
            assert batch['total_bytes'] == 2500000000
            print("✓ File counts updated")
            
            # Test 5: Update completion flags
            print("\nTest 5: Updating completion flags...")
            db.batches.update_completion_flags(
                test_batch_id,
                raw_to_jpg_complete=True,
                jpg_to_metadata_complete=False
            )
            db.commit()
            batch = db.batches.get_by_id(test_batch_id)
            assert batch['raw_to_jpg_complete'] is True
            assert batch['jpg_to_metadata_complete'] is False
            print("✓ Completion flags updated")
            
            # Test 6: Get batches by state
            print("\nTest 6: Getting batches by state...")
            batches = db.batches.get_by_state('MD', limit=10)
            assert isinstance(batches, list)
            found = any(b['batch_id'] == test_batch_id for b in batches)
            assert found, "Test batch not found in state query"
            print(f"✓ Found {len(batches)} batch(es) for state MD")
            
            # Test 7: Get batches by status
            print("\nTest 7: Getting batches by status...")
            batches = db.batches.get_by_status('in_progress', limit=10)
            found = any(b['batch_id'] == test_batch_id for b in batches)
            assert found, "Test batch not found in status query"
            print(f"✓ Found {len(batches)} batch(es) with status 'in_progress'")
            
            # ========================================
            # IMAGE TESTS
            # ========================================
            
            # Test 8: Insert image
            print("\nTest 8: Inserting image...")
            db.images.insert(
                image_id=test_image_id,
                batch_id=test_batch_id,
                file_name='TEST_IMG_001.raw',
                file_ext='raw',
                file_size_bytes=25000000,
                processing_status='pending',
                camera_make='Canon',
                camera_model='EOS R5',
                width=8192,
                height=5464
            )
            db.commit()
            print("✓ Image inserted successfully")
            
            # Verify image exists
            image = db.images.get_by_id(test_image_id)
            assert image is not None, "Image not found after insert"
            assert image['image_id'] == test_image_id
            assert image['batch_id'] == test_batch_id
            assert image['processing_status'] == 'pending'
            print(f"✓ Image verified: {image['image_id']}")
            
            # Test 9: Try to insert duplicate image (should fail)
            print("\nTest 9: Attempting to insert duplicate image...")
            try:
                db.images.insert(
                    image_id=test_image_id,
                    batch_id=test_batch_id,
                    file_name='TEST_IMG_001.raw'
                )
                raise AssertionError("Should have raised DuplicateImageError")
            except DuplicateImageError as e:
                print(f"✓ Correctly raised DuplicateImageError: {e}")
                # Rollback to reset transaction state
                db.rollback()
            
            # Test 10: Update image status
            print("\nTest 10: Updating image status...")
            db.images.update_status(test_image_id, 'dng_to_jpg')
            db.commit()
            image = db.images.get_by_id(test_image_id)
            assert image['processing_status'] == 'dng_to_jpg'
            print("✓ Image status updated")
            
            # Test 11: Update bounding boxes
            print("\nTest 11: Updating bounding boxes...")
            boxes = [
                {'x': 100, 'y': 200, 'width': 50, 'height': 50, 'class': 'deer', 'confidence': 0.95},
                {'x': 300, 'y': 400, 'width': 60, 'height': 70, 'class': 'deer', 'confidence': 0.88}
            ]
            db.images.update_bounding_boxes(test_image_id, boxes)
            db.commit()
            image = db.images.get_by_id(test_image_id)
            assert image['detection_count'] == 2
            assert len(image['bounding_boxes']) == 2
            print(f"✓ Bounding boxes updated: {image['detection_count']} detections")
            
            # Test 12: Get images by batch
            print("\nTest 12: Getting images by batch...")
            images = db.images.get_by_batch(test_batch_id)
            assert len(images) >= 1
            found = any(img['image_id'] == test_image_id for img in images)
            assert found, "Test image not found in batch query"
            print(f"✓ Found {len(images)} image(s) in batch")
            
            # Test 13: Get images by batch and status
            print("\nTest 13: Getting images by batch and status...")
            images = db.images.get_by_batch(test_batch_id, processing_status='dng_to_jpg')
            found = any(img['image_id'] == test_image_id for img in images)
            assert found, "Test image not found in batch/status query"
            print(f"✓ Found {len(images)} image(s) with status 'dng_to_jpg'")
            
            # Test 14: Get images with detections
            print("\nTest 14: Getting images with detections...")
            images = db.images.get_with_detections(batch_id=test_batch_id)
            found = any(img['image_id'] == test_image_id for img in images)
            assert found, "Test image not found in detections query"
            print(f"✓ Found {len(images)} image(s) with detections")
            
            # Test 15: Bulk insert images
            print("\nTest 15: Bulk inserting images...")
            bulk_images = [
                {
                    'image_id': 'TEST_IMG_002',
                    'batch_id': test_batch_id,
                    'file_name': 'TEST_IMG_002.raw',
                    'processing_status': 'pending'
                },
                {
                    'image_id': 'TEST_IMG_003',
                    'batch_id': test_batch_id,
                    'file_name': 'TEST_IMG_003.raw',
                    'processing_status': 'pending'
                }
            ]
            count = db.images.insert_bulk(bulk_images)
            db.commit()
            assert count == 2
            print(f"✓ Bulk inserted {count} images")
            
            # Test 16: Get batch summary
            print("\nTest 16: Getting batch summary...")
            summaries = db.batches.get_summary(batch_id=test_batch_id)
            assert len(summaries) == 1
            summary = summaries[0]
            assert summary['batch_id'] == test_batch_id
            assert summary['registered_images'] >= 3  # We inserted at least 3 images
            print(f"✓ Batch summary: {summary['registered_images']} registered images")
            
            # Clean up test data
            print("\nCleaning up test data...")
            db._connection.execute("DELETE FROM processed.images WHERE batch_id = %s", (test_batch_id,))
            db._connection.execute("DELETE FROM processed.batches WHERE batch_id = %s", (test_batch_id,))
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
    """Run all Phase 5 tests."""
    print("=" * 60)
    print("Phase 5 - Image & Batch Metadata Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_valid_statuses()
        test_image_metadata_initialization()
        test_batch_metadata_initialization()
        test_agirdb_integration()
        test_status_validation()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 5 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 5 Complete!")
        print("=" * 60)
        print("\nPhase 5 components are working correctly.")
        print("Ready to proceed to Phase 6 (Inventory Sync).")
        
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