"""
TransferManager

Query-driven detection of files and batches that must be transferred to JUNO.
Phase 1 focuses on identifying gaps; transfer execution and tracking will follow.

Relies on database views:
  - report.files_to_copy_to_juno
  - report.batches_to_copy_to_juno
"""

import subprocess
from typing import Dict, List, Optional, Literal, Tuple

from .connection import ConnectionManager

CopyDomain = Literal[
    "upload_raw",
    "developed_images_jpg",
    "developed_metadata_json",
    "cutouts",
]

PickPolicy = Literal["none", "most_files", "most_bytes"]


class TransferManager:
    """
    Manage detection and execution of data transfers to JUNO.

    Phase 1 (implemented):
      - Identify missing files
      - Summarize gaps at batch level
      - Aggregate statistics

    Phase 2 (planned):
      - Submit Globus transfers
      - Track task status
      - Retry / audit failed transfers
    """

    def __init__(self, connection: ConnectionManager):
        """Initialize with an active database connection."""
        self.conn = connection

    # ------------------------------------------------------------------
    # File-level gap detection
    # ------------------------------------------------------------------

    def get_files_to_transfer(
        self,
        copy_domain: Optional[CopyDomain] = None,
        batch_id: Optional[str] = None,
        site: Optional[str] = None,
        data_state: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """Return individual files missing on JUNO."""
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

        params: List = []

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

    # ------------------------------------------------------------------
    # Batch-level gap detection
    # ------------------------------------------------------------------

    def get_batches_to_transfer(
        self,
        copy_domain: Optional[CopyDomain] = None,
        data_state: Optional[str] = None,
        min_files: int = 1,
        limit: Optional[int] = None,
        pick_policy: PickPolicy = "none",
    ) -> List[Dict]:
        """
        Return batch candidates that have missing content on JUNO.

        Multiple rows per batch may exist if data lives in multiple roots.
        """
        query = """
            SELECT
                copy_domain,
                data_state,
                batch_id,
                site,
                endpoint,
                storage_root,
                parent_dir,
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

        if pick_policy == "most_files":
            query += """
                ORDER BY
                    n_files_missing_on_juno DESC,
                    n_bytes_missing_on_juno DESC,
                    batch_id DESC
            """
        elif pick_policy == "most_bytes":
            query += """
                ORDER BY
                    n_bytes_missing_on_juno DESC,
                    n_files_missing_on_juno DESC,
                    batch_id DESC
            """
        else:
            query += " ORDER BY batch_id DESC, copy_domain, site, storage_root"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        rows = self.conn.fetch_all(query, params)

        if pick_policy == "none":
            return rows

        # Collapse to one preferred location per batch
        best: Dict[Tuple, Dict] = {}
        for row in rows:
            key = (row["copy_domain"], row["data_state"], row["batch_id"])
            cur = best.get(key)

            if cur is None:
                best[key] = row
                continue

            if pick_policy == "most_files":
                score = (
                    row["n_files_missing_on_juno"],
                    row["n_bytes_missing_on_juno"],
                )
                cur_score = (
                    cur["n_files_missing_on_juno"],
                    cur["n_bytes_missing_on_juno"],
                )
            else:
                score = (
                    row["n_bytes_missing_on_juno"],
                    row["n_files_missing_on_juno"],
                )
                cur_score = (
                    cur["n_bytes_missing_on_juno"],
                    cur["n_files_missing_on_juno"],
                )

            if score > cur_score:
                best[key] = row

        return list(best.values())

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------

    def count_files_to_transfer(
        self,
        copy_domain: Optional[CopyDomain] = None,
        batch_id: Optional[str] = None,
        site: Optional[str] = None,
    ) -> int:
        """Fast count of files missing on JUNO."""
        query = "SELECT COUNT(*) FROM report.files_to_copy_to_juno WHERE 1=1"
        params: Tuple = ()

        if copy_domain:
            query += " AND copy_domain = %s"
            params += (copy_domain,)
        if batch_id:
            query += " AND batch_id = %s"
            params += (batch_id,)
        if site:
            query += " AND site = %s"
            params += (site,)

        return self.conn.fetch_one(query, params)["count"]

    # ------------------------------------------------------------------
    # Transfer helpers (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def build_globus_cmd(
        src_endpoint: str,
        dst_endpoint: str,
        src_path: str,
        dst_path: str,
        recursive: bool = True,
        label: Optional[str] = None
    ) -> List[str]:
        """Construct a Globus CLI transfer command."""
        cmd = [
            "globus",
            "transfer",
            f"{src_endpoint}:{src_path}",
            f"{dst_endpoint}:{dst_path}",
        ]

        if recursive:
            cmd.append("--recursive")

        if label:
            cmd += ["--label", label]
        
        return [c for c in cmd if c]

    @staticmethod
    def execute_transfer(
        cmd: List[str],
        dry_run: bool,
    ) -> Tuple[str, Optional[str]]:
        """Execute a Globus transfer command."""
        if dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            return "dry_run", None

        try:
            subprocess.run(cmd, check=True)
            return "submitted", None
        except subprocess.CalledProcessError as exc:
            return "failed", str(exc)
