"""
RunReport and Manifest builders for pipeline stages.

Every stage produces two JSON contracts:
  - run_report.json  — summary of the entire run
  - manifest.json    — per-item artifact detail

These builders accumulate state during a run and serialize the
final dicts.  They are intentionally DB-agnostic.

Output schemas match:
  - docs/examples/run_report_example.json
  - docs/examples/manifest_example.json
"""

import hashlib
import json
import uuid
import datetime
from pathlib import Path

from stages import EXIT_CODE_TO_STATUS


class RunReportBuilder:
    """
    Builds a run_report dict matching docs/examples/run_report_example.json.

    Usage:
        rb = RunReportBuilder(stage="raw_to_jpg", stage_version="1")
        rb.start()
        rb.set_provenance(config_path=cfg)
        rb.set_inputs(input_root="/data/raw", n_units_discovered=100)
        rb.add_error(unit_id="IMG_001", code="CONVERSION_FAILED", ...)
        rb.stop(exit_code=0)
        rb.set_outputs(output_root=out, run_root=run_dir, artifacts_dir=art_dir, ...)
        rb.write(path)
    """

    def __init__(
        self,
        stage: str,
        stage_version: str,
        run_id: str = None,
        batch_id: str = None,
        pipeline_run_id: str = None,
        orchestrator_id: str = None,
        window_key: str = None,
    ):
        self.stage = stage
        self.stage_version = stage_version
        self.run_id = run_id or str(uuid.uuid4())
        self.batch_id = batch_id
        self.pipeline_run_id = pipeline_run_id
        self.orchestrator_id = orchestrator_id
        self.window_key = window_key or ""

        self._started_at = None
        self._ended_at = None
        self._duration_ms = None
        self._exit_code = None
        self._status = None

        self._provenance = {
            "code_commit": None,
            "build_id": None,
            "config_path": None,
            "config_hash": None,
            "model_id": None,
            "deps_id": None,
            "container_image": None,
        }

        self._inputs = {
            "input_root": None,
            "n_units_discovered": 0,
            "unit_id_kind": "image_id",
            "inputs_manifest_path": None,
        }

        self._outputs = {
            "output_root": None,
            "run_root": None,
            "artifacts_dir": None,
            "report_path": None,
            "manifest_path": None,
            "schema_version": 1,
            "counts": {
                "n_units_succeeded": 0,
                "n_units_failed": 0,
                "n_units_skipped": 0,
            },
            "artifacts": [],
        }

        self._stage_error = None
        self._errors = []
        self._warnings = []
        self._pointers = {
            "errors_path": None,
            "warnings_path": None,
            "logs_path": None,
        }
        self._skip = {
            "skipped": False,
            "skip_reason": None,
        }


    # Monitor Timing
    def start(self):
        """Record the run start time."""
        self._started_at = datetime.datetime.now(datetime.UTC).isoformat()
        return self

    def stop(self, exit_code: int):
        """Record the run end time and derive status from exit_code."""
        self._ended_at = datetime.datetime.now(datetime.UTC).isoformat()
        self._exit_code = exit_code
        self._duration_ms = int(
            (
                datetime.datetime.fromisoformat(self._ended_at)
                - datetime.datetime.fromisoformat(self._started_at)
            ).total_seconds()
            * 1000
        )

        # default to failure if the exit code is not valid
        self._status = EXIT_CODE_TO_STATUS.get(exit_code, "failure")
        return self

    # Set Provenance (config, code, model, etc)
    def set_provenance(
        self,
        config_path: Path = None,
        code_commit: str = None,
        build_id: str = None,
        model_id: str = None,
        deps_id: str = None,
        container_image: str = None,
    ):
        if config_path is not None:
            config_path = Path(config_path)
            self._provenance["config_path"] = str(config_path)
            self._provenance["config_hash"] = (
                "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
            )
        if code_commit is not None:
            self._provenance["code_commit"] = code_commit
        if build_id is not None:
            self._provenance["build_id"] = build_id
        if model_id is not None:
            self._provenance["model_id"] = model_id
        if deps_id is not None:
            self._provenance["deps_id"] = deps_id
        if container_image is not None:
            self._provenance["container_image"] = container_image
        return self

    # Inputs
    def set_inputs(
        self,
        input_root: str,
        n_units_discovered: int,
        unit_id_kind: str = "image_id",
        inputs_manifest_path: str = None,
    ):
        self._inputs = {
            "input_root": str(input_root),
            "n_units_discovered": n_units_discovered,
            "unit_id_kind": unit_id_kind,
            "inputs_manifest_path": inputs_manifest_path,
        }
        return self

    # Outputs
    def set_outputs(
        self,
        output_root: str,
        run_root: str,
        artifacts_dir: str,
        n_succeeded: int,
        n_failed: int,
        n_skipped: int = 0,
    ):
        self._outputs["output_root"] = str(output_root)
        self._outputs["run_root"] = str(run_root)
        self._outputs["artifacts_dir"] = str(artifacts_dir)
        self._outputs["report_path"] = str(Path(run_root) / "run_report.json")
        self._outputs["manifest_path"] = str(Path(run_root) / "manifest.json")
        self._outputs["counts"] = {
            "n_units_succeeded": n_succeeded,
            "n_units_failed": n_failed,
            "n_units_skipped": n_skipped,
        }
        return self

    def add_artifact_type(self, artifact_type: str, path: str, n_files: int):
        self._outputs["artifacts"].append(
            {"artifact_type": artifact_type, "path": str(path), "n_files": n_files}
        )
        return self

    # Errors / Warnings
    def set_stage_error(self, message: str):
        self._stage_error = message
        return self

    def add_error(
        self,
        unit_id: str,
        code: str,
        error_type: str,
        message: str,
        retryable: bool = False,
        meta: dict = None,
    ):
        self._errors.append({
            "unit_id": unit_id,
            "code": code,
            "type": error_type,
            "message": message,
            "retryable": retryable,
            "meta": meta or {},
        })
        return self

    def add_warning(
        self,
        unit_id: str,
        code: str,
        warning_type: str,
        message: str,
        meta: dict = None,
    ):
        self._warnings.append({
            "unit_id": unit_id,
            "code": code,
            "type": warning_type,
            "message": message,
            "meta": meta or {},
        })
        return self

    # Pointers - detailed output files
    def set_pointers(
        self,
        errors_path: str = None,
        warnings_path: str = None,
        logs_path: str = None,
    ):
        if errors_path is not None:
            self._pointers["errors_path"] = str(errors_path)
        if warnings_path is not None:
            self._pointers["warnings_path"] = str(warnings_path)
        if logs_path is not None:
            self._pointers["logs_path"] = str(logs_path)
        return self

    def set_skip(self, skipped: bool, skip_reason: str = None):
        self._skip["skipped"] = bool(skipped)
        self._skip["skip_reason"] = skip_reason
        return self

    # Build / Write
    def build(self) -> dict:
        """
        Return the complete run_report dict.

        Matches docs/examples/run_report_example.json exactly.
        """
        report = {
            "stage": self.stage,
            "stage_version": self.stage_version,
            "run_id": self.run_id,
            "pipeline_run_id": self.pipeline_run_id,
            "batch_id": self.batch_id,
            "window_key": self.window_key,
            "orchestrator_id": self.orchestrator_id,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "duration_ms": self._duration_ms,
            "exit_code": self._exit_code,
            "status": self._status,
            "scope": "image",
            "provenance": self._provenance,
            "inputs": self._inputs,
            "outputs": self._outputs,
            "stage_error": self._stage_error,
            "errors": self._errors,
            "warnings": self._warnings,
            "pointers": self._pointers,
        }
        if self._skip["skipped"]:
            report["skipped"] = True
            report["skip_reason"] = self._skip["skip_reason"]
        return report

    def write(self, path: Path) -> Path:
        """Serialize the run_report to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.build(), f, indent=2)
        return path


class ManifestBuilder:
    """
    Builds a manifest dict matching docs/examples/manifest_example.json.

    Usage:
        mb = ManifestBuilder(stage="raw_to_jpg", stage_version="1",
                             run_id=run_id, artifacts_root=str(art_dir))
        mb.add_ok_item(image_id="IMG_001", artifacts={"jpg_path": "..."})
        mb.add_failed_item(image_id="IMG_002", error_type="ConvertError", ...)
        mb.write(path)
    """

    def __init__(
        self,
        stage: str,
        stage_version: str,
        run_id: str,
        artifacts_root: str,
        batch_id: str = None,
        window_key: str = None,
        schema_version: int = 1,
    ):
        self.stage = stage
        self.stage_version = stage_version
        self.run_id = run_id
        self.batch_id = batch_id
        self.window_key = window_key or ""
        self.schema_version = schema_version
        self.artifacts_root = str(artifacts_root)
        self._items = []

    def add_ok_item(
        self,
        image_id: str,
        artifacts: dict,
        checksum: dict = None,
        size_bytes: dict = None,
    ):
        self._items.append({
            "image_id": image_id,
            "status": "ok",
            "artifacts": artifacts,
            "checksum": checksum or {},
            "size_bytes": size_bytes or {},
        })
        return self

    def add_failed_item(
        self,
        image_id: str,
        error_type: str,
        message: str,
        retryable: bool = False,
    ):
        self._items.append({
            "image_id": image_id,
            "status": "failed",
            "error": {
                "error_type": error_type,
                "message": message,
                "retryable": retryable,
            },
        })
        return self

    def add_skipped_item(
        self,
        image_id: str,
        message: str = None,
    ):
        item = {
            "image_id": image_id,
            "status": "skipped",
        }
        if message is not None:
            item["message"] = message
        self._items.append(item)
        return self

    @property
    def items(self) -> list:
        return list(self._items)

    def build(self) -> dict:
        """
        Return the complete manifest dict.

        Matches docs/examples/manifest_example.json exactly.
        """
        return {
            "stage": self.stage,
            "stage_version": self.stage_version,
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "window_key": self.window_key,
            "schema_version": self.schema_version,
            "artifacts_root": self.artifacts_root,
            "items": self._items,
        }

    def write(self, path: Path) -> Path:
        """Serialize the manifest to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.build(), f, indent=2)
        return path
