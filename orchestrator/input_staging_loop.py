from __future__ import annotations

import os
import time
from pathlib import Path

from agir_db import AgirDB
from .config import load_stage_config


def _route_for_stage(stage: str, transfer_cfg: dict) -> dict:
    routes = transfer_cfg.get("routes", {})
    return routes.get(stage, {})


def build_globus_submission_context(item: dict, transfer_cfg: dict) -> dict:
    """Prepare endpoint and path context for Globus transfer submission."""
    route = _route_for_stage(item["stage"], transfer_cfg)
    source_mode = transfer_cfg.get("source_mode", "production")
    src_endpoint = transfer_cfg.get("juno_endpoint")
    src_root = route.get("source_root_juno")
    src_lts_ref = item["src_lts_ref"]

    if source_mode == "testing_ncsu":
        src_endpoint = transfer_cfg.get("ncsu_endpoint")
        src_root = route.get("source_root_ncsu")
        if src_root:
            src_lts_ref = str(Path(src_root) / item["batch_id"])

    dst_root = route.get("destination_root")
    dst_staging_ref = item["dst_staging_ref"]
    if dst_root and not dst_staging_ref.startswith(dst_root):
        dst_staging_ref = str(Path(dst_root) / item["batch_id"])

    return {
        "batch_id": item["batch_id"],
        "stage": item["stage"],
        "src_lts_ref": src_lts_ref,
        "dst_staging_ref": dst_staging_ref,
        "src_endpoint": src_endpoint,
        "dst_endpoint": transfer_cfg.get("ceres_endpoint"),
        "src_root": src_root,
        "dst_root": dst_root,
        "dry_run": bool(transfer_cfg.get("dry_run", False)),
    }


def run_input_staging_once(
    limit: int = 20,
    requested_by: str = "orchestrator.local",
    config_path: str = "configs/juno_transfer.yaml",
    batch_ids: list[str] | None = None,
) -> int:
    completed = 0
    submitted_transfer_ids: set[str] = set()
    with AgirDB() as db:
        cfg = load_stage_config(config_path)
        transfer_cfg = cfg.get("transfer", {})
        poll_interval = int(transfer_cfg.get("poll_interval_seconds", 10))
        max_poll_attempts = int(transfer_cfg.get("max_poll_attempts", 180))
        poll_timeout = int(transfer_cfg.get("poll_timeout_seconds", 1800))
        poll_deadline = time.monotonic() + max(0, poll_timeout)

        candidates = db.orchestration.get_batches_needing_input_staging(
            limit=limit,
            stages=["raw_to_jpg"],
            batch_ids=batch_ids,
        )
        for item in candidates:
            req = db.orchestration.request_input_transfer(
                batch_id=item["batch_id"],
                stage=item["stage"],
                transfer_profile_id=item["transfer_profile_id"],
                src_lts_ref=item["src_lts_ref"],
                dst_staging_ref=item["dst_staging_ref"],
                requested_by=requested_by,
                priority=int(item["priority"]),
            )
            if not req.get("accepted"):
                continue
            if req.get("state") in {"already_completed", "already_active"}:
                continue

            transfer_id = req["transfer_id"]
            context = build_globus_submission_context(item, transfer_cfg)

            try:
                src_path = context["src_lts_ref"]
                dst_path = context["dst_staging_ref"]
            except Exception as exc:
                db.orchestration.mark_input_transfer_status(
                    transfer_id,
                    "failed",
                    error_summary=f"path mapping error: {exc}",
                )
                continue

            label = db.transfers.build_input_staging_label(item["stage"], item["batch_id"])
            cmd = db.transfers.build_globus_cmd(
                src_endpoint=context["src_endpoint"],
                dst_endpoint=context["dst_endpoint"],
                src_path=src_path,
                dst_path=dst_path,
                recursive=True,
                label=label,
            )

            submit_status, globus_task_id, details = db.transfers.submit_globus_transfer(
                cmd=cmd,
                dry_run=context["dry_run"],
            )

            if submit_status != "submitted":
                db.orchestration.mark_input_transfer_status(
                    transfer_id,
                    "failed",
                    error_summary=details,
                )
                continue

            db.orchestration.mark_input_transfer_submitted(
                transfer_id=transfer_id,
                globus_task_id=globus_task_id,
                globus_src_endpoint=context["src_endpoint"],
                globus_dst_endpoint=context["dst_endpoint"],
                globus_label=label,
                submission_details=details,
            )
            submitted_transfer_ids.add(transfer_id)

        # Poll requested/active transfers until terminal state or timeout.
        while time.monotonic() <= poll_deadline:
            if submitted_transfer_ids:
                pending = db.orchestration.get_input_transfers_for_polling_by_ids(
                    transfer_ids=list(submitted_transfer_ids),
                )
            else:
                pending = db.orchestration.get_pending_input_transfers_for_polling(limit=limit)
            if not pending:
                break

            in_flight = 0
            for row in pending:
                if int(row.get("poll_attempts") or 0) >= max_poll_attempts:
                    db.orchestration.update_input_transfer_poll_result(
                        transfer_id=row["transfer_id"],
                        status="failed",
                        globus_status_text="POLL_MAX_ATTEMPTS_REACHED",
                        error_summary=f"poll attempts exceeded ({max_poll_attempts})",
                    )
                    continue

                polled_status, poll_details = db.transfers.poll_globus_task(
                    globus_task_id=row["globus_task_id"],
                    dry_run=bool(transfer_cfg.get("dry_run", False)),
                )

                error_summary = poll_details if polled_status == "failed" else None
                db.orchestration.update_input_transfer_poll_result(
                    transfer_id=row["transfer_id"],
                    status=polled_status,
                    globus_status_text=poll_details,
                    error_summary=error_summary,
                )

                if polled_status == "completed":
                    db.orchestration.register_90daydata_index_for_batch(row["batch_id"])
                    if row["transfer_id"] in submitted_transfer_ids:
                        completed += 1
                elif polled_status in {"requested", "active"}:
                    in_flight += 1

            if in_flight == 0:
                break
            time.sleep(max(1, poll_interval))

    return completed


if __name__ == "__main__":
    count = run_input_staging_once(limit=int(os.environ.get("INPUT_STAGING_LIMIT", "1")))
    print(f"input_staged={count}")
