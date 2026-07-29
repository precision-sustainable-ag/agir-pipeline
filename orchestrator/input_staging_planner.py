"""
Pure planning helpers for SQLite-backed input staging.

The planner answers "what should move, from where, and to where" using the
SQLite readiness views plus stage config. It does not submit transfers or write
state; callers can persist requests through orchestrator.sqlite_db.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence

from orchestrator.sqlite_db import (
    get_batches_needing_det_to_world,
    get_batches_needing_jpg_to_det,
    get_batches_needing_raw_to_jpg,
)


@dataclass(frozen=True)
class StageInputSpec:
    """
    Defines how a pipeline stage finds staged input data.

    Empty subdir values mean the stage uses the root source/destination
    directory directly.

    ``subdirs``, when set, marks a stage that needs multiple independent
    pieces of data (e.g. det_to_world needs both ``images`` and
    ``detections``), each resolved against the nearest site that already has
    it (destination cluster, then CERES, then JUNO) rather than a single
    fixed Juno route. When set, ``source_subdir``/``destination_subdir`` are
    unused.
    """
    stage_name: str
    readiness_view: str
    source_subdir: str = ""
    destination_subdir: str = ""
    default_priority: int = 100
    subdirs: Optional[Sequence[str]] = None


@dataclass(frozen=True)
class StagingRequest:
    batch_id: str
    stage: str
    src_endpoint: str
    dst_endpoint: str
    src_path: str
    dst_path: str
    priority: int = 100


STAGE_INPUT_SPECS: Dict[str, StageInputSpec] = {
    "raw_to_jpg": StageInputSpec(
        stage_name="raw_to_jpg",
        readiness_view="v_batches_needing_raw_to_jpg",
    ),
    "jpg_to_det": StageInputSpec(
        stage_name="jpg_to_det",
        readiness_view="v_batches_needing_jpg_to_det",
        source_subdir="images",
        destination_subdir="images",
    ),
    "det_to_world": StageInputSpec(
        stage_name="det_to_world",
        readiness_view="v_batches_needing_det_to_world",
        subdirs=("images", "detections"),
    ),
}

# data_state/parent_dir matched when checking whether a site already has a
# given det_to_world input subdir indexed (see _site_has_subdir).
_DET_TO_WORLD_PARENT_DIRS = {
    "images": "parent_dir = 'images'",
    "detections": "parent_dir IN ('detections', 'plant-detections', 'metadata')",
}


def _join_posix(*parts: str) -> str:
    cleaned = [str(p).strip("/") for p in parts if str(p or "").strip("/")]
    if not cleaned:
        return ""
    prefix = "/" if str(parts[0]).startswith("/") else ""
    return prefix + str(PurePosixPath(*cleaned))


def _rows_for_stage(
    conn: sqlite3.Connection,
    stage: str,
    *,
    site: Optional[str],
    limit: int,
    batch_ids: Optional[Sequence[str]] = None,
) -> List[Dict]:
    if stage not in STAGE_INPUT_SPECS:
        raise ValueError(f"Unsupported stage for input staging: {stage!r}")
    spec = STAGE_INPUT_SPECS[stage]

    if spec.readiness_view == "v_batches_needing_raw_to_jpg":
        return get_batches_needing_raw_to_jpg(conn, site=site, limit=limit, batch_ids=batch_ids)
    if spec.readiness_view == "v_batches_needing_jpg_to_det":
        return get_batches_needing_jpg_to_det(conn, site=site, limit=limit, batch_ids=batch_ids)
    if spec.readiness_view == "v_batches_needing_det_to_world":
        return get_batches_needing_det_to_world(conn, site=site, limit=limit, batch_ids=batch_ids)
    raise ValueError(f"Unsupported readiness view: {spec.readiness_view!r}")


def _site_has_subdir(conn: sqlite3.Connection, batch_id: str, subdir: str, site: str) -> bool:
    """Check whether ``site`` currently has ``subdir`` (images|detections) indexed for a batch."""
    parent_dir_sql = _DET_TO_WORLD_PARENT_DIRS[subdir]
    row = conn.execute(
        f"""
        SELECT 1
        FROM globus_file_index
        WHERE data_state = 'semifield-developed-images'
          AND entry_type = 'file'
          AND is_current = 1
          AND site       = ?
          AND batch_id   = ?
          AND {parent_dir_sql}
        LIMIT 1
        """,
        (site, batch_id),
    ).fetchone()
    return row is not None


def _plan_multi_site_requests(
    conn: sqlite3.Connection,
    cfg: Dict,
    *,
    stage: str,
    batch_id: str,
    subdirs: Sequence[str],
    priority: int,
) -> List[StagingRequest]:
    """
    Plan requests for a stage whose inputs may already be resident on the
    destination cluster, or may need pulling from CERES before falling back
    to JUNO. Used by stages with ``StageInputSpec.subdirs`` set (currently
    only det_to_world).

    Each subdir is resolved independently: if the destination site already
    has it, nothing is planned for it; otherwise the nearest available
    source (CERES, then JUNO) is used.
    """
    paths = cfg["paths"]
    transfer = cfg["transfer"]
    route = transfer.get("routes", {}).get(stage, {})

    dst_root = route.get("destination_root") or paths["input_staging_root"]
    dst_site = route.get("destination_site", "ATLAS")
    dst_endpoint = transfer.get("atlas_endpoint") or transfer.get("ceres_endpoint", "")
    if not dst_endpoint:
        raise ValueError("Config transfer block must define atlas_endpoint or ceres_endpoint")

    ceres_root = route.get("source_root_ceres")
    juno_root = route.get("source_root_juno")

    requests: List[StagingRequest] = []
    for subdir in subdirs:
        if _site_has_subdir(conn, batch_id, subdir, dst_site):
            continue  # already resident on the destination cluster

        if ceres_root and _site_has_subdir(conn, batch_id, subdir, "CERES"):
            src_endpoint = transfer.get("ceres_endpoint", "")
            src_root = ceres_root
        else:
            if not juno_root:
                raise ValueError(
                    f"No transfer route configured for stage {stage!r} subdir {subdir!r} "
                    "(missing routes.<stage>.source_root_juno)"
                )
            src_endpoint = transfer["juno_endpoint"]
            src_root = juno_root

        requests.append(
            StagingRequest(
                batch_id=batch_id,
                stage=stage,
                src_endpoint=src_endpoint,
                dst_endpoint=dst_endpoint,
                src_path=_join_posix(src_root, batch_id, subdir),
                dst_path=_join_posix(dst_root, batch_id, subdir),
                priority=priority,
            )
        )
    return requests


def plan_input_staging(
    conn: sqlite3.Connection,
    cfg: Dict,
    *,
    stage: str,
    site: Optional[str] = "JUNO",
    limit: int = 200,
    batch_ids: Optional[Sequence[str]] = None,
) -> List[StagingRequest]:
    """
    Build input staging requests from SQLite readiness rows and config.

    The config contract matches the existing submit config:
    ``paths.input_staging_root`` is the 90daydata destination root and
    ``transfer.routes.<stage>.source_root_juno`` is the JUNO/LTS source root.
    ``transfer.routes.<stage>.input_subdir`` is optionally appended to both
    paths for stages such as jpg_to_det. Use ``source_subdir`` when the source
    subdirectory differs from the destination.
    """
    if stage not in STAGE_INPUT_SPECS:
        raise ValueError(f"Unsupported stage for input staging: {stage!r}")

    spec = STAGE_INPUT_SPECS[stage]

    wanted = set(batch_ids or [])
    # When targeting specific batches, don't let the default/passed limit
    # truncate the readiness query before the batch_id filter is applied.
    effective_limit = max(limit, len(wanted)) if wanted else limit
    rows = _rows_for_stage(conn, stage, site=site, limit=effective_limit, batch_ids=batch_ids)
    wanted_rows = [row for row in rows if not wanted or row["batch_id"] in wanted]

    if spec.subdirs:
        # Multi-input stage: each subdir is resolved independently against
        # the nearest site that already has it, rather than one fixed route.
        requests: List[StagingRequest] = []
        for row in wanted_rows:
            requests.extend(
                _plan_multi_site_requests(
                    conn,
                    cfg,
                    stage=stage,
                    batch_id=row["batch_id"],
                    subdirs=spec.subdirs,
                    priority=spec.default_priority,
                )
            )
        return requests

    paths = cfg["paths"]
    transfer = cfg["transfer"]
    routes = transfer.get("routes", {})
    if stage not in routes:
        raise ValueError(f"No transfer route configured for stage {stage!r}")

    route = routes[stage]
    src_root = route["source_root_juno"]
    dst_root = route.get("destination_root") or paths["input_staging_root"]
    input_subdir = route.get("input_subdir", spec.destination_subdir)
    source_subdir = route.get("source_subdir", spec.source_subdir)
    priority = int(route.get("priority", spec.default_priority))

    src_endpoint = transfer["juno_endpoint"]
    dst_endpoint = transfer.get("atlas_endpoint") or transfer.get("ceres_endpoint", "")
    if not dst_endpoint:
        raise ValueError("Config transfer block must define atlas_endpoint or ceres_endpoint")

    requests = [
        StagingRequest(
            batch_id=row["batch_id"],
            stage=stage,
            src_endpoint=src_endpoint,
            dst_endpoint=dst_endpoint,
            src_path=_join_posix(src_root, row["batch_id"], source_subdir),
            dst_path=_join_posix(dst_root, row["batch_id"], input_subdir),
            priority=priority,
        )
        for row in wanted_rows
    ]
    return requests


def requests_as_dicts(requests: Iterable[StagingRequest]) -> List[Dict]:
    """Serialize staging requests for logs, dry-run output, or tests."""
    return [
        {
            "batch_id": r.batch_id,
            "stage": r.stage,
            "src_endpoint": r.src_endpoint,
            "dst_endpoint": r.dst_endpoint,
            "src_path": r.src_path,
            "dst_path": r.dst_path,
            "priority": r.priority,
        }
        for r in requests
    ]
