"""
EXIF metadata update for developed JPG images.

This module patches developed JPG files with:

1. SCINet-style timezone-safe EXIF timestamps derived from epoch-based filenames.
2. Required IFD0 structural tags needed for consistency with RawTherapee/SVS output.
3. Optional config-backed metadata refresh for known-safe camera/DNG-derived tags.

The timestamp convention is:

    DateTimeOriginal = local acquisition time
    OffsetTimeOriginal = UTC offset needed to recover GMT/UTC

Example:
    DateTimeOriginal   = 2025:04:25 10:00:27
    OffsetTimeOriginal = -04:00
    UTC/GMT            = 2025:04:25 14:00:27
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from shutil import which
from typing import Any

import pytz

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExifDateTime:
    """EXIF-compatible local datetime components."""

    base: str       # Example: "2025:04:25 10:00:27"
    subsec: str     # Example: "00"
    offset: str     # Example: "-04:00"


@dataclass(frozen=True)
class ExifTagSpec:
    """
    Mapping from nested YAML metadata to one ExifTool write argument.

    Example:
        section="image"
        key="Orientation"
        exiftool_tag="IFD0:Orientation"
        numeric=True

    Produces:
        -IFD0:Orientation#=1
    """

    section: str
    key: str
    exiftool_tag: str
    numeric: bool = False
    default: Any = None
    required: bool = False


@dataclass(frozen=True)
class ExifUpdateJob:
    """Worker-safe payload for one EXIF update task."""

    file_path: Path
    base_tags: dict[str, Any]
    rewrite_config_tags: bool = False
    debug_args: bool = False


# Required patch tags for developed JPG consistency.
#
# IMPORTANT:
# Keep SamplesPerPixel = 1 for these developed JPGs. Although the JPEG stream
# reports 3 color components, setting IFD0:SamplesPerPixel to 3 has been observed
# to create a tiny all-black image. This field is intentionally patched as 1.
REQUIRED_PATCH_TAGS: tuple[ExifTagSpec, ...] = (
    ExifTagSpec("image", "Orientation", "IFD0:Orientation", numeric=True, default=1),
    ExifTagSpec("image", "SamplesPerPixel", "IFD0:SamplesPerPixel", numeric=True, default=1),
    ExifTagSpec("image", "RowsPerStrip", "IFD0:RowsPerStrip", numeric=True, default=9520),
)


# Known-safe optional tags that may be refreshed from the nested config.
# These are not required because many are already present from raw_to_dng/raw_to_jpg.
OPTIONAL_CONFIG_TAGS: tuple[ExifTagSpec, ...] = (
    ExifTagSpec("camera", "Make", "Make"),
    ExifTagSpec("camera", "Model", "Model"),
    ExifTagSpec("camera", "SerialNumber", "SerialNumber"),
    ExifTagSpec("camera", "LensModel", "LensModel"),
    ExifTagSpec("camera", "FocalLength", "FocalLength"),
    ExifTagSpec("camera", "FocalLengthIn35mmFormat", "FocalLengthIn35mmFormat"),
    ExifTagSpec("camera", "FNumber", "FNumber"),
    ExifTagSpec("camera", "FocalPlaneXResolution", "FocalPlaneXResolution"),
    ExifTagSpec("camera", "FocalPlaneYResolution", "FocalPlaneYResolution"),
    ExifTagSpec("camera", "FocalPlaneResolutionUnit", "FocalPlaneResolutionUnit", numeric=True),
    ExifTagSpec("dng", "BlackLevel", "BlackLevel"),
    ExifTagSpec("dng", "WhiteLevel", "WhiteLevel"),
    ExifTagSpec("dng", "AsShotNeutral", "AsShotNeutral"),
    ExifTagSpec("dng", "BaselineExposure", "BaselineExposure"),
    ExifTagSpec("dng", "CalibrationIlluminant1", "CalibrationIlluminant1", numeric=True),
    ExifTagSpec("dng", "PreviewColorSpace", "PreviewColorSpace", numeric=True),
)


def epoch_to_exif_datetime_eastern(epoch: float) -> ExifDateTime:
    """
    Convert a UTC epoch timestamp to US/Eastern EXIF datetime components.

    Args:
        epoch: Unix epoch timestamp parsed from the image filename. May be
            fractional (e.g. SVCam-style "1784946469.9480019" filenames),
            in which case the fraction contributes to the subsecond field.

    Returns:
        EXIF-compatible local datetime, subsecond string, and UTC offset.
    """
    eastern = pytz.timezone("US/Eastern")
    dt = datetime.fromtimestamp(epoch, tz=pytz.utc).astimezone(eastern)

    base = dt.strftime("%Y:%m:%d %H:%M:%S")
    subsec = f"{dt.microsecond // 10000:02d}"

    raw_offset = dt.strftime("%z")  # Example: "-0400"
    offset = f"{raw_offset[:3]}:{raw_offset[3:]}"  # Example: "-04:00"

    return ExifDateTime(base=base, subsec=subsec, offset=offset)


def parse_epoch_from_filename(file_path: Path) -> float:
    """
    Parse epoch timestamp from filenames like:

        MD_1745589627_some_suffix.jpg
        SVCam_1784946469.9480019_raw.jpg

    Args:
        file_path: JPG path.

    Returns:
        Parsed epoch, as a float when the filename encodes fractional seconds.

    Raises:
        ValueError: If the filename does not contain a valid epoch in position 1.
    """
    parts = file_path.stem.split("_")

    if len(parts) < 2:
        raise ValueError(f"Expected filename with epoch as second underscore field: {file_path.name}")

    try:
        return float(parts[1])
    except ValueError:
        raise ValueError(f"Invalid epoch field {parts[1]!r} in filename: {file_path.name}") from None


def format_exiftool_value(value: Any) -> str:
    """
    Format Python config values for ExifTool CLI assignment.

    ExifTool accepts list-like numeric values as comma-separated strings.
    """
    if isinstance(value, list | tuple):
        return ",".join(map(str, value))

    return str(value)


def build_config_exif_args(
    base_tags: dict[str, Any],
    specs: tuple[ExifTagSpec, ...],
) -> list[str]:
    """
    Build ExifTool CLI args from nested metadata config.

    Args:
        base_tags: Nested config dictionary with sections like image/camera/dng.
        specs: Tag specs defining which config values to write and where.

    Returns:
        ExifTool CLI arguments.

    Example:
        ["-IFD0:Orientation#=1", "-Make=SVS_VISTEK"]
    """
    args: list[str] = []

    for spec in specs:
        section = base_tags.get(spec.section) or {}

        if not isinstance(section, dict):
            if spec.required:
                raise ValueError(f"Config section {spec.section!r} is missing or not a dictionary.")
            continue

        value = section.get(spec.key, spec.default)

        if value is None:
            if spec.required:
                raise ValueError(f"Missing required EXIF config value: {spec.section}.{spec.key}")
            continue

        # Safety rule discovered empirically for these developed JPGs.
        if spec.section == "image" and spec.key == "SamplesPerPixel":
            if value != 1:
                logger.warning(
                    "Overriding image.SamplesPerPixel=%s to 1 for developed JPG safety.",
                    value,
                )
            value = 1

        tag_name = spec.exiftool_tag
        if spec.numeric:
            tag_name = f"{tag_name}#"

        args.append(f"-{tag_name}={format_exiftool_value(value)}")

    return args


def build_datetime_exif_args(edt: ExifDateTime) -> list[str]:
    """Build SCINet-style timezone-safe EXIF timestamp arguments."""
    return [
        f"-EXIF:DateTimeOriginal={edt.base}",
        f"-EXIF:DateTimeDigitized={edt.base}",
        f"-EXIF:SubSecTimeOriginal={edt.subsec}",
        f"-EXIF:SubSecTimeDigitized={edt.subsec}",
        f"-EXIF:OffsetTimeOriginal={edt.offset}",
        f"-EXIF:OffsetTimeDigitized={edt.offset}",
    ]


def build_exiftool_args(
    file_path: Path,
    base_tags: dict[str, Any],
    rewrite_config_tags: bool = False,
) -> list[str]:
    """
    Build the full ExifTool argument list for one JPG.

    This always patches required structural tags and timestamp tags.
    It only refreshes broader config-backed metadata when rewrite_config_tags=True.
    """
    epoch = parse_epoch_from_filename(file_path)
    edt = epoch_to_exif_datetime_eastern(epoch)

    exif_args: list[str] = []

    # Always patch required developed-JPG consistency tags.
    exif_args.extend(build_config_exif_args(base_tags, REQUIRED_PATCH_TAGS))

    # Optional metadata refresh. Default should usually be False.
    if rewrite_config_tags:
        exif_args.extend(build_config_exif_args(base_tags, OPTIONAL_CONFIG_TAGS))

    # Always patch derived datetime fields.
    exif_args.extend(build_datetime_exif_args(edt))

    return exif_args


def write_exif_args_debug_file(file_path: Path, exif_args: list[str]) -> Path:
    """
    Write the exact ExifTool args used for a JPG.

    This is useful for verifying that the nested YAML was translated correctly.
    """
    debug_path = file_path.with_suffix(".exiftool_args.txt")

    with open(debug_path, "w", encoding="utf-8") as f:
        for arg in exif_args:
            f.write(f"{arg}\n")

    return debug_path


def run_exiftool(file_path: Path, exif_args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ExifTool for one JPG."""
    cmd = ["exiftool", "-overwrite_original", *exif_args, str(file_path)]

    logger.debug("[%s] exiftool command: %s", file_path.name, " ".join(cmd))

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _update_exif_worker(job: ExifUpdateJob) -> None:
    """Update EXIF metadata for one JPG. Intended for multiprocessing."""
    file_path = job.file_path

    try:
        exif_args = build_exiftool_args(
            file_path=file_path,
            base_tags=job.base_tags,
            rewrite_config_tags=job.rewrite_config_tags,
        )
    except Exception as e:
        logger.error("[%s] Error building EXIF args: %s", file_path.name, e)
        return

    if job.debug_args:
        debug_path = write_exif_args_debug_file(file_path, exif_args)
        logger.debug("[%s] Wrote ExifTool arg debug file: %s", file_path.name, debug_path)

    result = run_exiftool(file_path, exif_args)

    if result.returncode == 0:
        logger.info("[%s] EXIF updated successfully.", file_path.name)
    else:
        logger.error("[%s] EXIF update failed: %s", file_path.name, result.stderr.strip())

    if result.stdout:
        logger.debug("[%s] STDOUT: %s", file_path.name, result.stdout.strip())

    if result.stderr:
        logger.debug("[%s] STDERR: %s", file_path.name, result.stderr.strip())


def update_exif_single(
    jpg_path: Path,
    exif_tags: dict[str, Any],
    rewrite_config_tags: bool = False,
    debug_args: bool = False,
) -> None:
    """
    Update EXIF tags for a single developed JPG.

    Args:
        jpg_path: Path to JPG file.
        exif_tags: Nested metadata config dictionary.
        rewrite_config_tags: If True, refresh known-safe optional config tags.
        debug_args: If True, write a sidecar file with the ExifTool args.
    """
    job = ExifUpdateJob(
        file_path=jpg_path,
        base_tags=exif_tags,
        rewrite_config_tags=rewrite_config_tags,
        debug_args=debug_args,
    )
    _update_exif_worker(job)


def batch_update(
    jpg_dir: Path,
    exif_tags: dict[str, Any],
    max_workers: int = 16,
    rewrite_config_tags: bool = False,
    debug_args: bool = False,
) -> None:
    """
    Update EXIF tags in all JPG files in a directory using multiprocessing.

    Args:
        jpg_dir: Directory containing developed JPG files.
        exif_tags: Nested metadata config dictionary.
        max_workers: Number of worker processes.
        rewrite_config_tags: If True, refresh known-safe optional config tags.
        debug_args: If True, write one .exiftool_args.txt sidecar per JPG.
    """
    images = sorted(jpg_dir.glob("*.jpg")) + sorted(jpg_dir.glob("*.JPG"))

    if not images:
        logger.warning("No JPG images found in %s for EXIF update.", jpg_dir)
        return

    logger.info("Updating EXIF for %d images in %s", len(images), jpg_dir)

    jobs = [
        ExifUpdateJob(
            file_path=img,
            base_tags=exif_tags,
            rewrite_config_tags=rewrite_config_tags,
            debug_args=debug_args,
        )
        for img in images
    ]

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_update_exif_worker, jobs))


def ensure_exiftool_installed(setup_script_path: Path | None = None) -> None:
    """Ensure ExifTool is available; run setup script if it is not found."""
    if which("exiftool"):
        logger.info("ExifTool is available.")
        return

    logger.warning("ExifTool not found in PATH. Attempting installation...")

    if setup_script_path is None:
        setup_script_path = (
            Path(__file__).parent.parent.parent / "scripts" / "setup_exiftool.sh"
        )

    try:
        result = subprocess.run(
            ["bash", str(setup_script_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ExifTool setup failed: {e.stderr}") from e

    logger.info("ExifTool installed:\n%s", result.stdout)

    if result.stderr:
        logger.warning("Setup stderr:\n%s", result.stderr)