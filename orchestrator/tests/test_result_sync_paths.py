from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from orchestrator import result_sync
from orchestrator.result_sync import (
    ResultSyncRequestError,
    _is_registered,
    _require_atlas_bundle_path,
    validate_request_route,
)

ROOT = "/90daydata/dash_agir/tmp/stage_runs"
RUN_ID = "0325fd31-57f7-437a-9cb9-7d67617c0f5f"
CONFIG = SimpleNamespace(atlas_run_root=ROOT, ceres_run_root=ROOT)


def make_request(src_path: str, stage: str = "det_to_seg") -> dict:
    return {
        "run": {"run_id": RUN_ID, "stage": stage},
        "run_bundle": {
            "recursive": True,
            "src_path": src_path,
            "dst_path": f"{ROOT}/{RUN_ID}",
        },
    }


def test_stage_directory_accepted() -> None:
    _require_atlas_bundle_path(
        f"{ROOT}/det_to_seg/{RUN_ID}", ROOT, "src", stage="det_to_seg", allow_flat=False
    )


def test_flat_rejected_for_new_requests() -> None:
    with pytest.raises(ResultSyncRequestError, match=r"det_to_seg/<run_id>"):
        _require_atlas_bundle_path(
            f"{ROOT}/{RUN_ID}", ROOT, "src", stage="det_to_seg", allow_flat=False
        )


def test_flat_accepted_when_allowed() -> None:
    _require_atlas_bundle_path(
        f"{ROOT}/{RUN_ID}", ROOT, "src", stage="jpg_to_det", allow_flat=True
    )


@pytest.mark.parametrize(
    "path",
    [
        f"{ROOT}/jpg_to_det/{RUN_ID}",  # another stage's directory
        f"{ROOT}/det_to_seg/extra/{RUN_ID}",  # too deep
        ROOT,  # the root itself
        f"/90daydata/dash_agir/elsewhere/det_to_seg/{RUN_ID}",  # outside the root
    ],
)
def test_other_layouts_rejected(path: str) -> None:
    with pytest.raises(ResultSyncRequestError):
        _require_atlas_bundle_path(path, ROOT, "src", stage="det_to_seg", allow_flat=True)


def test_validate_request_route_uses_request_stage() -> None:
    validate_request_route(make_request(f"{ROOT}/det_to_seg/{RUN_ID}"), CONFIG)
    with pytest.raises(ResultSyncRequestError):
        validate_request_route(make_request(f"{ROOT}/det_to_seg/{RUN_ID}", "jpg_to_det"), CONFIG)


def test_validate_request_route_flat_only_when_allowed() -> None:
    flat = make_request(f"{ROOT}/{RUN_ID}", "jpg_to_det")
    with pytest.raises(ResultSyncRequestError):
        validate_request_route(flat, CONFIG)
    validate_request_route(flat, CONFIG, allow_flat_src=True)


@pytest.mark.parametrize("allow_flat", [False, True])
def test_validate_request_route_still_requires_run_id_leaf(allow_flat: bool) -> None:
    other = "11111111-2222-3333-4444-555555555555"
    for path in (f"{ROOT}/det_to_seg/{other}", f"{ROOT}/det_to_seg"):
        with pytest.raises(ResultSyncRequestError):
            validate_request_route(make_request(path), CONFIG, allow_flat_src=allow_flat)


def test_is_registered() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE result_syncs (run_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO result_syncs VALUES (?)", (RUN_ID,))
    assert _is_registered(conn, RUN_ID)
    assert not _is_registered(conn, "not-registered")


def _requested_row(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "status": "requested",
        "stage": "det_to_seg",
        "batch_id": "NC_2025-03-06",
        "src_endpoint": "atlas",
        "dst_endpoint": "ceres",
        "src_path": f"{ROOT}/det_to_seg/{run_id}",
        "dst_path": f"{ROOT}/{run_id}",
        "recursive": 1,
    }


def _submit_bundles(monkeypatch, rows, mark_func) -> tuple[list, list]:
    submitted: list[str] = []

    def fake_submit(request, **kwargs):
        submitted.append(request.label)
        return SimpleNamespace(
            status="submitted",
            globus_task_id=f"task-{len(submitted)}",
            details="ok",
        )

    monkeypatch.setattr(result_sync, "get_result_syncs", lambda conn, **kw: rows)
    monkeypatch.setattr(result_sync, "_validate_sync_row_route", lambda sync, config: None)
    monkeypatch.setattr(result_sync, "mark_result_sync_status", mark_func)
    outcomes = result_sync.submit_requested_bundle_transfers(
        None,
        CONFIG,
        limit=10,
        submit_func=fake_submit,
        sleep_func=lambda seconds: None,
    )
    return outcomes, submitted


def test_submit_retries_locked_status_write(monkeypatch) -> None:
    calls: list[dict] = []

    def flaky_mark(conn, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"status": kwargs["status"], "globus_task_id": kwargs["globus_task_id"]}

    outcomes, submitted = _submit_bundles(monkeypatch, [_requested_row(RUN_ID)], flaky_mark)

    assert len(submitted) == 1
    assert len(calls) == 2
    assert [(o.status, o.globus_task_id, o.is_error) for o in outcomes] == [
        ("transferring", "task-1", False)
    ]


def test_submit_stops_when_status_write_stays_locked(monkeypatch) -> None:
    other = "11111111-2222-3333-4444-555555555555"

    def locked_mark(conn, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    outcomes, submitted = _submit_bundles(
        monkeypatch,
        [_requested_row(RUN_ID), _requested_row(other)],
        locked_mark,
    )

    assert len(submitted) == 1
    assert [(o.run_id, o.status, o.globus_task_id, o.is_error) for o in outcomes] == [
        (RUN_ID, "unrecorded", "task-1", True)
    ]
