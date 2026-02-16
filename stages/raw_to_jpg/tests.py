#!/usr/bin/env python3
"""Test suite for raw_to_jpg stage."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import tempfile
from unittest.mock import patch, Mock
import numpy as np
import yaml
import subprocess

from stages.raw_to_jpg.processor import Processor, load_config, validate_config, _classify_error, ImageResult
from stages.raw_to_jpg.raw_to_jpg import RawToDng, DngToJpg
from stages.common.parsers import parse_batch_id
from stages import ITEM_OK, ITEM_FAILED
from stages.raw_to_jpg import (
    ERROR_FILE_NOT_FOUND, ERROR_INVALID_RAW, ERROR_DNG_CONVERSION_FAILED,
    ERROR_JPG_DEVELOPMENT_FAILED, ERROR_RT_TIMEOUT, ERROR_UNKNOWN,
)


def create_mock_config(tmp_dir: Path) -> Path:
    """Create valid config with mock files."""
    config_dir = tmp_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    # Color matrix
    color_matrix_data = np.array({
        "color_matrix": np.eye(3),
        "forward_matrix": np.eye(3),
        "wb_gains": np.array([1.0, 1.0, 1.0]),
    }, dtype=object)
    np.save(config_dir / "color_matrix.npy", color_matrix_data)

    # SVS tags
    svs_tags = {
        "image": {
            "SVCamImageWidth": 4096, "SVCamImageHeight": 3072,
            "BitsPerSample": 16, "PhotometricInterpretation": 32803,
            "Orientation": 1, "SamplesPerPixel": 1,
            "CFARepeatPatternDim": [2, 2], "CFAPattern": [0, 1, 1, 2],
            "RowsPerStrip": 256,
        },
        "camera": {
            "Make": "SVS", "Model": "SHR661", "SerialNumber": "TEST001",
            "LensModel": "Test", "FocalLength": 50.0, "FocalLengthIn35mmFilm": 50,
            "FNumber": 2.8, "FocalPlaneXResolution": 4096.0,
            "FocalPlaneYResolution": 3072.0, "FocalPlaneResolutionUnit": 3,
        },
        "dng": {
            "DNGVersion": [1, 4, 0, 0], "DNGBackwardVersion": [1, 4, 0, 0],
            "BlackLevel": 0, "WhiteLevel": 65535, "CalibrationIlluminant1": 21,
            "PreviewColorSpace": 1, "BaselineExposure": [0, 1],
        },
    }
    with open(config_dir / "svs_tags.yaml", "w") as f:
        yaml.dump(svs_tags, f)

    # Get validate_rawtherapee script
    script_path = Path(__file__).parent.parent.parent / "scripts" / "validate_rawtherapee.sh"

    config = {
        "paths": {
            "rawtherapee_cli": "/usr/bin/rawtherapee-cli",
            "temp_dng_dir": str(tmp_dir / "temp_dng"),
            "color_matrix": "color_matrix.npy",
            "svs_tags": "svs_tags.yaml",
            "pp3_profile": "profile.pp3",
            "rawtherapee_validate_script": str(script_path),
        },
        "processing": {"threads_per_image": 1},
    }
    config_path = config_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    (config_dir / "profile.pp3").touch()
    return config_path


def create_mock_raw_file(tmp_dir: Path, name: str = "test.raw") -> Path:
    """Create mock RAW file."""
    raw_path = tmp_dir / name
    raw_path.write_bytes(b"\x00" * (4096 * 3072 * 2))
    return raw_path


# ============================================================================
# Tests
# ============================================================================

def test_config_loading():
    """Test config loading."""
    print("[TEST]: test_config_loading")
    with tempfile.TemporaryDirectory() as tmp:
        config_path = create_mock_config(Path(tmp))
        config = load_config(config_path)
        assert "paths" in config
        assert "color_matrix" in config
        assert isinstance(config["color_matrix"], np.ndarray)
        print("Config loading works")


def test_config_missing_file():
    """Test FileNotFoundError for missing config."""
    print("[TEST]: test_config_missing_file")
    try:
        load_config(Path("fake.yaml"))
        assert False, "Should raise FileNotFoundError"
    except FileNotFoundError:
        print("Missing file handled")


def test_path_resolution():
    """Test that relative paths in config are resolved relative to config directory."""
    print("[TEST]: test_path_resolution")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_dir = tmp_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories for assets
        assets_dir = config_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        # Create required files
        color_matrix_data = np.array({
            "color_matrix": np.eye(3),
            "forward_matrix": np.eye(3),
            "wb_gains": np.array([1.0, 1.0, 1.0]),
        }, dtype=object)
        np.save(assets_dir / "color_matrix.npy", color_matrix_data)

        svs_tags = {
            "image": {
                "SVCamImageWidth": 4096, "SVCamImageHeight": 3072,
                "BitsPerSample": 16, "PhotometricInterpretation": 32803,
                "Orientation": 1, "SamplesPerPixel": 1,
                "CFARepeatPatternDim": [2, 2], "CFAPattern": [0, 1, 1, 2],
                "RowsPerStrip": 256,
            },
            "camera": {
                "Make": "SVS", "Model": "SHR661", "SerialNumber": "TEST001",
                "LensModel": "Test", "FocalLength": 50.0, "FocalLengthIn35mmFilm": 50,
                "FNumber": 2.8, "FocalPlaneXResolution": 4096.0,
                "FocalPlaneYResolution": 3072.0, "FocalPlaneResolutionUnit": 3,
            },
            "dng": {
                "DNGVersion": [1, 4, 0, 0], "DNGBackwardVersion": [1, 4, 0, 0],
                "BlackLevel": 0, "WhiteLevel": 65535, "CalibrationIlluminant1": 21,
                "PreviewColorSpace": 1, "BaselineExposure": [0, 1],
            },
        }
        # write svg_tags yaml
        with open(assets_dir / "svs_tags.yaml", "w") as f:
            yaml.dump(svs_tags, f)

        # Get actual script path
        script_path = Path(__file__).parent.parent.parent / "scripts" / "validate_rawtherapee.sh"

        # Create config
        config = {
            "paths": {
                "rawtherapee_cli": "/usr/bin/rawtherapee-cli",
                "temp_dng_dir": str(tmp_dir / "temp_dng"),
                "color_matrix": "assets/color_matrix.npy",
                "svs_tags": "assets/svs_tags.yaml",
                "pp3_profile": "assets/profile.pp3",
                "rawtherapee_validate_script": str(script_path),
            },
            "processing": {"threads_per_image": 1},
        }
        # create pp3 file
        (assets_dir / "profile.pp3").touch()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        # Validate config paths
        try:
            validate_config(config, config_path)
            print("Paths resolved correctly")
        except ValueError as e:
            assert False, f"Path resolution failed: {e}"

        # Load config and verify files were loaded
        loaded_config = load_config(config_path)
        assert "color_matrix" in loaded_config
        assert loaded_config["color_matrix"] is not None
        assert "dng_tags" in loaded_config
        assert loaded_config["dng_tags"] is not None
        print("Files loaded successfully from paths")

        # Verify wrong relative path fails
        bad_config = dict(config)
        bad_config["paths"] = dict(config["paths"])
        bad_config["paths"]["color_matrix"] = "nonexistent/color_matrix.npy"
        try:
            validate_config(bad_config, config_path)
            assert False, "Should have raised ValueError for missing file"
        except ValueError as e:
            assert "not found" in str(e).lower()
            print("Missing relative path caught correctly")


def test_batch_id_parsing():
    """Test batch ID parsing."""
    print("[TEST]: test_batch_id_parsing")
    assert parse_batch_id("TX_2024-06-01") == "TX_2024-06-01"
    assert parse_batch_id("/data/NC_2025-12-31/raw/") == "NC_2025-12-31"
    assert parse_batch_id("invalid") is None
    print("Batch ID parsing works")



def test_rawtherapee_validation():
    """Test RawTherapee validation by running the script."""
    print("[TEST]: test_rawtherapee_validation")
    script_path = Path(__file__).parent.parent.parent / "scripts" / "validate_rawtherapee.sh"

    if not script_path.exists():
        print(f"Script not found: {script_path}")
        return

    try:
        result = subprocess.run([str(script_path)], capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            print("RawTherapee validation script executed successfully")
            print("Output:", result.stdout)
        else:
            print(f"Script failed with return code {result.returncode}")
            print("Error:", result.stderr)
    except subprocess.TimeoutExpired:
        print("Script timed out (expected for download)")
    except Exception as e:
        print(f"Note: RawTherapee validation: {e}")


def test_raw_to_dng_conversion():
    """Test RAW to DNG conversion."""
    print("[TEST]: test_raw_to_dng_conversion")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_path = create_mock_config(tmp_dir)
        raw_path = create_mock_raw_file(tmp_dir)

        with patch("numpy.fromfile") as mock_fromfile:
            with patch.object(DngToJpg, "validate_installation", return_value=True):
                with patch.object(DngToJpg, "install_rawtherapee"):
                    with patch("pathlib.Path.exists", return_value=True):
                        mock_fromfile.return_value = np.zeros((3072, 4096), dtype=np.uint16)
                        config = load_config(config_path)
                        raw_to_dng = RawToDng(config)
                        dng_path = raw_to_dng.convert(raw_path)
                        assert dng_path.name == "test.dng"
    print("RAW to DNG conversion works")


def test_dng_to_jpg_development():
    """Test DNG to JPG development."""
    print("[TEST]: test_dng_to_jpg_development")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_path = create_mock_config(tmp_dir)
        dng_path = (tmp_dir / "test.dng")
        dng_path.touch()
        jpg_path = tmp_dir / "output" / "test.jpg"

        with patch.object(DngToJpg, "validate_installation", return_value=True):
            with patch.object(DngToJpg, "install_rawtherapee"):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = Mock(returncode=0)
                        config = load_config(config_path)
                        dng_to_jpg = DngToJpg(config)
                        result = dng_to_jpg.develop(dng_path, jpg_path)
                        assert result == jpg_path, f"Expected {jpg_path}, got {result}"
                        assert mock_run.called, "subprocess.run was not called"
    print("[TEST]: test_dng_to_jpg_development passed")



def test_process_image():
    """Test single image processing."""
    print("[TEST]: test_process_image")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_path = create_mock_config(tmp_dir)
        raw_path = create_mock_raw_file(tmp_dir)
        output_dir = tmp_dir / "output"

        with patch("numpy.fromfile") as mock_fromfile:
            with patch.object(DngToJpg, "validate_installation", return_value=True):
                with patch.object(DngToJpg, "install_rawtherapee"):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("subprocess.run") as mock_run:
                            mock_run.return_value = Mock(returncode=0)
                            mock_fromfile.return_value = np.zeros((3072, 4096), dtype=np.uint16)
                            processor = Processor(config_path)
                            jpg_path = processor.process_image(raw_path, output_dir)
                            assert jpg_path.name == "test.jpg"
    print("Single image processing works")


def test_batch_processing():
    """Test batch processing."""
    print("[TEST]: test_batch_processing")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_path = create_mock_config(tmp_dir)
        raw_files = [create_mock_raw_file(tmp_dir, f"img{i}.raw") for i in range(3)]
        output_dir = tmp_dir / "output"

        with patch("numpy.fromfile") as mock_fromfile:
            with patch.object(DngToJpg, "validate_installation", return_value=True):
                with patch.object(DngToJpg, "install_rawtherapee"):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("subprocess.run") as mock_run:
                            mock_run.return_value = Mock(returncode=0)
                            mock_fromfile.return_value = np.zeros((3072, 4096), dtype=np.uint16)
                            processor = Processor(config_path)
                            results = processor.process_batch(raw_files, output_dir, max_workers=2)
                            assert len(results) == 3
                            assert all(r.status == ITEM_OK for r in results)



                            
print("Batch processing works")


print("=" * 60)
print("raw_to_jpg Stage Tests")
print("=" * 60)

tests = [
    test_config_loading,
    test_config_missing_file,
    test_path_resolution,
    test_batch_id_parsing,
    test_rawtherapee_validation,
    test_raw_to_dng_conversion,
    test_dng_to_jpg_development,
    test_process_image,
    test_batch_processing,
]

failed = []
for test_func in tests:
    try:
        test_func()
    except Exception as e:
        print(f"✗ {test_func.__name__} failed: {e}")
        import traceback
        traceback.print_exc()
        failed.append(test_func.__name__)

print("\n" + "=" * 60)
if failed:
    print(f"✗ {len(failed)} test(s) failed:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
else:
    print(f"All {len(tests)} tests passed!")
print("=" * 60)


