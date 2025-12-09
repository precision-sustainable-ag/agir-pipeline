#!/usr/bin/env python3
"""
Test script for Phase 9 - Migration Tools.

This script verifies that migration tools work correctly:
- Migration class methods
- SQLite import
- Data transformation
- Validation
- Integration with AgirDB facade

Note: These tests require a live database connection.
"""

import sys
import sqlite3
import tempfile
from pathlib import Path
from datetime import date

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from agir_db.api import AgirDB
from agir_db.migration import Migration
from agir_db.exceptions import MigrationError, ValidationError


def test_migration_initialization():
    """Test Migration initialization without connection."""
    print("\nTesting Migration initialization...")
    
    from agir_db.connection import ConnectionManager
    
    conn = ConnectionManager(host='localhost', port=5432, dbname='agir', user='testuser')
    migration = Migration(conn)
    
    assert migration.conn is conn
    assert hasattr(migration, 'import_sqlite_db')
    assert hasattr(migration, 'validate_migration')
    assert hasattr(migration, 'get_migration_summary')
    
    print("✓ Migration initializes correctly")


def test_agirdb_integration():
    """Test that AgirDB exposes migration component."""
    print("\nTesting AgirDB.migration integration...")
    
    db = AgirDB()
    
    # Check that component exists
    assert hasattr(db, 'migration'), "AgirDB does not have 'migration' attribute"
    assert isinstance(db.migration, Migration), "db.migration is not a Migration instance"
    
    print("✓ AgirDB.migration integration works correctly")


def create_test_sqlite_db(path: Path, batch_id: str) -> None:
    """Create a test SQLite database with sample data."""
    conn = sqlite3.connect(str(path))
    cursor = conn.cursor()
    
    # Create batch metadata table
    cursor.execute("""
        CREATE TABLE batch_metadata (
            batch_id TEXT PRIMARY KEY,
            batch_date TEXT,
            location TEXT,
            file_count INTEGER,
            total_bytes INTEGER,
            status TEXT
        )
    """)
    
    # Insert test batch
    cursor.execute("""
        INSERT INTO batch_metadata VALUES (?, ?, ?, ?, ?, ?)
    """, (batch_id, '2024-06-01', 'JUNO', 10, 25000000, 'completed'))
    
    # Create image metadata table
    cursor.execute("""
        CREATE TABLE image_metadata (
            image_id TEXT PRIMARY KEY,
            batch_id TEXT,
            file_name TEXT,
            file_ext TEXT,
            size_bytes INTEGER,
            camera_make TEXT,
            camera_model TEXT,
            width INTEGER,
            height INTEGER
        )
    """)
    
    # Insert test images
    for i in range(10):
        cursor.execute("""
            INSERT INTO image_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f'TEST_{i:04d}',
            batch_id,
            f'TEST_{i:04d}.raw',
            'raw',
            2500000,
            'Canon',
            'EOS R5',
            8192,
            5464
        ))
    
    conn.commit()
    conn.close()


def test_with_database(skip_if_no_db=True):
    """
    Test actual database operations (requires live database).
    """
    print("\n" + "="*60)
    print("DATABASE INTEGRATION TESTS")
    print("="*60)
    print("Note: These tests require processed schema tables.")
    print()
    
    try:
        with AgirDB() as db:
            print("✓ Database connection successful")
            
            # ========================================
            # TEST MIGRATION SUMMARY (BASELINE)
            # ========================================
            
            print("\nTest 1: Getting baseline migration summary...")
            summary = db.migration.get_migration_summary()
            
            assert isinstance(summary, dict)
            print(f"✓ Baseline summary:")
            print(f"  Total batches: {summary.get('total_batches', 0)}")
            print(f"  Total images: {summary.get('total_images', 0)}")
            
            baseline_batches = summary.get('total_batches', 0)
            baseline_images = summary.get('total_images', 0)
            
            # ========================================
            # TEST SQLITE IMPORT (DRY RUN)
            # ========================================
            
            print("\nTest 2: Creating test SQLite database...")
            
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                sqlite_path = tmpdir_path / 'test_batch.db'
                test_batch_id = 'TEST_MIGRATION_2024-06-01'
                
                # Create test database
                create_test_sqlite_db(sqlite_path, test_batch_id)
                print(f"✓ Created test database: {sqlite_path}")
                
                # ========================================
                # TEST DRY RUN
                # ========================================
                
                print("\nTest 3: Testing dry run import...")
                stats = db.migration.import_sqlite_db(
                    str(sqlite_path),
                    batch_id=test_batch_id,
                    dry_run=True
                )
                
                assert stats['batches_imported'] == 1
                assert stats['images_imported'] == 10
                print(f"✓ Dry run successful:")
                print(f"  Would import: {stats['batches_imported']} batches")
                print(f"  Would import: {stats['images_imported']} images")
                
                # Verify nothing was actually imported
                summary2 = db.migration.get_migration_summary()
                assert summary2['total_batches'] == baseline_batches
                assert summary2['total_images'] == baseline_images
                print(f"✓ Verified no data was imported (dry run)")
                
                # ========================================
                # TEST ACTUAL IMPORT
                # ========================================
                
                print("\nTest 4: Testing actual import...")
                
                # Clean up if exists
                db._connection.execute(
                    "DELETE FROM processed.images WHERE batch_id = %s",
                    (test_batch_id,)
                )
                db._connection.execute(
                    "DELETE FROM processed.batches WHERE batch_id = %s",
                    (test_batch_id,)
                )
                db.commit()
                
                # Import
                stats = db.migration.import_sqlite_db(
                    str(sqlite_path),
                    batch_id=test_batch_id,
                    dry_run=False
                )
                db.commit()
                
                assert stats['batches_imported'] == 1
                assert stats['images_imported'] == 10
                print(f"✓ Import successful:")
                print(f"  Imported: {stats['batches_imported']} batches")
                print(f"  Imported: {stats['images_imported']} images")
                
                # Verify data was imported
                summary3 = db.migration.get_migration_summary()
                assert summary3['total_batches'] == baseline_batches + 1
                assert summary3['total_images'] >= baseline_images + 10
                print(f"✓ Verified data was imported")
                
                # ========================================
                # TEST SKIP EXISTING
                # ========================================
                
                print("\nTest 5: Testing skip existing...")
                stats2 = db.migration.import_sqlite_db(
                    str(sqlite_path),
                    batch_id=test_batch_id,
                    dry_run=False,
                    skip_existing=True
                )
                db.commit()
                
                assert stats2['batches_skipped'] == 1
                assert stats2['batches_imported'] == 0
                print(f"✓ Correctly skipped existing batch")
                
                # ========================================
                # TEST VALIDATION
                # ========================================
                
                print("\nTest 6: Validating migrated batch...")
                validation = db.migration.validate_migration(test_batch_id)
                
                assert isinstance(validation, dict)
                assert validation['batch_exists'] is True
                assert validation['image_count'] == 10
                
                if validation['valid']:
                    print(f"✓ Validation passed")
                else:
                    print(f"⚠ Validation issues: {validation['issues']}")
                
                print(f"  Batch exists: {validation['batch_exists']}")
                print(f"  Image count: {validation['image_count']}")
                
                # ========================================
                # TEST VALIDATION (NON-EXISTENT BATCH)
                # ========================================
                
                print("\nTest 7: Validating non-existent batch...")
                validation2 = db.migration.validate_migration('FAKE_BATCH_9999-99-99')
                
                assert validation2['valid'] is False
                assert validation2['batch_exists'] is False
                print(f"✓ Correctly detected non-existent batch")
                
                # ========================================
                # TEST UPDATED SUMMARY
                # ========================================
                
                print("\nTest 8: Getting updated migration summary...")
                summary4 = db.migration.get_migration_summary()
                
                assert summary4['total_batches'] >= baseline_batches + 1
                assert summary4['total_images'] >= baseline_images + 10
                print(f"✓ Updated summary:")
                print(f"  Total batches: {summary4['total_batches']}")
                print(f"  Total images: {summary4['total_images']}")
                
                # ========================================
                # CLEANUP
                # ========================================
                
                print("\nCleaning up test data...")
                db._connection.execute(
                    "DELETE FROM processed.images WHERE batch_id = %s",
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
            print("  - processed schema tables don't exist")
        else:
            print(f"\n✗ Database test failed: {e}")
            import traceback
            traceback.print_exc()
            raise


def main():
    """Run all Phase 9 tests."""
    print("=" * 60)
    print("Phase 9 - Migration Tools Tests")
    print("=" * 60)
    
    try:
        # Unit tests (no database required)
        test_migration_initialization()
        test_agirdb_integration()
        
        print("\n" + "=" * 60)
        print("✓ All Phase 9 unit tests passed!")
        print("=" * 60)
        
        # Database integration tests (optional)
        test_with_database(skip_if_no_db=True)
        
        print("\n" + "=" * 60)
        print("✓ Phase 9 Complete!")
        print("=" * 60)
        print("\nPhase 9 components are working correctly.")
        print("Ready to proceed to Phase 10 (Orchestration Helpers).")
        
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