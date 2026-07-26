"""Reusable validation for manifest-backed pipeline run artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ArtifactValidationError(ValueError):
    """Raised when a run bundle is not safe to promote or synchronize."""

    def __init__(self, message: str, *, outcome: str = "fail") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class ValidatedRunBundle:
    """Loaded run metadata and the verified artifact location."""

    run_report: dict[str, Any]
    manifest: dict[str, Any]
    items: list[dict[str, Any]]
    artifacts_root: Path


def sha256_file(path: Path) -> str:
    """Return a pipeline-formatted SHA-256 checksum for ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_run_bundle(
    run_dir: Path,
    *,
    artifacts_root: Path | None = None,
) -> ValidatedRunBundle:
    """
    Load and validate a successful, manifest-backed pipeline run.

    ``artifacts_root`` overrides the location recorded in the manifest. This
    allows the same validation to be applied after artifacts are copied to a
    different storage endpoint.
    """
    run_report_path = run_dir / "run_report.json"
    if not run_report_path.exists():
        raise ArtifactValidationError(f"run_report.json not found in {run_dir}")

    run_report = json.loads(run_report_path.read_text())
    exit_code = run_report.get("exit_code")
    if exit_code != 0:
        raise ArtifactValidationError(
            f"run_report exit_code={exit_code} (need 0)",
            outcome="skip",
        )

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise ArtifactValidationError(f"manifest.json not found in {run_dir}")

    manifest = json.loads(manifest_path.read_text())
    items = manifest.get("items", [])
    if not items:
        raise ArtifactValidationError("manifest has no items")

    failed_items = [item for item in items if item.get("status") != "ok"]
    if failed_items:
        details = [
            f"{len(failed_items)}/{len(items)} items failed — 100% success required"
        ]
        for item in failed_items:
            details.append(
                f"  FAILED: {item.get('image_id')} — "
                f"{item.get('error', {}).get('message', '')}"
            )
        raise ArtifactValidationError("\n".join(details), outcome="skip")

    resolved_artifacts_root = (
        Path(artifacts_root)
        if artifacts_root is not None
        else Path(manifest.get("artifacts_root", run_dir / "artifacts"))
    )

    for item in items:
        image_id = item.get("image_id", "<unknown>")
        artifacts = item.get("artifacts") or {}
        checksums = item.get("checksum") or {}

        if not artifacts:
            raise ArtifactValidationError(
                f"item {image_id} has no artifacts in manifest"
            )

        for artifact_key, artifact_rel in artifacts.items():
            if not artifact_rel:
                raise ArtifactValidationError(
                    f"item {image_id} key '{artifact_key}' has no path"
                )

            artifact_path = resolved_artifacts_root / artifact_rel
            if not artifact_path.exists():
                raise ArtifactValidationError(
                    f"artifact missing on disk: {artifact_path}"
                )

            expected_checksum = checksums.get(artifact_key)
            if expected_checksum:
                actual_checksum = sha256_file(artifact_path)
                if actual_checksum != expected_checksum:
                    raise ArtifactValidationError(
                        f"checksum mismatch for {image_id} [{artifact_key}]\n"
                        f"  expected: {expected_checksum}\n"
                        f"  actual:   {actual_checksum}"
                    )

    return ValidatedRunBundle(
        run_report=run_report,
        manifest=manifest,
        items=items,
        artifacts_root=resolved_artifacts_root,
    )
