#!/usr/bin/env python3
"""
CLI for RAW -> DNG -> JPG processing pipeline.

Outputs run_report.json and manifest.json — no database interaction.
"""

import argparse
import json
import hashlib
import uuid
import datetime
from pathlib import Path
from .processor import Processor
from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR

STAGE = "raw_to_jpg"


def build_run_report(
    run_id: str,
    raw_files: list,
    results: list,
    output_dir: Path,
    config_path: Path,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    exit_code: int,
) -> dict:
    """Build a run_report dict following the contract schema."""
    succeeded = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status == "failed"]

    status = "success" if exit_code == EXIT_SUCCESS else "partial" if exit_code == EXIT_PARTIAL else "failure"

    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()

    return {
        "run_report_version": "1.0",
        "stage": STAGE,
        "run_id": run_id,
        "pipeline_run_id": None,
        "batch_id": None,
        "orchestrator_id": None,

        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "exit_code": exit_code,

        "status": status,
        "scope": "image",

        "provenance": {
            "code_commit": None,
            "build_id": None,
            "config_path": str(config_path),
            "config_hash": f"sha256:{config_hash}",
            "model_id": None,
            "deps_id": None,
            "container_image": None,
        },

        "inputs": {
            "input_root": str(raw_files[0].parent) if raw_files else None,
            "n_units_discovered": len(raw_files),
            "unit_id_kind": "image_id",
            "inputs_manifest_path": None,
        },

        "outputs": {
            "output_root": str(output_dir),
            "run_root": str(output_dir / STAGE / run_id),
            "artifacts_dir": str(output_dir),
            "report_path": str(output_dir / STAGE / run_id / "run_report.json"),
            "manifest_path": str(output_dir / STAGE / run_id / "manifest.json"),
            "schema_version": 1,

            "counts": {
                "n_units_succeeded": len(succeeded),
                "n_units_failed": len(failed),
                "n_units_skipped": 0,
            },

            "artifacts": [
                {
                    "artifact_type": "jpg",
                    "path": str(output_dir),
                    "n_files": len(succeeded),
                }
            ],
        },

        "stage_error": None,

        "errors": [
            {
                "unit_id": r.image_id,
                "code": "CONVERSION_FAILED",
                "type": r.error_type,
                "message": r.error_message,
                "retryable": r.retryable,
                "meta": {},
            }
            for r in failed
        ],

        "warnings": [],

        "pointers": {
            "errors_path": None,
            "warnings_path": None,
            "logs_path": None,
        },
    }


def build_manifest(run_id: str, results: list, output_dir: Path) -> dict:
    """Build a manifest dict following the contract schema."""
    items = []
    for r in results:
        if r.status == "ok":
            jpg_rel = str(r.jpg_path.relative_to(output_dir)) if r.jpg_path else None
            items.append({
                "image_id": r.image_id,
                "status": "ok",
                "artifacts": {
                    "jpg_path": jpg_rel,
                },
                "checksum": {},
                "size_bytes": {},
            })
        else:
            items.append({
                "image_id": r.image_id,
                "status": "failed",
                "error": {
                    "error_type": r.error_type,
                    "message": r.error_message,
                    "retryable": r.retryable,
                },
            })

    return {
        "stage": STAGE,
        "run_id": run_id,
        "batch_id": None,
        "schema_version": 1,
        "artifacts_root": str(output_dir),
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process RAW images to DNG and then to JPG using a camera config."
    )
    parser.add_argument("--c", type=Path, required=True, help="Path to camera YAML configuration file.")
    parser.add_argument("--i", type=Path, required=True, help="Directory containing RAW images to process.")
    parser.add_argument("--o", type=Path, required=True, help="Directory where JPG images will be saved.")
    parser.add_argument("--t", type=int, default=0, help="Number of parallel threads. Default 0 = sequential processing.")
    parser.add_argument("--fs", action="store_true", help="Stop processing on first failure.")

    args = parser.parse_args()

    # Validate directories
    if not args.i.exists():
        print(f"Input directory does not exist: {args.i}")
        return EXIT_CONFIG_ERROR
    args.o.mkdir(parents=True, exist_ok=True)

    # Initialize processor
    try:
        processor = Processor(args.c)
    except Exception as e:
        print(f"Failed to load config: {e}")
        return EXIT_CONFIG_ERROR

    # Gather raw files
    raw_files = list(args.i.glob("*.RAW"))
    if not raw_files:
        print(f"No RAW files found in {args.i}")
        return EXIT_CONFIG_ERROR

    print(f"Processing {len(raw_files)} RAW files to {args.o} ...")

    run_id = str(uuid.uuid4())
    started_at = datetime.datetime.now(datetime.UTC).isoformat()

    # Process batch
    try:
        results = processor.process_batch(
            raw_images=raw_files,
            output_dir=args.o,
            fail_stop=args.fs,
            max_workers=args.t
        )
    except Exception as e:
        print(f"Batch processing failed: {e}")
        results = []

    ended_at = datetime.datetime.now(datetime.UTC).isoformat()
    duration_ms = int(
        (datetime.datetime.fromisoformat(ended_at) - datetime.datetime.fromisoformat(started_at)).total_seconds() * 1000
    )

    # Determine exit code
    num_succeeded = sum(1 for r in results if r.status == "ok")
    num_total = len(raw_files)

    if num_succeeded == num_total:
        exit_code = EXIT_SUCCESS
    elif num_succeeded > 0:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_FAILURE

    print(f"Finished: {num_succeeded}/{num_total} files processed successfully.")

    # Write run_report.json and manifest.json
    run_dir = args.o / STAGE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    report = build_run_report(
        run_id=run_id,
        raw_files=raw_files,
        results=results,
        output_dir=args.o,
        config_path=args.c,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        exit_code=exit_code,
    )

    manifest = build_manifest(run_id=run_id, results=results, output_dir=args.o)

    report_path = run_dir / "run_report.json"
    manifest_path = run_dir / "manifest.json"

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Report:   {report_path}")
    print(f"Manifest: {manifest_path}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
