"""Shared promotion logic for Atlas jobs and Ceres result synchronization."""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from orchestrator.artifact_validation import validate_promotable_run_bundle


@dataclass(frozen=True)
class PromotionResult:
    artifact_count: int
    destination: Path
    metadata_destination: Path


def _rewrite_run_report(report: dict, dest: Path, meta_dest: Path) -> dict:
    rewritten = copy.deepcopy(report)
    outputs = rewritten.get("outputs", {})
    old_artifacts_dir = outputs.get("artifacts_dir", "")
    outputs.update(
        output_root=str(meta_dest.parent),
        run_root=str(meta_dest),
        artifacts_dir=str(dest),
        report_path=str(meta_dest / "run_report.json"),
        manifest_path=str(meta_dest / "manifest.json"),
    )
    for entry in outputs.get("artifacts", []):
        path = entry.get("path", "")
        if path == old_artifacts_dir:
            entry["path"] = str(dest)
        elif old_artifacts_dir and path.startswith(old_artifacts_dir):
            entry["path"] = str(dest / Path(path).relative_to(old_artifacts_dir))

    inputs_manifest = rewritten.get("inputs", {}).get("inputs_manifest_path")
    if inputs_manifest:
        rewritten["inputs"]["inputs_manifest_path"] = str(
            meta_dest / Path(inputs_manifest).name
        )
    logs_path = rewritten.get("pointers", {}).get("logs_path")
    if logs_path:
        rewritten["pointers"]["logs_path"] = str(meta_dest / Path(logs_path).name)
    return rewritten


def _copy_metadata_file(
    run_dir: Path,
    recorded_path: str | None,
    destination: Path,
) -> None:
    if not recorded_path:
        return
    recorded = Path(recorded_path)
    source = run_dir / recorded.name
    if not source.exists():
        source = recorded
    if source.is_file():
        shutil.copy2(source, destination / source.name)


def promote_run_bundle(
    run_dir: Path,
    dest: Path,
    *,
    artifacts_root: Path | None = None,
) -> PromotionResult:
    """Validate and promote one successful run bundle."""
    validated = validate_promotable_run_bundle(
        run_dir,
        artifacts_root=artifacts_root,
    )
    report = validated.run_report
    manifest = validated.manifest
    stage = report.get("stage", "unknown_stage")
    batch_id = report.get("batch_id", "")

    dest.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for item in validated.items:
        for artifact_rel in (item.get("artifacts") or {}).values():
            if artifact_rel:
                source = validated.artifacts_root / artifact_rel
                shutil.copy2(source, dest / source.name)
                promoted += 1

    if batch_id:
        batch_csv = validated.artifacts_root / f"{batch_id}.csv"
        if batch_csv.is_file():
            shutil.copy2(batch_csv, dest / batch_csv.name)

    metadata_dest = dest.parent / stage
    metadata_dest.mkdir(parents=True, exist_ok=True)
    (metadata_dest / "run_report.json").write_text(
        json.dumps(_rewrite_run_report(report, dest, metadata_dest), indent=2),
        encoding="utf-8",
    )
    promoted_manifest = copy.deepcopy(manifest)
    promoted_manifest["artifacts_root"] = str(dest)
    (metadata_dest / "manifest.json").write_text(
        json.dumps(promoted_manifest, indent=2),
        encoding="utf-8",
    )
    _copy_metadata_file(
        run_dir,
        report.get("pointers", {}).get("logs_path"),
        metadata_dest,
    )
    _copy_metadata_file(
        run_dir,
        report.get("inputs", {}).get("inputs_manifest_path"),
        metadata_dest,
    )
    return PromotionResult(promoted, dest, metadata_dest)
