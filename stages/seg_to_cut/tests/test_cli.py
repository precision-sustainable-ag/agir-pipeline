from __future__ import annotations

import json
from pathlib import Path

import pytest

from stages import EXIT_CONFIG_ERROR, EXIT_SUCCESS
from stages.seg_to_cut.cli import build_parser, main

from .helpers import detection_row, make_input_paths, write_csv, write_image_and_mask


@pytest.fixture
def input_paths(tmp_path: Path) -> dict[str, Path]:
    return make_input_paths(tmp_path)


def _args(paths) -> list[str]:
    return [
        "--images",
        str(paths["images"]),
        "--segmentations",
        str(paths["masks"]),
        "--georeferenced-csv",
        str(paths["csv"]),
        "--species-catalog",
        str(paths["catalog"]),
    ]


def test_cli_defaults_to_eight_workers(input_paths) -> None:
    args = build_parser().parse_args(_args(input_paths))

    assert args.t == 8


def test_cli_reports_validation_counts(input_paths, capsys) -> None:
    write_image_and_mask(input_paths, "image_1")
    write_csv(input_paths, [detection_row("image_1", 0)])

    exit_code = main(_args(input_paths))

    assert exit_code == EXIT_SUCCESS
    assert json.loads(capsys.readouterr().out) == {
        "status": "validated",
        "images": 1,
        "detections": 1,
    }


def test_cli_reports_stable_validation_error(input_paths, capsys) -> None:
    write_csv(input_paths, [detection_row("missing", 0)])

    exit_code = main(_args(input_paths))

    assert exit_code == EXIT_CONFIG_ERROR
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    assert output["error_code"] == "E_IMAGE_MISSING"


def test_cli_rejects_negative_worker_count(input_paths, capsys) -> None:
    exit_code = main([*_args(input_paths), "--t", "-1"])

    assert exit_code == EXIT_CONFIG_ERROR
    output = json.loads(capsys.readouterr().out)
    assert output["error_code"] == "E_CONFIG_INVALID"
    assert output["message"] == "--t must be a non-negative integer"


def test_cli_rejects_parallel_fail_stop(input_paths, tmp_path, capsys) -> None:
    exit_code = main(
        [
            *_args(input_paths),
            "--output",
            str(tmp_path / "output"),
            "--fail-stop",
            "--t",
            "2",
        ]
    )

    assert exit_code == EXIT_CONFIG_ERROR
    output = json.loads(capsys.readouterr().out)
    assert output["error_code"] == "E_CONFIG_INVALID"
    assert output["message"] == "--fail-stop cannot be combined with --t greater than 1"
