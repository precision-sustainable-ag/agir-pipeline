#!/usr/bin/env python3
"""
CLI for remapping detection bboxes into world coordinates and assigning species.
"""

import argparse
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from packaging.version import Version

from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR
from stages.common import (
    RunReportBuilder,
    ManifestBuilder,
    calculate_sha256,
    get_git_commit,
    parse_batch_id,
    setup_logging,
)

from . import (
    STAGE,
    STAGE_VERSION,
    ERROR_CSV_INVALID,
    ERROR_GRID_NOT_FOUND,
    ERROR_REMAP_FAILED,
    ERROR_SHAPEFILE_UNREADABLE,
    ERROR_SPATIAL_JOIN_FAILED,
    ERROR_ZONE_TOO_FAR,
    ERROR_UNKNOWN_SPECIES_CODE,
)
from .remapper import (
    GEO_COLUMNS,
    load_detection_rows,
    remap_rows,
    write_georeferenced_csv,
)
from .species import (
    DEFAULT_MAX_NEAREST_DISTANCE_M,
    UnknownSpeciesCodeError,
    ZoneTooFarError,
    assign_monoculture,
    assign_spatial,
    enrich_with_catalog,
    load_catalog,
)

logger = logging.getLogger(__name__)

BBOT_VERSION_MAX = Version("3.1")
DEFAULT_SPECIES_CATALOG = Path(
    "/project/dash_agir/semifield-utils/species_information/species_catalog.generated.json"
)
SPECIES_COLUMNS = ["species_id", "assignment_method"]
# Shapefiles carry at most one of these optional human-readable attributes
# (see species.assign_spatial): comm_name for ordinary species zones,
# cultc_id/disp_name for cultivar seasons. These columns appear in a batch's
# CSV only when actually present in species_df, never as
# always-there-but-blank columns like GEO_COLUMNS, since neither concept
# applies to every season the way world coordinates do.
SPECIES_NAME_COLUMNS = ["species_name"]
CULTIVAR_COLUMNS = ["cultivar_id", "cultivar_name"]
# class_id has shown up in every zone shapefile checked so far but isn't
# assumed universal — same "only when actually present" treatment.
ZONE_CLASS_COLUMNS = ["class_id"]
# species.enrich_with_catalog()'s output columns — present whenever a
# species catalog was loaded (see --species-catalog); species_* columns are
# always added, cultivar_* only when the shapefile itself had cultivar_id.
SPECIES_CATALOG_COLUMNS = [
    "species_common_name", "species_family", "species_genus",
    "species_growth_habit", "species_category",
    "species_hex", "species_r", "species_g", "species_b",
]
CULTIVAR_CATALOG_COLUMNS = [
    "cultivar_display_name", "cultivar_line_name", "cultivar_registered",
    "cultivar_hex", "cultivar_r", "cultivar_g", "cultivar_b",
]
EXTRA_COLUMNS = (
    SPECIES_COLUMNS + SPECIES_NAME_COLUMNS + CULTIVAR_COLUMNS + ZONE_CLASS_COLUMNS
    + SPECIES_CATALOG_COLUMNS + CULTIVAR_CATALOG_COLUMNS
)


def _build_output_fieldnames(input_fieldnames: list[str], present_columns) -> list[str]:
    """Input columns + GEO_COLUMNS + whichever of EXTRA_COLUMNS are actually
    present in the species-assignment result, preserving that fixed order.
    """
    fieldnames = list(input_fieldnames)
    fieldnames += [column for column in GEO_COLUMNS if column not in fieldnames]
    fieldnames += [column for column in EXTRA_COLUMNS if column in present_columns and column not in fieldnames]
    return fieldnames


def _to_output_rows(df: pd.DataFrame, fieldnames: list[str]) -> list[dict[str, Any]]:
    """Pin a DataFrame to the CSV's fixed column set, blanking anything missing/NaN.

    Also drops incidental geopandas columns (geometry, index_right, ...) that
    aren't part of the output schema, since reindex only keeps what's listed.
    """
    reindexed = df.reindex(columns=fieldnames)
    reindexed = reindexed.where(reindexed.notna(), "")
    return reindexed.to_dict("records")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remap jpg_to_det detections into world coordinates using ASFM NPZ "
        "grids, then assign species via shapefile zone or monoculture config."
    )
    parser.add_argument("--i", type=Path, required=True, help="Input detection CSV from jpg_to_det.")
    parser.add_argument("--g", type=Path, required=True, help="Directory containing per-image NPZ grids.")
    parser.add_argument("--o", type=Path, required=True, help="Output directory.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID. Auto-inferred from input path if omitted.")
    parser.add_argument("--skip-remap", action="store_true", help="Skip remapping for monoculture batches.")
    parser.add_argument("--shp", type=Path, default=None, help="Species zone shapefile. Required unless --skip-remap.")
    parser.add_argument("--species", type=str, default=None, help="Species code for monoculture batches. Required with --skip-remap.")
    parser.add_argument(
        "--max-nearest-distance-m", type=float, default=DEFAULT_MAX_NEAREST_DISTANCE_M,
        help="Max distance (zone shapefile CRS units, meters for UTM) a detection outside every "
        f"zone polygon may fall back to its nearest zone before the run fails (default: {DEFAULT_MAX_NEAREST_DISTANCE_M}).",
    )
    parser.add_argument("--bbot-version", type=str, required=True, help="BBot version string.")
    parser.add_argument(
        "--species-catalog", type=Path, default=DEFAULT_SPECIES_CATALOG,
        help="Flat species/cultivar reference catalog (see orchestrator/species_catalog.py). "
        "Enrichment columns are skipped with a warning if this file is missing or unreadable.",
    )

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
    if args.skip_remap and args.species is None:
        logger.error("Monoculture batch (--skip-remap) requires --species.")
        return EXIT_CONFIG_ERROR
    if not args.skip_remap and (args.shp is None or not args.shp.exists()):
        logger.error("Spatial-join batch requires --shp pointing to an existing shapefile: %s", args.shp)
        return EXIT_CONFIG_ERROR

    try:
        bbot_ver = Version(args.bbot_version)
    except Exception as exc:
        logger.error("Invalid bbot_version string '%s': %s", args.bbot_version, exc)
        return EXIT_CONFIG_ERROR
    if bbot_ver > BBOT_VERSION_MAX:
        logger.error("BBot version %s is not supported (max %s).", bbot_ver, BBOT_VERSION_MAX)
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

    # Species/cultivar reference enrichment is additive, not load-bearing —
    # a missing/unreadable catalog degrades to the pre-enrichment output
    # (just SPECIES_COLUMNS/CULTIVAR_COLUMNS) rather than failing the batch.
    species_catalog = None
    if args.species_catalog.exists():
        try:
            species_catalog = load_catalog(args.species_catalog)
        except Exception as exc:
            logger.warning("Could not load species catalog %s: %s", args.species_catalog, exc)
    else:
        logger.warning(
            "Species catalog not found at %s; skipping species/cultivar enrichment.",
            args.species_catalog,
        )

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
        deps_id="scipy,geopandas,packaging",
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

    output_csv_path = artifacts_dir / f"{batch_id}_georeferenced.csv"

    # monoculture batches: skip remapping, assign the configured species code
    # directly to the original detection rows.
    if args.skip_remap:
        try:
            species_df = assign_monoculture(pd.DataFrame(rows), args.species)
        except Exception as exc:
            logger.error("Monoculture species assignment failed: %s", exc)
            report.set_stage_error(f"Monoculture species assignment failed: {exc}")
            report.add_error(
                unit_id="__stage__",
                code=ERROR_CSV_INVALID,
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

        if species_catalog is not None:
            try:
                species_df = enrich_with_catalog(species_df, species_catalog)
            except UnknownSpeciesCodeError as exc:
                logger.error("Species catalog enrichment failed: %s", exc)
                report.set_stage_error(f"Species catalog enrichment failed: {exc}")
                report.add_error(
                    unit_id="__stage__",
                    code=ERROR_UNKNOWN_SPECIES_CODE,
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

        output_fieldnames = _build_output_fieldnames(input_fieldnames, species_df.columns)
        output_rows = _to_output_rows(species_df, output_fieldnames)
        write_georeferenced_csv(output_rows, output_fieldnames, output_csv_path)

        csv_rel = str(output_csv_path.relative_to(artifacts_dir))
        csv_checksum = calculate_sha256(output_csv_path)
        csv_size = output_csv_path.stat().st_size

        for image_id in image_ids:
            manifest.add_ok_item(
                image_id=image_id,
                artifacts={"georeferenced_csv_path": csv_rel},
                checksum={"georeferenced_csv_path": csv_checksum},
                size_bytes={"georeferenced_csv_path": csv_size},
            )

        report.set_skip(True, "monoculture_batch")
        report.set_extra(
            assignment_mode="monoculture_config",
            species_override=args.species,
            bbot_version=args.bbot_version,
        )
        report.stop(EXIT_SUCCESS)
        report.set_outputs(
            output_root=str(args.o),
            run_root=str(run_dir),
            artifacts_dir=str(artifacts_dir),
            n_succeeded=len(image_ids),
            n_failed=0,
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

    # spatial-join species assignment on whichever rows remapped successfully.
    # A failure here (bad/missing shapefile, spatial join error) fails the
    # whole run regardless of remap success, since the single shared CSV is
    # unusable without species columns.
    if mapped_rows:
        try:
            species_df = assign_spatial(
                pd.DataFrame(mapped_rows), str(args.shp),
                max_nearest_distance_m=args.max_nearest_distance_m,
            )
        except Exception as exc:
            logger.error("Spatial species assignment failed: %s", exc)
            report.set_stage_error(f"Spatial species assignment failed: {exc}")
            if isinstance(exc, ZoneTooFarError):
                error_code = ERROR_ZONE_TOO_FAR
            elif "shapefile" in str(exc).lower():
                error_code = ERROR_SHAPEFILE_UNREADABLE
            else:
                error_code = ERROR_SPATIAL_JOIN_FAILED
            report.add_error(
                unit_id="__stage__",
                code=error_code,
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

        if species_catalog is not None:
            try:
                species_df = enrich_with_catalog(species_df, species_catalog)
            except UnknownSpeciesCodeError as exc:
                logger.error("Species catalog enrichment failed: %s", exc)
                report.set_stage_error(f"Species catalog enrichment failed: {exc}")
                report.add_error(
                    unit_id="__stage__",
                    code=ERROR_UNKNOWN_SPECIES_CODE,
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

        output_fieldnames = _build_output_fieldnames(input_fieldnames, species_df.columns)
        output_rows = _to_output_rows(species_df, output_fieldnames)
    else:
        # Nothing remapped (e.g. every image failed E_GRID_NOT_FOUND) — no
        # species_df was ever produced, so we don't know whether this
        # season's shapefile has cultivars. Falls back to just
        # SPECIES_COLUMNS; harmless either way since num_succeeded==0 forces
        # EXIT_FAILURE below regardless of this CSV's exact header.
        output_fieldnames = _build_output_fieldnames(input_fieldnames, SPECIES_COLUMNS)
        output_rows = []

    write_georeferenced_csv(output_rows, output_fieldnames, output_csv_path)

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

    report.set_extra(
        assignment_mode="spatial_join",
        bbot_version=args.bbot_version,
    )
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
