"""Planar bounding-box area estimate from fixed camera geometry."""

from __future__ import annotations

import math

from .config import SegToCutConfig


def estimate_camera_bbox_area_cm2(
    normalized_bbox: tuple[float, float, float, float] | None,
    image_width_px: int | None,
    image_height_px: int | None,
    config: SegToCutConfig,
) -> tuple[float | None, str | None]:
    """Estimate cm² from a diagonal sensor size and lens-to-surface height.

    Assumes a perpendicular camera and a flat surface at the configured
    height. The actual image aspect ratio splits the sensor diagonal into
    horizontal and vertical dimensions.
    """
    if normalized_bbox is None:
        return None, "normalized detection box is unavailable"
    if image_width_px is None or image_height_px is None or image_width_px <= 0 or image_height_px <= 0:
        return None, "source image dimensions are unavailable"
    if not all(math.isfinite(value) for value in normalized_bbox):
        return None, "normalized detection box is nonfinite"
    xmin, ymin, xmax, ymax = (min(1.0, max(0.0, value)) for value in normalized_bbox)
    if xmax <= xmin or ymax <= ymin:
        return None, "normalized detection box has no area inside the image"
    if not all(
        math.isfinite(value) and value > 0
        for value in (
            config.camera_focal_length_mm,
            config.camera_sensor_diagonal_mm,
            config.camera_height_cm,
        )
    ):
        return None, "camera focal length, sensor diagonal, or height is invalid"
    diagonal_px = math.hypot(image_width_px, image_height_px)
    sensor_width_mm = config.camera_sensor_diagonal_mm * image_width_px / diagonal_px
    sensor_height_mm = config.camera_sensor_diagonal_mm * image_height_px / diagonal_px
    footprint_width_cm = config.camera_height_cm * sensor_width_mm / config.camera_focal_length_mm
    footprint_height_cm = config.camera_height_cm * sensor_height_mm / config.camera_focal_length_mm
    area = (xmax - xmin) * (ymax - ymin) * footprint_width_cm * footprint_height_cm
    if not math.isfinite(area) or area <= 0:
        return None, "camera area calculation is nonfinite or nonpositive"
    return area, None
