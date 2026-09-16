"""Reproducible measurements of cleaned masks and original RGB crops.

These helpers return JSON-compatible cutout_props fields. The processor/writer
integration and run-report emission are handled by later implementation steps.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
import skimage
from numpy.typing import NDArray
from pyproj import CRS
from pyproj.exceptions import CRSError
from skimage.measure import blur_effect
from skimage.morphology import convex_hull_image

from .cleanup import resolve_border_band_widths
from .config import SegToCutConfig, parse_config
from .contracts import AreaMetricInput, PixelBoundingBox, WorldBoundingBox

SIDES = ("top", "bottom", "left", "right")
BLUR_H_SIZE = 11


def measurement_provenance(config: SegToCutConfig) -> dict[str, Any]:
    """Return the measurement contract to include in a future run report."""
    return {
        "cutout_version": config.cutout_version,
        "libraries": {
            "numpy": np.__version__,
            "opencv": cv2.__version__,
            "scikit_image": skimage.__version__,
        },
        "rgb_statistics": {
            "pixel_selection": "whole_unmasked_crop",
            "channel_order": "RGB",
            "scale_divisor": 255,
            "std_ddof": 0,
            "input": "before_jpeg_encoding",
        },
        "blur_effect": {
            "algorithm": "skimage.measure.blur_effect",
            "pixel_selection": "whole_black_masked_cutout",
            "input": "before_png_encoding",
            "background": "black",
            "h_size": BLUR_H_SIZE,
            "channel_axis": 2,
            "reduce_func": "max",
            "higher_is_blurrier": True,
            "undefined": None,
        },
        "edge_cut": {
            "band_fraction": config.border_band_fraction,
            "threshold": config.edge_threshold,
            "fraction": "foreground_pixels / actual_band_pixels",
            "comparison": "strictly_greater",
        },
        "solidity": {
            "algorithm": "skimage.morphology.convex_hull_image",
            "offset_coordinates": True,
            "include_borders": True,
            "scope": "all_cleaned_foreground",
        },
        "component_connectivity": 8,
        "area_metrics": {
            "bbox_area_source": config.bbox_area_source,
            "world_area_algorithm": "planar_shoelace",
            "world_area_output_unit": "cm2",
            "camera_area_status": "unavailable_pending_xyz_camera_source",
            "species_bbox_grouping": "resolved_category",
            "species_bbox_min_sample_size": config.species_bbox_min_sample_size,
            "abnormal_bbox_size_threshold": config.abnormal_bbox_size_threshold,
        },
    }


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    """Return whether two non-adjacent polygon edges intersect or are collinear."""

    def orientation(
        start: tuple[float, float],
        end: tuple[float, float],
        point: tuple[float, float],
    ) -> float:
        return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (
            point[0] - start[0]
        )

    values = (
        orientation(first_start, first_end, second_start),
        orientation(first_start, first_end, second_end),
        orientation(second_start, second_end, first_start),
        orientation(second_start, second_end, first_end),
    )
    if any(math.isclose(value, 0.0, abs_tol=1e-12) for value in values):
        return True
    return (values[0] > 0) != (values[1] > 0) and (values[2] > 0) != (values[3] > 0)


def calculate_bbox_area_cm2(world_bbox: WorldBoundingBox | None) -> float | None:
    """Calculate planar quadrilateral area in cm² for a supported projected CRS."""

    return _bbox_area_with_reason(world_bbox)[0]


def _bbox_area_with_reason(
    world_bbox: WorldBoundingBox | None,
) -> tuple[float | None, str | None]:

    if world_bbox is None:
        return None, "world coordinates or CRS missing, incomplete, or malformed"
    points = world_bbox.polygon
    try:
        coordinates_are_finite = all(
            math.isfinite(value) for point in points for value in point
        )
    except TypeError:
        return None, "world corners contain nonnumeric coordinates"
    if len(set(points)) != 4 or not coordinates_are_finite:
        return None, "world corners are repeated or contain nonfinite coordinates"
    if _segments_intersect(points[0], points[1], points[2], points[3]) or _segments_intersect(
        points[1], points[2], points[3], points[0]
    ):
        return None, "world box has intersecting or collinear edges"
    try:
        crs = CRS.from_user_input(world_bbox.crs)
    except (CRSError, TypeError, ValueError):
        return None, "CRS could not be parsed"
    if not crs.is_projected or len(crs.axis_info) < 2:
        return None, "CRS is not projected or lacks two coordinate axes"
    x_factor = crs.axis_info[0].unit_conversion_factor
    y_factor = crs.axis_info[1].unit_conversion_factor
    if not all(
        factor is not None and math.isfinite(factor) and factor > 0
        for factor in (x_factor, y_factor)
    ):
        return None, "CRS axis units cannot be converted to metres"
    # Translate near the origin before the shoelace sum to avoid cancellation
    # for large UTM eastings/northings surrounding a small plant box.
    origin_x, origin_y = points[0]
    local_points = tuple((x - origin_x, y - origin_y) for x, y in points)
    twice_native_area = abs(
        sum(
            point[0] * local_points[(index + 1) % len(local_points)][1]
            - local_points[(index + 1) % len(local_points)][0] * point[1]
            for index, point in enumerate(local_points)
        )
    )
    native_area = twice_native_area / 2.0
    area_cm2 = native_area * x_factor * y_factor * 10_000.0
    if not math.isfinite(area_cm2) or area_cm2 <= 0:
        return None, "calculated world area is nonfinite or nonpositive"
    return float(area_cm2), None


def null_metadata_reasons(
    metadata: dict[str, Any], *, world_bbox: WorldBoundingBox | None, config: SegToCutConfig
) -> dict[str, str]:
    """Explain unavailable fields without changing the cutout metadata contract."""

    reasons: dict[str, str] = {}
    props = metadata["cutout_props"]
    if props["bbox_area_cm2"] is None:
        reasons["bbox_area_cm2"] = (
            "camera area calculation unavailable pending authoritative XYZ input"
            if config.bbox_area_source == "camera"
            else _bbox_area_with_reason(world_bbox)[1] or "physical area unavailable"
        )
    if props["species_mean_bbox_area_cm2"] is None:
        reasons["species_mean_bbox_area_cm2"] = (
            f"category has {props['species_bbox_sample_size']} valid area samples; "
            f"requires at least {config.species_bbox_min_sample_size}"
        )
    if props["species_bbox_area_ratio"] is None:
        dependencies = [
            field for field in ("bbox_area_cm2", "species_mean_bbox_area_cm2")
            if props[field] is None
        ]
        reasons["species_bbox_area_ratio"] = (
            "unavailable dependencies: " + ", ".join(dependencies)
            if dependencies else "category mean is nonpositive or current area is invalid"
        )
    if props["abnormal_bbox_size"] is None:
        reasons["abnormal_bbox_size"] = "species_bbox_area_ratio is unavailable"
    if metadata["datetime"] is None:
        reasons["datetime"] = "source JPG has no valid EXIF DateTimeOriginal"
    if metadata["lens_model"] is None:
        reasons["lens_model"] = "source JPG has no EXIF LensModel and --lens-model was not supplied"
    if metadata["season"] is None:
        reasons["season"] = "--season was not supplied"
    return reasons


def calculate_area_properties(
    world_bbox: WorldBoundingBox | None, *, config: SegToCutConfig
) -> dict[str, float | None]:
    """Return the configured physical bounding-box area."""

    if config.bbox_area_source == "georeferenced_csv":
        area = calculate_bbox_area_cm2(world_bbox)
    elif config.bbox_area_source == "camera":
        # The future camera calculation will consume the authoritative XYZ
        # camera-location input. Until that contract exists, the value is null.
        area = None
    else:
        raise ValueError(f"unsupported bbox area source: {config.bbox_area_source!r}")
    return {"bbox_area_cm2": area}


def _valid_area(value: float | None) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _area_group(record: AreaMetricInput) -> tuple[str, str]:
    if not record.species_id:
        raise ValueError("species_id must be non-empty")
    if record.cultivar_id:
        return ("cultivar", record.cultivar_id)
    return ("species", record.species_id)


def finalize_species_bbox_metrics(
    records: Sequence[AreaMetricInput],
    *,
    config: SegToCutConfig,
) -> dict[tuple[str, int], dict[str, float | int | bool | None]]:
    """Finalize deterministic per-category statistics for valid cutouts.

    Callers pass detections that remain valid after mask cleanup. Only finite,
    positive bounding-box areas contribute to a group. When the configured
    source cannot provide an area, group sample sizes are zero and all derived
    values remain null.
    """

    parse_config(vars(config))
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for record in records:
        if record.identity in seen:
            raise ValueError(f"duplicate area-metric identity: {record.identity!r}")
        seen.add(record.identity)
        group = _area_group(record)
        if _valid_area(record.bbox_area_cm2):
            groups[group].append(float(record.bbox_area_cm2))

    finalized: dict[tuple[str, int], dict[str, float | int | bool | None]] = {}
    for record in records:
        values = groups[_area_group(record)]
        sample_size = len(values)
        mean = (
            float(math.fsum(values) / sample_size)
            if sample_size >= config.species_bbox_min_sample_size
            else None
        )
        ratio = None
        abnormal = None
        if mean is not None and mean > 0 and _valid_area(record.bbox_area_cm2):
            ratio = float(record.bbox_area_cm2 / mean)
            abnormal = abs(ratio - 1.0) > config.abnormal_bbox_size_threshold
        finalized[record.identity] = {
            "species_mean_bbox_area_cm2": mean,
            "species_bbox_sample_size": sample_size,
            "species_bbox_area_ratio": ratio,
            "abnormal_bbox_size": abnormal,
        }
    return finalized


def calculate_crop_properties(
    rgb_crop: NDArray[np.uint8], cleaned_mask: NDArray[np.uint8]
) -> dict[str, Any]:
    """Measure historical RGB and blur properties before artifact encoding.

    Callers loading through OpenCV must convert BGR to RGB before calling.
    RGB statistics use every unmasked crop pixel. Blur uses the whole rectangular
    crop after setting non-target pixels to black, matching ``GenCutoutProps``.
    """
    if (
        not isinstance(rgb_crop, np.ndarray)
        or rgb_crop.dtype != np.uint8
        or rgb_crop.ndim != 3
        or rgb_crop.shape[2] != 3
        or min(rgb_crop.shape[:2]) == 0
    ):
        raise ValueError("rgb_crop must be a non-empty HxWx3 uint8 RGB array")
    if (
        not isinstance(cleaned_mask, np.ndarray)
        or cleaned_mask.dtype != np.uint8
        or cleaned_mask.ndim != 2
        or cleaned_mask.shape != rgb_crop.shape[:2]
    ):
        raise ValueError("cleaned_mask must be a uint8 array matching the RGB crop")

    normalized = rgb_crop.astype(np.float64) / 255.0
    cutout = np.where(cleaned_mask[..., None] != 0, rgb_crop, 0).astype(np.uint8)
    blur = None
    if min(rgb_crop.shape[:2]) >= 3:
        with np.errstate(divide="ignore", invalid="ignore"):
            score = blur_effect(cutout, channel_axis=2)
        if np.isfinite(score):
            blur = float(score)
    return {
        "cropout_rgb_mean": normalized.mean(axis=(0, 1)).tolist(),
        "cropout_rgb_std": normalized.std(axis=(0, 1), ddof=0).tolist(),
        "blur_effect": blur,
    }


def calculate_mask_properties(
    cleaned_mask: NDArray[np.uint8],
    *,
    expected_class_id: int,
    pixel_bbox: PixelBoundingBox,
    image_width: int,
    image_height: int,
    config: SegToCutConfig,
) -> dict[str, Any]:
    """Measure a non-empty class-coded mask after border-sweep cleanup.

    Each side uses its own band, clipped to the corresponding crop dimension.
    Bands can overlap; occupancy uses the actual number of pixels in each band.
    Solidity counts pixels in a single raster hull across all retained components.
    """
    # Validate direct dataclass callers as well as YAML-loaded configurations.
    parse_config(vars(config))
    if (
        not isinstance(cleaned_mask, np.ndarray)
        or cleaned_mask.dtype != np.uint8
        or cleaned_mask.ndim != 2
        or cleaned_mask.size == 0
    ):
        raise ValueError("cleaned_mask must be a non-empty 2D uint8 array")
    if (
        isinstance(expected_class_id, bool)
        or not isinstance(expected_class_id, int)
        or not 1 <= expected_class_id <= 255
    ):
        raise ValueError("expected_class_id must be an integer in 1..255")
    if np.any((cleaned_mask != 0) & (cleaned_mask != expected_class_id)):
        raise ValueError("cleaned_mask contains an unexpected class value")
    target = cleaned_mask == expected_class_id
    if not target.any():
        raise ValueError("empty foreground must be skipped before measuring")
    coordinates = (
        image_width,
        image_height,
        pixel_bbox.xmin,
        pixel_bbox.ymin,
        pixel_bbox.xmax,
        pixel_bbox.ymax,
    )
    if any(isinstance(v, bool) or not isinstance(v, int) for v in coordinates):
        raise ValueError("image dimensions and pixel coordinates must be integers")
    if not (
        0 <= pixel_bbox.xmin < pixel_bbox.xmax <= image_width
        and 0 <= pixel_bbox.ymin < pixel_bbox.ymax <= image_height
        and cleaned_mask.shape == (pixel_bbox.height, pixel_bbox.width)
    ):
        raise ValueError("mask dimensions and clipped source bounding box must agree")

    vertical_band, horizontal_band = resolve_border_band_widths(
        target.shape, config.border_band_fraction
    )
    fractions = {
        "top": float(target[:vertical_band, :].mean()),
        "bottom": float(target[-vertical_band:, :].mean()),
        "left": float(target[:, :horizontal_band].mean()),
        "right": float(target[:, -horizontal_band:].mean()),
    }
    flagged = [side for side in SIDES if fractions[side] > config.edge_threshold]
    source_boundary = {
        "top": pixel_bbox.ymin == 0,
        "bottom": pixel_bbox.ymax == image_height,
        "left": pixel_bbox.xmin == 0,
        "right": pixel_bbox.xmax == image_width,
    }
    placement = "unrestricted"
    if len(flagged) == 1:
        placement = f"{flagged[0]}_edge_only"
    elif len(flagged) == 2 and set(flagged) not in ({"top", "bottom"}, {"left", "right"}):
        placement = f"{flagged[0]}_{flagged[1]}_corner_only"
    elif len(flagged) >= 2:
        placement = "unsuitable"

    component_count, _ = cv2.connectedComponents(target.astype(np.uint8), connectivity=8)
    hull = convex_hull_image(target, offset_coordinates=True, include_borders=True)
    return {
        "extends_border": bool(
            target[0, :].any() or target[-1, :].any() or target[:, 0].any() or target[:, -1].any()
        ),
        "edge_cut": {
            "flagged": bool(flagged),
            "threshold": config.edge_threshold,
            "band_fraction": config.border_band_fraction,
            "band_width_px": {
                "top_bottom": vertical_band,
                "left_right": horizontal_band,
            },
            "flagged_sides": flagged,
            "source_image_sides": [side for side in flagged if source_boundary[side]],
            "detection_box_truncation_sides": [
                side for side in flagged if not source_boundary[side]
            ],
            "synthetic_placement": placement,
            "plant_fraction": fractions,
        },
        "num_components": int(component_count - 1),
        "solidity": float(np.count_nonzero(target) / np.count_nonzero(hull)),
    }


def calculate_cutout_properties(
    rgb_crop: NDArray[np.uint8],
    cleaned_mask: NDArray[np.uint8],
    *,
    expected_class_id: int,
    pixel_bbox: PixelBoundingBox,
    image_width: int,
    image_height: int,
    config: SegToCutConfig,
    world_bbox: WorldBoundingBox | None = None,
) -> dict[str, Any]:
    """Combine mask and appearance fields for a validated, cleaned cutout."""
    properties = calculate_mask_properties(
        cleaned_mask,
        expected_class_id=expected_class_id,
        pixel_bbox=pixel_bbox,
        image_width=image_width,
        image_height=image_height,
        config=config,
    )
    area = calculate_area_properties(world_bbox, config=config)
    if rgb_crop.shape[:2] != cleaned_mask.shape:
        raise ValueError("RGB crop and cleaned mask dimensions must agree")
    appearance = calculate_crop_properties(rgb_crop, cleaned_mask)
    return {**properties, **appearance, **area}
