"""
Input manifest helpers for manifest-driven orchestration.

Input manifests define the exact workset for a stage/window. Directories are
storage locations; manifests are the source of truth for what a job should run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from uuid import uuid4


def build_manifest_run_id() -> str:
    """Return a unique id for one input-staging manifest attempt."""
    return uuid4().hex


def build_input_manifest_path(
    manifest_root: str | Path,
    stage: str,
    batch_id: str,
    window_key: str,
    manifest_run_id: Optional[str] = None,
) -> Path:
    """Return input manifest path for a stage/batch/window.

    ``manifest_run_id`` makes each staging attempt addressable in
    ``logs.transfer_runs``.  When omitted, the legacy canonical filename is
    returned for backwards compatibility.
    """
    filename = (
        f"input_manifest.{manifest_run_id}.json"
        if manifest_run_id
        else "input_manifest.json"
    )
    return Path(manifest_root) / stage / batch_id / window_key / filename




def build_staging_report_path(
    manifest_root: str | Path,
    stage: str,
    batch_id: str,
    window_key: str,
    manifest_run_id: str | None = None,
) -> Path:
    """Return staging report path for a stage/batch/window."""
    filename = (
        f"staging_report.{manifest_run_id}.json"
        if manifest_run_id
        else "staging_report.json"
    )
    return Path(manifest_root) / stage / batch_id / window_key / filename




def derive_image_id(file_name: str) -> str:
    """Derive a stable item ID from a filename by stripping its extension.

    Works for any file type (RAW, JPG, TXT, etc.). The stem of the filename
    is used as the canonical ID for that item across pipeline stages.
    """
    return Path(file_name).stem


def build_input_manifest(
    *,
    stage: str,
    batch_id: str,
    window_key: str,
    start_epoch: int,
    end_epoch: int,
    input_root: str,
    files: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build an input manifest for files staged into a flat batch directory.
 
     Works for any stage and file type. Each item's ID is derived from its
     filename stem via ``derive_image_id``.
     """
    items: List[Dict[str, Any]] = []

    for f in files:
        file_name = f["file_name"]
        items.append(
            {
                "image_id": derive_image_id(file_name),
                "file_name": file_name,
                "fname_ts_epoch": f.get("fname_ts_epoch"),
                "source_path": f["full_path"],
                "staged_path": f"{input_root.rstrip('/')}/{file_name}",
                "size_bytes": f.get("size_bytes"),
                "status": "staged",
            }
        )

    return {
        "schema_version": 1,
        "manifest_kind": "stage_inputs",
        "stage": stage,
        "batch_id": batch_id,
        "window_key": window_key,
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "input_root": input_root,
        "items": items,
    }


def write_json(path: str | Path, payload: Dict[str, Any]) -> Path:
    """Write JSON payload with stable formatting."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return path


def load_input_manifest(path: str | Path) -> Dict[str, Any]:
    """Load an input manifest JSON file."""
    return json.loads(Path(path).read_text())


def validate_input_manifest(
    manifest: Dict[str, Any],
    *,
    expected_stage: Optional[str] = None,
    expected_batch_id: Optional[str] = None,
    expected_window_key: Optional[str] = None,
) -> None:
    """Validate minimal input manifest shape and identity fields."""
    required = [
        "schema_version",
        "manifest_kind",
        "stage",
        "batch_id",
        "window_key",
        "input_root",
        "items",
    ]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"input manifest missing required fields: {missing}")

    if manifest["manifest_kind"] != "stage_inputs":
        raise ValueError(
            f"input manifest manifest_kind must be 'stage_inputs', got {manifest['manifest_kind']!r}"
        )

    if expected_stage is not None and manifest["stage"] != expected_stage:
        raise ValueError(
            f"input manifest stage mismatch: expected {expected_stage!r}, got {manifest['stage']!r}"
        )

    if expected_batch_id is not None and manifest["batch_id"] != expected_batch_id:
        raise ValueError(
            f"input manifest batch_id mismatch: expected {expected_batch_id!r}, got {manifest['batch_id']!r}"
        )

    if expected_window_key is not None and manifest["window_key"] != expected_window_key:
        raise ValueError(
            "input manifest window_key mismatch: "
            f"expected {expected_window_key!r}, got {manifest['window_key']!r}"
        )

    if not isinstance(manifest["items"], list):
        raise ValueError("input manifest items must be a list")

    if not manifest["items"]:
        raise ValueError("input manifest contains no items")

    for idx, item in enumerate(manifest["items"]):
        for key in ["image_id", "file_name", "staged_path", "status"]:
            if key not in item:
                raise ValueError(f"input manifest item {idx} missing required field {key!r}")