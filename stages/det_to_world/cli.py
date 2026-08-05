#!/usr/bin/env python3
"""
CLI for remapping detection bboxes into world coordinates.
"""

import argparse
import logging
from pathlib import Path

from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR
from stages.common import (
    RunReportBuilder,
    ManifestBuilder,
    calculate_sha256,
    get_git_commit,
    parse_batch_id,
    setup_logging,
)

from . import STAGE, STAGE_VERSION, ERROR_CSV_INVALID, ERROR_GRID_NOT_FOUND, ERROR_REMAP_FAILED
from .remapper import (
    GEO_COLUMNS,
    load_detection_rows,
    remap_rows,
    write_georeferenced_csv,
)

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remap jpg_to_det detections into world coordinates using ASFM NPZ grids."
    )
    parser.add_argument("--i", type=Path, required=True, help="Input detection CSV from jpg_to_det.")
    parser.add_argument("--g", type=Path, required=True, help="Directory containing per-image NPZ grids.")
    parser.add_argument("--o", type=Path, required=True, help="Output directory.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID. Auto-inferred from input path if omitted.")
    parser.add_argument("--skip-remap", action="store_true", help="Skip remapping for monoculture batches.")

    args = parser.parse_args()

    batch_id = args.batch_id or parse_batch_id(str(args.i))
    if not batch_id:
        logger.error("Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR

    if not args.i.exists():
        logger.error("Input detection CSV does not exist: %s", args.i)
        return EXIT_CONFIG_ERROR
    if not args.skip_remap and not args.g.exists():
        logger.error("Grid directory does not exist: %s", args.g)
        return EXIT_CONFIG_ERROR

    # create output directory
    args.o.mkdir(parents=True, exist_ok=True)

    # start report builder
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

    # initialize logger
    log_path = setup_logging(run_dir)
    logger.info("Run %s started for batch %s", run_id, batch_id)


    # start manifest builder
    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
    )

    report.set_provenance(
        code_commit=get_git_commit(logger),
        deps_id="scipy",
    )

    try:
        # load csv input
        input_fieldnames, rows = load_detection_rows(args.i)
    except Exception as exc:
        logger.error("Invalid detection CSV: %s", exc)
        report.set_stage_error(f"Invalid detection CSV: {exc}")
        report.add_error(
            unit_id="__stage__",
            code=ERROR_CSV_INVALID,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(
            input_root=str(args.i.parent),
            n_units_discovered=0,
        )
        report.set_outputs(
            output_root=str(args.o),
            run_root=str(run_dir),
            artifacts_dir=str(artifacts_dir),
            n_succeeded=0,
            n_failed=0,
        )
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_CONFIG_ERROR

    image_ids = []
    seen_image_ids = set()
    for row in rows:
        image_id = row["image_id"]
        if image_id in seen_image_ids:
            continue
        seen_image_ids.add(image_id)
        image_ids.append(image_id)

    
    report.set_inputs(
        input_root=str(args.i.parent),
        n_units_discovered=len(image_ids),
        inputs_manifest_path=str(args.i),
    )

    # build output paths
    # Preserve the original CSV columns and append georeferencing columns not already present.
    output_fieldnames = list(input_fieldnames) + [
        column for column in GEO_COLUMNS if column not in input_fieldnames
    ]
    output_csv_path = artifacts_dir / f"{batch_id}_georeferenced.csv"

    # skip remaping for monoculture batches
    if args.skip_remap:
        write_georeferenced_csv([], output_fieldnames, output_csv_path)

        for image_id in image_ids:
            manifest.add_skipped_item(image_id=image_id, message="monoculture_batch")

        report.set_skip(True, "monoculture_batch")
        report.stop(EXIT_SUCCESS)
        report.set_outputs(
            output_root=str(args.o),
            run_root=str(run_dir),
            artifacts_dir=str(artifacts_dir),
            n_succeeded=0,
            n_failed=0,
            n_skipped=len(image_ids),
        )
        report.add_artifact_type(
            artifact_type="georeferenced_csv",
            path=str(output_csv_path),
            n_files=1,
        )
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_SUCCESS

    # remap rows and handle errors
    try:
        mapped_rows, image_results = remap_rows(rows, args.g)
    except Exception as exc:
        logger.error("Batch remapping failed: %s", exc)
        report.set_stage_error(f"Batch remapping failed: {exc}")
        report.add_error(
            unit_id="__stage__",
            code=ERROR_REMAP_FAILED,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        report.stop(EXIT_FAILURE)
        report.set_outputs(
            output_root=str(args.o),
            run_root=str(run_dir),
            artifacts_dir=str(artifacts_dir),
            n_succeeded=0,
            n_failed=len(image_ids),
        )
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_FAILURE

    write_georeferenced_csv(mapped_rows, output_fieldnames, output_csv_path)

    num_succeeded = 0
    num_failed = 0
    num_grid_not_found = 0

    csv_rel = str(output_csv_path.relative_to(artifacts_dir))
    csv_checksum = calculate_sha256(output_csv_path)
    csv_size = output_csv_path.stat().st_size

    # parse results and populate manifest + report
    for result in image_results:
        for warning in result.warnings:
            report.add_warning(
                unit_id=warning.unit_id,
                code=warning.code,
                warning_type=warning.warning_type,
                message=warning.message,
                meta=warning.meta,
            )

        if result.status == "ok":
            num_succeeded += 1
            manifest.add_ok_item(
                image_id=result.image_id,
                artifacts={"georeferenced_csv_path": csv_rel},
                checksum={"georeferenced_csv_path": csv_checksum},
                size_bytes={"georeferenced_csv_path": csv_size},
            )
            continue

        num_failed += 1
        if result.error_code == ERROR_GRID_NOT_FOUND:
            num_grid_not_found += 1
        manifest.add_failed_item(
            image_id=result.image_id,
            error_type=result.error_type or "RuntimeError",
            message=result.error_message or "Unknown remap failure",
            retryable=False,
        )
        report.add_error(
            unit_id=result.image_id,
            code=result.error_code or ERROR_REMAP_FAILED,
            error_type=result.error_type or "RuntimeError",
            message=result.error_message or "Unknown remap failure",
            retryable=False,
        )

    # Images with no ASFM grid coverage (e.g. straggler frames captured
    # outside every reconstructed flight window) are an expected,
    # unactionable gap, not a stage problem — they're still recorded as
    # failed items above for traceability, but on their own they shouldn't
    # mark the whole run "partial" and block promotion. Only count other
    # failure types (actual remap errors) toward EXIT_PARTIAL/EXIT_FAILURE.
    num_other_failed = num_failed - num_grid_not_found

    if num_succeeded == 0:
        exit_code = EXIT_FAILURE
    elif num_other_failed > 0:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_SUCCESS

    report.stop(exit_code)
    report.set_outputs(
        output_root=str(args.o),
        run_root=str(run_dir),
        artifacts_dir=str(artifacts_dir),
        n_succeeded=num_succeeded,
        n_failed=num_failed,
    )
    report.add_artifact_type(
        artifact_type="georeferenced_csv",
        path=str(output_csv_path),
        n_files=1,
    )
    report.set_pointers(logs_path=str(log_path))

    report.write(run_dir / "run_report.json")
    manifest.write(run_dir / "manifest.json")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
