from __future__ import annotations

import sqlite3
from pathlib import Path

from orchestrator.input_staging_planner import (
    STAGE_INPUT_SPECS,
    plan_input_staging,
    requests_as_dicts,
)
from orchestrator.sqlite_db import (
    get_input_staging_requests,
    mark_input_staging_status,
    request_input_staging,
)


def make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "pipeline.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    schema = Path("schemas/sqlite/pipeline.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


def insert_indexed_file(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    data_state: str,
    file_ext: str,
    parent_dir: str,
    site: str = "JUNO",
    storage_root: str = "/LTS/project/dash_agir",
) -> None:
    rel_path = f"{data_state}/{batch_id}/{parent_dir}/image.{file_ext}"
    conn.execute(
        """
        INSERT INTO globus_file_index (
            endpoint, site, storage_domain, namespace, storage_root,
            rel_path, full_path, parent_dir, file_name,
            entry_type, file_ext, size_bytes, permissions, checksum,
            batch_id, batch_state, batch_date, data_state, is_current
        )
        VALUES (?, ?, 'dash_agir', 'LTS', ?, ?, ?, ?, ?, 'file', ?, 12, '644',
                NULL, ?, 'NC', '2025-01-01', ?, 1)
        """,
        (
            f"{site.lower()}-endpoint",
            site,
            storage_root,
            rel_path,
            f"{storage_root}/{rel_path}",
            parent_dir,
            f"image.{file_ext}",
            file_ext,
            batch_id,
            data_state,
        ),
    )
    conn.commit()


def test_plan_input_staging_for_raw_to_jpg(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn,
        batch_id="NC_2025-01-01",
        data_state="semifield-upload",
        file_ext="raw",
        parent_dir="",
    )
    cfg = {
        "paths": {"input_staging_root": "/90daydata/dash_agir/semifield-upload"},
        "transfer": {
            "juno_endpoint": "juno-uuid",
            "ceres_endpoint": "ceres-uuid",
            "routes": {
                "raw_to_jpg": {
                    "source_root_juno": "/LTS/project/dash_agir/semifield-upload",
                    "destination_root": "/90daydata/dash_agir/semifield-upload",
                }
            },
        },
    }

    requests = plan_input_staging(conn, cfg, stage="raw_to_jpg")

    assert requests_as_dicts(requests) == [
        {
            "batch_id": "NC_2025-01-01",
            "stage": "raw_to_jpg",
            "src_endpoint": "juno-uuid",
            "dst_endpoint": "ceres-uuid",
            "src_path": "/LTS/project/dash_agir/semifield-upload/NC_2025-01-01",
            "dst_path": "/90daydata/dash_agir/semifield-upload/NC_2025-01-01",
            "priority": 100,
        }
    ]


def test_stage_input_specs_define_current_stages() -> None:
    assert STAGE_INPUT_SPECS["raw_to_jpg"].readiness_view == "v_batches_needing_raw_to_jpg"
    assert STAGE_INPUT_SPECS["raw_to_jpg"].source_subdir == ""
    assert STAGE_INPUT_SPECS["raw_to_jpg"].destination_subdir == ""

    assert STAGE_INPUT_SPECS["jpg_to_det"].readiness_view == "v_batches_needing_jpg_to_det"
    assert STAGE_INPUT_SPECS["jpg_to_det"].source_subdir == "images"
    assert STAGE_INPUT_SPECS["jpg_to_det"].destination_subdir == "images"


def test_plan_input_staging_for_jpg_to_det_with_input_subdir(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn,
        batch_id="NC_2025-01-02",
        data_state="semifield-developed-images",
        file_ext="jpg",
        parent_dir="images",
    )
    cfg = {
        "paths": {"input_staging_root": "/90daydata/dash_agir/semifield-developed-images"},
        "transfer": {
            "juno_endpoint": "juno-uuid",
            "atlas_endpoint": "atlas-uuid",
            "routes": {
                "jpg_to_det": {
                    "source_root_juno": "/LTS/project/dash_agir/semifield-developed-images",
                    "priority": 50,
                }
            },
        },
    }

    requests = plan_input_staging(conn, cfg, stage="jpg_to_det")

    assert requests_as_dicts(requests) == [
        {
            "batch_id": "NC_2025-01-02",
            "stage": "jpg_to_det",
            "src_endpoint": "juno-uuid",
            "dst_endpoint": "atlas-uuid",
            "src_path": "/LTS/project/dash_agir/semifield-developed-images/NC_2025-01-02/images",
            "dst_path": "/90daydata/dash_agir/semifield-developed-images/NC_2025-01-02/images",
            "priority": 50,
        }
    ]


def test_request_input_staging_is_idempotent_and_reopenable(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    kwargs = {
        "batch_id": "NC_2025-01-03",
        "stage": "raw_to_jpg",
        "src_endpoint": "juno-uuid",
        "dst_endpoint": "ceres-uuid",
        "src_path": "/LTS/project/dash_agir/semifield-upload/NC_2025-01-03",
        "dst_path": "/90daydata/dash_agir/semifield-upload/NC_2025-01-03",
        "requested_by": "test",
        "priority": 10,
    }

    created = request_input_staging(conn, **kwargs)
    assert created["accepted"] is True
    assert created["state"] == "created"

    active = request_input_staging(conn, **kwargs)
    assert active["accepted"] is False
    assert active["state"] == "already_active"

    submitted = mark_input_staging_status(
        conn,
        staging_id=created["staging_id"],
        status="submitted",
        globus_task_id="task-1",
    )
    assert submitted["status"] == "submitted"
    assert submitted["globus_task_id"] == "task-1"

    completed = mark_input_staging_status(
        conn,
        staging_id=created["staging_id"],
        status="completed",
    )
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None

    already_completed = request_input_staging(conn, **kwargs)
    assert already_completed["accepted"] is False
    assert already_completed["state"] == "already_completed"

    retry_kwargs = {
        **kwargs,
        "dst_path": "/90daydata/dash_agir/semifield-upload/NC_2025-01-03-retry",
    }
    retry_created = request_input_staging(conn, **retry_kwargs)
    failed = mark_input_staging_status(
        conn,
        staging_id=retry_created["staging_id"],
        status="failed",
        error_summary="retry me",
    )
    assert failed["status"] == "failed"
    assert failed["error_summary"] == "retry me"

    reopened = request_input_staging(conn, **retry_kwargs)
    assert reopened["accepted"] is True
    assert reopened["state"] == "reopened"
    assert reopened["staging_id"] == retry_created["staging_id"]

    pending = get_input_staging_requests(conn, statuses=["requested"], stage="raw_to_jpg")
    assert [row["staging_id"] for row in pending] == [retry_created["staging_id"]]
