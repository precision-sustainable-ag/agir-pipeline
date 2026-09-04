"""Immutable input-validation results used by later cutout processing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PixelBoundingBox:
    xmin: int
    ymin: int
    xmax: int
    ymax: int

    @property
    def width(self) -> int:
        return self.xmax - self.xmin

    @property
    def height(self) -> int:
        return self.ymax - self.ymin


@dataclass(frozen=True)
class DetectionInput:
    image_id: str
    bounding_box_id: int
    normalized_bbox: tuple[float, float, float, float]
    pixel_bbox: PixelBoundingBox
    class_id: int
    species_id: str
    cultivar_id: str | None = None

    @property
    def identity(self) -> tuple[str, int]:
        return (self.image_id, self.bounding_box_id)


@dataclass(frozen=True)
class ValidatedImageInput:
    image_id: str
    image_path: Path
    mask_path: Path
    width: int
    height: int
    detections: tuple[DetectionInput, ...]


@dataclass(frozen=True)
class BatchValidationResult:
    images: tuple[ValidatedImageInput, ...]
    known_class_ids: frozenset[int]

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def detection_count(self) -> int:
        return sum(len(image.detections) for image in self.images)

