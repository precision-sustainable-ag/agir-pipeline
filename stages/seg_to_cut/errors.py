"""Stable validation errors for ``seg_to_cut``."""

from __future__ import annotations

from typing import Any


class SegToCutError(ValueError):
    """An input or configuration error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class SegToCutConfigError(SegToCutError):
    """Raised when the stage configuration is invalid."""


class SegToCutInputError(SegToCutError):
    """Raised when batch inputs violate the stage contract."""

