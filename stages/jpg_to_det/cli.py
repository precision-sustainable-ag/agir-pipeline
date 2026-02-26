#!/usr/bin/env python3
"""
CLI for JPG -> Detection processing pipeline.

Outputs run_report.json and manifest.json — no database interaction.
"""

import argparse
import csv
import hashlib
import logging
import subprocess
from pathlib import Path

from . import STAGE, STAGE_VERSION, ERROR_CFG_VALIDATION_FAILED, ERROR_UNKNOWN
from .processor import Processor
from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR, ITEM_OK
from stages.common import RunReportBuilder, ManifestBuilder, parse_batch_id, setup_logging

logger = logging.getLogger(__name__)


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return "sha256:" + sha256_hash.hexdigest()


def get_git_commit() -> str | None:
    """Get the current git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("Could not determine git commit hash")
        return None


def write_batch_csv(rows: list[dict], csv_path: Path) -> Path:
    """Write aggregated detection rows to a single batch CSV."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "image_id", "bounding_box_id", "xmin", "ymin", "xmax", "ymax",
        "conf", "class", "classname",
    ]

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return csv_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run multiscale YOLO plant detection on JPG images."
    )
    parser.add_argument("--c", type=Path, required=True, help="Path to detection YAML config file.")
    parser.add_argument("--m", type=Path, required=True, help="Path to YOLO model weights (.pt).")
    parser.add_argument("--i", type=Path, required=True, help="Directory containing JPG images.")
    parser.add_argument("--o", type=Path, required=True, help="Output directory.")
    parser.add_argument("--t", type=int, default=0, help="Number of parallel threads. Default 0 = sequential.")
    parser.add_argument("--fs", action="store_true", help="Stop processing on first failure.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID. Auto-inferred from input path if omitted.")
    parser.add_argument("--device", type=str, default="cpu", help="Torch device (cpu, cuda, cuda:0, etc.).")

    args = parser.parse_args()

    # Resolve batch_id
    batch_id = args.batch_id or parse_batch_id(str(args.i))
    if not batch_id:
        logger.error("Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR

    # Validate input directory
    if not args.i.exists():
        logger.error("Input directory does not exist: %s", args.i)
        return EXIT_CONFIG_ERROR
    args.o.mkdir(parents=True, exist_ok=True)

    # Set up builders
    report = RunReportBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        batch_id=batch_id,
    )
    report.start()

    run_id = report.run_id
    run_dir = args.o / STAGE / run_id
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Set up logging into run folder
    log_path = setup_logging(run_dir)
    logger.info("Run %s started for batch %s", run_id, batch_id)

    # Initialize processor
    try:
        processor = Processor(args.c, args.m, device=args.device)
    except Exception as e:
        logger.error("Failed to initialize processor: %s", e)
        report.set_stage_error(f"Processor init failed: {e}")
        report.add_error(
            unit_id="__stage__",
            code=ERROR_CFG_VALIDATION_FAILED,
            error_type=type(e).__name__,
            message=str(e),
        )
        report.stop(EXIT_CONFIG_ERROR)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        return EXIT_CONFIG_ERROR

    # Gather JPG files
    jpg_files = sorted(list(args.i.glob("*.jpg")) + list(args.i.glob("*.JPG")))
    if not jpg_files:
        logger.error("No JPG files found in %s", args.i)
        report.set_stage_error(f"No JPG files found in {args.i}")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        return EXIT_CONFIG_ERROR

    logger.info("Processing %d JPG files to %s", len(jpg_files), artifacts_dir)

    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
    )

    report.set_provenance(
        config_path=args.c,
        code_commit=get_git_commit(),
        model_id=str(args.m),
    )
    report.set_inputs(
        input_root=str(jpg_files[0].parent) if jpg_files else None,
        n_units_discovered=len(jpg_files),
    )

    # Process batch
    try:
        results = processor.process_batch(
            jpg_images=jpg_files,
            output_dir=artifacts_dir,
            fail_stop=args.fs,
            max_workers=args.t,
        )
    except Exception as e:
        logger.error("Batch processing failed: %s", e)
        report.set_stage_error(f"Batch processing failed: {e}")
        report.add_error(
            unit_id="__stage__",
            code=ERROR_UNKNOWN,
            error_type=type(e).__name__,
            message=str(e),
        )
        results = []

    # Populate manifest and aggregate CSV rows
    num_succeeded = 0
    num_failed = 0
    all_detection_rows = []

    for r in results:
        if r.status == ITEM_OK:
            num_succeeded += 1
            all_detection_rows.extend(r.detection_rows)

            txt_rel = str(r.txt_path.relative_to(artifacts_dir)) if r.txt_path else None

            checksum = {}
            size_bytes = {}
            # calculate checksum and byte size for validation
            if r.txt_path and r.txt_path.exists():
                checksum["det_txt_path"] = calculate_sha256(r.txt_path)
                size_bytes["det_txt_path"] = r.txt_path.stat().st_size

            manifest.add_ok_item(
                image_id=r.image_id,
                artifacts={"det_txt_path": txt_rel},
                checksum=checksum,
                size_bytes=size_bytes,
            )
        else:
            num_failed += 1
            logger.error("Image %s failed: [%s] %s", r.image_id, r.error_code, r.error_message)
            manifest.add_failed_item(
                image_id=r.image_id,
                error_type=r.error_type,
                message=r.error_message,
                retryable=r.retryable,
            )
            report.add_error(
                unit_id=r.image_id,
                code=r.error_code,
                error_type=r.error_type,
                message=r.error_message,
                retryable=r.retryable,
            )

    # Write batch CSV
    csv_path = None
    if all_detection_rows:
        csv_path = write_batch_csv(all_detection_rows, artifacts_dir / f"{batch_id}.csv")
        logger.info("Wrote batch CSV with %d rows to %s", len(all_detection_rows), csv_path)

        # Add csv path to all OK manifest items
        csv_rel = str(csv_path.relative_to(artifacts_dir))
        for item in manifest.items:
            if item.get("status") == "ok":
                item["artifacts"]["det_csv_path"] = csv_rel

    # Determine exit code
    num_total = len(jpg_files)
    if num_succeeded == num_total:
        exit_code = EXIT_SUCCESS
    elif num_succeeded > 0:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_FAILURE

    logger.info("Finished: %d/%d files processed successfully", num_succeeded, num_total)

    # Finalize report
    report.stop(exit_code)
    report.set_outputs(
        output_root=str(args.o),
        run_root=str(run_dir),
        artifacts_dir=str(artifacts_dir),
        n_succeeded=num_succeeded,
        n_failed=num_failed,
    )
    report.add_artifact_type(
        artifact_type="detection_txt",
        path=str(artifacts_dir),
        n_files=num_succeeded,
    )
    if csv_path:
        report.add_artifact_type(
            artifact_type="detection_csv",
            path=str(csv_path),
            n_files=1,
        )
    report.set_pointers(logs_path=str(log_path))

    # Write output JSON files
    report_path = report.write(run_dir / "run_report.json")
    manifest_path = manifest.write(run_dir / "manifest.json")

    logger.info("Report:   %s", report_path)
    logger.info("Manifest: %s", manifest_path)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
