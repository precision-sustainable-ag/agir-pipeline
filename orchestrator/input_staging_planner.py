"""
Pure planning helpers for SQLite-backed input staging.

The planner answers "what should move, from where, and to where" using the
SQLite readiness views plus stage config. It does not submit transfers or write
state; callers can persist requests through orchestrator.sqlite_db.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence

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

    ``require_all_staged_inputs`` selects the submission gate for stages
    whose independently planned pieces must all be completed before compute.
    """
    stage_name: str
    readiness_view: str
    source_subdir: str = ""
    destination_subdir: str = ""
    default_priority: int = 100
    subdirs: Optional[Sequence[str]] = None
    require_all_staged_inputs: bool = False


@dataclass(frozen=True)
class StagingRequest:
    batch_id: str
    stage: str
    src_endpoint: str
    dst_endpoint: str
    src_path: str
    dst_path: str
    priority: int = 100
    # True when the data was already resident at the destination at plan
    # time — src/dst are identical and there's nothing to actually transfer.
    # scripts/job/stage_inputs.py still records a staged_inputs row for
    # these (immediately marked 'completed', no Globus call), so multi-piece
    # stages like det_to_world can gate readiness on staged_inputs alone —
    # the same way raw_to_jpg/jpg_to_det do — without depending on a fresh
    # globus_file_index inventory scan.
    already_satisfied: bool = False
    # When set, only these file names (relative to src_path/dst_path) are
    # transferred, as individual items in one Globus batch transfer, instead
    # of a whole recursive directory sync — used for det_to_world's images/
    # subdir, since det_to_world only needs a handful of images for its
    # visualization sample, not every image in the batch (see
    # _sample_image_file_names / orchestrator/globus_transfer.py's batch-mode
    # command building).
    file_names: Optional[Sequence[str]] = None


STAGE_INPUT_SPECS: Dict[str, StageInputSpec] = {
    "raw_to_jpg": StageInputSpec(
        stage_name="raw_to_jpg",
        readiness_view="v_batches_needing_raw_to_jpg",
    ),
    "jpg_to_det": StageInputSpec(
        stage_name="jpg_to_det",
        readiness_view="v_batches_needing_jpg_to_det",
        subdirs=("images",),
    ),
    "det_to_world": StageInputSpec(
        stage_name="det_to_world",
        readiness_view="v_batches_needing_det_to_world",
        subdirs=("images", "detections"),
        require_all_staged_inputs=True,
    ),
}

# data_state/parent_dir matched when checking whether a site already has a
# given input subdir indexed (see _site_has_subdir). Shared by det_to_world
# (images + detections) and jpg_to_det (images only) — both stages' inputs
# live under the same semifield-developed-images convention.
_DEVELOPED_IMAGES_DATA_STATE = "semifield-developed-images"
_DET_TO_WORLD_PARENT_DIRS = {
    "images": "parent_dir = 'images'",
    "detections": "parent_dir IN ('detections', 'plant-detections', 'metadata')",
}

# ASFM pixel-to-world NPZ grids live under a separate data_state/root from
# images+detections, so they're planned with their own source/destination
# roots (see _plan_grid_request) rather than through _plan_multi_site_requests.
_GRIDS_DATA_STATE = "semifield-asfm"
_GRIDS_PARENT_DIR_SQL = "parent_dir = 'pixel_world_grids'"


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
        # site-agnostic: jpg_to_det's per-subdir multi-site resolver (see
        # _plan_multi_site_requests) checks destination/CERES/JUNO itself,
        # so scoping readiness to one --site would wrongly exclude batches
        # whose images landed on CERES or ATLAS instead of JUNO.
        return get_batches_needing_jpg_to_det(conn, site=None, limit=limit, batch_ids=batch_ids)
    if spec.readiness_view == "v_batches_needing_det_to_world":
        # site-agnostic: det_to_world's per-subdir multi-site resolver (see
        # _plan_multi_site_requests) checks destination/CERES/JUNO itself,
        # so scoping readiness to one --site would wrongly exclude batches
        # whose data landed somewhere else.
        return get_batches_needing_det_to_world(conn, site=None, limit=limit, batch_ids=batch_ids)
    raise ValueError(f"Unsupported readiness view: {spec.readiness_view!r}")


def _endpoint_for_site(transfer: Dict, site: str) -> str:
    """
    Resolve a Globus endpoint UUID for a given site name from the transfer
    config (e.g. site="CERES" -> transfer["ceres_endpoint"]).

    Used to pick the destination endpoint for det_to_world's multi-site
    staging, where destination_site is configurable (CPU-only stages like
    det_to_world run on CERES; GPU stages like jpg_to_det run on ATLAS) —
    unlike the raw_to_jpg/jpg_to_det single-route path, which can get away
    with "whichever of atlas_endpoint/ceres_endpoint is configured" since
    only one is ever set per stage config.
    """
    key = f"{site.strip().lower()}_endpoint"
    endpoint = transfer.get(key, "")
    if not endpoint:
        raise ValueError(
            f"Config transfer block must define {key!r} for destination_site={site!r}"
        )
    return endpoint


def _site_has_files(
    conn: sqlite3.Connection, batch_id: str, site: str, *, data_state: str, parent_dir_sql: str
) -> bool:
    """Check whether ``site`` currently has matching files indexed for a batch."""
    row = conn.execute(
        f"""
        SELECT 1
        FROM globus_file_index
        WHERE data_state = ?
          AND entry_type = 'file'
          AND is_current = 1
          AND site       = ?
          AND batch_id   = ?
          AND {parent_dir_sql}
        LIMIT 1
        """,
        (data_state, site, batch_id),
    ).fetchone()
    return row is not None


def _site_has_subdir(conn: sqlite3.Connection, batch_id: str, subdir: str, site: str) -> bool:
    """Check whether ``site`` currently has ``subdir`` (images|detections, used by
    det_to_world and jpg_to_det) indexed for a batch."""
    return _site_has_files(
        conn, batch_id, site,
        data_state=_DEVELOPED_IMAGES_DATA_STATE,
        parent_dir_sql=_DET_TO_WORLD_PARENT_DIRS[subdir],
    )


def _site_has_grids(conn: sqlite3.Connection, batch_id: str, site: str) -> bool:
    """Check whether ``site`` currently has ASFM pixel-to-world NPZ grids indexed for a batch."""
    return _site_has_files(
        conn, batch_id, site,
        data_state=_GRIDS_DATA_STATE,
        parent_dir_sql=_GRIDS_PARENT_DIR_SQL,
    )


# Non-JUNO sites checked (in this order, skipping whichever one is the
# destination) before falling back to JUNO LTS. ATLAS and CERES are both
# live compute clusters, so either can hold the freshest copy of a piece of
# data depending on which stage last touched it — e.g. jpg_to_det runs on
# ATLAS (GPU) while det_to_world runs on CERES (CPU-only), so a batch's
# images/detections commonly need to hop ATLAS -> CERES.
_INTERMEDIATE_FALLBACK_SITES = ("ATLAS", "CERES")


def _resolve_fallback_source(
    *,
    transfer: Dict,
    route: Dict,
    dst_site: str,
    root_key_prefix: str,
    juno_root_key: str,
    site_has_data: Callable[[str], bool],
) -> tuple[str, str, str]:
    """
    Resolve (src_endpoint, src_root, src_site) for whichever configured site
    nearest to ``dst_site`` actually has the data, checking
    ``_INTERMEDIATE_FALLBACK_SITES`` (skipping ``dst_site`` itself, already
    checked by the caller) before falling back to JUNO LTS.
    """
    for candidate_site in _INTERMEDIATE_FALLBACK_SITES:
        if candidate_site == dst_site:
            continue
        candidate_root = route.get(f"{root_key_prefix}_{candidate_site.lower()}")
        if candidate_root and site_has_data(candidate_site):
            return _endpoint_for_site(transfer, candidate_site), candidate_root, candidate_site

    juno_root = route.get(juno_root_key)
    if not juno_root:
        raise ValueError(
            f"No transfer route configured (missing routes.det_to_world.{juno_root_key})"
        )
    return transfer["juno_endpoint"], juno_root, "JUNO"


# Default number of images sampled per batch for det_to_world's (currently
# unimplemented) visualization step. The stage CLI itself never reads image
# pixels — only a small sample is needed so a future overlay viz has
# something to draw on — so images are fetched as individual files rather
# than the whole (often huge) images/ directory. Override per-config via
# transfer.routes.det_to_world.image_sample_size.
DEFAULT_IMAGE_SAMPLE_SIZE = 20


def _sample_image_file_names(
    conn: sqlite3.Connection, batch_id: str, site: str, sample_size: int
) -> List[str]:
    """
    Pick up to ``sample_size`` image file names indexed for ``batch_id`` at
    ``site``, deterministically (seeded by batch_id + site) so replanning the
    same batch doesn't churn the sample.
    """
    rows = conn.execute(
        """
        SELECT file_name
        FROM globus_file_index
        WHERE data_state = ?
          AND entry_type = 'file'
          AND is_current = 1
          AND site       = ?
          AND batch_id   = ?
          AND parent_dir = 'images'
        ORDER BY file_name
        """,
        (_DEVELOPED_IMAGES_DATA_STATE, site, batch_id),
    ).fetchall()
    file_names = [row["file_name"] for row in rows]
    if not file_names or sample_size <= 0:
        return []

    rng = random.Random(f"det_to_world:{batch_id}:{site}")
    return rng.sample(file_names, min(sample_size, len(file_names)))


def _plan_grid_request(
    conn: sqlite3.Connection, cfg: Dict, *, batch_id: str, priority: int
) -> StagingRequest:
    """
    Plan a request to fetch ASFM pixel-to-world NPZ grids for one batch.

    Grids live under a different data_state/root (semifield-asfm) than
    images/detections and are transferred whole-batch-directory (they're
    nested under per-time-range sub-batch dirs — see
    stages/det_to_world/remapper.py's GridCache — rather than one flat
    subdir), so this is planned separately from _plan_multi_site_requests.

    Always returns a request, even when grids are already resident at the
    destination — see StagingRequest.already_satisfied.
    """
    paths = cfg["paths"]
    transfer = cfg["transfer"]
    route = transfer.get("routes", {}).get("det_to_world", {})

    dst_root = route.get("grid_destination_root") or paths.get("grid_root")
    if not dst_root:
        raise ValueError("Config paths block must define grid_root for det_to_world grid staging")
    dst_site = route.get("destination_site", "ATLAS")
    dst_endpoint = _endpoint_for_site(transfer, dst_site)
    dst_path = _join_posix(dst_root, batch_id)

    if _site_has_grids(conn, batch_id, dst_site):
        return StagingRequest(
            batch_id=batch_id,
            stage="det_to_world",
            src_endpoint=dst_endpoint,
            dst_endpoint=dst_endpoint,
            src_path=dst_path,
            dst_path=dst_path,
            priority=priority,
            already_satisfied=True,
        )

    src_endpoint, src_root, _src_site = _resolve_fallback_source(
        transfer=transfer,
        route=route,
        dst_site=dst_site,
        root_key_prefix="source_root_grids",
        juno_root_key="source_root_grids_juno",
        site_has_data=lambda site: _site_has_grids(conn, batch_id, site),
    )

    return StagingRequest(
        batch_id=batch_id,
        stage="det_to_world",
        src_endpoint=src_endpoint,
        dst_endpoint=dst_endpoint,
        src_path=_join_posix(src_root, batch_id),
        dst_path=dst_path,
        priority=priority,
    )


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
    destination cluster, or may need pulling from another site before
    falling back to JUNO. Used by stages with ``StageInputSpec.subdirs`` set
    (currently only det_to_world).

    Each subdir is resolved independently: if the destination site already
    has it, an already_satisfied request is planned (see
    StagingRequest.already_satisfied); otherwise the nearest available
    source (see _INTERMEDIATE_FALLBACK_SITES, then JUNO) is used.
    """
    paths = cfg["paths"]
    transfer = cfg["transfer"]
    route = transfer.get("routes", {}).get(stage, {})

    dst_root = route.get("destination_root") or paths["input_staging_root"]
    dst_site = route.get("destination_site", "ATLAS")
    dst_endpoint = _endpoint_for_site(transfer, dst_site)
    priority = int(route.get("priority", priority))

    requests: List[StagingRequest] = []
    for subdir in subdirs:
        dst_path = _join_posix(dst_root, batch_id, subdir)

        if _site_has_subdir(conn, batch_id, subdir, dst_site):
            requests.append(
                StagingRequest(
                    batch_id=batch_id,
                    stage=stage,
                    src_endpoint=dst_endpoint,
                    dst_endpoint=dst_endpoint,
                    src_path=dst_path,
                    dst_path=dst_path,
                    priority=priority,
                    already_satisfied=True,
                )
            )
            continue

        src_endpoint, src_root, src_site = _resolve_fallback_source(
            transfer=transfer,
            route=route,
            dst_site=dst_site,
            root_key_prefix="source_root",
            juno_root_key="source_root_juno",
            site_has_data=lambda site, subdir=subdir: _site_has_subdir(conn, batch_id, subdir, site),
        )

        file_names: Optional[Sequence[str]] = None
        if stage == "det_to_world" and subdir == "images":
            # Only det_to_world's images subdir is a visualization sample —
            # the stage CLI itself never reads image pixels. Other stages
            # with an "images" subdir (e.g. jpg_to_det) need every file, so
            # file_names stays None below, which build_transfer_command()
            # treats as a full recursive directory transfer.
            sample_size = int(route.get("image_sample_size", DEFAULT_IMAGE_SAMPLE_SIZE))
            file_names = tuple(
                _sample_image_file_names(conn, batch_id, src_site, sample_size)
            )

        requests.append(
            StagingRequest(
                batch_id=batch_id,
                stage=stage,
                src_endpoint=src_endpoint,
                dst_endpoint=dst_endpoint,
                src_path=_join_posix(src_root, batch_id, subdir),
                dst_path=dst_path,
                priority=priority,
                file_names=file_names,
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
    paths for single-route stages (e.g. raw_to_jpg). Use ``source_subdir``
    when the source subdirectory differs from the destination. Stages with
    ``StageInputSpec.subdirs`` set (jpg_to_det, det_to_world) instead resolve
    each subdir independently via ``_plan_multi_site_requests`` — see there
    for that config shape (``destination_site``, ``source_root_<site>``).
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
            batch_id = row["batch_id"]
            requests.extend(
                _plan_multi_site_requests(
                    conn,
                    cfg,
                    stage=stage,
                    batch_id=batch_id,
                    subdirs=spec.subdirs,
                    priority=spec.default_priority,
                )
            )
            if stage == "det_to_world":
                requests.append(
                    _plan_grid_request(conn, cfg, batch_id=batch_id, priority=spec.default_priority)
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
            "already_satisfied": r.already_satisfied,
            "file_names": list(r.file_names) if r.file_names else None,
        }
        for r in requests
    ]


def expected_staged_input_dst_paths(cfg: Dict, stage: str, batch_id: str) -> List[str]:
    """
    Return the ``staged_inputs.dst_path`` values planned for one stage/batch.

    This mirrors the path construction in ``plan_input_staging`` so a
    submission gate can check the exact destinations the planner records.
    ``det_to_world`` retains its separate grids destination in addition to
    its developed-images subdirectories.
    """
    if stage not in STAGE_INPUT_SPECS:
        raise ValueError(f"Unsupported stage for input staging: {stage!r}")

    spec = STAGE_INPUT_SPECS[stage]
    paths = cfg["paths"]
    transfer = cfg["transfer"]
    route = transfer.get("routes", {}).get(stage)
    if route is None:
        raise ValueError(f"No transfer route configured for stage {stage!r}")

    input_root = route.get("destination_root") or paths["input_staging_root"]
    if spec.subdirs:
        expected = [_join_posix(input_root, batch_id, subdir) for subdir in spec.subdirs]
    else:
        input_subdir = route.get("input_subdir", spec.destination_subdir)
        expected = [_join_posix(input_root, batch_id, input_subdir)]

    if stage == "det_to_world":
        grid_root = route.get("grid_destination_root") or paths.get("grid_root")
        if not grid_root:
            raise ValueError("Config paths block must define grid_root for det_to_world")
        expected.append(_join_posix(grid_root, batch_id))

    return expected


def det_to_world_expected_dst_paths(cfg: Dict, batch_id: str) -> List[str]:
    """Compatibility wrapper for the stage-aware expected-path helper."""
    return expected_staged_input_dst_paths(cfg, "det_to_world", batch_id)
