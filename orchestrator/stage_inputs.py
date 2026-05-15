"""
Targeted input staging: transfer time-windowed files from LTS to 90daydata.

Physical files are staged into the existing flat batch directory:

    <dst_root>/<batch_id>/<file_name>

The exact per-window workset is recorded in an input manifest:

    <input_manifest_root>/<stage>/<batch_id>/<window_key>/input_manifest.json

Downstream jobs should use the manifest, not directory contents, to decide which
files belong to a window.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Dict, List, NamedTuple, Optional

from agir_db import AgirDB

from .batch_list import BatchEntry, make_window_key
from .input_manifest import (
    build_input_manifest_path,
    build_raw_input_manifest,
    build_staging_report_path,
    write_json,
)

logger = logging.getLogger(__name__)


class StageInputResult(NamedTuple):
    batch_id: str
    window_key: str
    status: str
    input_manifest_path: Optional[str]
    staging_report_path: Optional[str]
    transfer_id: Optional[str]
    globus_task_id: Optional[str]
    error: Optional[str]


class WindowStagingContext:
    """Per-window state and output helpers for input staging."""

    def __init__(
        self,
        *,
        entry: BatchEntry,
        stage: str,
        input_root: str,
        files: List[dict],
        input_manifest_path: Path,
        staging_report_path: Path,
    ) -> None:
        self.entry = entry
        self.stage = stage
        self.input_root = input_root
        self.files = files
        self.input_manifest_path = input_manifest_path
        self.staging_report_path = staging_report_path

        self.window_key = make_window_key(entry.start_epoch, entry.end_epoch)
        self.result_key = f"{entry.batch_id}:{self.window_key}"

        self.transfer_id: Optional[str] = None
        self.globus_task_id: Optional[str] = None

    @property
    def batch_id(self) -> str:
        return self.entry.batch_id

    @property
    def start_epoch(self) -> int:
        return self.entry.start_epoch

    @property
    def end_epoch(self) -> int:
        return self.entry.end_epoch

    @property
    def n_files(self) -> int:
        return len(self.files)

    def set_transfer(
        self,
        *,
        transfer_id: Optional[str],
        globus_task_id: Optional[str] = None,
    ) -> None:
        self.transfer_id = transfer_id
        self.globus_task_id = globus_task_id

    def append_result(
        self,
        results: List[StageInputResult],
        *,
        status: str,
        include_manifest: bool = False,
        include_report: bool = True,
        error: Optional[str] = None,
    ) -> None:
        results.append(
            StageInputResult(
                batch_id=self.batch_id,
                window_key=self.window_key,
                status=status,
                input_manifest_path=(
                    str(self.input_manifest_path) if include_manifest else None
                ),
                staging_report_path=(
                    str(self.staging_report_path) if include_report else None
                ),
                transfer_id=self.transfer_id,
                globus_task_id=self.globus_task_id,
                error=error,
            )
        )

    def write_ready_input_manifest(self) -> Path:
        manifest = build_raw_input_manifest(
            stage=self.stage,
            batch_id=self.batch_id,
            window_key=self.window_key,
            start_epoch=self.start_epoch,
            end_epoch=self.end_epoch,
            input_root=self.input_root,
            files=self.files,
        )
        return write_json(self.input_manifest_path, manifest)

    def write_staging_report(
        self,
        *,
        status: str,
        n_files_staged: int = 0,
        include_manifest: bool = False,
        error: Optional[str] = None,
    ) -> Path:
        report = {
            "schema_version": 1,
            "report_kind": "input_staging",
            "stage": self.stage,
            "batch_id": self.batch_id,
            "window_key": self.window_key,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "status": status,
            "transfer_id": self.transfer_id,
            "globus_task_id": self.globus_task_id,
            "input_root": self.input_root,
            "input_manifest_path": (
                str(self.input_manifest_path) if include_manifest else None
            ),
            "n_files_expected": self.n_files,
            "n_files_staged": n_files_staged,
            "error": error,
        }
        return write_json(self.staging_report_path, report)

    def mark_ready(
        self,
        results: List[StageInputResult],
        *,
        status: str,
    ) -> None:
        self.write_ready_input_manifest()
        self.write_staging_report(
            status=status,
            n_files_staged=self.n_files,
            include_manifest=True,
        )
        self.append_result(
            results,
            status=status,
            include_manifest=True,
            include_report=True,
        )

    def mark_not_ready(
        self,
        results: List[StageInputResult],
        *,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        self.write_staging_report(
            status=status,
            n_files_staged=0,
            include_manifest=False,
            error=error,
        )
        self.append_result(
            results,
            status=status,
            include_manifest=False,
            include_report=True,
            error=error,
        )


def stage_inputs_for_batches(
    batch_entries: List[BatchEntry],
    config_path: str,
    stage: str,
    requested_by: str = "orchestrator.manual",
    dry_run: Optional[bool] = None,
) -> List[StageInputResult]:
    """Transfer time-windowed files for each batch from LTS to 90daydata."""
    results: List[StageInputResult] = []

    with AgirDB() as db:
        cfg = db.transfers.load_transfer_config(config_path)
        transfer_cfg = cfg.get("transfer", {})
        paths_cfg = cfg.get("paths", {})

        input_manifest_root = paths_cfg.get("input_manifest_root")
        if not input_manifest_root:
            raise ValueError(
                f"Config '{config_path}' is missing required "
                "'paths.input_manifest_root' field."
            )

        src_endpoint = transfer_cfg.get("juno_endpoint")
        dst_endpoint = transfer_cfg.get("ceres_endpoint")
        route = transfer_cfg.get("routes", {}).get(stage, {})
        if not route:
            raise ValueError(
                f"No transfer route found for stage '{stage}' in config '{config_path}'. "
                f"Add a 'transfer.routes.{stage}' section."
            )

        src_root = route.get("source_root_juno")
        dst_root = route.get("destination_root")
        if not src_endpoint:
            raise ValueError(f"Config '{config_path}' is missing 'transfer.juno_endpoint'.")
        if not dst_endpoint:
            raise ValueError(f"Config '{config_path}' is missing 'transfer.ceres_endpoint'.")
        if not src_root:
            raise ValueError(
                f"Config '{config_path}' is missing "
                f"'transfer.routes.{stage}.source_root_juno'."
            )
        if not dst_root:
            raise ValueError(
                f"Config '{config_path}' is missing "
                f"'transfer.routes.{stage}.destination_root'."
            )

        _dry_run = dry_run if dry_run is not None else bool(transfer_cfg.get("dry_run", False))

        poll_interval = int(transfer_cfg.get("poll_interval_seconds", 30))
        poll_timeout = int(transfer_cfg.get("poll_timeout_seconds", 14400))
        max_poll_attempts = int(transfer_cfg.get("max_poll_attempts", 480))

        submitted: Dict[str, str] = {}
        ctx_by_wkey: Dict[str, WindowStagingContext] = {}

        for entry in batch_entries:
            batch_id = entry.batch_id
            window_key = make_window_key(entry.start_epoch, entry.end_epoch)
            result_key = f"{batch_id}:{window_key}"
            input_root = f"{dst_root}/{batch_id}"

            input_manifest_path = build_input_manifest_path(
                input_manifest_root,
                stage,
                batch_id,
                window_key,
            )
            staging_report_path = build_staging_report_path(
                input_manifest_root,
                stage,
                batch_id,
                window_key,
            )

            logger.info(
                "[%s] Querying file index for window [%d, %d]",
                batch_id,
                entry.start_epoch,
                entry.end_epoch,
            )
            files = db.orchestration.get_raw_files_for_batch_window(
                batch_id=batch_id,
                start_epoch=entry.start_epoch,
                end_epoch=entry.end_epoch,
            )

            ctx = WindowStagingContext(
                entry=entry,
                stage=stage,
                input_root=input_root,
                files=files,
                input_manifest_path=input_manifest_path,
                staging_report_path=staging_report_path,
            )

            if not files:
                logger.warning("[%s] No RAW files found in window — skipping", batch_id)
                ctx.mark_not_ready(
                    results,
                    status="no_files",
                    error="No RAW files found in window",
                )
                continue

            logger.info("[%s] Found %d RAW files in window", batch_id, len(files))

            file_pairs = [
                (f["full_path"], f"{input_root}/{f['file_name']}")
                for f in files
            ]

            batch_fd, batch_file_path = tempfile.mkstemp(
                prefix=f"globus_{batch_id}_",
                suffix=".txt",
            )

            submit_status: Optional[str] = None
            globus_task_id: Optional[str] = None
            details: Optional[str] = None
            transfer_id: Optional[str] = None

            try:
                with os.fdopen(batch_fd, "w") as fh:
                    for src_path, dst_path in file_pairs:
                        fh.write(f"{src_path} {dst_path}\n")

                src_dir = f"{src_root}/{batch_id}"
                dst_dir = input_root

                req = db.orchestration.request_windowed_input_transfer(
                    batch_id=batch_id,
                    stage=stage,
                    start_epoch=entry.start_epoch,
                    end_epoch=entry.end_epoch,
                    src_lts_ref=src_dir,
                    dst_staging_ref=dst_dir,
                    requested_by=requested_by,
                    priority=100,
                )

                if not req.get("accepted"):
                    logger.error("[%s] Transfer request rejected by DB", batch_id)
                    ctx.mark_not_ready(
                        results,
                        status="request_failed",
                        error="Transfer request rejected by DB",
                    )
                    continue

                state = req.get("state")

                if state == "already_completed":
                    logger.info(
                        "[%s] Transfer already completed — ensuring input manifest exists",
                        batch_id,
                    )
                    ctx.mark_ready(results, status="already_completed")
                    continue

                if state == "already_active":
                    logger.info("[%s] Transfer already active — joining poll loop", batch_id)

                    transfer_id = req["transfer_id"]
                    ctx.set_transfer(transfer_id=transfer_id)

                    submitted[result_key] = transfer_id
                    ctx_by_wkey[result_key] = ctx

                    continue

                transfer_id = req["transfer_id"]
                label = db.transfers.build_input_staging_label(stage, batch_id)

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

                if transfer_id:
                    db.orchestration.mark_input_transfer_status(
                        transfer_id,
                        "failed",
                        error_summary=details,
                    )
                ctx.set_transfer(
                    transfer_id=transfer_id,
                    globus_task_id=globus_task_id,
                )
                ctx.mark_not_ready(
                    results,
                    status="submit_failed",
                    error=details,
                )
                continue

            db.orchestration.mark_input_transfer_submitted(
                transfer_id=transfer_id,
                globus_src_endpoint=src_endpoint,
                globus_dst_endpoint=dst_endpoint,
                globus_task_id=globus_task_id,
                globus_label=label,
                submission_details=details,
            )

            ctx.set_transfer(
                transfer_id=transfer_id,
                globus_task_id=globus_task_id,
            )

            submitted[result_key] = transfer_id
            ctx_by_wkey[result_key] = ctx

            logger.info(
                "[%s] Submitted Globus task %s (window %s)",
                batch_id,
                globus_task_id,
                result_key,
            )

        if not submitted:
            return results

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
                if not wkey:
                    raise RuntimeError(f"No polling context found for transfer_id={tid}")

                ctx = ctx_by_wkey[wkey]
                ctx.globus_task_id = row.get("globus_task_id")

                if int(row.get("poll_attempts") or 0) >= max_poll_attempts:
                    error = f"exceeded {max_poll_attempts} poll attempts"
                    db.orchestration.update_input_transfer_poll_result(
                        transfer_id=tid,
                        status="failed",
                        globus_status_text="POLL_MAX_ATTEMPTS_REACHED",
                        error_summary=error,
                    )
                    ctx.mark_not_ready(
                        results,
                        status="poll_timeout",
                        error=error,
                    )
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
                    if _dry_run:
                        ctx.mark_not_ready(
                            results,
                            status="dry_run",
                            error="dry_run=True; files were not transferred",
                        )
                        submitted.pop(wkey, None)
                        logger.info("[%s] Dry-run transfer completed", ctx.batch_id)
                        continue

                    db.orchestration.register_windowed_90daydata_files(
                        batch_id=ctx.batch_id,
                        start_epoch=ctx.start_epoch,
                        end_epoch=ctx.end_epoch,
                    )

                    ctx.mark_ready(results, status="completed")
                    submitted.pop(wkey, None)

                    logger.info(
                        "[%s] Transfer completed; wrote input manifest %s",
                        ctx.batch_id,
                        ctx.input_manifest_path,
                    )

                elif polled_status == "failed":
                    ctx.mark_not_ready(
                        results,
                        status="failed",
                        error=poll_details,
                    )
                    submitted.pop(wkey, None)
                    logger.error("[%s] Transfer failed", ctx.batch_id)

                else:
                    in_flight += 1

            if submitted:
                logger.info(
                    "  %d in-flight, %d remaining windows — sleeping %ds",
                    in_flight,
                    len(submitted),
                    poll_interval,
                )
                time.sleep(max(1, poll_interval))

        for wkey in list(submitted.keys()):
            ctx = ctx_by_wkey[wkey]
            logger.error("Poll timeout exceeded for window %s", wkey)

            ctx.mark_not_ready(
                results,
                status="poll_timeout",
                error="poll timeout exceeded",
            )

    return results