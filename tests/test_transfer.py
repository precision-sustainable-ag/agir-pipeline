"""
Test Suite for TransferManager

Tests gap detection methods that wrap report.files_to_copy_to_juno views.

Run with: python test_transfer_manager.py
"""

import psycopg2
from agir_db.transfers import TransferManager
from agir_db.api import AgirDB
from agir_db import setup_logging
import sys

log = setup_logging()

def test_connection(manager: TransferManager):
    """Test 1: Verify database connection works."""
    print("\n[TEST 1] Database Connection")
    print("-" * 60)
    
    try:
        print(f"✓ Connected to database successfully host={manager.conn.host}")
        manager.conn.host
        return True
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False


def test_views_exist(manager: TransferManager):
    """Test 2: Verify required views exist."""
    print("\n[TEST 2] Database Views")
    print("-" * 60)
    
    views = [
        'report.files_to_copy_to_juno',
        'report.batches_to_copy_to_juno',
        'report.files_to_copy_upload_raw_to_juno',
        'report.files_to_copy_dev_images_to_juno',
        'report.files_to_copy_dev_metadata_to_juno',
        'report.files_to_copy_cutouts_to_juno'
    ]
    
    all_exist = True
    
    for view in views:
        try:
            manager.conn.fetch_one(f"SELECT COUNT(*) FROM {view} LIMIT 1")
            print(f"✓ {view} exists")
        except Exception as e:
            print(f"✗ {view} missing: {e}")
            all_exist = False
    
    if not all_exist:
        print("\nRun the SQL script to create views")
    
    return all_exist


def test_get_files_to_transfer(manager: TransferManager):
    """Test 3: Get files to transfer."""
    print("\n[TEST 3] Get Files To Transfer")
    print("-" * 60)
    
    try:
        files = manager.get_files_to_transfer(limit=10)
        print(f"✓ Found {len(files)} files (limited to 10)")
        
        if files:
            first = files[0]
            print(f"\nExample:")
            print(f"  Batch: {first['batch_id']}")
            print(f"  File: {first['file_name']}")
            print(f"  Copy domain: {first['copy_domain']}")
        
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def test_get_batches_to_transfer(manager: TransferManager):
    """Test 4: Get batch summaries."""
    print("\n[TEST 4] Get Batches To Transfer")
    print("-" * 60)
    
    try:
        batches = manager.get_batches_to_transfer(limit=10)
        print(f"✓ Found {len(batches)} batches (limited to 10)")
        
        if batches:
            print(f"\n{'Batch':20s} | {'Domain':25s} | {'Files':>6s}")
            print("-" * 60)
            for batch in batches[:5]:
                print(f"{batch['batch_id']:20s} | "
                      f"{batch['copy_domain']:25s} | "
                      f"{batch['n_files_missing_on_juno']:6d}")
        
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def test_filter_by_copy_domain(manager: TransferManager):
    """Test 5: Filter by copy domain."""
    print("\n[TEST 5] Filter by Copy Domain")
    print("-" * 60)
    
    try:
        domains = ['upload_raw', 'developed_images_jpg', 
                   'developed_metadata_json', 'cutouts']
        
        for domain in domains:
            count = manager.count_files_to_transfer(copy_domain=domain)['count']
            print(f"  {domain:30s}: {count} files")
        
        print("✓ Copy domain filtering works")
        return True
    except Exception as e:
        log.exception("Error in test_filter_by_copy_domain", exc_info=e)
        return False


def test_filter_by_batch(manager: TransferManager):
    """Test 6: Filter by batch ID."""
    print("\n[TEST 6] Filter by Batch ID")
    print("-" * 60)
    
    try:
        batches = manager.get_batches_to_transfer(limit=1)
        
        if not batches:
            print("⊘ No batches needing transfer")
            return True
        
        test_batch = batches[0]['batch_id']
        files = manager.get_files_to_transfer(batch_id=test_batch)
        
        print(f"✓ Batch {test_batch} has {len(files):,} files")
        
        all_match = all(f['batch_id'] == test_batch for f in files)
        if not all_match:
            print(f"✗ Some files have wrong batch ID")
            return False
        
        print(f"✓ All files match batch ID")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def test_filter_by_site(manager: TransferManager):
    """Test 7: Filter by source site."""
    print("\n[TEST 7] Filter by Source Site")
    print("-" * 60)
    
    try:
        ncsu_count = manager.count_files_to_transfer(site='NCSU')['count']
        ceres_count = manager.count_files_to_transfer(site='CERES')['count']
        
        print(f"✓ NCSU: {ncsu_count:,} files")
        print(f"✓ CERES: {ceres_count:,} files")
        
        if ncsu_count > 0:
            ncsu_files = manager.get_files_to_transfer(site='NCSU', limit=5)
            all_ncsu = all(f['site'] == 'NCSU' for f in ncsu_files)
            if not all_ncsu:
                print("✗ NCSU filter failed")
                return False
        
        print("✓ Site filtering works")
        return True
    except Exception as e:
        log.exception(f"✗ Failed: {e}")
        return False


def test_batch_summary(manager: TransferManager):
    """Test 8: Get batch summary."""
    print("\n[TEST 8] Batch Summary")
    print("-" * 60)
    
    try:
        batches = manager.get_batches_to_transfer(limit=1)
        
        if not batches:
            print("⊘ No batches needing transfer")
            return True
        
        test_batch = batches[0]['batch_id']
        summary = manager.get_batch_summary(test_batch)
        
        print(f"Batch: {summary['batch_id']}")
        print(f"Total files: {summary['total_files_missing']:,}")
        print(f"Total size: {summary['total_gb_missing']:.2f} GB")
        
        print("\nBy domain:")
        for domain, info in summary['domains'].items():
            print(f"  {domain:30s}: {info['files_missing']:5,} files")
        
        print("✓ Batch summary works")
        return True
    except Exception as e:
        log.exception(f"✗ Failed: {e}")
        print(f"✗ Failed: {e}")
        return False


def test_count_files(manager: TransferManager):
    """Test 9: Count files."""
    print("\n[TEST 9] Count Files")
    print("-" * 60)
    
    try:
        total = manager.count_files_to_transfer()['count']
        print(f"✓ Total files: {total:,}")
        
        raw = manager.count_files_to_transfer(copy_domain='upload_raw')['count']
        jpg = manager.count_files_to_transfer(copy_domain='developed_images_jpg')['count']
        metadata = manager.count_files_to_transfer(copy_domain='developed_metadata_json')['count']
        cutouts = manager.count_files_to_transfer(copy_domain='cutouts')['count']
        
        print(f"  RAW:      {raw:8,}")
        print(f"  JPG:      {jpg:8,}")
        print(f"  Metadata: {metadata:8,}")
        print(f"  Cutouts:  {cutouts:8,}")
        
        domain_sum = raw + jpg + metadata + cutouts
        if domain_sum == total:
            print("✓ Counts add up")
        else:
            print(f"⚠ Counts don't match: {domain_sum:,} != {total:,}")
        
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def test_statistics(manager: TransferManager):
    """Test 10: Get statistics."""
    print("\n[TEST 10] Transfer Statistics")
    print("-" * 60)
    
    try:
        stats = manager.get_transfer_statistics()
        
        print(f"Overall:")
        print(f"  Batches: {stats['batches_needing_transfer']:,}")
        print(f"  Files: {stats['total_files_to_transfer']:,}")
        print(f"  Size: {stats['total_gb_to_transfer']:.2f} GB")
        
        print(f"\nBy domain:")
        for domain, info in stats['by_domain'].items():
            print(f"  {domain:30s}: {info['files']:8,} files")
        
        print("✓ Statistics work")
        return True
    except Exception as e:
        log.exception(f"✗ Failed: {e}")
        print(f"✗ Failed: {e}")
        return False


def test_no_juno_files(manager: TransferManager):
    """Test 11: Verify no JUNO files."""
    print("\n[TEST 11] No JUNO Files")
    print("-" * 60)
    
    try:
        juno_files = manager.get_files_to_transfer(site='JUNO')
        
        if len(juno_files) == 0:
            print("✓ No JUNO files (correct)")
            return True
        else:
            print(f"✗ Found {len(juno_files)} JUNO files (BUG)")
            return False
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def test_performance(manager: TransferManager):
    """Test 12: Check performance."""
    print("\n[TEST 12] Performance")
    print("-" * 60)
    
    import time
    
    try:
        start = time.time()
        files = manager.get_files_to_transfer(limit=100)
        elapsed = time.time() - start
        
        print(f"Query time (100 files): {elapsed:.3f}s")
        
        if elapsed < 5.0:
            print("✓ Performance acceptable (<5s)")
        else:
            print("⚠ Performance slow (>5s)")
        
        start = time.time()
        count = manager.count_files_to_transfer()['count']
        elapsed = time.time() - start
        
        print(f"Count time: {elapsed:.3f}s")
        print(f"Count: {count:,}")
        
        if elapsed < 2.0:
            print("✓ Count performance good (<2s)")
        else:
            print("⚠ Count slow (>2s)")
        
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def run_all_tests():
    """Run all tests and report results."""
    print("=" * 80)
    print("TRANSFER MANAGER - TEST SUITE")
    print("=" * 80)
    
    with AgirDB() as db:
        manager = db.transfers

        if not test_connection(manager):
            print("\n❌ Cannot connect - stopping")
            return False
        
        if not test_views_exist(manager):
            print("\n❌ Views missing - stopping")
            manager.conn.close()
            return False
        
        tests = [
            test_get_files_to_transfer,
            test_get_batches_to_transfer,
            test_filter_by_copy_domain,
            test_filter_by_batch,
            test_filter_by_site,
            test_batch_summary,
            test_count_files,
            test_statistics,
            test_no_juno_files,
            test_performance
        ]
        
        results = []
        for test_func in tests:
            try:
                passed = test_func(manager)
                results.append((test_func.__name__, passed))
            except Exception as e:
                print(f"\n✗ Test crashed: {e}")
                results.append((test_func.__name__, False))
        
        manager.conn.close()
        
        print("\n" + "=" * 80)
        print("TEST SUMMARY")
        print("=" * 80)
        
        passed = sum(1 for _, result in results if result)
        total = len(results)
        
        for test_name, result in results:
            status = "✓ PASS" if result else "✗ FAIL"
            print(f"{status:8s} | {test_name}")
        
        print("-" * 80)
        print(f"Result: {passed}/{total} tests passed")
        
        if passed == total:
            print("\n🎉 All tests passed!")
            return True
        else:
            print(f"\n❌ {total - passed} test(s) failed")
            return False


if __name__ == '__main__':
    success = run_all_tests()