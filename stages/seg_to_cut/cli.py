#!/usr/bin/env python3
"""Create plant cutouts from images, segmentation masks, and detection rows."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from stages import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    ITEM_FAILED,
    ITEM_OK,
    ITEM_SKIPPED,
)
from stages.common import (
    ManifestBuilder,
    RunReportBuilder,
    calculate_sha256,
    get_git_commit,
    parse_batch_id,
    setup_logging,
)

from . import ERROR_CONFIG_INVALID, ERROR_PROCESSING_FAILED, STAGE, STAGE_VERSION
from .config import DEFAULT_CONFIG_PATH, SegToCutConfig, load_config
from .contracts import BatchValidationResult, CutoutProcessingResult
from .errors import SegToCutError
from .metadata import measurement_provenance
from .processor import discover_and_validate_inputs, process_validated_batch

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--segmentations", type=Path, required=True)
    parser.add_argument("--georeferenced-csv", type=Path, required=True)
    parser.add_argument("--species-catalog", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--output", "--o", dest="output", type=Path, default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument(
        "--t",
        type=int,
        default=8,
        help="Number of parallel image workers. Default: 8; use 0 or 1 for sequential processing.",
    )
    parser.add_argument("--fail-stop", "--fs", dest="fail_stop", action="store_true")
    parser.add_argument("--input-manifest", type=Path, default=None)
    parser.add_argument("--season", default=None)
    parser.add_argument("--bbot-version", default=None)
    parser.add_argument("--lens-model", default=None)
    return parser


def _validation_payload(result) -> dict[str, object]:
    return {
        "status": "validated",
        "images": result.image_count,
        "detections": result.detection_count,
    }


def _load_validation(
    args: argparse.Namespace,
) -> tuple[SegToCutConfig, BatchValidationResult]:
    config = load_config(args.config)
    validation = discover_and_validate_inputs(
        images_dir=args.images,
        masks_dir=args.segmentations,
        georeferenced_csv=args.georeferenced_csv,
        species_catalog=args.species_catalog,
        config=config,
        max_workers=args.t,
    )
    return config, validation


def _exit_code(results: tuple[CutoutProcessingResult, ...]) -> int:
    succeeded = sum(result.status == ITEM_OK for result in results)
    failed = sum(result.status == ITEM_FAILED for result in results)
    if failed == 0:
        return EXIT_SUCCESS
    if succeeded:
        return EXIT_PARTIAL
    return EXIT_FAILURE


def _artifact_details(
    result: CutoutProcessingResult, artifacts_dir: Path
) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    assert result.artifacts is not None
    names = ("cropout_path", "cutout_path", "mask_path", "metadata_path")
    paths = result.artifacts.paths
    artifacts = {
        name: str(path.relative_to(artifacts_dir)) for name, path in zip(names, paths, strict=True)
    }
    checksums = {name: calculate_sha256(path) for name, path in zip(names, paths, strict=True)}
    sizes = {name: path.stat().st_size for name, path in zip(names, paths, strict=True)}
    return artifacts, checksums, sizes


def _record_results(
    results: tuple[CutoutProcessingResult, ...],
    *,
    manifest: ManifestBuilder,
    report: RunReportBuilder,
    artifacts_dir: Path,
) -> tuple[int, int, int]:
    succeeded = failed = skipped = 0
    null_counts = Counter()
    for result in results:
        if result.status == ITEM_OK:
            succeeded += 1
            for field, reason in sorted(result.null_metadata_reasons.items()):
                null_counts[(field, reason)] += 1
                logger.info(
                    "Null metadata: image_id=%s cutout_id=%s field=%s reason=%s",
                    result.image_id,
                    result.cutout_id,
                    field,
                    reason,
                )
            artifacts, checksums, sizes = _artifact_details(result, artifacts_dir)
            manifest.add_ok_item(
                image_id=result.cutout_id,
                artifacts=artifacts,
                checksum=checksums,
                size_bytes=sizes,
            )
        elif result.status == ITEM_SKIPPED:
            skipped += 1
            manifest.add_skipped_item(
                image_id=result.cutout_id,
                message=result.skip_reason,
            )
        else:
            failed += 1
            message = result.error_message or "Cutout processing failed"
            error_type = result.error_type or "CutoutProcessingError"
            manifest.add_failed_item(
                image_id=result.cutout_id,
                error_type=error_type,
                message=message,
                retryable=result.retryable,
            )
            report.add_error(
                unit_id=result.cutout_id,
                code=result.error_code or ERROR_PROCESSING_FAILED,
                error_type=error_type,
                message=message,
                retryable=result.retryable,
            )
    summary = [
        {"field": field, "reason": reason, "count": count}
        for (field, reason), count in sorted(null_counts.items())
    ]
    for entry in summary:
        logger.info(
            "Null metadata summary: field=%s count=%d reason=%s",
            entry["field"], entry["count"], entry["reason"],
        )
    return succeeded, failed, skipped


def _write_run_contracts(
    *,
    report: RunReportBuilder,
    manifest: ManifestBuilder,
    run_dir: Path,
    artifacts_dir: Path,
    output_root: Path,
    log_path: Path,
    exit_code: int,
    counts: tuple[int, int, int],
) -> None:
    succeeded, failed, skipped = counts
    report.stop(exit_code)
    report.set_outputs(
        output_root=str(output_root),
        run_root=str(run_dir),
        artifacts_dir=str(artifacts_dir),
        n_succeeded=succeeded,
        n_failed=failed,
        n_skipped=skipped,
    )
    report.add_artifact_type(
        artifact_type="cutout_bundle",
        path=str(artifacts_dir / "cutouts"),
        n_files=succeeded * 4,
    )
    report.set_pointers(logs_path=str(log_path))
    manifest.write(run_dir / "manifest.json")
    report.write(run_dir / "run_report.json")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.t < 0:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": ERROR_CONFIG_INVALID,
                    "message": "--t must be a non-negative integer",
                }
            )
        )
        return EXIT_CONFIG_ERROR
    if args.output is not None and args.fail_stop and args.t > 1:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": ERROR_CONFIG_INVALID,
                    "message": "--fail-stop cannot be combined with --t greater than 1",
                }
            )
        )
        return EXIT_CONFIG_ERROR

    # Supplying --output selects the complete stage flow. Without it, retain
    # the scaffold's useful validation-only behavior for existing callers.
    if args.output is None:
        try:
            _, validation = _load_validation(args)
        except SegToCutError as exc:
            print(json.dumps({"status": "failed", "error_code": exc.code, "message": str(exc)}))
            return EXIT_CONFIG_ERROR
        print(json.dumps(_validation_payload(validation), sort_keys=True))
        return EXIT_SUCCESS

    batch_id = (
        args.batch_id
        or parse_batch_id(str(args.georeferenced_csv))
        or parse_batch_id(str(args.images))
    )
    if not batch_id:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "E_CONFIG_INVALID",
                    "message": "Could not determine batch_id; pass --batch-id",
                }
            )
        )
        return EXIT_CONFIG_ERROR

    args.output.mkdir(parents=True, exist_ok=True)
    report = RunReportBuilder(stage=STAGE, stage_version=STAGE_VERSION, batch_id=batch_id)
    report.start()
    run_dir = args.output / STAGE / report.run_id
    artifacts_dir = run_dir / "artifacts"
    cutouts_dir = artifacts_dir / "cutouts"
    cutouts_dir.mkdir(parents=True, exist_ok=True)
    log_path = setup_logging(run_dir / "logs")
    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=report.run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
    )
    config_path = args.config or DEFAULT_CONFIG_PATH
    if config_path.is_file():
        report.set_provenance(config_path=config_path, code_commit=get_git_commit(logger))
    else:
        report.set_provenance(code_commit=get_git_commit(logger))
    report.set_inputs(
        input_root=str(args.images),
        n_units_discovered=0,
        unit_id_kind="cutout_id",
        inputs_manifest_path=str(args.input_manifest) if args.input_manifest else None,
    )

    try:
        config, validation = _load_validation(args)
    except SegToCutError as exc:
        logger.error("Input or configuration validation failed: %s", exc)
        report.set_stage_error(str(exc))
        report.add_error(
            unit_id="__stage__",
            code=exc.code,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        _write_run_contracts(
            report=report,
            manifest=manifest,
            run_dir=run_dir,
            artifacts_dir=artifacts_dir,
            output_root=args.output,
            log_path=log_path,
            exit_code=EXIT_CONFIG_ERROR,
            counts=(0, 0, 0),
        )
        return EXIT_CONFIG_ERROR

    report.set_inputs(
        input_root=str(args.images),
        n_units_discovered=validation.detection_count,
        unit_id_kind="cutout_id",
        inputs_manifest_path=str(args.input_manifest) if args.input_manifest else None,
    )
    report.set_extra(
        segmentations_path=str(args.segmentations),
        georeferenced_csv_path=str(args.georeferenced_csv),
        species_catalog_path=str(args.species_catalog),
        measurement_provenance=measurement_provenance(config),
    )

    logger.info(
        "Run %s started for batch %s with %d detections",
        report.run_id,
        batch_id,
        validation.detection_count,
    )
    try:
        results = process_validated_batch(
            validation,
            output_dir=cutouts_dir,
            batch_id=batch_id,
            config=config,
            fail_stop=args.fail_stop,
            max_workers=args.t,
            season=args.season,
            bbot_version=args.bbot_version,
            lens_model=args.lens_model,
        )
    except Exception as exc:
        logger.exception("Batch processing failed")
        report.set_stage_error(f"Batch processing failed: {exc}")
        report.add_error(
            unit_id="__stage__",
            code=getattr(exc, "code", ERROR_PROCESSING_FAILED),
            error_type=type(exc).__name__,
            message=str(exc),
        )
        results = ()
        exit_code = EXIT_FAILURE
    else:
        exit_code = _exit_code(results)

    counts = _record_results(
        results,
        manifest=manifest,
        report=report,
        artifacts_dir=artifacts_dir,
    )
    if validation.detection_count == 0:
        report.set_skip(True, "NO_DETECTIONS")
    elif counts[0] == 0 and counts[1] == 0 and counts[2] > 0:
        report.set_skip(True, "NO_VALID_CUTOUTS")
    _write_run_contracts(
        report=report,
        manifest=manifest,
        run_dir=run_dir,
        artifacts_dir=artifacts_dir,
        output_root=args.output,
        log_path=log_path,
        exit_code=exit_code,
        counts=counts,
    )
    logger.info(
        "Finished with %d succeeded, %d failed, and %d skipped cutouts",
        *counts,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
