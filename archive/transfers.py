"""
TransferManager - Manage file transfers to JUNO

Handles:
  1. Gap detection (find files needing transfer)
  2. Transfer execution (TBD - Requirement 2)
  3. Status tracking (TBD - Requirement 7)

Based on SQL views:
  - report.files_to_copy_to_juno
  - report.batches_to_copy_to_juno
"""

import psycopg2
from .connection import ConnectionManager
from typing import List, Dict, Optional, Literal


CopyDomain = Literal['upload_raw', 'developed_images_jpg', 'developed_metadata_json', 'cutouts']
PickPolicy = Literal["none", "most_files", "most_bytes"]

class TransferManager:
    """
    Manage file transfers to JUNO archive.
    
    Phase 1 (current): Gap detection
      - Identify files needing transfer
      - Batch-level summaries
      - Statistics
    
    Phase 2 (future): Transfer execution
      - Submit Globus transfers
      - Track transfer status
      - Handle retries
    
    Usage:
        manager = TransferManager(connection)
        
        # Gap detection (Phase 1)
        files = manager.get_files_to_transfer(copy_domain='upload_raw')
        batches = manager.get_batches_to_transfer()
        summary = manager.get_batch_summary('MD_2025-04-10')
        
        # Transfer execution (Phase 2 - TBD)
        # task_id = manager.submit_transfer(batch_id, copy_domain, ...)
        # status = manager.get_transfer_status(task_id)
        
        manager.conn.close()
    """
    
    def __init__(self, connection: ConnectionManager):
        """
        Initialize TransferManager.
        
        Args:
            connection: ConnectionManager object
        """
        self.conn = connection
    
    # ========================================================================
    # GAP DETECTION METHODS (Phase 1)
    # ========================================================================
    
    def get_files_to_transfer(
        self,
        copy_domain: Optional[CopyDomain] = None,
        batch_id: Optional[str] = None,
        site: Optional[str] = None,
        data_state: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[Dict]:
        """
        Get list of files that need to be transferred to JUNO.
        
        Args:
            copy_domain: Type of files ('upload_raw', 'developed_images_jpg', 
                        'developed_metadata_json', 'cutouts')
            batch_id: Filter to specific batch
            site: Filter to source site ('NCSU' or 'CERES')
            data_state: Filter to data state
            limit: Maximum results
            
        Returns:
            List of dicts with file details including 'copy_domain'
            
        Example:
            # Get RAW files for specific batch
            files = manager.get_files_to_transfer(
                copy_domain='upload_raw',
                batch_id='MD_2024-06-01'
            )
            
            # Each file has: full_path, storage_root, endpoint, size_bytes, etc.
            for f in files:
                print(f"{f['rel_path']} - {f['size_bytes']} bytes")
        """
        query = """
            SELECT 
                file_id,
                endpoint,
                site,
                storage_domain,
                namespace,
                storage_root,
                rel_path,
                full_path,
                parent_dir,
                file_name,
                entry_type,
                file_ext,
                size_bytes,
                permissions,
                checksum,
                batch_id,
                batch_state,
                batch_date,
                data_state,
                mtime_iso,
                fname_ts_epoch,
                fname_ts_iso,
                created_at_ts_iso,
                copy_domain
            FROM report.files_to_copy_to_juno
            WHERE 1=1
        """
        
        params = []
        
        if copy_domain:
            query += " AND copy_domain = %s"
            params.append(copy_domain)
        
        if batch_id:
            query += " AND batch_id = %s"
            params.append(batch_id)
        
        if site:
            query += " AND site = %s"
            params.append(site)
        
        if data_state:
            query += " AND data_state = %s"
            params.append(data_state)
        
        query += " ORDER BY batch_date DESC, batch_id, copy_domain, file_name"
        
        if limit:
            query += " LIMIT %s"
            params.append(limit)
        
        return self.conn.fetch_all(query, params)
    

    

    def get_batches_to_transfer(
        self,
        copy_domain: Optional[CopyDomain] = None,
        data_state: Optional[str] = None,
        min_files: int = 1,
        limit: Optional[int] = None,
        pick_policy: PickPolicy = "none",     # <-- new
    ) -> List[Dict]:
        """
        Returns batch *candidates* from report.batches_to_copy_to_juno.
        Each batch may appear multiple times if present in multiple locations/roots.
        """
        query = """
            SELECT
                copy_domain,
                data_state,
                batch_id,
                site,
                endpoint,
                storage_root,
                n_files_missing_on_juno,
                n_bytes_missing_on_juno
            FROM report.batches_to_copy_to_juno
            WHERE n_files_missing_on_juno >= %s
        """
        params = [min_files]

        if copy_domain:
            query += " AND copy_domain = %s"
            params.append(copy_domain)

        if data_state:
            query += " AND data_state = %s"
            params.append(data_state)

        # If you want to choose later in Python, ordering here can still help.
        if pick_policy == "most_files":
            query += """
                ORDER BY
                    n_files_missing_on_juno DESC,
                    n_bytes_missing_on_juno DESC,
                    batch_id DESC,
                    copy_domain,
                    storage_root
            """
        elif pick_policy == "most_bytes":
            query += """
                ORDER BY
                    n_bytes_missing_on_juno DESC,
                    n_files_missing_on_juno DESC,
                    batch_id DESC,
                    copy_domain,
                    storage_root
            """
        else:
            query += " ORDER BY batch_id DESC, copy_domain, site, storage_root"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        rows = self.conn.fetch_all(query, params)

        # If you set pick_policy to "none", return all candidates (possibly multiple per batch)
        if pick_policy == "none":
            return rows

        # Otherwise: collapse to one chosen presence per batch in Python
        best: Dict[tuple, Dict] = {}
        for r in rows:
            key = (r["copy_domain"], r["data_state"], r["batch_id"])
            cur = best.get(key)
            if cur is None:
                best[key] = r
                continue

            if pick_policy == "most_files":
                better = (
                    (r["n_files_missing_on_juno"], r["n_bytes_missing_on_juno"], r["storage_root"])
                    >
                    (cur["n_files_missing_on_juno"], cur["n_bytes_missing_on_juno"], cur["storage_root"])
                )
            else:  # most_bytes
                better = (
                    (r["n_bytes_missing_on_juno"], r["n_files_missing_on_juno"], r["storage_root"])
                    >
                    (cur["n_bytes_missing_on_juno"], cur["n_files_missing_on_juno"], cur["storage_root"])
                )

            if better:
                best[key] = r

        return list(best.values())

    
    def get_batch_summary(self, batch_id: str) -> Dict:
        """
        Get detailed transfer summary for specific batch.
        Shows what needs to be transferred across all copy domains.
        
        Args:
            batch_id: Batch to check
            
        Returns:
            Dict with details by copy_domain:
            {
                'batch_id': 'MD_2024-06-01',
                'domains': {
                    'upload_raw': {
                        'files_missing': 1234,
                        'bytes_missing': 32000000000,
                        'gb_missing': 29.8,
                        'present_in_roots': ['/path/to/root']
                    },
                    'developed_images_jpg': {...},
                    'developed_metadata_json': {...},
                    'cutouts': {...}
                },
                'has_gaps': True,
                'total_files_missing': 2090,
                'total_bytes_missing': 40000000000,
                'total_gb_missing': 37.25
            }
            
        Example:
            summary = manager.get_batch_summary('MD_2024-06-01')
            
            if summary['has_gaps']:
                print(f"Batch {summary['batch_id']} incomplete:")
                for domain, info in summary['domains'].items():
                    print(f"  {domain}: {info['files_missing']} files")
            else:
                print(f"Batch {summary['batch_id']} complete on JUNO")
        """
        query = """
            SELECT 
                copy_domain,
                n_files_missing_on_juno,
                n_bytes_missing_on_juno,
                present_in_roots
            FROM report.batches_to_copy_to_juno
            WHERE batch_id = %s
            ORDER BY copy_domain
        """
        
        rows = self.conn.fetch_all(query, [batch_id])
        
        if not rows:
            return {
                'batch_id': batch_id,
                'domains': {},
                'has_gaps': False,
                'total_files_missing': 0,
                'total_bytes_missing': 0,
                'total_gb_missing': 0.0
            }
        
        # Build summary
        domains = {}
        total_files = 0
        total_bytes = 0
        
        for row in rows:
            bytes_val = row['n_bytes_missing_on_juno'] or 0
            gb_val = round(float(bytes_val) / 1024.0 / 1024.0 / 1024.0, 2)
            
            domains[row['copy_domain']] = {
                'files_missing': row['n_files_missing_on_juno'],
                'bytes_missing': bytes_val,
                'gb_missing': gb_val,
                'present_in_roots': row['present_in_roots']
            }
            
            total_files += row['n_files_missing_on_juno']
            total_bytes += bytes_val
        
        return {
            'batch_id': batch_id,
            'domains': domains,
            'has_gaps': True,
            'total_files_missing': total_files,
            'total_bytes_missing': total_bytes,
            'total_gb_missing': round(float(total_bytes) / 1024.0**3, 2)
        }
    
    def count_files_to_transfer(
        self,
        copy_domain: Optional[CopyDomain] = None,
        batch_id: Optional[str] = None,
        site: Optional[str] = None
    ) -> int:
        """
        Get count of files needing transfer (fast query).
        
        Args:
            copy_domain: Type of files
            batch_id: Filter to batch
            site: Filter to site
            
        Returns:
            Count of files needing transfer
            
        Example:
            count = manager.count_files_to_transfer(copy_domain='upload_raw')
            print(f"{count:,} RAW files need transfer to JUNO")
        """
        query = "SELECT COUNT(*) FROM report.files_to_copy_to_juno WHERE 1=1"
        params = tuple()
        
        if copy_domain:
            query += " AND copy_domain = %s"
            params += (copy_domain,)
        
        if batch_id:
            query += " AND batch_id = %s"
            params += (batch_id,)
        
        if site:
            query += " AND site = %s"
            params += (site,)
        return self.conn.fetch_one(query, params)
    
    def get_transfer_statistics(self) -> Dict:
        """
        Get overall statistics about files needing transfer.
        
        Returns:
            Dict with summary stats:
            {
                'batches_needing_transfer': 45,
                'total_files_to_transfer': 123456,
                'total_bytes_to_transfer': 2500000000000,
                'total_gb_to_transfer': 2328.3,
                'by_domain': {
                    'upload_raw': {
                        'batches': 30,
                        'files': 98765,
                        'bytes': 2100000000000,
                        'gb': 1956.3
                    },
                    ...
                },
                'by_site': {
                    'NCSU': {'files': 100000, 'bytes': ..., 'gb': ...},
                    'CERES': {'files': 23456, 'bytes': ..., 'gb': ...}
                }
            }
            
        Example:
            stats = manager.get_transfer_statistics()
            
            print(f"Total: {stats['total_files_to_transfer']:,} files, "
                  f"{stats['total_gb_to_transfer']:.1f} GB")
            
            for domain, info in stats['by_domain'].items():
                print(f"  {domain}: {info['files']:,} files ({info['gb']:.1f} GB)")
        """
        # Stats by copy_domain
        query_domain = """
            SELECT 
                copy_domain,
                COUNT(DISTINCT batch_id) as batches,
                COUNT(*) as files,
                SUM(size_bytes) as bytes
            FROM report.files_to_copy_to_juno
            GROUP BY copy_domain
            ORDER BY copy_domain
        """
        
        domain_rows = self.conn.fetch_all(query_domain)
        
        by_domain = {}
        total_files = 0
        total_bytes = 0
        
        for row in domain_rows:
            bytes_val = row['bytes'] or 0
            by_domain[row['copy_domain']] = {
                'batches': row['batches'],
                'files': row['files'],
                'bytes': bytes_val,
                'gb': round(float(bytes_val) / 1024.0**3, 2)
            }
            total_files += row['files']
            total_bytes += bytes_val
        
        # Stats by site
        query_site = """
            SELECT 
                site,
                COUNT(*) as files,
                SUM(size_bytes) as bytes
            FROM report.files_to_copy_to_juno
            GROUP BY site
            ORDER BY site
        """
        
        site_rows = self.conn.fetch_all(query_site)
        
        by_site = {}
        for row in site_rows:
            bytes_val = row['bytes'] or 0
            by_site[row['site']] = {
                'files': row['files'],
                'bytes': bytes_val,
                'gb': round(float(bytes_val) / 1024.0**3, 2)
            }
        
        # Get unique batch count
        query_batches = """
            SELECT COUNT(DISTINCT batch_id) 
            FROM report.files_to_copy_to_juno
        """

        batches_needing_transfer = self.conn.fetch_one(query_batches)   
        
        return {
            'batches_needing_transfer': batches_needing_transfer['count'],
            'total_files_to_transfer': total_files,
            'total_bytes_to_transfer': total_bytes,
            'total_gb_to_transfer': round(float(total_bytes) / 1024.0**3, 2),
            'by_domain': by_domain,
            'by_site': by_site
        }
    
    # ========================================================================
    # TRANSFER EXECUTION METHODS (Phase 2 - TBD)
    # ========================================================================
    
    # def submit_transfer(self, batch_id, copy_domain, source_site, ...):
    #     """Submit Globus transfer for batch."""
    #     pass
    
    # def get_transfer_status(self, task_id):
    #     """Get status of Globus transfer task."""
    #     pass
    
    # def cancel_transfer(self, task_id):
    #     """Cancel in-progress transfer."""
    #     pass
    
    # def retry_failed_transfer(self, task_id):
    #     """Retry failed transfer with exponential backoff."""
    #     pass


def get_transfer_manager(dbname='agirdb', user=None, host=None, port=None):
    """
    Create TransferManager with database connection.
    Uses ~/.pgpass for password.
    
    Example:
        manager = get_transfer_manager()
        files = manager.get_files_to_transfer(copy_domain='upload_raw')
        manager.conn.close()
    """
    conn = psycopg2.connect(dbname=dbname, user=user, host=host, port=port)
    return TransferManager(conn)