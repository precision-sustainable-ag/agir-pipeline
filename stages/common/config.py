"""Config parsing and path resolution helpers for pipeline stages."""

import re
from pathlib import Path



def parse_batch_id(path: str) -> str | None:
    """
    Extract a batch_id like 'TX_2024-06-01' from a file path.

    Scans each path segment for the XX_YYYY-MM-DD pattern and
    returns the first match, or None if no segment matches.
    """

    # Batch format: TX_2024-06-01, MD_2025-01-01, etc.
    batch_pattern = re.compile(r"^([A-Z]{2})_(\d{4}-\d{2}-\d{2})$")

    for segment in str(path).replace("\\", "/").split("/"):
        candidate = Path(segment).stem
        if batch_pattern.match(segment):
            return segment
        if batch_pattern.match(candidate):
            return candidate
    return None


def resolve_path(path_str: str, base_dir: Path) -> Path:
    """
    Resolve a path string to an absolute Path.

    Args:
        path_str: Path string from config (absolute or relative)
        base_dir: Base directory for resolving relative paths (typically config file parent)

    Returns:
        Absolute Path object
    """
    path = Path(path_str)
    if not path.is_absolute():
        path = base_dir / path
    return path
