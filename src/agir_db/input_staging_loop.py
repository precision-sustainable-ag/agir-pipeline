from __future__ import annotations

import os
import shutil
from pathlib import Path

from agir_db import AgirDB


def build_globus_submission_context(item: dict, cfg: dict) -> dict:
    """
    Prepare transfer context for NCSU local Globus -> JUNO Globus submission.

    TODO: Confirm whether src_lts_ref and dst_staging_ref should be used directly,
    or converted to endpoint-relative paths from configured storage roots.
    """
    transfer_cfg = cfg.get("transfer", {})
    return {
        "batch_id": item["batch_id"],
        "stage": item["stage"],
        "src_lts_ref": item["src_lts_ref"],
        "dst_staging_ref": item["dst_staging_ref"],
        "src_endpoint": transfer_cfg.get("ncsu_endpoint"),
        "dst_endpoint": transfer_cfg.get("juno_endpoint"),
        "dst_root": transfer_cfg.get("dest_root"),
    }


def globus_placeholder_transfer(src: str, dst: str) -> None:
    # Placeholder for real Globus integration; local copy only for now.
    # TODO: Replace local copy with NCSU local Globus -> JUNO Globus transfer submission.
    # TODO: Confirm whether src_lts_ref and dst_staging_ref are already valid Globus paths.
    src_p = Path(src)
    dst_p = Path(dst)

    if src_p.is_dir():
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        if dst_p.exists():
            shutil.rmtree(dst_p)
        shutil.copytree(src_p, dst_p)
    else:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, dst_p)


def run_input_staging_once(limit: int = 20, requested_by: str = "orchestrator.local") -> int:
    moved = 0
    with AgirDB() as db:
        # TODO: Load shared JUNO Globus transfer config here before switching
        # from the local placeholder copy path to transfer submission.
        candidates = db.orchestration.get_batches_needing_input_staging(limit=limit, stages=["raw_to_jpg"])
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
            try:
                # TODO: Use build_globus_submission_context(...) when wiring
                # the real Globus submission path into this loop.
                src_path = Path(item["src_lts_ref"])
                if not src_path.exists():
                    db.orchestration.mark_input_transfer_status(
                        transfer_id,
                        "failed",
                        error_summary=f"missing source path: {src_path}",
                    )
                    continue
                dst_path = Path(item["dst_staging_ref"])
                if not str(dst_path).startswith("/90daydata/dash_agir/"):
                    db.orchestration.mark_input_transfer_status(
                        transfer_id,
                        "failed",
                        error_summary=f"invalid phase2 destination: {dst_path}",
                    )
                    continue
                db.orchestration.mark_input_transfer_status(transfer_id, "active")
                globus_placeholder_transfer(item["src_lts_ref"], item["dst_staging_ref"])
                db.orchestration.register_90daydata_index_for_batch(item["batch_id"])
                db.orchestration.mark_input_transfer_status(transfer_id, "completed")
                moved += 1
            except Exception as exc:
                db.orchestration.mark_input_transfer_status(transfer_id, "failed", error_summary=str(exc))
                raise
    return moved


if __name__ == "__main__":
    count = run_input_staging_once(limit=int(os.environ.get("INPUT_STAGING_LIMIT", "20")))
    print(f"input_staged={count}")
