#!/usr/bin/env python3
"""CLI for DET -> SEG processing pipeline."""

from __future__ import annotations

import argparse
import hashlib
import logging
import subprocess
from pathlib import Path

from stages import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_PARTIAL, EXIT_SUCCESS, ITEM_OK
from stages.common import ManifestBuilder, RunReportBuilder, parse_batch_id, setup_logging
from .processor import Processor

from . import (
    ERROR_CFG_VALIDATION_FAILED,
    ERROR_DET_READ_FAILED,
    ERROR_UNKNOWN,
    STAGE,
    STAGE_VERSION,
)

logger = logging.getLogger(__name__)



def calculate_sha256(file_path: Path) -> str:
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return "sha256:" + sha256_hash.hexdigest()



def get_git_commit() -> str | None:
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



def _build_jpg_index(jpg_dir: Path) -> dict[str, Path]:
    """Build a dictionary for JPG/JPEG paths keyed by stem."""
    jpg_index: dict[str, Path] = {}

    for path in sorted(jpg_dir.iterdir()):
        if not path.is_file():
            continue

        if path.suffix.lower() not in {".jpg", ".jpeg"}:
            continue

        jpg_stem = path.stem.lower()
        if jpg_stem not in jpg_index:
            jpg_index[jpg_stem] = path

    return jpg_index



def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run segmentation within YOLO detection boxes and composite full-image masks."
    )
    # Parse arguements
    parser.add_argument("--i", type=Path, required=True, help="Det artifacts dir containing per-image .txt files.")
    parser.add_argument("--j", type=Path, required=True, help="Directory containing original JPG images.")
    parser.add_argument("--c", type=Path, required=True, help="Path to segmentation YAML config file.")
    parser.add_argument("--o", type=Path, required=True, help="Output directory.")
    parser.add_argument(
        "--t",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--fs", action="store_true", help="Stop processing on first failure.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID. Auto-inferred from input path if omitted.")
    parser.add_argument("--device", type=str, default="cpu", help="Torch device (e.g. cpu, cuda:0).")

    args = parser.parse_args()

    if args.t not in (0, 1):
        logger.error("det_to_seg is single-process; --t must be 0 or 1")
        return EXIT_CONFIG_ERROR

    # validate input/output paths
    if not args.i.exists():
        logger.error("Detection input directory does not exist: %s", args.i)
        return EXIT_CONFIG_ERROR
    if not args.j.exists():
        logger.error("JPG input directory does not exist: %s", args.j)
        return EXIT_CONFIG_ERROR
    args.o.mkdir(parents=True, exist_ok=True)


    # See if batch id was passed, or if it exists in input directories
    batch_id = args.batch_id or parse_batch_id(str(args.i)) or parse_batch_id(str(args.j))
    if not batch_id:
        logger.error("Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR


    report = RunReportBuilder(stage=STAGE, stage_version=STAGE_VERSION, batch_id=batch_id)
    report.start()

    run_id = report.run_id
    run_dir = args.o / STAGE / run_id
    artifacts_dir = run_dir / "artifacts"
    masks_dir = artifacts_dir / "masks"
    masks_dir.mkdir(parents=True, exist_ok=True)

    log_path = setup_logging(run_dir)
    logger.info("Run %s started for batch %s", run_id, batch_id)

    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
    )

    try:
        processor = Processor(args.c, device=args.device)
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

    # sort text files for reproducibility/consistency
    txt_files = sorted(list(args.i.glob("*.txt")) + list(args.i.glob("*.TXT")))
    if not txt_files:
        logger.error("No detection txt files found in %s", args.i)
        report.set_stage_error(f"No detection txt files found in {args.i}")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        return EXIT_CONFIG_ERROR

    # Build dict of jpg name (stem) to path
    jpg_index = _build_jpg_index(args.j)

    image_pairs: list[tuple[Path, Path]] = []
    num_missing_jpg = 0
    # match each txt file to jpg by stem, else log and record failure in manifest/report
    for txt_path in txt_files:
        jpg_path = jpg_index.get(txt_path.stem.lower())
        
        # no corresponding jpg found
        if jpg_path is None:
            num_missing_jpg += 1
            msg = f"No matching JPG for detection file {txt_path.name}"
            logger.warning(msg)
            manifest.add_failed_item(
                image_id=txt_path.stem,
                error_type="MissingImageError",
                message=msg,
                retryable=False,
            )
            report.add_error(
                unit_id=txt_path.stem,
                code=ERROR_DET_READ_FAILED,
                error_type="MissingImageError",
                message=msg,
                retryable=False,
            )
            if args.fs:
                break
            continue
        image_pairs.append((txt_path, jpg_path))

    report.set_provenance(
        config_path=args.c,
        code_commit=get_git_commit(),
        model_id=str(processor.config.get("weights")),
    )
    report.set_inputs(input_root=str(args.i), n_units_discovered=len(txt_files))

    results = []
    if image_pairs:
        try:
            # processor image txt pairs
            results = processor.process_batch(
                image_pairs=image_pairs,
                output_dir=masks_dir,
                fail_stop=args.fs,
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

    num_succeeded = 0
    num_failed = num_missing_jpg

    # record results in manifest and report
    for r in results:
        if r.status == ITEM_OK:
            num_succeeded += 1
            mask_rel = str(r.mask_path.relative_to(artifacts_dir)) if r.mask_path else None
            checksum = {}
            size_bytes = {}
            if r.mask_path and r.mask_path.exists():
                checksum["mask_path"] = calculate_sha256(r.mask_path)
                size_bytes["mask_path"] = r.mask_path.stat().st_size

            manifest.add_ok_item(
                image_id=r.image_id,
                artifacts={"mask_path": mask_rel},
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

    num_total = len(txt_files)
    if num_succeeded == num_total:
        exit_code = EXIT_SUCCESS
    elif num_succeeded > 0:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_FAILURE

    logger.info("Finished: %d/%d files processed successfully", num_succeeded, num_total)

    report.stop(exit_code)
    report.set_outputs(
        output_root=str(args.o),
        run_root=str(run_dir),
        artifacts_dir=str(artifacts_dir),
        n_succeeded=num_succeeded,
        n_failed=num_failed,
        n_skipped=0,
    )
    report.add_artifact_type(
        artifact_type="segmentation_mask",
        path=str(masks_dir),
        n_files=num_succeeded,
    )
    report.set_pointers(logs_path=str(log_path))

    report_path = report.write(run_dir / "run_report.json")
    manifest_path = manifest.write(run_dir / "manifest.json")

    logger.info("Report:   %s", report_path)
    logger.info("Manifest: %s", manifest_path)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
