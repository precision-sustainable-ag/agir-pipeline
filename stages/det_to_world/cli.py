#!/usr/bin/env python3
"""
CLI for remapping detection bboxes into world coordinates and assigning species.
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from packaging.version import Version

from stages import EXIT_SUCCESS, EXIT_PARTIAL, EXIT_FAILURE, EXIT_CONFIG_ERROR, ITEM_OK
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
    ERROR_REMAP_FAILED,
    ERROR_SHAPEFILE_UNREADABLE,
    ERROR_SPATIAL_JOIN_FAILED,
    ERROR_UNKNOWN_SPECIES_CODE,
    ERROR_PRIMARY_SELECTION_FAILED,
    WARNING_NO_DETECTION_FILE,
    WARNING_ZONE_TOO_FAR,
)
from .primary_inputs import (
    combine_reference_pairs,
    select_with_references,
    write_combined_references,
)
from .remapper import (
    GEO_COLUMNS,
    GridCache,
    ImageRemapResult,
    NO_DETECTIONS_ASSIGNMENT_METHOD,
    is_no_detection_row,
    load_detection_rows,
    remap_rows,
    resolve_detection_source,
    write_georeferenced_csv,
)
from .species import (
    DEFAULT_MAX_NEAREST_DISTANCE_M,
    UNKNOWN_NOT_GEOREFERENCED_METHOD,
    UNKNOWN_SPECIES_ID,
    UNKNOWN_TOO_FAR_METHOD,
    UnknownSpeciesCodeError,
    assign_fallback_labels,
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
    """Input columns + GEO_COLUMNS + is_primary + whichever EXTRA_COLUMNS are actually
    present in the species-assignment result, preserving that fixed order.
    """
    fieldnames = list(input_fieldnames)
    fieldnames += [column for column in GEO_COLUMNS if column not in fieldnames]
    if "is_primary" not in fieldnames:
        fieldnames.append("is_primary")
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


def _append_no_detection_rows(
    output_rows: list[dict[str, Any]],
    fieldnames: list[str],
    input_rows: list[dict[str, str]],
) -> None:
    """Add one identifiable, otherwise blank output row for each empty TXT file."""
    seen: set[str] = set()
    for input_row in input_rows:
        if not is_no_detection_row(input_row):
            continue
        image_id = str(input_row["image_id"])
        if image_id in seen:
            continue
        seen.add(image_id)
        placeholder = dict.fromkeys(fieldnames, "")
        placeholder["image_id"] = image_id
        # Downstream readers use this sentinel to distinguish a deliberate
        # zero-detection row from a malformed detection row.
        placeholder["assignment_method"] = NO_DETECTIONS_ASSIGNMENT_METHOD
        output_rows.append(placeholder)


def _expected_image_ids(batch_root: Path) -> tuple[set[str], bool]:
    """Every image in the batch, from the raw_to_jpg/jpg_to_det manifests staged
    next to detections/ (the job's images/ is only a sample, but is included too).
    Returns (image_ids, whether any manifest was found).
    """
    image_ids: set[str] = set()
    found_manifest = False
    for stage in ("raw_to_jpg", "jpg_to_det"):
        manifest_path = batch_root / stage / "manifest.json"
        if not manifest_path.is_file():
            continue
        found_manifest = True
        with open(manifest_path, encoding="utf-8") as f:
            items = json.load(f).get("items") or []
        for item in items:
            # A failed raw_to_jpg item never produced a JPG; a failed jpg_to_det
            # item is exactly an image with no detection file.
            if stage == "raw_to_jpg" and item.get("status") != "ok":
                continue
            if item.get("image_id"):
                image_ids.add(str(item["image_id"]))
    images_dir = batch_root / "images"
    if images_dir.is_dir():
        image_ids.update(path.stem for path in images_dir.glob("*.jpg"))
    return image_ids, found_manifest


def _has_asfm_references(grid_dir: Path | None) -> bool:
    return grid_dir is not None and grid_dir.is_dir() and any(grid_dir.rglob("camera_reference.csv"))


def _unmapped_image_results(rows: list[dict[str, str]]) -> list[ImageRemapResult]:
    """Per-image results for a batch that isn't georeferenced at all: every
    detection row is kept un-georeferenced (no-detections placeholders are
    appended separately, as in remap_rows)."""
    rows_by_image: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_image.setdefault(row["image_id"], []).append(row)
    results = []
    for image_id, image_rows in rows_by_image.items():
        detections = [row for row in image_rows if not is_no_detection_row(row)]
        results.append(ImageRemapResult(
            image_id=image_id,
            status=ITEM_OK,
            n_input_rows=len(detections),
            n_output_rows=0,
            unmapped_rows=detections,
        ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remap jpg_to_det detections into world coordinates using ASFM NPZ "
        "grids, then assign species via shapefile zone or monoculture config."
    )
    parser.add_argument(
        "--i", type=Path, required=True,
        help="Detection CSV from jpg_to_det, or a detections directory: its <batch_id>.csv is "
        "used if present, otherwise the per-image .txt files (older batches without a CSV).",
    )
    parser.add_argument(
        "--g", type=Path, default=None,
        help="ASFM batch directory (per-image NPZ grids plus camera_reference.csv/fov.csv). "
        "Required unless --species is supplied; a --species batch without ASFM references "
        "is written entirely un-georeferenced.",
    )
    parser.add_argument("--o", type=Path, required=True, help="Output directory.")
    parser.add_argument("--batch-id", type=str, default=None, help="Batch ID. Auto-inferred from input path if omitted.")
    parser.add_argument(
        "--skip-remap", action="store_true",
        help="Don't georeference even if ASFM output exists (requires --species): every "
        "detection is written with blank world coordinates and is_primary False.",
    )
    parser.add_argument("--shp", type=Path, default=None, help="Species zone shapefile. Required unless --species is supplied.")
    parser.add_argument("--species", type=str, default=None, help="Species code for monoculture batches, assigned to every detection whether or not it was georeferenced.")
    parser.add_argument(
        "--max-nearest-distance-m", type=float, default=DEFAULT_MAX_NEAREST_DISTANCE_M,
        help="Max distance (zone shapefile CRS units, meters for UTM) a detection outside every "
        f"zone polygon may fall back to its nearest zone; farther ones get PLANT (default: {DEFAULT_MAX_NEAREST_DISTANCE_M}).",
    )
    parser.add_argument("--bbot-version", type=str, required=True, help="BBot version string.")
    parser.add_argument(
        "--species-catalog", type=Path, default=DEFAULT_SPECIES_CATALOG,
        help="Flat species/cultivar reference catalog (see orchestrator/species_catalog.py). "
        "Enrichment columns are skipped with a warning if this file is missing or unreadable.",
    )

    args = parser.parse_args()

    if args.skip_remap and args.species is None:
        logger.error("--skip-remap requires --species (no location to zone-assign from).")
        return EXIT_CONFIG_ERROR

    batch_id = args.batch_id or parse_batch_id(str(args.i))
    if not batch_id:
        logger.error("Could not determine batch_id. Pass --batch-id or use a path containing XX_YYYY-MM-DD.")
        return EXIT_CONFIG_ERROR

    if not args.i.exists():
        logger.error("Input detection path does not exist: %s", args.i)
        if args.i.suffix == ".csv":
            logger.error("For batches without a CSV, pass the detections directory as --i to use its .txt files.")
        return EXIT_CONFIG_ERROR
    # Georeferencing (and so primary selection) needs ASFM camera/FOV
    # references. Only a monoculture batch can do without: it has a species
    # without a location, so its detections are written un-georeferenced.
    georeference = not args.skip_remap and _has_asfm_references(args.g)
    if not georeference and args.species is None:
        logger.error(
            "Georeferencing needs --g pointing at ASFM camera/FOV references (got %s); "
            "only a --species batch can run without them.",
            args.g,
        )
        return EXIT_CONFIG_ERROR
    if args.species is None and (args.shp is None or not args.shp.exists()):
        logger.error("Species assignment requires --species or an existing --shp: %s", args.shp)
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
        deps_id="scipy,geopandas,packaging,shapely",
    )

    # a directory input is its own root; a CSV's root is the folder holding it
    input_root = args.i if args.i.is_dir() else args.i.parent

    try:
        # load csv input (or per-image .txt files when a directory has no batch csv)
        det_source = resolve_detection_source(args.i, batch_id)
        logger.info("Reading detections from %s", det_source)
        input_fieldnames, rows = load_detection_rows(det_source)
    except Exception as exc:
        logger.error("Invalid detection input: %s", exc)
        report.set_stage_error(f"Invalid detection input: {exc}")
        report.add_error(
            unit_id="__stage__",
            code=ERROR_CSV_INVALID,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        report.stop(EXIT_CONFIG_ERROR)
        report.set_inputs(
            input_root=str(input_root),
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

    # Images with no detection file at all (rare: jpg_to_det failed on them)
    # still get a placeholder row, identified from the batch's manifests.
    detections_dir = det_source if det_source.is_dir() else det_source.parent
    expected_image_ids, found_manifest = _expected_image_ids(detections_dir.parent)
    if not found_manifest:
        logger.warning(
            "No raw_to_jpg/jpg_to_det manifest under %s; images with no detection "
            "file can't be identified",
            detections_dir.parent,
        )
    missing_detection_ids = sorted(expected_image_ids - {row["image_id"] for row in rows})
    for image_id in missing_detection_ids:
        message = f"No detection file for image {image_id}; adding it as a no-detections row"
        logger.warning(message)
        report.add_warning(
            unit_id=image_id,
            code=WARNING_NO_DETECTION_FILE,
            warning_type="NoDetectionFileWarning",
            message=message,
            meta={"image_id": image_id},
        )
        rows.append({**dict.fromkeys(input_fieldnames, ""), "image_id": image_id})

    image_ids = []
    seen_image_ids = set()
    for row in rows:
        image_id = row["image_id"]
        if image_id in seen_image_ids:
            continue
        seen_image_ids.add(image_id)
        image_ids.append(image_id)


    report.set_inputs(
        input_root=str(input_root),
        n_units_discovered=len(image_ids),
        inputs_manifest_path=str(det_source),
    )

    output_csv_path = artifacts_dir / f"{batch_id}_georeferenced.csv"

    if georeference:
        # remap rows and handle errors
        grid_cache = GridCache(args.g)
        try:
            # Only images with a camera/FOV reference row are georeferenced, since
            # primary selection needs one for every mapped image.
            combined_references = combine_reference_pairs(args.g)
            mapped_rows, image_results = remap_rows(
                rows, args.g, cache=grid_cache,
                referenced_image_ids={row["label"] for row in combined_references.camera_rows},
            )
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

        try:
            mapped_rows = select_with_references(mapped_rows, grid_cache, combined_references)
            n_primary = sum(bool(row["is_primary"]) for row in mapped_rows)
            report.set_extra(
                primary_reference_paths=list(combined_references.source_paths),
                combined_camera_reference_rows=len(combined_references.camera_rows),
                combined_fov_rows=len(combined_references.fov_rows),
                n_primary_detections=n_primary,
                n_non_primary_detections=len(mapped_rows) - n_primary,
            )
            logger.info(
                "Primary selection: %s/%s detections primary",
                n_primary,
                len(mapped_rows),
            )
        except Exception as exc:
            logger.error("Primary selection failed: %s", exc)
            report.set_stage_error(f"Primary selection failed: {exc}")
            report.add_error(
                unit_id="__stage__",
                code=ERROR_PRIMARY_SELECTION_FAILED,
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
    else:
        logger.warning(
            "Not georeferencing batch %s (%s); writing all detections un-georeferenced "
            "with monoculture species %s",
            batch_id,
            "--skip-remap" if args.skip_remap
            else "no --g given" if args.g is None
            else f"no ASFM camera/FOV references under {args.g}",
            args.species,
        )
        combined_references = None
        mapped_rows, image_results = [], _unmapped_image_results(rows)

    # Detections that couldn't be georeferenced are still written out, with
    # blank world coordinates and is_primary False. Without a location they
    # can't be zone-assigned: they keep the monoculture species if there is
    # one, else get the unknown-plant species (color checkers are relabeled below).
    unmapped_rows = [row for result in image_results for row in result.unmapped_rows]

    # Assign species on whichever rows remapped successfully.
    # A failure here (bad/missing shapefile, spatial join error) fails the
    # whole run regardless of remap success, since the single shared CSV is
    # unusable without species columns.
    if mapped_rows or unmapped_rows:
        try:
            frames = []
            if mapped_rows:
                if args.species is not None:
                    mapped_df = assign_monoculture(pd.DataFrame(mapped_rows), args.species)
                else:
                    mapped_df = assign_spatial(
                        pd.DataFrame(mapped_rows), str(args.shp),
                        max_nearest_distance_m=args.max_nearest_distance_m,
                    )
                frames.append(pd.DataFrame(mapped_df))
            if unmapped_rows:
                unmapped_df = pd.DataFrame(unmapped_rows)
                if args.species is not None:
                    unmapped_df = assign_monoculture(unmapped_df, args.species)
                else:
                    unmapped_df["species_id"] = UNKNOWN_SPECIES_ID
                    unmapped_df["assignment_method"] = UNKNOWN_NOT_GEOREFERENCED_METHOD
                unmapped_df["is_primary"] = False
                frames.append(unmapped_df)
            species_df = pd.concat(frames, ignore_index=True)

            beyond = species_df[species_df["assignment_method"] == UNKNOWN_TOO_FAR_METHOD]
            for row in beyond.itertuples():
                report.add_warning(
                    unit_id=f"{row.image_id}:{row.bounding_box_id}",
                    code=WARNING_ZONE_TOO_FAR,
                    warning_type="ZoneTooFarWarning",
                    message=(
                        f"Detection {row.bounding_box_id} for image {row.image_id} is more than "
                        f"{args.max_nearest_distance_m}m outside every zone polygon; "
                        f"assigned {UNKNOWN_SPECIES_ID}"
                    ),
                    meta={"image_id": row.image_id, "bounding_box_id": row.bounding_box_id},
                )

            species_df = assign_fallback_labels(species_df, species_catalog)
            if "class_id" in species_df.columns:
                # Mixing in rows without a zone class_id turns the column float ("28.0").
                species_df["class_id"] = pd.to_numeric(species_df["class_id"]).astype("Int64")
        except Exception as exc:
            logger.error("Species assignment failed: %s", exc)
            report.set_stage_error(f"Species assignment failed: {exc}")
            if "shapefile" in str(exc).lower():
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
        # No detection rows at all (every image is a no-detections placeholder) — no
        # species_df was ever produced, so we don't know whether this
        # season's shapefile has cultivars. Falls back to just
        # SPECIES_COLUMNS; harmless either way since num_succeeded==0 forces
        # EXIT_FAILURE below regardless of this CSV's exact header.
        output_fieldnames = _build_output_fieldnames(input_fieldnames, SPECIES_COLUMNS)
        output_rows = []

    _append_no_detection_rows(output_rows, output_fieldnames, rows)
    write_georeferenced_csv(output_rows, output_fieldnames, output_csv_path)
    # artifact key -> path, for the manifest and report. A batch that wasn't
    # georeferenced has no camera/FOV references to write.
    artifact_paths = {"georeferenced_csv": output_csv_path}
    if combined_references is not None:
        artifact_paths["camera_reference_csv"], artifact_paths["fov_csv"] = (
            write_combined_references(combined_references, artifacts_dir, batch_id)
        )

    num_succeeded = 0
    num_failed = 0

    item_artifacts = {
        f"{key}_path": str(path.relative_to(artifacts_dir)) for key, path in artifact_paths.items()
    }
    item_checksums = {f"{key}_path": calculate_sha256(path) for key, path in artifact_paths.items()}
    item_sizes = {f"{key}_path": path.stat().st_size for key, path in artifact_paths.items()}

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
                artifacts=item_artifacts,
                checksum=item_checksums,
                size_bytes=item_sizes,
            )
            continue

        num_failed += 1
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

    # Images with no ASFM grid coverage are OK items with a W_GRID_NOT_FOUND
    # warning (their detections are in the CSV un-georeferenced), so only
    # actual remap errors count toward EXIT_PARTIAL/EXIT_FAILURE.

    if num_succeeded == 0:
        exit_code = EXIT_FAILURE
    elif num_failed > 0:
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_SUCCESS

    report.set_extra(
        assignment_mode="monoculture_config" if args.species is not None else "spatial_join",
        detection_source="txt_dir" if det_source.is_dir() else "csv",
        georeferenced=georeference,
        n_not_georeferenced_detections=len(unmapped_rows),
        n_missing_detection_files=len(missing_detection_ids),
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
    for artifact_type, path in artifact_paths.items():
        report.add_artifact_type(artifact_type=artifact_type, path=str(path), n_files=1)
    report.set_pointers(logs_path=str(log_path))

    report.write(run_dir / "run_report.json")
    manifest.write(run_dir / "manifest.json")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
