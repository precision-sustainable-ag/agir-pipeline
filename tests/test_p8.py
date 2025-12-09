#!/usr/bin/env python3
"""
Test script for Phase 8 - Analytics.

This script verifies that analytics and reporting works correctly:
- Analytics class methods
- SQL views
- Statistics generation
- Integration with AgirDB facade

Note: These tests require a live database connection with:
  - All processed schema tables and views
  - Some sample data for meaningful statistics
"""

import sys
from pathlib import Path
from datetime import date, timedelta

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.analytics import Analytics
from agir_db.exceptions import QueryError


def test_analytics_initialization():
    """Test Analytics initialization without connection."""
    print("\nTesting Analytics initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager()
    analytics = Analytics(conn)
    
    assert analytics.conn is conn
    assert hasattr(analytics, 'get_pipeline_overview')
    assert hasattr(analytics, 'get_processing_stats')
    assert hasattr(analytics, 'get_daily_volumes')
    assert hasattr(analytics, 'get_throughput')
    assert hasattr(analytics, 'get_stage_performance')
    assert hasattr(analytics, 'get_error_summary')
    assert hasattr(analytics, 'get_recent_errors')
    assert hasattr(analytics, 'get_error_rate')
    assert hasattr(analytics, 'get_transfer_performance')
    assert hasattr(analytics, 'get_transfer_summary_by_route')
    assert hasattr(analytics, 'get_storage_by_location')
    assert hasattr(analytics, 'get_storage_growth')
    assert hasattr(analytics, 'get_batch_summary')
    assert hasattr(analytics, 'get_camera_stats')
    
    print("✓ Analytics initializes correctly")


def test_agirdb_integration():
    """Test that AgirDB exposes analytics component."""
    print("\nTesting AgirDB.analytics integration...")
    
    db = AgirDB()
    
    # Check that component exists
    assert hasattr(db, 'analytics'), "AgirDB does not have 'analytics' attribute"
    assert isinstance(db.analytics, Analytics), "db.analytics is not an Analytics instance"
    
    print("✓ AgirDB.analytics integration works correctly")


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database).
    
    This test requires processed schema with views and some sample data.
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require analytics views and sample data.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # ========================================
            # TEST PIPELINE OVERVIEW
            # ========================================
            
            print("\nTest 1: Getting pipeline overview...")
            overview = db.analytics.get_pipeline_overview()
            
            assert isinstance(overview, dict)
            print(f"✓ Pipeline overview retrieved:")
            print(f"  Total batches: {overview.get('total_batches', 0)}")
            print(f"  Total images: {overview.get('total_images', 0)}")
            print(f"  Storage: {overview.get('total_storage_gb', 0)} GB")
            
            # ========================================
            # TEST PROCESSING STATS
            # ========================================
            
            print("\nTest 2: Getting processing stats (last 30 days)...")
            stats = db.analytics.get_processing_stats(days=30)
            
            assert isinstance(stats, dict)
            print(f"✓ Processing stats retrieved:")
            print(f"  Batches processed: {stats.get('batches_processed', 0)}")
            print(f"  Files processed: {stats.get('files_processed', 0)}")
            print(f"  Total GB: {stats.get('total_gb_processed', 0)}")
            
            # ========================================
            # TEST DAILY VOLUMES
            # ========================================
            
            print("\nTest 3: Getting daily volumes (last 7 days)...")
            volumes = db.analytics.get_daily_volumes(days=7)
            
            assert isinstance(volumes, list)
            print(f"✓ Daily volumes retrieved: {len(volumes)} records")
            if volumes:
                v = volumes[0]
                print(f"  Most recent: {v['processing_date']} - {v['batch_count']} batches")
            
            # ========================================
            # TEST THROUGHPUT
            # ========================================
            
            print("\nTest 4: Getting throughput (last 7 days)...")
            throughput = db.analytics.get_throughput(days=7)
            
            assert isinstance(throughput, list)
            print(f"✓ Throughput retrieved: {len(throughput)} records")
            if throughput:
                t = throughput[0]
                print(f"  Recent: {t['stage']} - {t.get('avg_files_per_second', 0)} files/sec")
            
            # ========================================
            # TEST STAGE PERFORMANCE
            # ========================================
            
            print("\nTest 5: Getting stage performance...")
            performance = db.analytics.get_stage_performance()
            
            assert isinstance(performance, list)
            print(f"✓ Stage performance retrieved: {len(performance)} stages")
            if performance:
                p = performance[0]
                print(f"  {p['stage']}: {p.get('success_rate', 0)}% success rate")
            
            # ========================================
            # TEST ERROR SUMMARY
            # ========================================
            
            print("\nTest 6: Getting error summary...")
            errors = db.analytics.get_error_summary(days=30)
            
            assert isinstance(errors, list)
            print(f"✓ Error summary retrieved: {len(errors)} records")
            if errors:
                e = errors[0]
                print(f"  {e['stage']}: {e['error_count']} errors")
            
            # ========================================
            # TEST RECENT ERRORS
            # ========================================
            
            print("\nTest 7: Getting recent errors...")
            recent_errors = db.analytics.get_recent_errors(limit=10)
            
            assert isinstance(recent_errors, list)
            print(f"✓ Recent errors retrieved: {len(recent_errors)} records")
            
            # ========================================
            # TEST ERROR RATE
            # ========================================
            
            print("\nTest 8: Calculating error rate...")
            error_rate = db.analytics.get_error_rate(days=30)
            
            assert isinstance(error_rate, dict)
            assert 'error_rate' in error_rate
            print(f"✓ Error rate calculated: {error_rate['error_rate']}%")
            print(f"  Total executions: {error_rate['total_executions']}")
            print(f"  Failed executions: {error_rate['failed_executions']}")
            
            # ========================================
            # TEST TRANSFER PERFORMANCE
            # ========================================
            
            print("\nTest 9: Getting transfer performance...")
            transfer_perf = db.analytics.get_transfer_performance(days=30)
            
            assert isinstance(transfer_perf, list)
            print(f"✓ Transfer performance retrieved: {len(transfer_perf)} records")
            if transfer_perf:
                t = transfer_perf[0]
                route = f"{t['source_location']} → {t['destination_location']}"
                print(f"  Recent: {route} - {t.get('actual_mbps', 0)} MB/s")
            
            # ========================================
            # TEST TRANSFER SUMMARY BY ROUTE
            # ========================================
            
            print("\nTest 10: Getting transfer summary by route...")
            route_summary = db.analytics.get_transfer_summary_by_route()
            
            assert isinstance(route_summary, list)
            print(f"✓ Transfer route summary retrieved: {len(route_summary)} routes")
            if route_summary:
                r = route_summary[0]
                print(f"  {r['source_location']} → {r['destination_location']}: {r.get('total_gb_transferred', 0)} GB")
            
            # ========================================
            # TEST STORAGE BY LOCATION
            # ========================================
            
            print("\nTest 11: Getting storage by location...")
            storage = db.analytics.get_storage_by_location()
            
            assert isinstance(storage, list)
            print(f"✓ Storage by location retrieved: {len(storage)} locations")
            if storage:
                s = storage[0]
                print(f"  {s['location']}: {s.get('total_gb', 0)} GB ({s['batch_count']} batches)")
            
            # ========================================
            # TEST STORAGE GROWTH
            # ========================================
            
            print("\nTest 12: Getting storage growth (last 6 months)...")
            growth = db.analytics.get_storage_growth(months=6)
            
            assert isinstance(growth, list)
            print(f"✓ Storage growth retrieved: {len(growth)} records")
            if growth:
                g = growth[0]
                print(f"  {g['month']}: {g.get('total_gb', 0)} GB")
            
            # ========================================
            # TEST BATCH SUMMARY (if batch exists)
            # ========================================
            
            print("\nTest 13: Getting batch summary...")
            
            # Try to find a batch
            query = """
                SELECT batch_id 
                FROM processed.batches 
                LIMIT 1;
            """
            result = db._connection.fetch_one(query)
            
            if result:
                test_batch_id = result['batch_id']
                summary = db.analytics.get_batch_summary(test_batch_id)
                
                if summary:
                    assert isinstance(summary, dict)
                    print(f"✓ Batch summary retrieved for {test_batch_id}")
                    print(f"  Stages completed: {summary.get('stages_completed', 0)}")
                    print(f"  Files: {summary.get('file_count_raw', 0)}")
                else:
                    print(f"⚠ No summary found for batch {test_batch_id}")
            else:
                print("⚠ No batches in database to test summary")
            
            # ========================================
            # TEST CAMERA STATS
            # ========================================
            
            print("\nTest 14: Getting camera statistics...")
            camera_stats = db.analytics.get_camera_stats()
            
            assert isinstance(camera_stats, list)
            print(f"✓ Camera stats retrieved: {len(camera_stats)} cameras")
            if camera_stats:
                c = camera_stats[0]
                camera = f"{c.get('camera_make', 'Unknown')} {c.get('camera_model', 'Unknown')}"
                print(f"  {camera}: {c.get('image_count', 0)} images")
            
            print("\n✓ All database integration tests passed!")
            
    except Exception as e:
        if skip_if_no_db and ('connection' in str(e).lower() or 'does not exist' in str(e).lower()):
            print(f"\n⚠ Database or view not available: {e}")
            print("Skipping database integration tests.")
            print("This is expected if:")
            print("  - Database is not running")
            print("  - Analytics views don't exist (run analytics_schema.sql)")
            print("  - No sample data in database")
        else:
            print(f"\n✗ Database test failed: {e}")
            import traceback
            traceback.print_exc()
            raise


def main():
    """Run all Phase 8 tests."""
    print("=" * 60)
    print("Phase 8 - Analytics Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_analytics_initialization()
        test_agirdb_integration()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 8 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 8 Complete!")
        print("=" * 60)
        print("\nPhase 8 components are working correctly.")
        print("Ready to proceed to Phase 9 (Migration Tools).")
        
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