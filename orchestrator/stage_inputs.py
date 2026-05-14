"""
Targeted input staging: transfer time-windowed RAW files from JUNO LTS to 90daydata.

Unlike the automated input_staging_loop (which discovers batches via DB views and
transfers full directories), this module accepts an explicit batch list with per-batch
time windows and transfers only the RAW files whose filename epoch falls within each
window.  Transfer tracking and polling reuse the same logs.transfer_runs machinery.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Dict, List, Optional

from agir_db import AgirDB

from .batch_list import BatchEntry

logger = logging.getLogger(__name__)


def stage_inputs_for_batches(
    batch_entries: List[BatchEntry],
    config_path: str,
    requested_by: str = "orchestrator.manual",
    dry_run: Optional[bool] = None,
) -> Dict[str, str]:
    """Transfer time-windowed RAW files for each batch from JUNO LTS to 90daydata.

    Blocks until all submitted transfers reach a terminal state (completed/failed)
    or the poll timeout is exceeded.

    Returns a dict mapping "{batch_id}:{start_epoch}:{end_epoch}" to final status:
      "completed", "failed", "no_files", "request_failed",
      "submit_failed", "already_completed", "already_active", "poll_timeout"
    """
    results: Dict[str, str] = {}

    with AgirDB() as db:
        cfg = db.transfers.load_transfer_config(config_path)
        transfer_cfg = cfg.get("transfer", {})

        src_endpoint = transfer_cfg.get("juno_endpoint")
        dst_endpoint = transfer_cfg.get("ceres_endpoint")
        route = transfer_cfg.get("routes", {}).get("raw_to_jpg", {})
        src_root = route.get("source_root_juno")
        dst_root = route.get("destination_root")
        _dry_run = dry_run if dry_run is not None else bool(transfer_cfg.get("dry_run", False))

        poll_interval = int(transfer_cfg.get("poll_interval_seconds", 30))
        poll_timeout = int(transfer_cfg.get("poll_timeout_seconds", 7200))
        max_poll_attempts = int(transfer_cfg.get("max_poll_attempts", 240))

        # window_key -> transfer_id for submitted transfers still being polled
        # window_key = f"{batch_id}:{start_epoch}:{end_epoch}"
        submitted: Dict[str, str] = {}

        # window_key -> BatchEntry for lookups during polling
        entry_by_wkey: Dict[str, BatchEntry] = {}

        for entry in batch_entries:
            batch_id = entry.batch_id
            start_epoch = entry.start_epoch
            end_epoch = entry.end_epoch
            wkey = f"{batch_id}:{start_epoch}:{end_epoch}"

            logger.info(
                "[%s] Querying file index for window [%d, %d]",
                batch_id, start_epoch, end_epoch,
            )
            files = db.orchestration.get_raw_files_for_batch_window(
                batch_id=batch_id,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
            )
            if not files:
                logger.warning("[%s] No RAW files found in window — skipping", batch_id)
                results[wkey] = "no_files"
                continue

            logger.info("[%s] Found %d RAW files in window", batch_id, len(files))

            # Build per-file src->dst pairs preserving flat batch directory structure
            file_pairs = [
                (f["full_path"], f"{dst_root}/{batch_id}/{f['file_name']}")
                for f in files
            ]

            # Write Globus batch file to a temp path
            batch_fd, batch_file_path = tempfile.mkstemp(
                prefix=f"globus_{batch_id}_", suffix=".txt"
            )
            try:
                with os.fdopen(batch_fd, "w") as fh:
                    for src_path, dst_path in file_pairs:
                        fh.write(f"{src_path} {dst_path}\n")

                src_dir = f"{src_root}/{batch_id}"
                dst_dir = f"{dst_root}/{batch_id}"

                req = db.orchestration.request_windowed_input_transfer(
                    batch_id=batch_id,
                    stage="raw_to_jpg",
                    start_epoch=start_epoch,
                    end_epoch=end_epoch,
                    src_lts_ref=src_dir,
                    dst_staging_ref=dst_dir,
                    requested_by=requested_by,
                    priority=100,
                )

                if not req.get("accepted"):
                    logger.error("[%s] Transfer request rejected by DB", batch_id)
                    results[wkey] = "request_failed"
                    continue

                state = req.get("state")
                if state in {"already_completed", "already_active"}:
                    logger.info("[%s] Transfer %s — skipping submission", batch_id, state)
                    results[wkey] = state
                    continue

                transfer_id = req["transfer_id"]
                label = db.transfers.build_input_staging_label("raw_to_jpg", batch_id)

                cmd = db.transfers.build_globus_batch_cmd(
                    src_endpoint=src_endpoint,
                    dst_endpoint=dst_endpoint,
                    batch_file_path=batch_file_path,
                    label=label,
                )

                submit_status, globus_task_id, details = db.transfers.submit_globus_transfer(
                    cmd=cmd,
                    dry_run=_dry_run,
                )
            finally:
                try:
                    os.unlink(batch_file_path)
                except OSError:
                    pass

            if submit_status != "submitted":
                logger.error("[%s] Globus submission failed: %s", batch_id, details)
                db.orchestration.mark_input_transfer_status(
                    transfer_id, "failed", error_summary=details
                )
                results[wkey] = "submit_failed"
                continue

            db.orchestration.mark_input_transfer_submitted(
                transfer_id=transfer_id,
                globus_src_endpoint=src_endpoint,
                globus_dst_endpoint=dst_endpoint,
                globus_task_id=globus_task_id,
                globus_label=label,
                submission_details=details,
            )
            submitted[wkey] = transfer_id
            entry_by_wkey[wkey] = entry
            logger.info("[%s] Submitted Globus task %s (window %s)", batch_id, globus_task_id, wkey)

        if not submitted:
            return results

        # Reverse map: transfer_id -> window_key for efficient polling lookup
        transfer_to_wkey: Dict[str, str] = {v: k for k, v in submitted.items()}

        deadline = time.monotonic() + poll_timeout
        logger.info("Polling %d transfer(s) to completion...", len(submitted))

        while time.monotonic() <= deadline and submitted:
            rows = db.orchestration.get_input_transfers_for_polling_by_ids(
                list(submitted.values())
            )
            in_flight = 0

            for row in rows:
                tid = str(row["transfer_id"])
                wkey = transfer_to_wkey.get(tid)

                if int(row.get("poll_attempts") or 0) >= max_poll_attempts:
                    db.orchestration.update_input_transfer_poll_result(
                        transfer_id=tid,
                        status="failed",
                        globus_status_text="POLL_MAX_ATTEMPTS_REACHED",
                        error_summary=f"exceeded {max_poll_attempts} poll attempts",
                    )
                    if wkey:
                        results[wkey] = "poll_timeout"
                        submitted.pop(wkey, None)
                    continue

                polled_status, poll_details = db.transfers.poll_globus_task(
                    globus_task_id=row["globus_task_id"],
                    dry_run=_dry_run,
                )
                db.orchestration.update_input_transfer_poll_result(
                    transfer_id=tid,
                    status=polled_status,
                    globus_status_text=poll_details,
                    error_summary=poll_details if polled_status == "failed" else None,
                )

                if polled_status == "completed":
                    if not _dry_run and wkey:
                        entry = entry_by_wkey.get(wkey)
                        if entry:
                            db.orchestration.register_windowed_90daydata_files(
                                batch_id=entry.batch_id,
                                start_epoch=entry.start_epoch,
                                end_epoch=entry.end_epoch,
                            )
                    if wkey:
                        results[wkey] = "completed"
                        submitted.pop(wkey, None)
                    logger.info("[%s] Transfer completed", row["batch_id"])
                elif polled_status == "failed":
                    if wkey:
                        results[wkey] = "failed"
                        submitted.pop(wkey, None)
                    logger.error("[%s] Transfer failed", row["batch_id"])
                else:
                    in_flight += 1

            if submitted:
                logger.info(
                    "  %d in-flight, %d remaining windows — sleeping %ds",
                    in_flight, len(submitted), poll_interval,
                )
                time.sleep(max(1, poll_interval))

        for wkey in list(submitted.keys()):
            if wkey not in results:
                logger.error("Poll timeout exceeded for window %s", wkey)
                results[wkey] = "poll_timeout"

    return results
