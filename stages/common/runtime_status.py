"""Shared runtime helpers for pipeline stages."""

import hashlib
import logging
import subprocess
from pathlib import Path


def calculate_sha256(file_path: Path) -> str:
    """Calculate a SHA256 checksum for a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return "sha256:" + sha256_hash.hexdigest()


def get_git_commit(logger: logging.Logger | None = None) -> str | None:
    """Get the current repository commit hash when available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if logger is not None:
            logger.warning("Could not determine git commit hash")
        return None
