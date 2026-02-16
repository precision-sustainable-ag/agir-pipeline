"""
RAW -> DNG -> JPG processing pipeline.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml
import numpy as np

from . import (
    ERROR_DNG_CONVERSION_FAILED,
    ERROR_JPG_DEVELOPMENT_FAILED,
    ERROR_RAW_READ_FAILED,
    ERROR_FILE_NOT_FOUND,
    ERROR_INVALID_RAW,
    ERROR_RT_TIMEOUT,
    ERROR_RT_NOT_FOUND,
    ERROR_UNKNOWN,
)
from .raw_to_jpg import RawToDng, DngToJpg
from stages import ITEM_OK, ITEM_FAILED

logger = logging.getLogger(__name__)


@dataclass
class ImageResult:
    """Result of processing a single image."""
    image_id: str
    status: str
    jpg_path: Optional[Path] = None
    error_code: str = ""
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False


def _classify_error(exc: Exception) -> str:
    """Map an exception to a standardized error code."""
    msg = str(exc).lower()

    # Check for RawTherapee not found
    if isinstance(exc, FileNotFoundError) and "rawtherapee" in msg:
        return ERROR_RT_NOT_FOUND
    if isinstance(exc, FileNotFoundError):
        return ERROR_FILE_NOT_FOUND
    if isinstance(exc, ValueError):
        return ERROR_INVALID_RAW
    if "rawtherapee" in msg or "jpg" in msg:
        if "timeout" in msg or isinstance(exc, TimeoutError):
            return ERROR_RT_TIMEOUT
        return ERROR_JPG_DEVELOPMENT_FAILED
    if "dng" in msg:
        return ERROR_DNG_CONVERSION_FAILED
    if "raw" in msg or "read" in msg:
        return ERROR_RAW_READ_FAILED
    return ERROR_UNKNOWN


def validate_config(config: dict, config_path: Path) -> None:
    """
    Validate camera configuration has all required fields.

    Args:
        config: Configuration dictionary
        config_path: Path to config file (for relative path resolution)

    Raises:
        ValueError: If required fields are missing or invalid
    """
    # Check top-level required keys
    if 'paths' not in config:
        raise ValueError("Config missing required 'paths' section")

    paths = config['paths']

    # Required path fields
    required_paths = [
        'rawtherapee_cli',
        'temp_dng_dir',
        'color_matrix',
        'svs_tags',
    ]

    for key in required_paths:
        if key not in paths:
            raise ValueError(f"Config missing required field: paths.{key}")
        if not paths[key]:
            raise ValueError(f"Config field paths.{key} cannot be empty")

    # Optional path fields (but log if missing)
    optional_paths = ['pp3_profile', 'rawtherapee_validate_script']
    for key in optional_paths:
        if key not in paths or not paths[key]:
            logger.warning("Config field paths.%s is not specified", key)

    # Validate files exist or can be created
    config_dir = Path(config_path).parent

    # Check color matrix file exists
    color_matrix_path = config_dir / paths['color_matrix']
    if not color_matrix_path.exists():
        raise ValueError(f"Color matrix file not found: {color_matrix_path}")

    # Check SVS tags file exists
    svs_tags_path = config_dir / paths['svs_tags']
    if not svs_tags_path.exists():
        raise ValueError(f"SVS tags file not found: {svs_tags_path}")

    # Check RawTherapee CLI exists
    rt_cli_path = Path(paths['rawtherapee_cli'])
    if not rt_cli_path.exists():
        logger.warning("RawTherapee CLI not found at %s (will attempt auto-install)", rt_cli_path)

    # Validate temp directory can be created
    try:
        temp_dir = Path(paths['temp_dng_dir'])
        temp_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        raise ValueError(f"Cannot create temp directory {paths['temp_dng_dir']}: {e}")

    # Check optional files if specified
    if paths.get('pp3_profile'):
        pp3_path = config_dir / paths['pp3_profile']
        if not pp3_path.exists():
            logger.warning("PP3 profile not found: %s", pp3_path)

    if paths.get('rawtherapee_validate_script'):
        script_path = Path(paths['rawtherapee_validate_script'])
        if not script_path.exists():
            logger.warning("RawTherapee validation script not found: %s", script_path)


def load_config(config_path: Path) -> dict:
    """
    Load camera configuration from YAML file.

    Args:
        config_path: Path to YAML config file

    Returns:
        dict with camera settings and color_matrix as numpy array

    Raises:
        ValueError: If config validation fails
        FileNotFoundError: If config file not found
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        config = yaml.safe_load(f)

    if config is None:
        raise ValueError(f"Config file is empty or invalid: {config_path}")

    # Validate config structure
    validate_config(config, config_path)

    # Load color matrix
    try:
        matrix_path = config_path.parent / config['paths']['color_matrix']
        config['color_matrix'] = np.load(matrix_path, allow_pickle=True)
        logger.debug("Loaded color matrix from %s", matrix_path)
    except Exception as e:
        raise ValueError(f"Failed to load color matrix from {matrix_path}: {e}")

    # Load SVS tags
    try:
        tags_path = config_path.parent / config['paths']['svs_tags']
        with open(tags_path) as f:
            config['dng_tags'] = yaml.safe_load(f)

        if config['dng_tags'] is None:
            raise ValueError("SVS tags file is empty")

        logger.debug("Loaded DNG tags from %s", tags_path)
    except Exception as e:
        raise ValueError(f"Failed to load SVS tags from {tags_path}: {e}")

    return config



class Processor:
    """
    Image Processor to Communicate with the CLI and Conversion parts.
    """

    def __init__(
        self,
        config_path: Path,
    ):
        self.config = load_config(config_path)
        self.raw_to_dng = RawToDng(self.config)
        self.dng_to_jpg = DngToJpg(self.config)


    def process_image(self, raw_path: Path, output_dir: Path) -> Path:
        """
        Process a single RAW image to JPG.
        """
        raw_path = Path(raw_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)


        dng_path = None
        try:
            # Raw to DNG conversion
            dng_path = self.raw_to_dng.convert(raw_path)

            # DNG to JPG conversion
            self.dng_to_jpg.develop(dng_path, output_dir)
            jpg_path = output_dir / raw_path.with_suffix(".jpg").name
        except Exception as e:
            raise RuntimeError(f"Failed to convert DNG to JPG for {raw_path}: {e}")
        finally:
            # Clean up intermediate DNG file
            if dng_path and dng_path.exists():
                dng_path.unlink()

        return jpg_path

    def process_batch(
        self,
        raw_images: Iterable[Path],
        output_dir: Path,
        fail_stop: bool = True,
        max_workers: int = 0,
    ) -> List[ImageResult]:
        """
        Process multiple RAW images, optionally in parallel.

        Args:
            raw_images: iterable of RAW paths
            output_dir: output directory for JPGs
            fail_stop: stop on first failure if True
            max_workers: number of parallel threads (default 0 = sequential)

        Returns:
            List of ImageResult for each processed image
        """
        results: List[ImageResult] = []
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Sequential processing with no threading (default)
        if max_workers <= 1:
            for raw_path in raw_images:
                try:
                    jpg_path = self.process_image(raw_path, output_dir)
                    logger.info("Processed %s -> %s", raw_path.name, jpg_path.name)
                    results.append(ImageResult(
                        image_id=raw_path.stem,
                        status=ITEM_OK,
                        jpg_path=jpg_path,
                    ))
                except Exception as e:
                    error_code = _classify_error(e)
                    logger.error("Failed processing %s [%s]: %s", raw_path.name, error_code, e)
                    results.append(ImageResult(
                        image_id=raw_path.stem,
                        status=ITEM_FAILED,
                        error_code=error_code,
                        error_type=type(e).__name__,
                        error_message=str(e),
                        retryable=True,
                    ))
                    if fail_stop:
                        raise
                    else:
                        continue
        else:
            # Parallel processing using threads
            futures = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # threaded submission of tasks
                for raw_path in raw_images:
                    future = executor.submit(self.process_image, raw_path, output_dir)
                    futures[future] = raw_path

                # sequential running of results verification
                for future in as_completed(futures):
                    raw_path = futures[future]
                    try:
                        jpg_path = future.result()
                        logger.info("Processed %s -> %s", raw_path.name, jpg_path.name)
                        results.append(ImageResult(
                            image_id=raw_path.stem,
                            status=ITEM_OK,
                            jpg_path=jpg_path,
                        ))
                    except Exception as e:
                        error_code = _classify_error(e)
                        logger.error("Failed processing %s [%s]: %s", raw_path.name, error_code, e)
                        results.append(ImageResult(
                            image_id=raw_path.stem,
                            status=ITEM_FAILED,
                            error_code=error_code,
                            error_type=type(e).__name__,
                            error_message=str(e),
                            retryable=True,
                        ))
                        if fail_stop:
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise

        return results
