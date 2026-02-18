#!/usr/bin/env python3
"""
Phase 1 orchestration smoke test.

Checks:
1) ready_work has at least one raw_to_jpg row
2) lease claim works
3) lease release works (and repeat release is non-error)
4) ingest_run_report is idempotent by run_id
5) success run removes batch from ready_work
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from agir_db import AgirDB
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)


def load_env_from_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_env_from_file(Path("pg_coords.env"))

    test_orchestrator_id = f"orch-phase1-test-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    report_path = Path("/tmp") / f"run_report_phase1_{run_id}.json"

    with AgirDB() as db:
        # 1) Find ready work
        ready = db.orchestration.get_ready_work(limit=20, stages=["raw_to_jpg"])
        assert ready, "No rows in report.ready_work for raw_to_jpg"

        item = ready[0]
        batch_id = item["batch_id"]
        stage = item["stage"]
        print(f"[OK] Ready work found: {batch_id} / {stage}")

        # 2) Claim lease
        claim = db.orchestration.claim_stage_lease(
            batch_id=batch_id,
            stage=stage,
            orchestrator_id=test_orchestrator_id,
            ttl_seconds=300,
        )
        assert claim.get("claimed") is True, f"Claim failed: {claim}"
        lease_id = str(claim["lease_id"])
        print(f"[OK] Lease claimed: {lease_id}")

        # 3) Release lease twice (non-error idempotency)
        rel1 = db.orchestration.release_stage_lease(
            lease_id=lease_id,
            orchestrator_id=test_orchestrator_id,
            release_reason="completed",
        )
        assert rel1.get("lease_id") is not None, f"Release failed: {rel1}"
        print(f"[OK] First release response: {rel1}")

        rel2 = db.orchestration.release_stage_lease(
            lease_id=lease_id,
            orchestrator_id=test_orchestrator_id,
            release_reason="completed",
        )
        assert rel2.get("lease_id") is not None, f"Second release errored: {rel2}"
        print(f"[OK] Second release response: {rel2}")

        # 4) Ingest run report twice (idempotent by run_id)
        report = {
            "run_id": run_id,
            "batch_id": batch_id,
            "stage": stage,
            "attempt": int(claim.get("attempt") or 1),
            "status": "success",
            "exit_code": 0,
            "started_at": (now - timedelta(minutes=2)).isoformat(),
            "ended_at": now.isoformat(),
            "output_ref": f"/90daydata/dash_agir/stage_runs/{run_id}",
        }
        report_path.write_text(json.dumps(report))

        out1 = db.orchestration.ingest_run_report(str(report_path))
        out2 = db.orchestration.ingest_run_report(str(report_path))
        assert out1["run_id"] == run_id and out2["run_id"] == run_id, "Ingest upsert failed"
        print(f"[OK] ingest_run_report idempotent for run_id={run_id}")

        # 5) Ensure successful run removes batch from ready_work
        still_ready = db.orchestration.get_ready_work(limit=200, stages=["raw_to_jpg"])
        still_ready_ids = {(r["batch_id"], r["stage"]) for r in still_ready}
        assert (batch_id, stage) not in still_ready_ids, "Batch still in ready_work after success"
        print(f"[OK] Success removed batch from ready_work: {batch_id}")

    print("\nPASS: Phase 1 orchestration test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
