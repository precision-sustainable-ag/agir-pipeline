#!/usr/bin/env python3
"""
CLI for RAW -> DNG -> JPG processing pipeline.

Outputs run_report.json and manifest.json — no database interaction.
"""

import argparse
from pathlib import Path
from . import STAGE, STAGE_VERSION
from .processor import Processor
from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR
from stages.common import RunReportBuilder, ManifestBuilder, parse_batch_id


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

    args = parser.parse_args()

    # Resolve batch_id: explicit flag or inferred from input path
    batch_id = args.batch_id or parse_batch_id(str(args.i))
    if not batch_id:
        print(f"Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR

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

    # set up builders
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

    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
    )

    report.set_provenance(config_path=args.c)
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
        print(f"Batch processing failed: {e}")
        results = []

    # Populate manifest and report errors
    num_succeeded = 0
    num_failed = 0

    for r in results:
        if r.status == "ok":
            num_succeeded += 1
            jpg_rel = str(r.jpg_path.relative_to(artifacts_dir)) if r.jpg_path else None
            manifest.add_ok_item(
                image_id=r.image_id,
                artifacts={"jpg_path": jpg_rel},
            )
        else:
            num_failed += 1
            manifest.add_failed_item(
                image_id=r.image_id,
                error_type=r.error_type,
                message=r.error_message,
                retryable=r.retryable,
            )
            report.add_error(
                unit_id=r.image_id,
                code="CONVERSION_FAILED",
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

    print(f"Finished: {num_succeeded}/{num_total} files processed successfully.")

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

    # Write output json filess
    report_path = report.write(run_dir / "run_report.json")
    manifest_path = manifest.write(run_dir / "manifest.json")

    print(f"Report:   {report_path}")
    print(f"Manifest: {manifest_path}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
