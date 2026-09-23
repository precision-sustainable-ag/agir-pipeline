"""Combine ASFM references and apply primary-detection selection."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .primary_selection import select_primary_rows
from .remapper import GridCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CombinedReferences:
    """Batch-level camera and FOV tables assembled from paired ASFM files."""

    camera_fieldnames: tuple[str, ...]
    camera_rows: tuple[dict[str, str], ...]
    fov_fieldnames: tuple[str, ...]
    fov_rows: tuple[dict[str, str], ...]
    source_paths: tuple[str, ...]


def _read_reference_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "label" not in reader.fieldnames:
            raise ValueError(f"Reference CSV has no label column: {path}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def _extend_fieldnames(target: list[str], incoming: list[str]) -> None:
    target.extend(field for field in incoming if field not in target)


def combine_reference_pairs(root: Path) -> CombinedReferences:
    """Combine paired references, keeping the first complete row for each label.

    Source pairs are visited in sorted path order. Camera and FOV rows are
    claimed together, so duplicate handling can never mix reconstructions.
    """
    if not root.is_dir():
        raise ValueError(f"ASFM batch directory does not exist: {root}")

    camera_paths = sorted(root.rglob("camera_reference.csv"))
    fov_paths = sorted(root.rglob("fov.csv"))
    pairs = []
    for camera_path in camera_paths:
        fov_path = camera_path.with_name("fov.csv")
        if fov_path.is_file():
            pairs.append((camera_path, fov_path))
        else:
            logger.warning("Ignoring camera reference without sibling FOV file: %s", camera_path)
    camera_parents = {path.parent for path in camera_paths}
    for fov_path in fov_paths:
        if fov_path.parent not in camera_parents:
            logger.warning("Ignoring FOV file without sibling camera reference: %s", fov_path)
    if not pairs:
        raise ValueError(f"No paired camera_reference.csv and fov.csv found under {root}")

    camera_fieldnames: list[str] = []
    fov_fieldnames: list[str] = []
    combined_camera: dict[str, dict[str, str]] = {}
    combined_fov: dict[str, dict[str, str]] = {}
    source_paths: list[str] = []

    for camera_path, fov_path in pairs:
        camera_fields, camera_rows = _read_reference_table(camera_path)
        fov_fields, fov_rows = _read_reference_table(fov_path)
        _extend_fieldnames(camera_fieldnames, camera_fields)
        _extend_fieldnames(fov_fieldnames, fov_fields)

        camera_by_label: dict[str, dict[str, str]] = {}
        for row in camera_rows:
            label = (row.get("label") or "").strip()
            if not label:
                logger.warning("Ignoring camera reference with empty label in %s", camera_path)
            elif label in camera_by_label:
                logger.warning("Ignoring duplicate camera label %s in %s", label, camera_path)
            else:
                camera_by_label[label] = row

        fov_by_label: dict[str, dict[str, str]] = {}
        for row in fov_rows:
            label = (row.get("label") or "").strip()
            if not label:
                logger.warning("Ignoring FOV reference with empty label in %s", fov_path)
            elif label in fov_by_label:
                logger.warning("Ignoring duplicate FOV label %s in %s", label, fov_path)
            else:
                fov_by_label[label] = row

        complete_labels = camera_by_label.keys() & fov_by_label.keys()
        for label in camera_by_label:
            if label not in complete_labels:
                logger.warning(
                    "Ignoring camera reference without paired FOV label %s in %s",
                    label,
                    camera_path,
                )
                continue
            if label in combined_camera:
                logger.warning(
                    "Ignoring later reference pair for duplicate label %s in %s; first occurrence retained",
                    label,
                    camera_path.parent,
                )
                continue
            combined_camera[label] = camera_by_label[label]
            combined_fov[label] = fov_by_label[label]

        for label in fov_by_label.keys() - camera_by_label.keys():
            logger.warning(
                "Ignoring FOV reference without paired camera label %s in %s", label, fov_path
            )
        source_paths.extend((str(camera_path), str(fov_path)))

    if not combined_camera:
        raise ValueError(f"No complete camera/FOV reference rows found under {root}")
    logger.info(
        "Combined %s camera/FOV source pairs into %s batch reference rows",
        len(pairs),
        len(combined_camera),
    )
    return CombinedReferences(
        camera_fieldnames=tuple(camera_fieldnames),
        camera_rows=tuple(combined_camera.values()),
        fov_fieldnames=tuple(fov_fieldnames),
        fov_rows=tuple(combined_fov.values()),
        source_paths=tuple(source_paths),
    )


def write_combined_references(
    references: CombinedReferences, artifacts_dir: Path, batch_id: str
) -> tuple[Path, Path]:
    """Write flat, batch-named camera and FOV CSVs beside georeferenced output."""
    outputs: list[Path] = []
    for kind, fieldnames, rows in (
        ("camera_reference", references.camera_fieldnames, references.camera_rows),
        ("fov", references.fov_fieldnames, references.fov_rows),
    ):
        path = artifacts_dir / f"{batch_id}_{kind}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        logger.info("Wrote %s combined reference rows to %s", len(rows), path)
        outputs.append(path)
    return outputs[0], outputs[1]


def select_with_references(
    rows: list[dict[str, Any]], cache: GridCache
) -> tuple[list[dict[str, Any]], CombinedReferences]:
    """Select primary detections using one combined batch-level reference pair."""
    references = combine_reference_pairs(cache.grid_dir)
    if not rows:
        return [], references
    if len({row.get("crs") for row in rows}) != 1 or not rows[0].get("crs"):
        raise ValueError("Legacy primary selection requires one common, populated CRS")

    cameras = {row["label"]: row for row in references.camera_rows}
    fovs = {row["label"]: row for row in references.fov_rows}
    dimensions = {}
    for image_id in dict.fromkeys(row["image_id"] for row in rows):
        grid = cache.get(image_id)
        dimensions[image_id] = (grid.sensor_width, grid.sensor_height)
    return select_primary_rows(rows, cameras, fovs, dimensions), references
