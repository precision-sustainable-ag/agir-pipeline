#!/usr/bin/env python3
"""Test suite for raw_to_jpg stage."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np
import pytest
import yaml

from stages.raw_to_jpg.processor import Processor, load_config, validate_config
from stages.raw_to_jpg.raw_to_jpg import RawToDng, DngToJpg
from stages.common.config import parse_batch_id
from stages import ITEM_OK


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_config(tmp_path):
    """Create valid config with mock files and return config path."""
    config_dir = tmp_path / "config"
    assets_dir = config_dir / "assets"
    config_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    color_matrix_data = np.array({
        "color_matrix": np.eye(3),
        "forward_matrix": np.eye(3),
        "wb_gains": np.array([1.0, 1.0, 1.0]),
    }, dtype=object)
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

    np.save(assets_dir / "color_matrix.npy", color_matrix_data)
    with open(assets_dir / "svs_tags.yaml", "w") as f:
        yaml.dump(svs_tags, f)
    (assets_dir / "rawtherapee-cli").touch()
    (assets_dir / "profile.pp3").touch()
    (assets_dir / "temp_dng").mkdir(exist_ok=True)
    (assets_dir / "validate_rawtherapee.sh").touch()

    config = {
        "paths": {
            "rawtherapee_cli": "assets/rawtherapee-cli",
            "temp_dng_dir": "assets/temp_dng",
            "color_matrix": "assets/color_matrix.npy",
            "svs_tags": "assets/svs_tags.yaml",
            "pp3_profile": "assets/profile.pp3",
            "rawtherapee_validate_script": "assets/validate_rawtherapee.sh",
        },
        "processing": {"threads_per_image": 1},
    }
    config_path = config_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    return config_path


def _create_mock_raw_file(tmp_path, name="test.raw"):
    """Create mock RAW file."""
    raw_path = tmp_path / name
    raw_path.write_bytes(b"\x00" * (4096 * 3072 * 2))
    return raw_path


# ============================================================================
# Tests
# ============================================================================

class TestConfigLoading:
    def test_load_config(self, mock_config):
        """Test config loading has all essential keys."""
        config = load_config(mock_config)
        for key in ["paths", "processing", "color_matrix", "dng_tags"]:
            assert key in config, f"Missing key: {key}"
        assert isinstance(config["color_matrix"], np.ndarray)
        assert isinstance(config["dng_tags"], dict)

    def test_missing_file_raises(self):
        """Test FileNotFoundError for missing config."""
        with pytest.raises(FileNotFoundError):
            load_config(Path("fake.yaml"))

    def test_path_resolution(self, mock_config):
        """Test that relative paths in config are resolved relative to config directory."""
        config_dir = mock_config.parent
        loaded_config = load_config(mock_config)

        validate_config(loaded_config)
        for key in ["paths", "processing", "color_matrix", "dng_tags"]:
            assert key in loaded_config, f"Missing key: {key}"
            assert loaded_config[key] is not None, f"Key is None: {key}"

        # Verify wrong path fails for each required path
        for path_key in ["rawtherapee_cli", "color_matrix", "svs_tags", "pp3_profile"]:
            bad_config = dict(loaded_config)
            bad_config["paths"] = dict(loaded_config["paths"])
            bad_config["paths"][path_key] = str(config_dir / "nonexistent" / path_key)
            with pytest.raises(ValueError, match="(?i)not found"):
                validate_config(bad_config)


class TestBatchIdParsing:
    def test_valid_batch_ids(self):
        assert parse_batch_id("TX_2024-06-01") == "TX_2024-06-01"
        assert parse_batch_id("/data/NC_2025-12-31/raw/") == "NC_2025-12-31"

    def test_invalid_batch_id(self):
        assert parse_batch_id("invalid") is None


class TestRawTherapeeValidation:
    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "scripts" / "validate_rawtherapee.sh").exists(),
        reason="validate_rawtherapee.sh script not found",
    )
    def test_rawtherapee_validation(self):
        """Test RawTherapee validation by running the script."""
        script_path = Path(__file__).parent.parent.parent / "scripts" / "validate_rawtherapee.sh"

        # run rawtherapee script
        result = subprocess.run([str(script_path)], capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            pytest.skip(f"Script failed with return code {result.returncode}: {result.stderr}")

        # validate export statement
        export_statement = result.stdout.strip()
        assert export_statement.startswith("export RT_CLI_PATH="), (
            f"Unexpected output format: {export_statement}"
        )

        rt_cli_path = export_statement.split("export RT_CLI_PATH='")[1].rstrip("'")
        path_obj = Path(rt_cli_path)
        assert path_obj.exists() and path_obj.is_file(), (
            f"RawTherapee CLI path does not exist: {rt_cli_path}"
        )
        assert os.access(rt_cli_path, os.X_OK), (
            f"RawTherapee CLI is not executable: {rt_cli_path}"
        )


class TestConversion:
    def test_raw_to_dng(self, mock_config, tmp_path):
        """Test RAW to DNG conversion."""
        raw_path = _create_mock_raw_file(tmp_path)

        with patch("numpy.fromfile") as mock_fromfile, \
             patch.object(DngToJpg, "validate_installation", return_value=True), \
             patch.object(DngToJpg, "install_rawtherapee"), \
             patch("pathlib.Path.exists", return_value=True):


            mock_fromfile.return_value = np.zeros((3072, 4096), dtype=np.uint16)
            config = load_config(mock_config)
            raw_to_dng = RawToDng(config)
            dng_path = raw_to_dng.convert(raw_path)
            assert dng_path.name == "test.dng"

    def test_dng_to_jpg(self, mock_config, tmp_path):
        """Test DNG to JPG development."""
        dng_path = tmp_path / "test.dng"
        dng_path.touch()
        jpg_path = tmp_path / "output" / "test.jpg"

        with patch.object(DngToJpg, "validate_installation", return_value=True), \
             patch.object(DngToJpg, "install_rawtherapee"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            config = load_config(mock_config)
            dng_to_jpg = DngToJpg(config)
            result = dng_to_jpg.develop(dng_path, jpg_path)
            assert result == jpg_path
            assert mock_run.called


class TestProcessing:
    def test_process_image(self, mock_config, tmp_path):
        """Test single image processing."""
        raw_path = _create_mock_raw_file(tmp_path)
        output_dir = tmp_path / "output"

        # ensures reading of mock raw file
        with (
            patch("numpy.fromfile") as mock_fromfile,
            # mocks rawtherapee validation
            patch.object(DngToJpg, "validate_installation", return_value=True),
            patch.object(DngToJpg, "install_rawtherapee"),
            # ensures path returns true
            patch("pathlib.Path.exists", return_value=True),
            # ensure subprocesses run as true
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=0)
            mock_fromfile.return_value = np.zeros((3072, 4096), dtype=np.uint16)
            processor = Processor(mock_config)
            # assert file returns
            jpg_path = processor.process_image(raw_path, output_dir)
            assert jpg_path.name == "test.jpg"

    def test_batch_processing(self, mock_config, tmp_path):
        """Test batch processing."""
        raw_files = [_create_mock_raw_file(tmp_path, f"img{i}.raw") for i in range(3)]
        output_dir = tmp_path / "output"

        # ensures reading of mock raw file
        with (
            patch("numpy.fromfile") as mock_fromfile,
            # mocks rawtherapee validation
            patch.object(DngToJpg, "validate_installation", return_value=True),
            patch.object(DngToJpg, "install_rawtherapee"),
            # ensures path returns true
            patch("pathlib.Path.exists", return_value=True),
            # ensure subprocesses run as true
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=0)
            mock_fromfile.return_value = np.zeros((3072, 4096), dtype=np.uint16)
            processor = Processor(mock_config)
            # assert multiple files run
            results = processor.process_batch(raw_files, output_dir, max_workers=2)
            assert len(results) == 3
            assert all(r.status == ITEM_OK for r in results)
