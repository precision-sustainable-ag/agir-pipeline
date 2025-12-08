#!/usr/bin/env python3
"""
Move whole batches to JUNO using recursive Globus transfers.

Steps:
1. Read config and connect to DB (via utils/db.py)
2. Load report CSV and extract batch_ids
3. Query source.globus_file_index for distinct batch sources
4. For each batch, run a single `globus transfer --recursive`
5. Ensure logs.juno_transfers exists (via external SQL file)
6. Log one row per batch transfer in logs.juno_transfers
"""

import argparse
import csv
import datetime as dt
import os
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

import yaml
import psycopg2.extras

from utils.db import get_conn  # your existing DB helper


# ---------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------

@dataclass
class ReportConfig:
    csv_path: str
    batch_column: str = "batch_id"


@dataclass
class TransferConfig:
    data_state: str
    juno_endpoint: str
    dest_root: str
    dry_run: bool = False
    location_priority: Optional[List[str]] = None
    lts_root_priority: Optional[List[str]] = None


# ---------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------

def load_config(path: str) -> Dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def parse_report_cfg(cfg: Dict) -> ReportConfig:
    r = cfg["report"]
    return ReportConfig(
        csv_path=r["csv_path"],
        batch_column=r.get("batch_column", "batch_id"),
    )


def parse_transfer_cfg(cfg: Dict) -> TransferConfig:
    t = cfg["transfer"]
    return TransferConfig(
        data_state=t["data_state"],
        juno_endpoint=t["juno_endpoint"],
        dest_root=t["dest_root"],
        dry_run=bool(t.get("dry_run", False)),
        location_priority=t.get("location_priority"),
        lts_root_priority=t.get("lts_root_priority"),
    )


# ---------------------------------------------------------------------
# Helpers: schema, CSV, DB
# ---------------------------------------------------------------------

def run_sql_file(conn, sql_path: pathlib.Path) -> None:
    sql_text = sql_path.read_text()
    with conn.cursor() as cur:
        cur.execute(sql_text)
    conn.commit()


def ensure_logs_schema(conn) -> None:
    """
    Run the logs.juno_transfers.sql file to ensure the schema/table exists.
    Assumes this script lives in scripts/, and SQL is at ../sql/05_logs/.
    """
    here = pathlib.Path(__file__).resolve()
    schema_file = here.parent.parent / "sql" / "05_logs" / "logs.juno_transfers.sql"
    if not schema_file.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_file}")
    run_sql_file(conn, schema_file)


def read_batch_ids(report_cfg: ReportConfig) -> Set[str]:
    batch_ids: Set[str] = set()
    with open(report_cfg.csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if report_cfg.batch_column not in reader.fieldnames:
            raise ValueError(
                f"Report CSV {report_cfg.csv_path} missing column '{report_cfg.batch_column}'. "
                f"Columns: {reader.fieldnames}"
            )
        for row in reader:
            b = (row[report_cfg.batch_column] or "").strip()
            if b:
                batch_ids.add(b)
    return batch_ids


def fetch_batch_sources(conn, batch_ids: List[str], data_state: str) -> List[Dict]:
    """
    Return one row per (batch_id, endpoint, root_path, location, lts_root).

    We assume:
      - Entire batch is missing on JUNO.
      - We just need to know where that batch lives now.
    """
    sql = """
        SELECT DISTINCT
            batch_id,
            endpoint,
            location,
            lts_root,
            root_path
        FROM source.globus_file_index
        WHERE data_state = %s
          AND batch_id = ANY(%s)
          AND location <> 'JUNO'
          AND entry_type = 'file'
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (data_state, batch_ids))
        return cur.fetchall()


def choose_primary_sources(
    batch_sources: List[Dict],
    location_priority: Optional[List[str]] = None,
    lts_root_priority: Optional[List[str]] = None,
) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
    """
    Given possibly multiple sources per batch (different locations and/or lts_roots),
    pick EXACTLY ONE (the "best") source per batch.
    """
    if location_priority is None:
        location_priority = []
    if lts_root_priority is None:
        lts_root_priority = []

    loc_prio = {loc: i for i, loc in enumerate(location_priority)}
    lts_prio = {root: i for i, root in enumerate(lts_root_priority)}
    default_loc_score = len(location_priority)
    default_lts_score = len(lts_root_priority)

    best_by_batch: Dict[str, Dict] = {}
    best_score: Dict[str, Tuple[int, int]] = {}
    duplicates: Dict[str, List[Dict]] = defaultdict(list)

    for rec in batch_sources:
        batch = rec["batch_id"]
        loc = rec.get("location")
        root = rec.get("lts_root")

        loc_score = loc_prio.get(loc, default_loc_score)
        root_score = lts_prio.get(root, default_lts_score)
        score = (loc_score, root_score)

        if batch not in best_by_batch:
            best_by_batch[batch] = rec
            best_score[batch] = score
        else:
            current_score = best_score[batch]
            if score < current_score:
                duplicates[batch].append(best_by_batch[batch])
                best_by_batch[batch] = rec
                best_score[batch] = score
            else:
                duplicates[batch].append(rec)

    primary_sources = list(best_by_batch.values())
    return primary_sources, duplicates


# ---------------------------------------------------------------------
# Globus + logging
# ---------------------------------------------------------------------

def build_batch_paths(rec: Dict, dest_root: str) -> Dict[str, str]:
    src_dir = os.path.join(rec["root_path"].rstrip("/"), rec["batch_id"])
    dst_dir = os.path.join(dest_root.rstrip("/"), rec["batch_id"])
    return {"src_dir": src_dir, "dst_dir": dst_dir}


def build_globus_cmd(
    src_endpoint: str,
    dst_endpoint: str,
    src_dir: str,
    dst_dir: str,
    label: str,
) -> List[str]:
    return [
        "globus", "transfer",
        f"{src_endpoint}:{src_dir}",
        f"{dst_endpoint}:{dst_dir}",
        "--recursive",
        "--label", label,
        "--skip-activation-check",
    ]


def run_globus_cmd(cmd: List[str], dry_run: bool) -> Tuple[str, Optional[str]]:
    if dry_run:
        print("[DRY-RUN]", " ".join(cmd))
        return "dry_run", None

    try:
        print("[GLOBUS]", " ".join(cmd))
        subprocess.run(cmd, check=True)
        return "submitted", None
    except subprocess.CalledProcessError as e:
        return "failed", str(e)


def log_batch_transfer(
    conn,
    rec: Dict,
    src_dir: str,
    dst_dir: str,
    data_state: str,
    status: str,
    error_message: Optional[str],
) -> None:
    sql = """
        INSERT INTO logs.juno_transfers (
            batch_id,
            endpoint,
            location,
            lts_root,
            root_path,
            data_state,
            source_dir,
            destination_dir,
            transfer_time,
            status,
            error_message
        )
        VALUES (
            %(batch_id)s,
            %(endpoint)s,
            %(location)s,
            %(lts_root)s,
            %(root_path)s,
            %(data_state)s,
            %(source_dir)s,
            %(destination_dir)s,
            %(transfer_time)s,
            %(status)s,
            %(error_message)s
        );
    """
    params = {
        "batch_id": rec["batch_id"],
        "endpoint": rec["endpoint"],
        "location": rec["location"],
        "lts_root": rec["lts_root"],
        "root_path": rec["root_path"],
        "data_state": data_state,
        "source_dir": src_dir,
        "destination_dir": dst_dir,
        "transfer_time": dt.datetime.now(dt.timezone.utc),
        "status": status,
        "error_message": error_message,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Move whole batches to JUNO via Globus.")
    parser.add_argument("--config", required=True, help="Path to YAML config.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    r_cfg = parse_report_cfg(cfg)
    t_cfg = parse_transfer_cfg(cfg)

    print(f"[INFO] Report CSV: {r_cfg.csv_path}")
    print(f"[INFO] data_state: {t_cfg.data_state}")
    print(f"[INFO] dest_root on JUNO: {t_cfg.dest_root}")
    print(f"[INFO] dry_run: {t_cfg.dry_run}")

    batch_ids = sorted(read_batch_ids(r_cfg))
    print(f"[INFO] Found {len(batch_ids)} unique batches in report.")

    if not batch_ids:
        print("[WARN] No batches found. Exiting.")
        return

    conn = get_conn()

    ensure_logs_schema(conn)

    print("[INFO] Fetching batch sources from source.globus_file_index...")
    batch_sources = fetch_batch_sources(conn, batch_ids, t_cfg.data_state)
    print(f"[INFO] {len(batch_sources)} batch source records found.")

    primary_sources, duplicates = choose_primary_sources(
        batch_sources,
        location_priority=t_cfg.location_priority,
        lts_root_priority=t_cfg.lts_root_priority,
    )

    print(
        f"[INFO] Using {len(primary_sources)} primary sources "
        f"({len(duplicates)} batches had multiple candidates)."
    )

    if duplicates:
        sample = list(duplicates.keys())[:5]
        print("[INFO] Example batches with multiple sources:", sample)

    processed = 0

    for rec in primary_sources:
        paths = build_batch_paths(rec, t_cfg.dest_root)
        src_dir, dst_dir = paths["src_dir"], paths["dst_dir"]

        label = f"agir_{t_cfg.data_state}_{rec['batch_id']}"
        cmd = build_globus_cmd(
            src_endpoint=rec["endpoint"],
            dst_endpoint=t_cfg.juno_endpoint,
            src_dir=src_dir,
            dst_dir=dst_dir,
            label=label,
        )

        status, err = run_globus_cmd(cmd, t_cfg.dry_run)
        log_batch_transfer(conn, rec, src_dir, dst_dir, t_cfg.data_state, status, err)

        processed += 1
        if processed % 50 == 0:
            conn.commit()
            print(f"[INFO] Logged {processed} batch transfers so far...")

    conn.commit()
    conn.close()
    print(f"[DONE] Processed {processed} batch-level transfers. dry_run={t_cfg.dry_run}")


if __name__ == "__main__":
    main()
