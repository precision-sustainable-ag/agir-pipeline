#!/usr/bin/env python3
"""
Multiprocessing Globus file indexer for PostgreSQL.

Changes from SQLite version:
- Uses PostgreSQL (psycopg2) instead of SQLite
- Adds site parameter (JUNO/NCSU/CERES)
- Separates file_name from rel_path
- Stores file_ext without dot
- Uses DATE type for batch_date
- Uses TIMESTAMPTZ for timestamps
- Inserts into source.globus_file_index schema
"""
import argparse
import json
import logging
import re
import subprocess
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ============================================================
#                     LOGGING SETUP
# ============================================================

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("globus_index")
    logger.setLevel(logging.INFO)

    if logger.handlers:  # already configured
        return logger

    fmt = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    fh = RotatingFileHandler(
        log_file,
        maxBytes=50_000_000,  # 50 MB
        backupCount=5,
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)

    return logger


# ============================================================
#                      REGEX HELPERS
# ============================================================

# Batch format: MD_2025-01-01
BATCH_PATTERN = re.compile(r"^([A-Z]{2})_(\d{4}-\d{2}-\d{2})$")

def extract_batch_id(path: str) -> Optional[str]:
    """
    Extract something like MD_2025-01-01 from a path.

    We look for the pattern anywhere in the path and return
    the first match.
    """
    parts = path.split("/")
    for p in parts:
        if BATCH_PATTERN.match(p):
            return p
    return None


def split_batch_id(batch_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    From 'MD_2025-01-01' get:
      batch_state = 'MD'
      batch_date  = '2025-01-01'  (date string)
    """
    m = BATCH_PATTERN.match(batch_id)
    if not m:
        return None, None

    state_code = m.group(1)      # 'MD'
    date_str = m.group(2)        # '2025-01-01'
    return state_code, date_str


def parse_last_modified_to_epoch(last_modified: Optional[str]) -> Optional[int]:
    """Parse Globus 'last_modified' string into epoch seconds."""
    if not last_modified:
        return None
    s = last_modified.strip()
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def extract_epoch_from_filename(filename: str) -> Tuple[Optional[int], Optional[datetime]]:
    """
    Extract a Unix epoch timestamp from a filename.

    Returns:
        (epoch_seconds, epoch_as_datetime_utc) or (None, None)

    Examples:
        "MD_1764960482.jpg"                -> 1764960482
        "MD_1764960482_234.jpg"            -> 1764960482
        "MD_Row_2_2_1764960482_32.jpg"     -> 1764960482
        "no_epoch_here.json"               -> None
    """

    # Strip ALL extensions — keep only the part before the first dot
    base = filename.split(".", 1)[0]

    # Look for sequences of 10–13 digits (seconds or ms)
    matches = re.findall(r"(?<!\d)(\d{10,13})(?!\d)", base)
    if not matches:
        return None, None

    # Convert first match to int
    epoch = int(matches[0])

    # If it's a millisecond epoch (13 digits), convert to seconds
    if epoch > 1_000_000_000_000:  
        epoch = epoch // 1000

    dt_epoch = datetime.fromtimestamp(epoch, tz=timezone.utc)

    return epoch, dt_epoch

def epoch_to_timestamptz(epoch: Optional[int]) -> Optional[datetime]:
    """Convert epoch seconds to datetime with UTC timezone."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def infer_file_ext(rel_path: str, entry_type: str) -> Optional[str]:
    """
    Derive file extension from path (without the dot).
    Returns None for directories.
    """
    if entry_type == "dir":
        return None

    ext = Path(rel_path).suffix
    if ext:
        return ext.lstrip('.')  # Remove leading dot
    return None

# ============================================================
#                     DATABASE HELPERS
# ============================================================

def apply_schema(conn: psycopg2.extensions.connection, schema_file: str, logger: logging.Logger) -> None:
    
    if not Path(schema_file).is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_file}")
    logger.info(f"Applying schema file: {schema_file}")
    
    with open(schema_file, "r") as f:
        sql = f.read()

    with conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()

    logger.info("Schema applied successfully.")


def init_db(conn_params: Dict, schema_path: str, logger: logging.Logger) -> psycopg2.extensions.connection:
    """Initialize PostgreSQL connection and create table/indexes if needed. apply schema file."""
    logger.info(f"Connecting to PostgreSQL database: {conn_params.get('dbname', 'agir')}")
    conn = psycopg2.connect(**conn_params)
    logger.info("Connected to PostgreSQL database")
    conn.autocommit = False

    # Load schema from file
    apply_schema(conn, schema_path, logger)

    return conn

def get_parent_folder(rel_path: str, entry_type: str) -> Optional[str]:
    """
    Return the immediate parent folder name for a file, based on rel_path.

    Examples:
        rel_path = "MD_2025-01-01/raws/file.raw"  -> "raws"
        rel_path = "file.raw"                     -> None

    For directories (entry_type == "dir"), this returns None.
    """
    if entry_type == "dir" or not rel_path:
        return None

    # Drop any trailing slash just in case
    path = rel_path.rstrip("/")

    # If there is no slash, the file lives directly under storage_root
    if "/" not in path:
        return None

    parent_dir = path.rsplit("/", 1)[0]
    if not parent_dir:
        return None

    return parent_dir.split("/")[-1]

def insert_rows(conn: psycopg2.extensions.connection, rows: List[Tuple], logger: logging.Logger) -> None:
    """Insert rows into PostgreSQL using batch insert."""
    if not rows:
        return
    
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            INSERT INTO source.globus_file_index (
                endpoint, site, storage_domain, namespace, storage_root,
                rel_path, full_path, parent_dir, file_name,
                entry_type, file_ext,
                size_bytes, permissions, checksum,
                batch_id, batch_state, batch_date,
                data_state,
                mtime_iso,
                fname_ts_epoch, fname_ts_iso
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (endpoint, data_state, storage_root, rel_path) DO NOTHING
            """,
            rows,
        )
        conn.commit()
        logger.info("Inserted %d rows into source.globus_file_index", len(rows))

def clear_site_index(
    conn: psycopg2.extensions.connection,
    endpoint: str,
    site: str,
    data_state: str,
    storage_root: str,
    logger: logging.Logger
) -> int:
    """
    Clear existing index entries for a specific site before re-indexing.
    Returns number of rows deleted.
    """
    delete_query = """
        DELETE FROM source.globus_file_index
        WHERE endpoint = %s
        AND site = %s
        AND data_state = %s
        AND storage_root = %s;
    """
    
    with conn.cursor() as cur:
        cur.execute(delete_query, (endpoint, site, data_state, storage_root))
        deleted_count = cur.rowcount
        conn.commit()
        logger.info(f"Cleared {deleted_count} existing records for {site}/{data_state}")
        return deleted_count

# ============================================================
#                     GLOBUS WORKER
# ============================================================

def ls_worker(args) -> Tuple[str, List[Dict]]:
    """
    Worker function run in subprocess.

    Returns (path, entries_list) or raises RuntimeError on failure.
    """
    endpoint, path = args
    target = f"{endpoint}:{path}"
    proc = subprocess.run(
        ["globus", "ls", "--format", "json", target],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"globus ls failed for {target}: {proc.stderr[:300]}")
    obj = json.loads(proc.stdout)
    data = obj.get("DATA") or []
    return path, data


# ============================================================
#                   MULTIPROCESS CRAWL
# ============================================================

def crawl_tree_mp(
    conn: psycopg2.extensions.connection,
    endpoint: str,
    site: str,
    storage_domain: str,
    namespace: str,
    storage_root: str,
    data_state: str,
    batch_size: int,
    max_workers: int,
    logger: logging.Logger,
) -> None:
    """
    Multiprocessing recursive crawl:
    - Queue of directories to visit
    - ProcessPoolExecutor to run globus-ls in parallel
    - Main process parses results and writes to PostgreSQL
    """
    data_dir = Path(storage_root) / data_state
    dir_queue = deque([str(data_dir)])
    futures = {}
    rows: List[Tuple] = []
    total_seen = 0
    max_inflight = max_workers * 4

    logger.info(f"Starting multiprocessing crawl at root: {data_dir}")
    logger.info(f"max_workers={max_workers}, max_inflight={max_inflight}")

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        while dir_queue or futures:
            # Submit new directories while we have capacity
            while dir_queue and len(futures) < max_inflight:
                current = dir_queue.popleft()
                future = executor.submit(ls_worker, (endpoint, current))
                futures[future] = current
                logger.info(f"Submitted ls for directory: {current}")

            if not futures:
                continue

            done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)

            for fut in done:
                current = futures.pop(fut)
                try:
                    path, entries = fut.result()
                except Exception as e:
                    logger.warning(f"Skipping directory due to error: {current}: {e}")
                    continue

                logger.info(f"Processing directory: {path} ({len(entries)} entries)")

                for item in entries:
                    name = item.get("name", "")
                    entry_type = item.get("type", "file")  # 'file' or 'dir'
                    size_bytes = item.get("size")          # bytes from Globus
                    last_modified = item.get("last_modified")
                    permissions = item.get("permissions")

                    full_path = f"{path.rstrip('/')}/{name}"
                    if entry_type == "dir":
                        full_path += "/"

                    rel_path = full_path[len(storage_root):].lstrip("/")
                    batch_id = extract_batch_id(full_path)

                    if batch_id:
                        batch_state, batch_date_str = split_batch_id(batch_id)
                        # Convert to DATE object for PostgreSQL
                        try:
                            batch_date = datetime.strptime(batch_date_str, "%Y-%m-%d").date() if batch_date_str else None
                        except ValueError:
                            batch_date = None
                    else:
                        batch_state, batch_date = None, None

                    mtime_epoch = parse_last_modified_to_epoch(last_modified)
                    fname_ts_epoch, fname_ts_dt = extract_epoch_from_filename(name)
                    mtime_iso = epoch_to_timestamptz(mtime_epoch)
                    file_ext = infer_file_ext(rel_path, entry_type)
                    parent_dir = get_parent_folder(rel_path, entry_type)

                    rows.append((
                        endpoint,
                        site,
                        storage_domain,
                        namespace,
                        storage_root,
                        rel_path,
                        full_path,
                        parent_dir,
                        name,              # file_name
                        entry_type,
                        file_ext,
                        size_bytes,
                        permissions,
                        None,              # checksum (not available yet)
                        batch_id,
                        batch_state,
                        batch_date,
                        data_state,
                        mtime_iso,
                        fname_ts_epoch,
                        fname_ts_dt       # fname_ts_iso (TIMESTAMPTZ)
                    ))
                    total_seen += 1

                    if entry_type == "dir":
                        dir_queue.append(full_path)

                    if len(rows) >= batch_size:
                        insert_rows(conn, rows, logger)
                        rows.clear()

        # Final flush
        if rows:
            insert_rows(conn, rows, logger)

    logger.info(f"Multiprocessing crawl complete. Total items processed: {total_seen}")


# ============================================================
#                          MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multiprocessing index of Globus endpoint paths into PostgreSQL."
    )
    parser.add_argument("--schema", required=True, help="Path to SQL schema file.")
    parser.add_argument("--host", default="localhost", help="PostgreSQL host.")
    parser.add_argument("--port", type=int, default=5432, help="PostgreSQL port.")
    parser.add_argument("--dbname", default="agir", help="PostgreSQL database name.")
    parser.add_argument("--user", required=True, help="PostgreSQL username.")
    parser.add_argument("--password", help="PostgreSQL password (optional, use .pgpass if not provided).")
    parser.add_argument("--endpoint", required=True, help="Globus endpoint ID.")
    parser.add_argument("--site", required=True, choices=["JUNO", "NCSU", "CERES"], help="Physical site of the endpoint.")
    parser.add_argument("--storage-domain", required=True, choices=["screberg", "dash_agir", "national_plant_image_repository"], help="Logical storage domain tag.")
    parser.add_argument("--namespace", required=True, help="Logical namespace tag.")
    parser.add_argument("--storage-root", required=True, help="Logical storage root tag.")
    parser.add_argument("--state",required=True,choices=["semifield-upload", "semifield-developed-images", "semifield-cutouts"], help="Logical data-state label for this tree.")
    parser.add_argument("--batch-size", type=int, default=2000, help="Insert batch size.")
    parser.add_argument("--max-workers", type=int, default=8, help="Number of worker processes.")
    parser.add_argument("--log-file", default="./globus_index_pg.log", help="Log file path (default: ./globus_index_pg.log)")
    parser.add_argument("--clean-slate", action="store_true", help="Clear existing records before indexing (ensures DB matches filesystem)")
    args = parser.parse_args()

    log_file = args.log_file or "./globus_index_pg.log"

    logger = setup_logging(log_file)
    logger.info("========================================")
    logger.info("  STARTING GLOBUS FILE INDEXING (PG)   ")
    logger.info("========================================")
    logger.info(f"Database: {args.dbname}@{args.host}:{args.port}")
    logger.info(f"Endpoint: {args.endpoint}")
    logger.info(f"Site: {args.site}")
    logger.info(f"Storage domain: {args.storage_domain}")
    logger.info(f"Namespace: {args.namespace}")
    logger.info(f"Storage root: {args.storage_root}")
    logger.info(f"Data state: {args.state}")
    logger.info(f"Batch size: {args.batch_size}")
    logger.info(f"Max workers: {args.max_workers}")

    # Build connection parameters
    conn_params = {
        "host": args.host,
        "port": args.port,
        "dbname": args.dbname,
        "user": args.user
    }
    if args.password:
        conn_params["password"] = args.password
    schema_path = args.schema  # or compute default here
    conn = init_db(conn_params, schema_path, logger)

    try:
        if args.clean_slate:
            deleted = clear_site_index(
                conn, 
                args.endpoint,
                args.site,
                args.state,
                args.storage_root,
                logger
            )
            logger.info(f"Clean slate mode: removed {deleted} old records")
    except Exception as e:
        logger.error(f"Error during clean slate operation: {e}")
        conn.close()
        return

    try:
        crawl_tree_mp(
            conn=conn,
            endpoint=args.endpoint,
            site=args.site,
            storage_domain=args.storage_domain,
            namespace=args.namespace,
            storage_root=args.storage_root.rstrip("/"),
            data_state=args.state,
            batch_size=args.batch_size,
            max_workers=args.max_workers,
            logger=logger,
        )
    finally:
        conn.close()
        logger.info("Indexing run complete.")


if __name__ == "__main__":
    main()