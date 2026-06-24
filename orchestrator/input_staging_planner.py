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
    get_batches_needing_jpg_to_det,
    get_batches_needing_raw_to_jpg,
)


@dataclass(frozen=True)
class StageInputSpec:
    stage_name: str
    readiness_view: str
    source_subdir: str = ""
    destination_subdir: str = ""
    default_priority: int = 100


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
) -> List[Dict]:
    if stage not in STAGE_INPUT_SPECS:
        raise ValueError(f"Unsupported stage for input staging: {stage!r}")
    spec = STAGE_INPUT_SPECS[stage]

    if spec.readiness_view == "v_batches_needing_raw_to_jpg":
        return get_batches_needing_raw_to_jpg(conn, site=site, limit=limit)
    if spec.readiness_view == "v_batches_needing_jpg_to_det":
        return get_batches_needing_jpg_to_det(conn, site=site, limit=limit)
    raise ValueError(f"Unsupported readiness view: {spec.readiness_view!r}")


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

    wanted = set(batch_ids or [])
    rows = _rows_for_stage(conn, stage, site=site, limit=limit)

    requests: List[StagingRequest] = []
    for row in rows:
        batch_id = row["batch_id"]
        if wanted and batch_id not in wanted:
            continue
        requests.append(
            StagingRequest(
                batch_id=batch_id,
                stage=stage,
                src_endpoint=src_endpoint,
                dst_endpoint=dst_endpoint,
                src_path=_join_posix(src_root, batch_id, source_subdir),
                dst_path=_join_posix(dst_root, batch_id, input_subdir),
                priority=priority,
            )
        )
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
