#!/usr/bin/env python3
"""
CLI for assigning species to georeferenced detections.

Supports two modes:
  - spatial_join:       assign species via shapefile zone polygons
  - monoculture_config: assign a single species code to all detections
"""

import argparse
import json
import logging
from pathlib import Path

from packaging.version import Version

from stages import EXIT_SUCCESS, EXIT_FAILURE, EXIT_CONFIG_ERROR
from stages.common import (
    RunReportBuilder,
    ManifestBuilder,
    calculate_sha256,
    get_git_commit,
    parse_batch_id,
    setup_logging,
)

from . import STAGE, STAGE_VERSION, ERROR_SHAPEFILE_UNREADABLE, ERROR_SPATIAL_JOIN_FAILED, ERROR_CSV_INVALID
from .assigner import assign_spatial, assign_monoculture

logger = logging.getLogger(__name__)

BBOT_VERSION_MAX = Version("3.1")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assign species to georeferenced detections via shapefile or monoculture flag."
    )
    parser.add_argument("--geo", type=Path, required=True, help="Georeferenced CSV from det_to_world.")
    parser.add_argument("--det", type=Path, required=True, help="Original detection CSV from jpg_to_det (monoculture fallback).")
    parser.add_argument("--geo-report", type=Path, required=True, help="run_report.json from det_to_world.")
    parser.add_argument("--shp", type=Path, required=True, help="Species zone shapefile.")
    parser.add_argument("--bbot-version", type=str, required=True, help="BBot version string.")
    parser.add_argument("--species", type=str, default=None, help="Species code for monoculture batches.")
    parser.add_argument("--o", type=Path, required=True, help="Output directory.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID. Auto-inferred from input path if omitted.")

    args = parser.parse_args()

    # Validate batch_id
    batch_id = args.batch_id or parse_batch_id(str(args.geo))
    if not batch_id:
        logger.error("Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR

    # initialize output directory
    args.o.mkdir(parents=True, exist_ok=True)

    report = RunReportBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        batch_id=batch_id,
    )
    report.start()

    # create run report + artifact directories
    run_id = report.run_id
    run_dir = args.o / STAGE / run_id
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Setup logging + manifest builder
    log_path = setup_logging(run_dir)
    logger.info("Run %s started for batch %s", run_id, batch_id)

    manifest = ManifestBuilder(
        stage=STAGE,
        stage_version=STAGE_VERSION,
        run_id=run_id,
        artifacts_root=str(artifacts_dir),
        batch_id=batch_id,
    )

    report.set_provenance(
        code_commit=get_git_commit(logger),
    )

    # validate bench bot version
    try:
        bbot_ver = Version(args.bbot_version)
    except Exception as exc:
        logger.error("Invalid bbot_version string '%s': %s", args.bbot_version, exc)
        report.set_stage_error(f"Invalid bbot_version: {exc}")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(input_root=str(args.geo.parent), n_units_discovered=0)
        report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_CONFIG_ERROR

    # invalid version
    if bbot_ver > BBOT_VERSION_MAX:
        logger.error("BBot version %s is not supported (max %s).", bbot_ver, BBOT_VERSION_MAX)
        report.set_stage_error(f"BBot version {bbot_ver} > {BBOT_VERSION_MAX} is not supported.")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(input_root=str(args.geo.parent), n_units_discovered=0)
        report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_CONFIG_ERROR

    # identify monoculture or spatial join (mulitple species) report
    try:
        with open(args.geo_report) as f:
            geo_report = json.load(f)
    except Exception as exc:
        logger.error("Could not read geo_report: %s", exc)
        report.set_stage_error(f"Could not read geo_report: {exc}")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(input_root=str(args.geo.parent), n_units_discovered=0)
        report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_CONFIG_ERROR


    # see if geo report was skipped (monoculture) or not (spatial join)
    if geo_report.get("skipped"):
        is_monoculture = True
    else:
        is_monoculture = False

    # error: monoculture batch with no species argument
    if is_monoculture and args.species is None:
        logger.error("Monoculture batch detected but --species was not provided.")
        report.set_stage_error("Monoculture batch requires --species flag.")
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(input_root=str(args.det.parent), n_units_discovered=0)
        report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_CONFIG_ERROR


    # error: spatial join batch but shapefile does not exist
    if not is_monoculture and not args.shp.exists():
        logger.error("Shapefile does not exist: %s", args.shp)
        report.set_stage_error(f"Shapefile not found: {args.shp}")
        report.add_error(
            unit_id="__stage__",
            code=ERROR_SHAPEFILE_UNREADABLE,
            error_type="FileNotFoundError",
                message=f"Shapefile not found: {args.shp}",
            )
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(input_root=str(args.geo.parent), n_units_discovered=0)
        report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
        report.set_pointers(logs_path=str(log_path))
        report.write(run_dir / "run_report.json")
        manifest.write(run_dir / "manifest.json")
        return EXIT_CONFIG_ERROR

    # assign species
    output_csv_path = artifacts_dir / f"{batch_id}_species_assigned.csv"

    if is_monoculture:
        assignment_mode = "monoculture_config"
        input_csv = str(args.det)
        try:
            result_df = assign_monoculture(input_csv, args.species)
        except Exception as exc:
            logger.error("Monoculture assignment failed: %s", exc)
            report.set_stage_error(f"Monoculture assignment failed: {exc}")
            report.add_error(
                unit_id="__stage__",
                code=ERROR_CSV_INVALID,
                error_type=type(exc).__name__,
                message=str(exc),
            )
            report.stop(EXIT_FAILURE)
            report.set_inputs(input_root=str(args.det.parent), n_units_discovered=0)
            report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
            report.set_pointers(logs_path=str(log_path))
            report.write(run_dir / "run_report.json")
            manifest.write(run_dir / "manifest.json")
            return EXIT_FAILURE
    else:
        assignment_mode = "spatial_join"
        input_csv = str(args.geo)
        try:
            result_df = assign_spatial(input_csv, str(args.shp))
        except Exception as exc:
            logger.error("Spatial assignment failed: %s", exc)
            report.set_stage_error(f"Spatial assignment failed: {exc}")
            error_code = ERROR_SHAPEFILE_UNREADABLE if "shapefile" in str(exc).lower() else ERROR_SPATIAL_JOIN_FAILED
            report.add_error(
                unit_id="__stage__",
                code=error_code,
                error_type=type(exc).__name__,
                message=str(exc),
            )
            report.stop(EXIT_FAILURE)
            report.set_inputs(input_root=str(args.geo.parent), n_units_discovered=0)
            report.set_outputs(output_root=str(args.o), run_root=str(run_dir), artifacts_dir=str(artifacts_dir), n_succeeded=0, n_failed=0)
            report.set_pointers(logs_path=str(log_path))
            report.write(run_dir / "run_report.json")
            manifest.write(run_dir / "manifest.json")
            return EXIT_FAILURE

    # 8. Write output CSV
    result_df.to_csv(output_csv_path, index=False)
    logger.info("Wrote species-assigned CSV: %s", output_csv_path)

    csv_checksum = calculate_sha256(output_csv_path)
    csv_size = output_csv_path.stat().st_size
    csv_rel = str(output_csv_path.relative_to(artifacts_dir))

    n_rows = len(result_df)
    manifest.add_ok_item(
        image_id=batch_id,
        artifacts={"species_assigned_csv_path": csv_rel},
        checksum={"species_assigned_csv_path": csv_checksum},
        size_bytes={"species_assigned_csv_path": csv_size},
    )

    # 9. Record run report fields
    report.set_extra(
        assignment_mode=assignment_mode,
        species_override=args.species,
        bbot_version=args.bbot_version,
    )

    input_root = str(args.det.parent) if is_monoculture else str(args.geo.parent)
    report.set_inputs(
        input_root=input_root,
        n_units_discovered=n_rows,
        inputs_manifest_path=input_csv,
    )

    report.stop(EXIT_SUCCESS)
    report.set_outputs(
        output_root=str(args.o),
        run_root=str(run_dir),
        artifacts_dir=str(artifacts_dir),
        n_succeeded=1,
        n_failed=0,
    )
    report.add_artifact_type(
        artifact_type="species_assigned_csv",
        path=str(output_csv_path),
        n_files=1,
    )
    report.set_pointers(logs_path=str(log_path))

    # 10. Write run_report.json and manifest.json
    report.write(run_dir / "run_report.json")
    manifest.write(run_dir / "manifest.json")
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
