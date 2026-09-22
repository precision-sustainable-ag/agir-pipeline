"""Physical bounding-box estimates from per-image FOV reference CSVs.

The coordinates in fov.csv describe the image footprint, not a detection's
georeferenced box. They are assumed to be metres on one surface plane. A
homography captures variation in image scale across that plane; FOV width and
height provide a simpler fallback when the footprint corners are unusable.
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

logger = logging.getLogger(__name__)
_CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")
_IMAGE_CORNERS = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


@dataclass(frozen=True)
class FovAreaReference:
    """Parsed footprint for one image, in metres, from fov.csv."""

    width_m: float | None
    height_m: float | None
    homography: tuple[float, ...] | None
    homography_issue: str | None = None
    dimensions_issue: str | None = None


def _positive_finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _polygon_area(points: np.ndarray) -> float:
    """Shoelace area after translation to limit cancellation at large eastings."""
    local = points - points[0]
    x, y = local[:, 0], local[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2.0


def _homography(row: Mapping[str, str]) -> tuple[tuple[float, ...] | None, str | None]:
    try:
        points = np.array(
            [[float(row[f"{corner}_x"]), float(row[f"{corner}_y"])] for corner in _CORNERS],
            dtype=np.float64,
        )
    except (KeyError, TypeError, ValueError):
        return None, "FOV footprint corners are missing or nonnumeric"
    if not np.isfinite(points).all():
        return None, "FOV footprint corners are nonfinite"
    points -= points[0]
    turns = []
    for i in range(4):
        first = points[(i + 1) % 4] - points[i]
        second = points[(i + 2) % 4] - points[(i + 1) % 4]
        turns.append(first[0] * second[1] - first[1] * second[0])
    if _polygon_area(points) <= 0 or not (all(t > 1e-12 for t in turns) or all(t < -1e-12 for t in turns)):
        return None, "FOV footprint corners do not form a convex quadrilateral"
    matrix = []
    targets = []
    for (u, v), (x, y) in zip(_IMAGE_CORNERS, points, strict=True):
        matrix.append((u, v, 1, 0, 0, 0, -u * x, -v * x))
        matrix.append((0, 0, 0, u, v, 1, -u * y, -v * y))
        targets.extend((x, y))
    try:
        fit = np.linalg.solve(np.asarray(matrix), np.asarray(targets))
    except np.linalg.LinAlgError:
        return None, "FOV footprint cannot define a perspective projection"
    if not np.isfinite(fit).all():
        return None, "FOV perspective projection is nonfinite"
    return tuple(float(value) for value in (*fit, 1.0)), None


def parse_fov_reference(row: Mapping[str, str]) -> FovAreaReference:
    width = _positive_finite(row.get("width"))
    height = _positive_finite(row.get("height"))
    homography, issue = _homography(row)
    return FovAreaReference(
        width_m=width,
        height_m=height,
        homography=homography,
        homography_issue=issue,
        dimensions_issue=None if width is not None and height is not None else "FOV width or height is missing or invalid",
    )


def load_fov_references(root: Path | None) -> dict[str, FovAreaReference]:
    """Load unique image labels from one fov.csv or a batch reference tree.

    Ambiguous labels are discarded rather than matched to an arbitrary
    reconstruction. Unavailable or malformed references do not fail cutouts;
    callers will report a null estimate for affected images.
    """
    if root is None:
        logger.info("FOV area fallback unavailable: batch contains no images to locate references")
        return {}
    paths = [root] if root.is_file() else sorted(root.rglob("fov.csv")) if root.is_dir() else []
    if not paths:
        logger.warning("FOV area fallback unavailable: no fov.csv found under %s", root)
        return {}
    references: dict[str, FovAreaReference] = {}
    ambiguous: set[str] = set()
    for path in paths:
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "label" not in reader.fieldnames:
                    logger.warning("FOV reference ignored: %s has no label column", path)
                    continue
                for row in reader:
                    label = (row.get("label") or "").strip()
                    if not label:
                        logger.warning("FOV reference row ignored: empty label in %s", path)
                        continue
                    if label in references or label in ambiguous:
                        references.pop(label, None)
                        ambiguous.add(label)
                        logger.warning("FOV reference unavailable for image %s: duplicate label", label)
                        continue
                    reference = parse_fov_reference(row)
                    references[label] = reference
                    if reference.homography is None:
                        if reference.width_m is not None and reference.height_m is not None:
                            logger.warning(
                                "FOV corner projection unavailable for image %s: %s; using FOV dimensions",
                                label, reference.homography_issue,
                            )
                        else:
                            logger.warning(
                                "FOV area reference unusable for image %s: %s; %s",
                                label, reference.homography_issue, reference.dimensions_issue,
                            )
        except (OSError, csv.Error) as exc:
            logger.warning("FOV reference ignored: could not read %s: %s", path, exc)
    logger.info("Loaded %d unique FOV area references from %d CSV file(s)", len(references), len(paths))
    return references


def estimate_fov_bbox_area_cm2(
    normalized_bbox: tuple[float, float, float, float] | None,
    reference: FovAreaReference | None,
) -> tuple[float | None, str | None, str | None]:
    """Return (area, method, unavailable reason) without detection world data."""
    if normalized_bbox is None:
        return None, None, "normalized detection box is unavailable"
    if reference is None:
        return None, None, "matching FOV reference is unavailable"
    if not all(math.isfinite(value) for value in normalized_bbox):
        return None, None, "normalized detection box is nonfinite"
    xmin, ymin, xmax, ymax = (min(1.0, max(0.0, value)) for value in normalized_bbox)
    if xmax <= xmin or ymax <= ymin:
        return None, None, "normalized detection box has no area inside the image"
    if reference.homography is not None:
        transform = np.asarray(reference.homography).reshape(3, 3)
        corners = np.asarray(((xmin, ymin, 1), (xmax, ymin, 1), (xmax, ymax, 1), (xmin, ymax, 1)))
        projected = corners @ transform.T
        if np.isfinite(projected).all() and np.all(np.abs(projected[:, 2]) > 1e-12):
            area = _polygon_area(projected[:, :2] / projected[:, 2, None]) * 10_000.0
            if math.isfinite(area) and area > 0:
                return area, "fov_homography", None
    if reference.width_m is not None and reference.height_m is not None:
        area = (xmax - xmin) * (ymax - ymin) * reference.width_m * reference.height_m * 10_000.0
        if math.isfinite(area) and area > 0:
            return area, "fov_dimensions", None
    return None, None, "; ".join(
        issue for issue in (reference.homography_issue, reference.dimensions_issue) if issue
    ) or "FOV area calculation failed"
