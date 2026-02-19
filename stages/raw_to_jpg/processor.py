"""
RAW -> DNG -> JPG processing pipeline.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List
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
from stages.common import resolve_path

logger = logging.getLogger(__name__)


@dataclass
class ImageResult:
    """Result of processing a single image."""
    image_id: str
    status: str
    jpg_path: Path | None = None
    error_code: str = ""
    error_type: str | None = None
    error_message: str | None = None
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


def validate_config(config: dict) -> None:
    """
    Validate resolved paths exist.

    Args:
        config: Configuration dictionary with already-resolved paths

    Raises:
        ValueError: If required paths don't exist
    """
    if 'paths' not in config:
        raise ValueError("Config missing required 'paths' section")

    config_paths = config['paths']

    # Check required paths exist
    required_config_paths = [
        'rawtherapee_cli',
        'temp_dng_dir',
        'color_matrix',
        'svs_tags',
        'pp3_profile',
    ]

    for key in required_config_paths:
        if key not in config_paths:
            raise ValueError(f"Config missing required field: paths.{key}")
        if not config_paths[key]:
            raise ValueError(f"Config field paths.{key} cannot be empty")

        path = Path(config_paths[key])
        if not path.exists():
            raise ValueError(f"Path not found: {path}")



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

    # Resolve all paths to absolute paths
    base_dir = config_path.parent
    paths = config['paths']

    # resolve mandatory paths in conifg
    for key in ['rawtherapee_cli', 'temp_dng_dir', 'color_matrix', 'svs_tags', 'pp3_profile']:
        paths[key] = str(resolve_path(paths[key], base_dir))

    if paths.get('rawtherapee_validate_script'):
        paths['rawtherapee_validate_script'] = str(resolve_path(paths['rawtherapee_validate_script'], base_dir))

    # Validate resolved paths exist
    validate_config(config)

    # Load color matrix
    try:
        matrix_path = Path(config['paths']['color_matrix'])
        config['color_matrix'] = np.load(matrix_path, allow_pickle=True)
        logger.debug("Loaded color matrix from %s", matrix_path)
    except Exception as e:
        raise ValueError(f"Failed to load color matrix from {matrix_path}: {e}")

    # Load DNG tags from file or use embedded config
    if 'dng_tags' in config and config['dng_tags']:
        logger.debug("Using DNG tags embedded in config")
    else:
        try:
            tags_path = Path(config['paths']['svs_tags'])
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
    ) -> None:
        self.config = load_config(config_path)
        self.raw_to_dng = RawToDng(self.config)
        self.dng_to_jpg = DngToJpg(self.config)


    def process_image(self, raw_path: Path, output_dir: Path) -> Path:
        """
        Process a single RAW image to JPG.

        Args:
            raw_path: Path to input RAW image file
            output_dir: Directory where output JPG will be saved

        Returns:
            Path to the generated JPG file

        Raises:
            RuntimeError: If RAW to DNG or DNG to JPG conversion fails
        """
        raw_path = Path(raw_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        dng_path = None
        try:
            # Raw to DNG conversion
            dng_path = self.raw_to_dng.convert(raw_path)

            # DNG to JPG conversion
            jpg_path = output_dir / raw_path.with_suffix(".jpg").name
            self.dng_to_jpg.develop(dng_path, jpg_path)
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
