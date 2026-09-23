"""Compatibility imports for detection class resolution.

The implementation is shared by pipeline stages in :mod:`stages.common.class_ids`.
"""

from stages.common.class_ids import (
    FALLBACK_CLASS_ID,
    UINT8_MAX,
    ClassIdIndex,
    ClassIdResolutionError,
    DetectionBox,
    DetectionClassResolution,
    DuplicateDetectionKeyError,
    InvalidClassIdError,
    UnknownSpeciesError,
    build_class_id_index,
    load_class_id_index,
    load_species_catalog,
    resolve_detection_class_ids,
)

__all__ = [
    "FALLBACK_CLASS_ID",
    "UINT8_MAX",
    "ClassIdIndex",
    "ClassIdResolutionError",
    "DetectionBox",
    "DetectionClassResolution",
    "DuplicateDetectionKeyError",
    "InvalidClassIdError",
    "UnknownSpeciesError",
    "build_class_id_index",
    "load_class_id_index",
    "load_species_catalog",
    "resolve_detection_class_ids",
]
