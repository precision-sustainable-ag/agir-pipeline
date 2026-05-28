"""
orchestrator/sqlite_stage_inputs.py
=====================================

Globus input staging driven by globus_file_index (SQLite).

Called by scripts/sqlite/stage_inputs.py and scripts/sqlite/run_stage.py.
No PostgreSQL dependency.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from orchestrator.sqlite_db import open_db, get_batches_needing_raw_to_jpg

logger = logging.getLogger(__name__)

STAGE = "raw_to_jpg"

_RAW_EXTS = ("raw", "arw", "nef", "cr2", "cr3", "dng", "rw2", "raf", "orf")


# ── Result type ───────────────────────────────────────────────────────────────

class StageResult(NamedTuple):
    batch_id: str
    status: str          # completed | failed | no_files | dry_run | timeout
    n_files: int
    globus_task_id: Optional[str]
    error: Optional[str]


# ── SQLite query ──────────────────────────────────────────────────────────────

def get_raw_files_for_batch(conn, batch_id: str, site: str) -> List[Dict]:
    """
    Return current RAW files for *batch_id* from globus_file_index,
    filtered to *site* so we only transfer files from the correct endpoint.
    """
    placeholders = ",".join(f"'{e}'" for e in _RAW_EXTS)
    rows = conn.execute(
        f"""
        SELECT file_name, full_path, rel_path, storage_root,
               endpoint, site, size_bytes, fname_ts_epoch
        FROM   globus_file_index
        WHERE  batch_id   = ?
          AND  site       = ?
          AND  data_state = 'semifield-upload'
          AND  entry_type = 'file'
          AND  is_current = 1
          AND  LOWER(COALESCE(file_ext,'')) IN ({placeholders})
        ORDER  BY fname_ts_epoch ASC, file_name ASC
        """,
        (batch_id, site),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Globus helpers ────────────────────────────────────────────────────────────

def _write_batch_file(files: List[Dict], dst_root: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="agir_transfer_")
    try:
        with os.fdopen(fd, "w") as fh:
            for f in files:
                src = f["full_path"]
                dst = f"{dst_root.rstrip('/')}/{f['file_name']}"
                fh.write(f"{src} {dst}\n")
    except Exception:
        os.unlink(path)
        raise
    return path


def _submit_globus_transfer(
    src_endpoint: str,
    dst_endpoint: str,
    batch_file: str,
    label: str,
    dry_run: bool,
) -> Tuple[str, Optional[str], str]:
    cmd = [
        "globus", "transfer",
        src_endpoint, dst_endpoint,
        "--batch", batch_file,
        "--label", label,
        "--format", "json",
    ]
    if dry_run:
        logger.info("[DRY-RUN] Would run: %s", " ".join(cmd))
        return "dry_run", f"dry-run-{label}", "[DRY-RUN]"

    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        output = (proc.stdout or "").strip()
        task_id = None
        try:
            task_id = json.loads(output).get("task_id")
        except (json.JSONDecodeError, AttributeError):
            pass
        if not task_id:
            import re
            m = re.search(r"Task ID:\s*([a-f0-9-]+)", output, re.IGNORECASE)
            task_id = m.group(1) if m else None
        return "submitted", task_id, output
    except subprocess.CalledProcessError as exc:
        details = ((exc.stderr or "") + "\n" + (exc.stdout or "")).strip()
        return "failed", None, details


def _poll_task(task_id: str, dry_run: bool) -> str:
    if dry_run or task_id.startswith("dry-run-"):
        return "completed"
    try:
        proc = subprocess.run(
            ["globus", "task", "show", task_id, "--format", "json"],
            check=True, capture_output=True, text=True,
        )
        state = ""
        try:
            state = (json.loads(proc.stdout).get("status") or "").upper()
        except (json.JSONDecodeError, AttributeError):
            import re
            m = re.search(r"Status:\s*(\w+)", proc.stdout, re.IGNORECASE)
            state = m.group(1).upper() if m else ""
        if state in ("SUCCEEDED", "SUCCESS"):
            return "completed"
        if state in ("FAILED", "CANCELED"):
            return "failed"
        return "active"
    except subprocess.CalledProcessError:
        return "failed"


def _poll_until_done(
    task_id: str,
    *,
    dry_run: bool,
    poll_interval: int,
    poll_timeout: int,
    batch_id: str,
) -> str:
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        status = _poll_task(task_id, dry_run)
        logger.info("[%s] Transfer %s: %s", batch_id, task_id, status)
        if status in ("completed", "failed"):
            return status
        time.sleep(poll_interval)
    return "timeout"


# ── Per-batch staging (main reusable entry point) ─────────────────────────────

def stage_batch(
    batch_id: str,
    *,
    conn,
    site: str,
    src_endpoint: str,
    dst_endpoint: str,
    input_staging_root: str,
    dry_run: bool,
    poll_interval: int,
    poll_timeout: int,
) -> StageResult:
    files = get_raw_files_for_batch(conn, batch_id, site)
    if not files:
        logger.warning(
            "[%s] No current RAW files in globus_file_index for site=%s — skipping", batch_id, site
        )
        return StageResult(batch_id, "no_files", 0, None,
                           f"No current RAW files in globus_file_index for site={site}")

    logger.info("[%s] Found %d RAW file(s) to stage", batch_id, len(files))
    dst_dir = f"{input_staging_root.rstrip('/')}/{batch_id}"
    label   = f"agir:{STAGE}:{batch_id}:input_stage"

    batch_file = _write_batch_file(files, dst_dir)
    try:
        submit_status, task_id, details = _submit_globus_transfer(
            src_endpoint, dst_endpoint, batch_file, label, dry_run,
        )
    finally:
        try:
            os.unlink(batch_file)
        except OSError:
            pass

    if submit_status == "dry_run":
        logger.info("[%s] Dry-run: %d files → %s", batch_id, len(files), dst_dir)
        return StageResult(batch_id, "dry_run", len(files), task_id, None)

    if submit_status != "submitted":
        logger.error("[%s] Globus submission failed: %s", batch_id, details)
        return StageResult(batch_id, "failed", len(files), None, details)

    logger.info("[%s] Globus task submitted: %s", batch_id, task_id)
    final = _poll_until_done(
        task_id,
        dry_run=dry_run,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        batch_id=batch_id,
    )

    if final == "completed":
        dst_path = Path(dst_dir)
        if dst_path.exists():
            n_landed = sum(
                1 for f in dst_path.iterdir()
                if f.suffix.lstrip(".").lower() in _RAW_EXTS
            )
            logger.info("[%s] %d RAW file(s) landed in %s", batch_id, n_landed, dst_dir)
        else:
            logger.warning(
                "[%s] Destination not visible from this node: %s", batch_id, dst_dir
            )
        return StageResult(batch_id, "completed", len(files), task_id, None)

    if final == "timeout":
        msg = f"Timed out after {poll_timeout}s (task {task_id})"
        logger.error("[%s] %s", batch_id, msg)
        return StageResult(batch_id, "timeout", len(files), task_id, msg)

    logger.error("[%s] Globus task %s failed", batch_id, task_id)
    return StageResult(batch_id, "failed", len(files), task_id,
                       f"Globus task failed: {task_id}")


# ── Batch runner ──────────────────────────────────────────────────────────────

def stage_batches(
    batch_ids: List[str],
    cfg: dict,
    *,
    dry_run: bool,
    site: str = "JUNO",
) -> List[StageResult]:
    transfer_cfg        = cfg.get("transfer", {})
    src_endpoint        = transfer_cfg["juno_endpoint"]
    dst_endpoint        = transfer_cfg["ceres_endpoint"]
    input_staging_root  = cfg["paths"]["input_staging_root"]
    poll_interval       = int(transfer_cfg.get("poll_interval_seconds", 30))
    poll_timeout        = int(transfer_cfg.get("poll_timeout_seconds", 7200))
    _dry_run            = dry_run or bool(transfer_cfg.get("dry_run", False))
    db_path             = Path(cfg["paths"]["db"])

    logger.info(
        "Staging %d batch(es) | src=%s dst=%s dry_run=%s",
        len(batch_ids), src_endpoint, dst_endpoint, _dry_run,
    )

    conn = open_db(db_path, readonly=True, local_copy=True)
    results: List[StageResult] = []
    try:
        for batch_id in batch_ids:
            results.append(stage_batch(
                batch_id,
                conn=conn,
                site=site,
                src_endpoint=src_endpoint,
                dst_endpoint=dst_endpoint,
                input_staging_root=input_staging_root,
                dry_run=_dry_run,
                poll_interval=poll_interval,
                poll_timeout=poll_timeout,
            ))
    finally:
        conn.close()
    return results