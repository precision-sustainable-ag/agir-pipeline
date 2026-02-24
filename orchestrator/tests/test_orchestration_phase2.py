#!/usr/bin/env python3
"""
Phase 2 orchestration smoke test.

Checks:
1) batch appears in report.batches_needing_input_staging (Q8)
2) batch does not appear in report.ready_work before staging
3) Q9 request_input_transfer returns created then already_active (idempotent active dedupe)
4) input staging loop copies LTS -> /90daydata and records transfer
5) Q9 returns already_completed after staging
6) batch disappears from report.batches_needing_input_staging after staging
7) batch appears in report.ready_work after staging
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from agir_db import AgirDB
from agir_db.input_staging_loop import run_input_staging_once


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


def make_seed_paths(batch_id: str) -> tuple[Path, Path, list[Path]]:
    rel_dir = Path("NC_2025-12-03/images/upload_raw") / batch_id
    src_root = Path("/tmp/agir/LTS")
    dst_root = Path("/tmp/agir/90daydata")
    src_dir = src_root / rel_dir
    dst_dir = dst_root / rel_dir
    src_dir.mkdir(parents=True, exist_ok=True)

    files = [src_dir / "img1.raw", src_dir / "img2.raw"]
    files[0].write_text("phase2-a\n")
    files[1].write_text("phase2-b\n")
    return src_root, dst_dir, files


def refs_for_batch(src_root: Path, batch_id: str) -> tuple[str, str]:
    src = src_root / "NC_2025-12-03/images/upload_raw" / batch_id
    dst = Path(str(src).replace("/LTS/", "/90daydata/"))
    return str(src), str(dst)


def cleanup_batch(batch_id: str) -> None:
    with AgirDB() as db:
        db.orchestration.conn.execute("DELETE FROM logs.transfer_runs WHERE batch_id = %s", (batch_id,))
        db.orchestration.conn.execute("DELETE FROM source.globus_file_index WHERE batch_id = %s", (batch_id,))


def seed_lts_index(batch_id: str, src_root: Path, files: list[Path]) -> None:
    with AgirDB() as db:
        for p in files:
            rel = p.relative_to(src_root)
            db.orchestration.conn.execute(
                """
                INSERT INTO source.globus_file_index (
                    endpoint, site, storage_domain, namespace, storage_root,
                    rel_path, full_path, parent_dir, file_name, entry_type, file_ext,
                    size_bytes, permissions, checksum, batch_id, batch_state, batch_date,
                    data_state
                )
                VALUES (
                    'local', 'NCSU', 'dash_agir', 'LTS', %s,
                    %s, %s, %s, %s, 'file', 'raw',
                    %s, '644', NULL, %s, 'new', DATE '2025-12-03',
                    'semifield-upload'
                )
                """,
                (
                    str(src_root),
                    str(rel),
                    str(p),
                    str(p.parent),
                    p.name,
                    p.stat().st_size,
                    batch_id,
                ),
            )


def main() -> int:
    load_env_from_file(Path("pg_coords.env"))

    stage_batch_id = f"000_PHASE2_STAGE_{uuid.uuid4().hex[:8]}"
    q9_batch_id = f"000_PHASE2_Q9_{uuid.uuid4().hex[:8]}"
    src_root, dst_dir, stage_files = make_seed_paths(stage_batch_id)
    _, _, q9_files = make_seed_paths(q9_batch_id)

    try:
        cleanup_batch(stage_batch_id)
        cleanup_batch(q9_batch_id)
        seed_lts_index(stage_batch_id, src_root, stage_files)
        seed_lts_index(q9_batch_id, src_root, q9_files)

        with AgirDB() as db:
            # 1) Appears in Q8
            q8 = db.orchestration.get_batches_needing_input_staging(limit=500, stages=["raw_to_jpg"])
            assert any(r["batch_id"] == stage_batch_id for r in q8), "Stage batch missing from Q8 staging candidates"
            assert any(r["batch_id"] == q9_batch_id for r in q8), "Q9 batch missing from Q8 staging candidates"
            print(f"[OK] Batches appear in Q8: {stage_batch_id}, {q9_batch_id}")

            # 2) Not ready before staging
            ready_before = db.orchestration.get_ready_work(limit=500, stages=["raw_to_jpg"])
            assert not any(r["batch_id"] == stage_batch_id for r in ready_before), "Stage batch unexpectedly ready before staging"
            assert not any(r["batch_id"] == q9_batch_id for r in ready_before), "Q9 batch unexpectedly ready before staging"
            print(f"[OK] Batches not in ready_work before staging")

            # 3) Q9 direct test: created -> already_active
            q9_src, q9_dst = refs_for_batch(src_root, q9_batch_id)
            req1 = db.orchestration.request_input_transfer(
                batch_id=q9_batch_id,
                stage="raw_to_jpg",
                transfer_profile_id="raw_to_jpg.default",
                src_lts_ref=q9_src,
                dst_staging_ref=q9_dst,
                requested_by="phase2-test",
                priority=100,
            )
            assert req1.get("state") == "created", f"Expected created, got {req1}"
            req2 = db.orchestration.request_input_transfer(
                batch_id=q9_batch_id,
                stage="raw_to_jpg",
                transfer_profile_id="raw_to_jpg.default",
                src_lts_ref=q9_src,
                dst_staging_ref=q9_dst,
                requested_by="phase2-test",
                priority=100,
            )
            assert req2.get("state") == "already_active", f"Expected already_active, got {req2}"
            print(f"[OK] Q9 created -> already_active idempotency works: {q9_batch_id}")

        moved = run_input_staging_once(limit=2, requested_by="phase2-test")
        assert moved == 1, f"Expected one staged batch, got {moved}"
        print(f"[OK] Staging loop moved one batch: {stage_batch_id}")

        # 4) Files exist in /90daydata
        assert (dst_dir / "img1.raw").exists(), "Missing staged file img1.raw"
        assert (dst_dir / "img2.raw").exists(), "Missing staged file img2.raw"
        print(f"[OK] Files copied to /90daydata: {dst_dir}")

        with AgirDB() as db:
            # 5) Q9 returns already_completed after staging
            stage_src, stage_dst = refs_for_batch(src_root, stage_batch_id)
            req3 = db.orchestration.request_input_transfer(
                batch_id=stage_batch_id,
                stage="raw_to_jpg",
                transfer_profile_id="raw_to_jpg.default",
                src_lts_ref=stage_src,
                dst_staging_ref=stage_dst,
                requested_by="phase2-test",
                priority=100,
            )
            assert req3.get("state") == "already_completed", f"Expected already_completed, got {req3}"
            print(f"[OK] Q9 already_completed works after staging: {stage_batch_id}")

            # 6) Not in Q8 after staging
            q8_after = db.orchestration.get_batches_needing_input_staging(limit=500, stages=["raw_to_jpg"])
            assert not any(r["batch_id"] == stage_batch_id for r in q8_after), "Stage batch still appears in Q8 after staging"
            print(f"[OK] Stage batch removed from Q8: {stage_batch_id}")

            # 7) Ready after staging
            ready_after = db.orchestration.get_ready_work(limit=500, stages=["raw_to_jpg"])
            assert any(r["batch_id"] == stage_batch_id for r in ready_after), "Stage batch not present in ready_work after staging"
            print(f"[OK] Stage batch appears in ready_work: {stage_batch_id}")

            # transfer log status
            tr = db.orchestration.conn.fetch_one(
                """
                SELECT status, error_summary
                FROM logs.transfer_runs
                WHERE batch_id = %s AND stage = 'raw_to_jpg'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (stage_batch_id,),
            )
            assert tr is not None and tr["status"] == "completed", f"Transfer status is not completed: {tr}"
            print(f"[OK] Transfer completed in logs.transfer_runs: {stage_batch_id}")

        print("\nPASS: Phase 2 orchestration test passed.")
        return 0
    finally:
        cleanup_batch(stage_batch_id)
        cleanup_batch(q9_batch_id)


if __name__ == "__main__":
    raise SystemExit(main())
