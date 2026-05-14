#!/usr/bin/env python3
"""
CLI for RAW -> DNG -> JPG processing pipeline.

Outputs run_report.json and manifest.json — no database interaction.
"""

import argparse
import logging
from pathlib import Path

from . import STAGE, STAGE_VERSION, ERROR_CFG_VALIDATION_FAILED, ERROR_UNKNOWN
from .processor import Processor
from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR, ITEM_OK
from stages.common import (
    RunReportBuilder,
    ManifestBuilder,
    calculate_sha256,
    get_git_commit,
    parse_batch_id,
    setup_logging,
)

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process RAW images to DNG and then to JPG using a camera config."
    )
    parser.add_argument("--c", type=Path, required=True, help="Path to camera YAML configuration file.")
    parser.add_argument("--i", type=Path, required=True, help="Directory containing RAW images to process.")
    parser.add_argument("--o", type=Path, required=True, help="Directory where JPG images will be saved.")
    parser.add_argument("--t", type=int, default=0, help="Number of parallel threads. Default 0 = sequential processing.")
    parser.add_argument("--fs", action="store_true", help="Stop processing on first failure.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID (e.g. TX_2024-06-01). Auto-inferred from input path if omitted.")
    parser.add_argument("--window-key", type=str, default="", help="Time-window key (e.g. 1760975625_1760975685). Written into run_report and manifest.")

    args = parser.parse_args()

    # Resolve batch_id: explicit flag or inferred from input path
    batch_id = args.batch_id or parse_batch_id(str(args.i))
    if not batch_id:
        logger.error("Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR

    # Validate directories
    if not args.i.exists():
        logger.error("Input directory does not exist: %s", args.i)
        return EXIT_CONFIG_ERROR
    args.o.mkdir(parents=True, exist_ok=True)

    # set up builders
    report = RunReportBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        batch_id=batch_id,
        window_key=args.window_key,
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
        processor = Processor(args.c)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        report.set_stage_error(f"Config load failed: {e}")
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

    # Gather raw files
    raw_files = list(args.i.glob("*.RAW"))
    if not raw_files:
        logger.error("No RAW files found in %s", args.i)
        report.set_stage_error(f"No RAW files found in {args.i}")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        return EXIT_CONFIG_ERROR

    logger.info("Processing %d RAW files to %s", len(raw_files), artifacts_dir)

    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
        window_key=args.window_key,
    )

    report.set_provenance(
        config_path=args.c,
        code_commit=get_git_commit(logger)
    )
    report.set_inputs(
        input_root=str(raw_files[0].parent) if raw_files else None,
        n_units_discovered=len(raw_files),
    )

    # process batch
    try:
        results = processor.process_batch(
            raw_images=raw_files,
            output_dir=artifacts_dir,
            fail_stop=args.fs,
            max_workers=args.t
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

    # Populate manifest and report errors
    num_succeeded = 0
    num_failed = 0

    for r in results:
        if r.status == ITEM_OK:
            num_succeeded += 1
            jpg_rel = str(r.jpg_path.relative_to(artifacts_dir)) if r.jpg_path else None

            # Calculate checksum and size for the JPG
            checksum = {}
            size_bytes = {}
            if r.jpg_path and r.jpg_path.exists():
                checksum = {"jpg_path": calculate_sha256(r.jpg_path)}
                size_bytes = {"jpg_path": r.jpg_path.stat().st_size}

            manifest.add_ok_item(
                image_id=r.image_id,
                artifacts={"jpg_path": jpg_rel},
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

    # Determine exit code
    num_total = len(raw_files)
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
        artifact_type="jpg",
        path=str(artifacts_dir),
        n_files=num_succeeded,
    )
    report.set_pointers(logs_path=str(log_path))

    # Write output json files
    report_path = report.write(run_dir / "run_report.json")
    manifest_path = manifest.write(run_dir / "manifest.json")

    logger.info("Report:   %s", report_path)
    logger.info("Manifest: %s", manifest_path)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
