"""
RAW -> DNG -> JPG processing pipeline.
"""

from pathlib import Path
from typing import Iterable, List
from .raw_to_jpg import RawToDng, DngToJpg
from concurrent.futures import ThreadPoolExecutor, as_completed
import yaml
import numpy as np


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
    ) -> List[Path]:
        """
        Process multiple RAW images, optionally in parallel.

        Args:
            raw_images: iterable of RAW paths
            output_dir: output directory for JPGs
            fail_stop: stop on first failure if True
            max_workers: number of parallel threads (default 0 = sequential)

        Returns:
            List of successfully generated JPG paths
        """
        results: List[Path] = []
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Sequential processing with no threading (default)
        if max_workers <= 1:
            for raw_path in raw_images:
                try:
                    jpg_path = self.process_image(raw_path, output_dir)
                    results.append(jpg_path)
                except Exception as e:
                    print(f"Failed processing {raw_path}: {e}")
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
                        results.append(jpg_path)
                    except Exception as e:
                        print(f"Failed processing {raw_path}: {e}")
                        if fail_stop:
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise

        return results
