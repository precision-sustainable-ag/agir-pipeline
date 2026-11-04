from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.promotion import promote_run_bundle
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


SAMPLE_ZIP = "det_to_seg_sample_MD_2022-06-28.zip"


def make_run_bundle(run_dir: Path) -> None:
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "a.png").write_bytes(b"mask")
    (run_dir / "run_report.json").write_text(
        json.dumps({"stage": "det_to_seg", "batch_id": "MD_2022-06-28", "exit_code": 0})
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {"items": [{"image_id": "a", "status": "ok", "artifacts": {"mask": "a.png"}}]}
        )
    )


def test_promotion_replaces_sample_zip_from_bundle(tmp_path: Path) -> None:
    run_dir = tmp_path / RUN_ID
    make_run_bundle(run_dir)
    (run_dir / SAMPLE_ZIP).write_bytes(b"new")
    promoted_zip = tmp_path / "MD_2022-06-28" / "det_to_seg" / SAMPLE_ZIP
    promoted_zip.parent.mkdir(parents=True)
    promoted_zip.write_bytes(b"old")

    result = promote_run_bundle(
        run_dir,
        tmp_path / "MD_2022-06-28" / "segmentations",
        artifacts_root=run_dir / "artifacts",
    )

    assert promoted_zip.read_bytes() == b"new"
    # Kept out of the inventory check, like the other stage-folder metadata.
    assert promoted_zip not in result.promoted_paths


def test_promotion_keeps_existing_sample_zip_for_older_bundle(tmp_path: Path) -> None:
    run_dir = tmp_path / RUN_ID
    make_run_bundle(run_dir)
    promoted_zip = tmp_path / "MD_2022-06-28" / "det_to_seg" / SAMPLE_ZIP
    promoted_zip.parent.mkdir(parents=True)
    promoted_zip.write_bytes(b"old")

    promote_run_bundle(
        run_dir,
        tmp_path / "MD_2022-06-28" / "segmentations",
        artifacts_root=run_dir / "artifacts",
    )

    assert promoted_zip.read_bytes() == b"old"
