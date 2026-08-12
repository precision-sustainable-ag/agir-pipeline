#!/usr/bin/env python3
"""Register one completed Atlas run bundle for later Ceres synchronization.

This script performs no Globus or database work. It reads a completed run
bundle and atomically writes ``<run_id>.result-sync.json`` to the Atlas
result-sync outbox.
For explicit failures that happen before a stage creates its normal run
directory, it can first create a minimal failed run bundle.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.result_sync_request import (
    ResultSyncRequestError,
    build_result_sync_request,
    write_result_sync_request,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(started_at: str, ended_at: str) -> int:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResultSyncRequestError(
            "Failure-bundle timestamps must be ISO-8601 date-times"
        ) from exc
    if started.tzinfo is None or ended.tzinfo is None:
        raise ResultSyncRequestError("Failure-bundle timestamps must include a timezone")
    if ended < started:
        raise ResultSyncRequestError("Failure-bundle end time precedes its start time")
    return int((ended - started).total_seconds() * 1000)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _minimal_manifest(
    *,
    run_id: str,
    batch_id: str,
    stage: str,
    artifacts_dir: Path,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "stage_version": "orchestrator-failure-v1",
        "run_id": run_id,
        "batch_id": batch_id,
        "schema_version": 1,
        "artifacts_root": str(artifacts_dir),
        "items": [],
    }


def create_failure_run_bundle(
    run_bundle: Path,
    *,
    run_id: str,
    batch_id: str,
    stage: str,
    failure_reason: str,
    started_at: str,
    job_log: Path | None = None,
) -> Path:
    """Create a self-contained run bundle for a pre-stage or setup failure."""
    if run_bundle.name != run_id:
        raise ResultSyncRequestError(
            f"Failure run bundle must end in run_id {run_id!r}: {run_bundle}"
        )
    if (run_bundle / "run_report.json").exists():
        raise ResultSyncRequestError(
            f"Refusing to replace existing run report in {run_bundle}"
        )

    artifacts_dir = run_bundle / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    log_path = run_bundle / "run.log"
    if job_log is not None and job_log.exists():
        shutil.copy2(job_log, log_path)
    else:
        log_path.write_text(
            f"[{_utc_now()}] Early job failure: {failure_reason}\n",
            encoding="utf-8",
        )

    ended_at = _utc_now()
    report = {
        "run_report_version": "1.0",
        "stage": stage,
        "stage_version": "orchestrator-failure-v1",
        "run_id": run_id,
        "pipeline_run_id": None,
        "batch_id": batch_id,
        "orchestrator_id": None,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": _duration_ms(started_at, ended_at),
        "exit_code": 2,
        "status": "failed",
        "scope": "batch",
        "provenance": {
            "code_commit": "",
            "build_id": None,
            "config_path": None,
            "config_hash": None,
            "model_id": None,
            "deps_id": None,
            "container_image": None,
        },
        "inputs": {
            "input_root": "",
            "n_units_discovered": 0,
            "unit_id_kind": "image_id",
            "inputs_manifest_path": None,
        },
        "outputs": {
            "output_root": str(run_bundle.parent),
            "run_root": str(run_bundle),
            "artifacts_dir": str(artifacts_dir),
            "report_path": str(run_bundle / "run_report.json"),
            "manifest_path": str(run_bundle / "manifest.json"),
            "schema_version": 1,
            "counts": {
                "n_units_succeeded": 0,
                "n_units_failed": 0,
                "n_units_skipped": 0,
            },
            "artifacts": [],
        },
        "stage_error": {"message": failure_reason},
        "errors": [
            {
                "unit_id": batch_id,
                "code": "JOB_FINALIZATION_FAILURE",
                "type": "OrchestratorFailure",
                "message": failure_reason,
                "retryable": True,
                "meta": {},
            }
        ],
        "warnings": [],
        "pointers": {
            "errors_path": None,
            "warnings_path": None,
            "logs_path": str(log_path),
        },
    }

    _write_json(run_bundle / "run_report.json", report)
    _write_json(
        run_bundle / "manifest.json",
        _minimal_manifest(
            run_id=run_id,
            batch_id=batch_id,
            stage=stage,
            artifacts_dir=artifacts_dir,
        ),
    )
    return run_bundle


def _load_run_report(run_bundle: Path) -> dict[str, Any]:
    report_path = run_bundle / "run_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultSyncRequestError(
            f"Unable to read run report {report_path}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise ResultSyncRequestError(f"Run report must be an object: {report_path}")
    return report


def _ensure_manifest(run_bundle: Path, report: dict[str, Any]) -> None:
    manifest_path = run_bundle / "manifest.json"
    if manifest_path.exists():
        return
    if report.get("status") == "success":
        raise ResultSyncRequestError(
            f"Successful run bundle is missing manifest.json: {run_bundle}"
        )

    artifacts_dir = run_bundle / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        manifest_path,
        _minimal_manifest(
            run_id=str(report.get("run_id") or ""),
            batch_id=str(report.get("batch_id") or ""),
            stage=str(report.get("stage") or ""),
            artifacts_dir=artifacts_dir,
        ),
    )


def register_run_bundle(
    run_bundle: Path,
    *,
    outbox: Path,
    src_endpoint: str,
    dst_endpoint: str,
    dst_run_root: Path,
    promotion_succeeded: bool,
    promoted_at: str | None,
) -> Path:
    """Validate one bundle and atomically write its outbox request."""
    report = _load_run_report(run_bundle)
    _ensure_manifest(run_bundle, report)

    run_id = str(report.get("run_id") or "")
    if run_bundle.name != run_id:
        raise ResultSyncRequestError(
            f"Run bundle directory {run_bundle.name!r} does not match run_id {run_id!r}"
        )

    request = build_result_sync_request(
        run_report=report,
        promotion_succeeded=promotion_succeeded,
        promoted_at=promoted_at,
        src_endpoint=src_endpoint,
        dst_endpoint=dst_endpoint,
        src_path=run_bundle,
        dst_path=dst_run_root / run_id,
    )
    request_path = outbox / f"{run_id}.result-sync.json"
    write_result_sync_request(request_path, request)
    logger.info("Registered result sync request: %s", request_path)
    return request_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write an Atlas run-bundle request to the result-sync outbox."
    )
    parser.add_argument("--run-bundle", required=True, type=Path)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--src-endpoint", required=True)
    parser.add_argument("--dst-endpoint", required=True)
    parser.add_argument("--dst-run-root", required=True, type=Path)
    parser.add_argument("--promotion-succeeded", action="store_true")
    parser.add_argument("--promoted-at")

    parser.add_argument("--create-failure-bundle", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--stage")
    parser.add_argument("--failure-reason")
    parser.add_argument("--started-at")
    parser.add_argument("--job-log", type=Path)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    try:
        if args.create_failure_bundle:
            required = {
                "--run-id": args.run_id,
                "--batch-id": args.batch_id,
                "--stage": args.stage,
                "--failure-reason": args.failure_reason,
                "--started-at": args.started_at,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ResultSyncRequestError(
                    f"Failure bundle creation requires: {', '.join(missing)}"
                )
            if args.promotion_succeeded or args.promoted_at is not None:
                raise ResultSyncRequestError(
                    "Failure bundles cannot record successful promotion"
                )
            create_failure_run_bundle(
                args.run_bundle,
                run_id=args.run_id,
                batch_id=args.batch_id,
                stage=args.stage,
                failure_reason=args.failure_reason,
                started_at=args.started_at,
                job_log=args.job_log,
            )

        register_run_bundle(
            args.run_bundle,
            outbox=args.outbox,
            src_endpoint=args.src_endpoint,
            dst_endpoint=args.dst_endpoint,
            dst_run_root=args.dst_run_root,
            promotion_succeeded=args.promotion_succeeded,
            promoted_at=args.promoted_at,
        )
    except (OSError, ResultSyncRequestError) as exc:
        logger.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
