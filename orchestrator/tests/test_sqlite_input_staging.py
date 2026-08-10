from __future__ import annotations

import sqlite3
from pathlib import Path

from orchestrator.input_staging_planner import (
    DEFAULT_IMAGE_SAMPLE_SIZE,
    STAGE_INPUT_SPECS,
    det_to_world_expected_dst_paths,
    plan_input_staging,
    requests_as_dicts,
)
from orchestrator.sqlite_db import (
    get_batches_needing_det_to_world,
    get_det_to_world_staged_batch_ids,
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


def insert_indexed_images(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    site: str,
    storage_root: str,
    file_names: list[str],
) -> None:
    """Insert several distinctly-named indexed image files (unlike
    insert_indexed_file, which always writes a single hardcoded
    ``image.<ext>``) so sampling behavior can be exercised against a
    realistic multi-file images/ directory."""
    for file_name in file_names:
        rel_path = f"semifield-developed-images/{batch_id}/images/{file_name}"
        conn.execute(
            """
            INSERT INTO globus_file_index (
                endpoint, site, storage_domain, namespace, storage_root,
                rel_path, full_path, parent_dir, file_name,
                entry_type, file_ext, size_bytes, permissions, checksum,
                batch_id, batch_state, batch_date, data_state, is_current
            )
            VALUES (?, ?, 'dash_agir', 'LTS', ?, ?, ?, 'images', ?, 'file', 'jpg', 12, '644',
                    NULL, ?, 'NC', '2025-01-01', 'semifield-developed-images', 1)
            """,
            (
                f"{site.lower()}-endpoint",
                site,
                storage_root,
                rel_path,
                f"{storage_root}/{rel_path}",
                file_name,
                batch_id,
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
            "already_satisfied": False,
            "file_names": None,
        }
    ]


def test_stage_input_specs_define_current_stages() -> None:
    assert STAGE_INPUT_SPECS["raw_to_jpg"].readiness_view == "v_batches_needing_raw_to_jpg"
    assert STAGE_INPUT_SPECS["raw_to_jpg"].source_subdir == ""
    assert STAGE_INPUT_SPECS["raw_to_jpg"].destination_subdir == ""

    assert STAGE_INPUT_SPECS["jpg_to_det"].readiness_view == "v_batches_needing_jpg_to_det"
    assert STAGE_INPUT_SPECS["jpg_to_det"].subdirs == ("images",)

    assert STAGE_INPUT_SPECS["det_to_world"].readiness_view == "v_batches_needing_det_to_world"
    assert STAGE_INPUT_SPECS["det_to_world"].subdirs == ("images", "detections")


def _jpg_to_det_cfg() -> dict:
    return {
        "paths": {"input_staging_root": "/90daydata/dash_agir/semifield-developed-images"},
        "transfer": {
            "juno_endpoint": "juno-uuid",
            "atlas_endpoint": "atlas-uuid",
            "ceres_endpoint": "ceres-uuid",
            "routes": {
                "jpg_to_det": {
                    "destination_site": "ATLAS",
                    "source_root_ceres": "/90daydata/dash_agir/semifield-developed-images",
                    "source_root_juno": "/LTS/project/dash_agir/semifield-developed-images",
                }
            },
        },
    }


def test_plan_input_staging_for_jpg_to_det_falls_back_to_juno(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn,
        batch_id="NC_2025-01-02",
        data_state="semifield-developed-images",
        file_ext="jpg",
        parent_dir="images",
        site="JUNO",
    )
    cfg = _jpg_to_det_cfg()
    cfg["transfer"]["routes"]["jpg_to_det"]["priority"] = 50

    requests = plan_input_staging(conn, cfg, stage="jpg_to_det", site=None)

    assert requests_as_dicts(requests) == [
        {
            "batch_id": "NC_2025-01-02",
            "stage": "jpg_to_det",
            "src_endpoint": "juno-uuid",
            "dst_endpoint": "atlas-uuid",
            "src_path": "/LTS/project/dash_agir/semifield-developed-images/NC_2025-01-02/images",
            "dst_path": "/90daydata/dash_agir/semifield-developed-images/NC_2025-01-02/images",
            "priority": 50,
            "already_satisfied": False,
            "file_names": None,
        }
    ]


def test_plan_input_staging_for_jpg_to_det_prefers_ceres_over_juno(tmp_path: Path) -> None:
    # Real-world layout: raw_to_jpg (CERES) writes JPGs there before
    # jpg_to_det (ATLAS) needs them — CERES should win over JUNO LTS.
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-01-05", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-01-05", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="CERES",
        storage_root="/90daydata/dash_agir",
    )

    requests = requests_as_dicts(
        plan_input_staging(conn, _jpg_to_det_cfg(), stage="jpg_to_det", site=None)
    )

    assert len(requests) == 1
    assert requests[0]["src_endpoint"] == "ceres-uuid"
    assert requests[0]["src_path"] == "/90daydata/dash_agir/semifield-developed-images/NC_2025-01-05/images"


def test_plan_input_staging_for_jpg_to_det_marks_already_local_satisfied(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-01-06", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )

    requests = plan_input_staging(conn, _jpg_to_det_cfg(), stage="jpg_to_det", site=None)

    assert len(requests) == 1
    assert requests[0].already_satisfied is True
    assert requests[0].src_path == requests[0].dst_path


def test_plan_input_staging_for_jpg_to_det_transfers_full_directory_not_sampled(
    tmp_path: Path,
) -> None:
    # Regression guard: det_to_world's "images" subdir is sampled for
    # visualization, but jpg_to_det's detector reads every image, so its
    # images subdir must always be a full recursive transfer (file_names
    # stays None) even though the subdir has the same name.
    conn = make_conn(tmp_path)
    n_source_images = DEFAULT_IMAGE_SAMPLE_SIZE + 12
    insert_indexed_images(
        conn,
        batch_id="NC_2025-01-07",
        site="JUNO",
        storage_root="/LTS/project/dash_agir",
        file_names=[f"img_{i:03d}.jpg" for i in range(n_source_images)],
    )

    requests = plan_input_staging(conn, _jpg_to_det_cfg(), stage="jpg_to_det", site=None)

    assert len(requests) == 1
    assert requests[0].file_names is None


def _det_to_world_cfg() -> dict:
    return {
        "paths": {
            "input_staging_root": "/90daydata/dash_agir/semifield-developed-images",
            "grid_root": "/90daydata/dash_agir/semifield-asfm",
        },
        "transfer": {
            "juno_endpoint": "juno-uuid",
            "atlas_endpoint": "atlas-uuid",
            "ceres_endpoint": "ceres-uuid",
            "routes": {
                "det_to_world": {
                    "destination_site": "ATLAS",
                    "source_root_atlas": "/90daydata/dash_agir/semifield-developed-images",
                    "source_root_ceres": "/90daydata/dash_agir/semifield-developed-images",
                    "source_root_juno": "/LTS/project/dash_agir/semifield-developed-images",
                    "source_root_grids_ceres": "/90daydata/dash_agir/semifield-asfm",
                    "source_root_grids_juno": "/LTS/project/dash_agir/semifield-asfm",
                }
            },
        },
    }


def test_readiness_view_requires_images_detections_and_grids(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-06-01", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-01", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections",
    )

    # No grids yet: batch should not appear.
    assert get_batches_needing_det_to_world(conn, batch_ids=["NC_2025-06-01"]) == []

    insert_indexed_file(
        conn, batch_id="NC_2025-06-01", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids",
    )
    rows = get_batches_needing_det_to_world(conn, batch_ids=["NC_2025-06-01"])
    assert len(rows) == 1
    assert rows[0]["grid_count"] == 1
    assert rows[0]["georef_count"] == 0

    # Once georeferenced output exists, the batch drops out of readiness.
    insert_indexed_file(
        conn, batch_id="NC_2025-06-01", data_state="semifield-developed-images",
        file_ext="csv", parent_dir="georeferenced",
    )
    assert get_batches_needing_det_to_world(conn, batch_ids=["NC_2025-06-01"]) == []


def test_det_to_world_expected_dst_paths() -> None:
    cfg = _det_to_world_cfg()
    assert det_to_world_expected_dst_paths(cfg, "NC_2025-06-02") == [
        "/90daydata/dash_agir/semifield-developed-images/NC_2025-06-02/images",
        "/90daydata/dash_agir/semifield-developed-images/NC_2025-06-02/detections",
        "/90daydata/dash_agir/semifield-asfm/NC_2025-06-02",
    ]


def test_get_det_to_world_staged_batch_ids_requires_all_pieces_completed(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    expected = {
        "NC_2025-06-02": [
            "/90daydata/dash_agir/semifield-developed-images/NC_2025-06-02/images",
            "/90daydata/dash_agir/semifield-developed-images/NC_2025-06-02/detections",
            "/90daydata/dash_agir/semifield-asfm/NC_2025-06-02",
        ]
    }

    assert get_det_to_world_staged_batch_ids(conn, expected_dst_paths=expected) == set()

    for dst_path in expected["NC_2025-06-02"][:2]:
        created = request_input_staging(
            conn,
            batch_id="NC_2025-06-02",
            stage="det_to_world",
            src_endpoint="atlas-uuid",
            dst_endpoint="ceres-uuid",
            src_path=dst_path,
            dst_path=dst_path,
        )
        mark_input_staging_status(conn, staging_id=created["staging_id"], status="completed")

    # Only 2 of 3 expected pieces completed -> not ready yet.
    assert get_det_to_world_staged_batch_ids(conn, expected_dst_paths=expected) == set()

    grids_path = expected["NC_2025-06-02"][2]
    created = request_input_staging(
        conn,
        batch_id="NC_2025-06-02",
        stage="det_to_world",
        src_endpoint="ceres-uuid",
        dst_endpoint="ceres-uuid",
        src_path=grids_path,
        dst_path=grids_path,
    )
    mark_input_staging_status(conn, staging_id=created["staging_id"], status="completed")

    assert get_det_to_world_staged_batch_ids(conn, expected_dst_paths=expected) == {"NC_2025-06-02"}


def test_plan_input_staging_for_det_to_world_falls_back_to_juno(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-06-03", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-03", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-03", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="JUNO",
    )

    requests = requests_as_dicts(
        plan_input_staging(conn, _det_to_world_cfg(), stage="det_to_world", site=None)
    )

    assert {r["dst_path"] for r in requests} == {
        "/90daydata/dash_agir/semifield-developed-images/NC_2025-06-03/images",
        "/90daydata/dash_agir/semifield-developed-images/NC_2025-06-03/detections",
        "/90daydata/dash_agir/semifield-asfm/NC_2025-06-03",
    }
    assert all(r["src_endpoint"] == "juno-uuid" for r in requests)


def test_plan_input_staging_for_det_to_world_prefers_ceres_per_subdir(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-06-04", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-04", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="CERES",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-04", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="JUNO",
    )

    requests = requests_as_dicts(
        plan_input_staging(conn, _det_to_world_cfg(), stage="det_to_world", site=None)
    )
    by_subdir = {r["dst_path"].rsplit("/", 1)[-1]: r for r in requests}

    assert by_subdir["images"]["src_endpoint"] == "juno-uuid"
    assert by_subdir["detections"]["src_endpoint"] == "ceres-uuid"


def test_plan_input_staging_for_det_to_world_marks_already_local_pieces_satisfied(
    tmp_path: Path,
) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-06-05", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-05", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-05", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )

    requests = plan_input_staging(conn, _det_to_world_cfg(), stage="det_to_world", site=None)

    # Nothing to transfer, but a request per piece is still planned so
    # stage_inputs.py records each as an immediately-completed staged_inputs
    # row (see StagingRequest.already_satisfied) — no requests are simply
    # dropped.
    assert len(requests) == 3
    assert all(r.already_satisfied for r in requests)
    assert all(r.src_path == r.dst_path for r in requests)


def test_plan_input_staging_for_det_to_world_stages_grids_from_ceres(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-06-06", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-06", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-06", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="CERES",
        storage_root="/90daydata/dash_agir",
    )

    requests = requests_as_dicts(
        plan_input_staging(conn, _det_to_world_cfg(), stage="det_to_world", site=None)
    )

    # images/detections already on ATLAS -> already_satisfied requests;
    # only the grid piece needs an actual transfer.
    assert len(requests) == 3
    by_key = {r["dst_path"].rsplit("/", 1)[-1]: r for r in requests}
    assert by_key["images"]["already_satisfied"] is True
    assert by_key["detections"]["already_satisfied"] is True

    grid_req = by_key["NC_2025-06-06"]
    assert grid_req["already_satisfied"] is False
    assert grid_req["src_endpoint"] == "ceres-uuid"
    assert grid_req["src_path"] == "/90daydata/dash_agir/semifield-asfm/NC_2025-06-06"
    assert grid_req["dst_path"] == "/90daydata/dash_agir/semifield-asfm/NC_2025-06-06"


def test_plan_input_staging_for_det_to_world_uses_destination_site_endpoint(tmp_path: Path) -> None:
    # CPU-only det_to_world commonly runs on CERES, not ATLAS — dst_endpoint
    # must follow destination_site, not default to whichever of
    # atlas_endpoint/ceres_endpoint happens to be configured first.
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="NC_2025-06-07", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-07", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-07", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="JUNO",
    )

    cfg = _det_to_world_cfg()
    cfg["transfer"]["routes"]["det_to_world"]["destination_site"] = "CERES"

    requests = requests_as_dicts(
        plan_input_staging(conn, cfg, stage="det_to_world", site=None)
    )
    assert requests, "expected staging requests to be planned"
    assert all(r["dst_endpoint"] == "ceres-uuid" for r in requests)


def test_plan_input_staging_for_det_to_world_stages_images_detections_from_atlas(
    tmp_path: Path,
) -> None:
    # Real-world layout: jpg_to_det (GPU) promotes images/detections on
    # ATLAS; det_to_world (CPU-only) runs on CERES and needs them fetched
    # over. Grids are already on CERES (from ASFM), so only images/
    # detections should be planned, sourced from ATLAS.
    conn = make_conn(tmp_path)
    insert_indexed_file(
        conn, batch_id="MD_2026-03-19", data_state="semifield-developed-images",
        file_ext="jpg", parent_dir="images", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="MD_2026-03-19", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="ATLAS",
        storage_root="/90daydata/dash_agir",
    )
    insert_indexed_file(
        conn, batch_id="MD_2026-03-19", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="CERES",
        storage_root="/90daydata/dash_agir",
    )

    cfg = _det_to_world_cfg()
    cfg["transfer"]["routes"]["det_to_world"]["destination_site"] = "CERES"

    requests = requests_as_dicts(
        plan_input_staging(conn, cfg, stage="det_to_world", site=None)
    )

    # Grids already on CERES (the destination) -> already_satisfied request;
    # images+detections need a real transfer, both sourced from ATLAS.
    assert len(requests) == 3
    by_key = {r["dst_path"].rsplit("/", 1)[-1]: r for r in requests}
    assert by_key["images"]["src_endpoint"] == "atlas-uuid"
    assert by_key["images"]["already_satisfied"] is False
    assert by_key["detections"]["src_endpoint"] == "atlas-uuid"
    assert by_key["detections"]["already_satisfied"] is False
    assert by_key["MD_2026-03-19"]["already_satisfied"] is True
    assert all(r["dst_endpoint"] == "ceres-uuid" for r in requests)


def test_plan_input_staging_for_det_to_world_samples_images_for_visualization(
    tmp_path: Path,
) -> None:
    conn = make_conn(tmp_path)
    n_source_images = DEFAULT_IMAGE_SAMPLE_SIZE + 12
    all_file_names = [f"img_{i:03d}.jpg" for i in range(n_source_images)]
    insert_indexed_images(
        conn,
        batch_id="NC_2025-06-08",
        site="JUNO",
        storage_root="/LTS/project/dash_agir",
        file_names=all_file_names,
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-08", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-08", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="JUNO",
    )

    requests = plan_input_staging(conn, _det_to_world_cfg(), stage="det_to_world", site=None)
    by_key = {r.dst_path.rsplit("/", 1)[-1]: r for r in requests}

    # Only a sample of images is staged (DEFAULT_IMAGE_SAMPLE_SIZE), not all
    # of them — the CLI never reads image pixels, so the full directory
    # would be wasted transfer. detections/grids are unaffected (file_names
    # stays None).
    images_req = by_key["images"]
    assert images_req.file_names is not None
    assert len(images_req.file_names) == DEFAULT_IMAGE_SAMPLE_SIZE
    assert set(images_req.file_names).issubset(set(all_file_names))
    assert by_key["detections"].file_names is None
    assert by_key["NC_2025-06-08"].file_names is None

    # Deterministic: replanning the same batch picks the same sample.
    requests_again = plan_input_staging(conn, _det_to_world_cfg(), stage="det_to_world", site=None)
    images_req_again = {r.dst_path.rsplit("/", 1)[-1]: r for r in requests_again}["images"]
    assert set(images_req_again.file_names) == set(images_req.file_names)


def test_plan_input_staging_for_det_to_world_respects_configured_sample_size(
    tmp_path: Path,
) -> None:
    conn = make_conn(tmp_path)
    insert_indexed_images(
        conn,
        batch_id="NC_2025-06-09",
        site="JUNO",
        storage_root="/LTS/project/dash_agir",
        file_names=[f"img_{i:03d}.jpg" for i in range(20)],
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-09", data_state="semifield-developed-images",
        file_ext="txt", parent_dir="detections", site="JUNO",
    )
    insert_indexed_file(
        conn, batch_id="NC_2025-06-09", data_state="semifield-asfm",
        file_ext="npz", parent_dir="pixel_world_grids", site="JUNO",
    )

    cfg = _det_to_world_cfg()
    cfg["transfer"]["routes"]["det_to_world"]["image_sample_size"] = 3

    requests = plan_input_staging(conn, cfg, stage="det_to_world", site=None)
    by_key = {r.dst_path.rsplit("/", 1)[-1]: r for r in requests}
    assert len(by_key["images"].file_names) == 3


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
