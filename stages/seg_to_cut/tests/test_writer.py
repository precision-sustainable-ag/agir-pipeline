from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import stages.seg_to_cut.writer as writer_module
from stages.seg_to_cut import ERROR_ARTIFACT_WRITE
from stages.seg_to_cut.errors import SegToCutArtifactError
from stages.seg_to_cut.writer import JPEG_QUALITY, artifact_paths, write_cutout_artifacts


def sample_artifacts():
    crop = np.array(
        [
            [[255, 0, 0], [0, 255, 0], [0, 0, 255]],
            [[20, 40, 60], [80, 100, 120], [140, 160, 180]],
        ],
        dtype=np.uint8,
    )
    mask = np.array([[11, 0, 0], [11, 11, 0]], dtype=np.uint8)
    metadata = {
        "image_id": "image_1",
        "cutout_id": "image_1_7",
        "cutout_num": 7,
        "cutout_height": 2,
        "cutout_width": 3,
        "validated": False,
        "cutout_props": {"blur_effect": None},
        "category": {"species_id": "ABUTH", "class_id": 11, "cultivar_class_id": None},
    }
    return crop, mask, metadata


def test_writes_and_reopens_four_contract_artifacts(tmp_path: Path) -> None:
    crop, mask, metadata = sample_artifacts()

    result = write_cutout_artifacts(
        tmp_path,
        cutout_id="image_1_7",
        rgb_crop=crop,
        cleaned_mask=mask,
        expected_class_id=11,
        metadata=metadata,
    )

    assert result == artifact_paths(tmp_path, "image_1_7")
    assert [path.name for path in result.paths] == [
        "image_1_7.jpg",
        "image_1_7.png",
        "image_1_7_mask.png",
        "image_1_7.json",
    ]
    assert all(path.is_file() for path in result.paths)
    decoded_jpg = cv2.imread(str(result.cropout_path), cv2.IMREAD_UNCHANGED)
    decoded_cutout = cv2.imread(str(result.cutout_path), cv2.IMREAD_UNCHANGED)
    decoded_mask = cv2.imread(str(result.mask_path), cv2.IMREAD_UNCHANGED)
    assert decoded_jpg.shape == crop.shape
    assert decoded_cutout.shape == (*mask.shape, 4)
    assert np.array_equal(decoded_cutout[..., 3], np.where(mask != 0, 255, 0))
    assert np.all(decoded_cutout[mask == 0, :3] == 0)
    assert np.array_equal(decoded_mask, mask)
    assert json.loads(result.metadata_path.read_text(encoding="utf-8")) == metadata
    assert not list(tmp_path.glob(".*.tmp"))


def test_jpeg_is_encoded_at_quality_100(monkeypatch, tmp_path: Path) -> None:
    crop, mask, metadata = sample_artifacts()
    original = cv2.imencode
    calls = []

    def capture(extension, image, params=None):
        calls.append((extension, params))
        return original(extension, image, params or [])

    monkeypatch.setattr("stages.seg_to_cut.writer.cv2.imencode", capture)
    write_cutout_artifacts(
        tmp_path,
        cutout_id="image_1_7",
        rgb_crop=crop,
        cleaned_mask=mask,
        expected_class_id=11,
        metadata=metadata,
    )
    assert calls[0] == (".jpg", [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])


def test_json_is_stable_across_output_directories(tmp_path: Path) -> None:
    crop, mask, metadata = sample_artifacts()
    first = write_cutout_artifacts(
        tmp_path / "first",
        cutout_id="image_1_7",
        rgb_crop=crop,
        cleaned_mask=mask,
        expected_class_id=11,
        metadata=metadata,
    )
    second = write_cutout_artifacts(
        tmp_path / "second",
        cutout_id="image_1_7",
        rgb_crop=crop,
        cleaned_mask=mask,
        expected_class_id=11,
        metadata=dict(reversed(list(metadata.items()))),
    )
    assert first.metadata_path.read_bytes() == second.metadata_path.read_bytes()


def test_interrupted_publication_removes_complete_set(monkeypatch, tmp_path: Path) -> None:
    crop, mask, metadata = sample_artifacts()
    calls = 0
    publish = writer_module._publish_temp_artifact

    def fail_on_third(temporary_path, final_path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated interruption")
        publish(temporary_path, final_path)

    monkeypatch.setattr("stages.seg_to_cut.writer._publish_temp_artifact", fail_on_third)
    with pytest.raises(SegToCutArtifactError, match="simulated interruption") as caught:
        write_cutout_artifacts(
            tmp_path,
            cutout_id="image_1_7",
            rgb_crop=crop,
            cleaned_mask=mask,
            expected_class_id=11,
            metadata=metadata,
        )

    assert caught.value.code == ERROR_ARTIFACT_WRITE
    assert list(tmp_path.iterdir()) == []


def test_interrupted_temporary_write_removes_all_files(monkeypatch, tmp_path: Path) -> None:
    crop, mask, metadata = sample_artifacts()
    calls = 0
    write_bytes = writer_module._write_bytes

    def fail_on_third(path, content):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated write failure")
        write_bytes(path, content)

    monkeypatch.setattr("stages.seg_to_cut.writer._write_bytes", fail_on_third)
    with pytest.raises(SegToCutArtifactError, match="simulated write failure"):
        write_cutout_artifacts(
            tmp_path,
            cutout_id="image_1_7",
            rgb_crop=crop,
            cleaned_mask=mask,
            expected_class_id=11,
            metadata=metadata,
        )

    assert list(tmp_path.iterdir()) == []


def test_refuses_to_overwrite_any_existing_member(tmp_path: Path) -> None:
    crop, mask, metadata = sample_artifacts()
    (tmp_path / "image_1_7.png").write_bytes(b"existing")

    with pytest.raises(SegToCutArtifactError, match="refusing to overwrite"):
        write_cutout_artifacts(
            tmp_path,
            cutout_id="image_1_7",
            rgb_crop=crop,
            cleaned_mask=mask,
            expected_class_id=11,
            metadata=metadata,
        )
    assert (tmp_path / "image_1_7.png").read_bytes() == b"existing"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda crop, mask, metadata: (crop.astype(np.float32), mask, metadata), "rgb_crop"),
        (lambda crop, mask, metadata: (crop, mask[:, :2], metadata), "cleaned_mask"),
        (
            lambda crop, mask, metadata: (
                crop,
                np.where(mask != 0, 19, 0).astype(np.uint8),
                metadata,
            ),
            "expected class",
        ),
        (
            lambda crop, mask, metadata: (crop, mask, {**metadata, "cutout_width": 4}),
            "cutout_width",
        ),
        (
            lambda crop, mask, metadata: (
                crop,
                mask,
                {**metadata, "category": {"class_id": 19, "cultivar_class_id": None}},
            ),
            "class value",
        ),
    ],
)
def test_rejects_invalid_contract_before_writing(tmp_path: Path, change, message: str) -> None:
    crop, mask, metadata = change(*sample_artifacts())
    with pytest.raises(SegToCutArtifactError, match=message):
        write_cutout_artifacts(
            tmp_path,
            cutout_id="image_1_7",
            rgb_crop=crop,
            cleaned_mask=mask,
            expected_class_id=11,
            metadata=metadata,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cutout_id", ["", "../escape", "with/slash", "with\\slash"])
def test_rejects_unsafe_cutout_ids(tmp_path: Path, cutout_id: str) -> None:
    crop, mask, metadata = sample_artifacts()
    metadata["cutout_id"] = cutout_id
    with pytest.raises(SegToCutArtifactError):
        write_cutout_artifacts(
            tmp_path,
            cutout_id=cutout_id,
            rgb_crop=crop,
            cleaned_mask=mask,
            expected_class_id=11,
            metadata=metadata,
        )
