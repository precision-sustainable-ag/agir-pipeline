"""
Logging setup shared across all pipeline stages.

Configures the root logger with both a file handler (into the run folder)
and a console handler so every module that calls
``logging.getLogger(__name__)`` automatically inherits both.
"""

import logging
from pathlib import Path


def setup_logging(log_dir: Path) -> Path:
    """Configure root logger with console + file handler in the run folder.

    Parameters
    ----------
    log_dir : Path
        Directory where ``run.log`` will be written (typically the run folder).

    Returns
    -------
    Path
        Absolute path to the created log file.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run.log"

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
    )

    # Get the root logger — all module loggers (getLogger(__name__)) inherit from this
    root = logging.getLogger()

    # Set severity level
    root.setLevel("INFO")

    # Remove any previously attached handlers to avoid duplicate log lines
    root.handlers.clear()

    # File handler — writes log lines to run.log in the run folder
    fh = logging.FileHandler(log_path)
    fh.setLevel("INFO")
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # Console handler — prints log lines to stdout
    ch = logging.StreamHandler()
    ch.setLevel("INFO")
    ch.setFormatter(formatter)
    root.addHandler(ch)

    return log_path
