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
    if isinstance(exc, FileNotFoundError):
        return ERROR_FILE_NOT_FOUND
    if isinstance(exc, ValueError):
        return ERROR_INVALID_RAW
    msg = str(exc).lower()
    if "rawtherapee" in msg or "jpg" in msg:
        if "timeout" in msg or isinstance(exc, TimeoutError):
            return ERROR_RT_TIMEOUT
        return ERROR_JPG_DEVELOPMENT_FAILED
    if "dng" in msg:
        return ERROR_DNG_CONVERSION_FAILED
    if "raw" in msg or "read" in msg:
        return ERROR_RAW_READ_FAILED
    return ERROR_UNKNOWN


def load_config(config_path: Path) -> dict:
    """
    Load camera configuration from YAML file.

    Args:
        config_path: Path to YAML config file

    Returns:
        dict with camera settings and color_matrix as numpy array
    """
    config_path = Path(config_path)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Load color matrix if specified
    if 'color_matrix' in config['paths']:
        matrix_path = config_path.parent / config['paths']['color_matrix']
        config['color_matrix'] = np.load(matrix_path, allow_pickle=True)

    if 'svs_tags' in config['paths']:
        tags_path = config_path.parent / config['paths']['svs_tags']
        with open(tags_path) as f:
            config['dng_tags'] = yaml.safe_load(f)

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
