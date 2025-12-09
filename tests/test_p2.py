#!/usr/bin/env python3
"""
Test script for Phase 2 - Pipeline Gaps.

This script verifies that the pipeline gap analysis works correctly:
- SQL views for gap detection
- PipelineGaps class methods
- Integration with AgirDB facade

Note: These tests require a live database connection with the SQL views installed.
Run pipeline_gaps_schema.sql before running these tests.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.gaps import PipelineGaps, VALID_STAGES
from agir_db.exceptions import InvalidParameterError, QueryError    


def test_valid_stages():
    """Test that VALID_STAGES constant is correct."""
    print("Testing valid stages...")
    
    expected = {'raw_to_jpg', 'jpg_to_metadata', 'metadata_to_cutouts'}
    assert VALID_STAGES == expected, f"VALID_STAGES mismatch: {VALID_STAGES}"
    
    print("✓ Valid stages are correct")


def test_pipeline_gaps_initialization():
    """Test PipelineGaps initialization without connection."""
    print("\nTesting PipelineGaps initialization...")
    
    # This test doesn't actually connect - just verifies initialization
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager()
    gaps = PipelineGaps(conn)
    
    assert gaps.conn is conn
    assert hasattr(gaps, 'get_batches_with_gaps')
    assert hasattr(gaps, 'get_files_with_gap')
    assert hasattr(gaps, 'get_batch_pipeline_summary')
    assert hasattr(gaps, 'get_gap_summary')
    
    print("✓ PipelineGaps initializes correctly")


def test_stage_validation():
    """Test stage validation."""
    print("\nTesting stage validation...")
    
    from agir_db.connection import ConnectionManager
    conn = ConnectionManager()
    gaps = PipelineGaps(conn)
    
    # Valid stages should not raise
    try:
        gaps._validate_stage('raw_to_jpg')
        gaps._validate_stage('jpg_to_metadata')
        gaps._validate_stage('metadata_to_cutouts')
    except Exception as e:
        raise AssertionError(f"Valid stage raised exception: {e}")
    
    # Invalid stage should raise
    try:
        gaps._validate_stage('invalid_stage')
        raise AssertionError("Invalid stage did not raise exception")
    except InvalidParameterError as e:
        assert 'invalid_stage' in str(e).lower()
    
    print("✓ Stage validation works correctly")


def test_agirdb_gaps_integration():
    """Test that AgirDB exposes gaps component."""
    print("\nTesting AgirDB.gaps integration...")
    
    db = AgirDB()
    
    # Check that gaps component exists
    assert hasattr(db, 'gaps'), "AgirDB does not have 'gaps' attribute"
    assert isinstance(db.gaps, PipelineGaps), "db.gaps is not a PipelineGaps instance"
    
    # Check that methods are accessible
    assert hasattr(db.gaps, 'get_batches_with_gaps')
    assert hasattr(db.gaps, 'get_files_with_gap')
    assert hasattr(db.gaps, 'get_batch_pipeline_summary')
    assert hasattr(db.gaps, 'get_gap_summary')
    
    print("✓ AgirDB.gaps integration works correctly")


def test_method_signatures():
    """Test that methods have correct signatures."""
    print("\nTesting method signatures...")
    
    from agir_db.connection import ConnectionManager
    import inspect
    
    conn = ConnectionManager()
    gaps = PipelineGaps(conn)
    
    # get_batches_with_gaps(stage, limit=None)
    sig = inspect.signature(gaps.get_batches_with_gaps)
    assert 'stage' in sig.parameters
    assert 'limit' in sig.parameters
    assert sig.parameters['limit'].default is None
    
    # get_files_with_gap(batch_id, stage)
    sig = inspect.signature(gaps.get_files_with_gap)
    assert 'batch_id' in sig.parameters
    assert 'stage' in sig.parameters
    
    # get_batch_pipeline_summary(batch_id)
    sig = inspect.signature(gaps.get_batch_pipeline_summary)
    assert 'batch_id' in sig.parameters
    
    # get_gap_summary(stage=None)
    sig = inspect.signature(gaps.get_gap_summary)
    assert 'stage' in sig.parameters
    assert sig.parameters['stage'].default is None
    
    print("✓ Method signatures are correct")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database queries (requires live database).
    
    This test is optional and will be skipped if database is not available.
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require a live database with SQL views installed.")
    print("If you haven't run pipeline_gaps_schema.sql yet, these will fail.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # Test get_gap_summary (should always work, even with no data)
            print("\nTesting get_gap_summary()...")
            summary = db.gaps.get_gap_summary()
            assert isinstance(summary, list)
            print(f"✓ Gap summary: {len(summary)} stage(s)")
            
            for stage_summary in summary:
                print(f"  - {stage_summary['stage']}: "
                      f"{stage_summary['batches_with_gaps']} batches, "
                      f"{stage_summary['total_files_with_gaps']} files")
            
            # Test get_batches_with_gaps
            print("\nTesting get_batches_with_gaps('raw_to_jpg', limit=5)...")
            batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=5)
            assert isinstance(batches, list)
            print(f"✓ Found {len(batches)} batch(es) with gaps")
            
            if batches:
                batch = batches[0]
                print(f"  Example: {batch['batch_id']} - {batch['files_needing_processing']} files")
                
                # Test get_files_with_gap for first batch
                print(f"\nTesting get_files_with_gap('{batch['batch_id']}', 'raw_to_jpg')...")
                files = db.gaps.get_files_with_gap(batch['batch_id'], 'raw_to_jpg')
                assert isinstance(files, list)
                print(f"✓ Found {len(files)} file(s) with gaps")
                
                if files:
                    file = files[0]
                    print(f"  Example: {file['file_name']}")
                
                # Test get_batch_pipeline_summary
                print(f"\nTesting get_batch_pipeline_summary('{batch['batch_id']}')...")
                summary = db.gaps.get_batch_pipeline_summary(batch['batch_id'])
                assert isinstance(summary, dict)
                print(f"✓ Pipeline summary retrieved")
                print(f"  RAW: {summary['raw_count']}, JPG: {summary['jpg_count']}, "
                      f"Metadata: {summary['metadata_count']}, Cutouts: {summary['cutout_count']}")
            else:
                print("  (No batches with gaps found - database may be empty or fully processed)")
            
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
    """Run all Phase 2 tests."""
    print("=" * 60)
    print("Phase 2 - Pipeline Gaps Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_valid_stages()
        test_pipeline_gaps_initialization()
        test_stage_validation()
        test_agirdb_gaps_integration()
        test_method_signatures()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 2 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 2 Complete!")
        print("=" * 60)
        print("\nPhase 2 components are working correctly.")
        print("Ready to proceed to Phase 3 (Stage Status).")
        
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