"""Input validation and parsing helpers for pipeline stages."""

import re

# Batch format: TX_2024-06-01, MD_2025-01-01, etc.
BATCH_PATTERN = re.compile(r"^([A-Z]{2})_(\d{4}-\d{2}-\d{2})$")


def parse_batch_id(path: str) -> str | None:
    """
    Extract a batch_id like 'TX_2024-06-01' from a file path.

    Scans each path segment for the XX_YYYY-MM-DD pattern and
    returns the first match, or None if no segment matches.
    """
    for segment in str(path).replace("\\", "/").split("/"):
        if BATCH_PATTERN.match(segment):
            return segment
    return None
