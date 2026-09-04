"""Tests for det_to_seg class-ID resolution."""

import json

import pytest

from stages.common.class_ids import (
    ClassIdResolutionError,
    DetectionBox,
    DuplicateDetectionKeyError,
    InvalidClassIdError,
    UnknownSpeciesError,
    build_class_id_index,
    load_class_id_index,
    resolve_detection_class_ids,
)


def _catalog():
    return {
        "species": {
            "ABUTH": {"class_id": 11},
            "BETVU": {"class_id": 19},
        },
        "cultivars": {},
    }


def _write_csv(tmp_path, rows, *, include_cultivar=True):
    columns = ["image_id", "bounding_box_id", "species_id"]
    if include_cultivar:
        columns.append("cultivar_id")
    path = tmp_path / "batch_georeferenced.csv"
    path.write_text(
        ",".join(columns)
        + "\n"
        + "\n".join(",".join(str(row.get(column, "")) for column in columns) for row in rows)
        + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return path


def _box(bounding_box_id):
    return DetectionBox(bounding_box_id=bounding_box_id, xyxy=(1, 2, 3, 4))


def test_resolves_species_and_matches_image_id_case_insensitively(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        [{"image_id": "Image_01", "bounding_box_id": 0, "species_id": "ABUTH"}],
    )

    index = build_class_id_index(csv_path, _catalog())
    resolution = resolve_detection_class_ids("image_01", [_box(0)], index)

    assert resolution.detections[0].class_id == 11
    assert resolution.fallback_count == 0


def test_populated_cultivar_id_takes_precedence(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        [
            {
                "image_id": "image_01",
                "bounding_box_id": 0,
                "species_id": "UNKNOWN",
                "cultivar_id": 107,
            }
        ],
    )

    index = build_class_id_index(csv_path, _catalog())
    resolution = resolve_detection_class_ids("image_01", [_box(0)], index)

    assert resolution.detections[0].class_id == 107
    assert resolution.fallback_count == 0


def test_missing_georeferenced_row_uses_plant_fallback(tmp_path):
    csv_path = _write_csv(tmp_path, [], include_cultivar=False)

    index = build_class_id_index(csv_path, _catalog())
    resolution = resolve_detection_class_ids("image_01", [_box(3)], index)

    assert resolution.detections[0].class_id == 27
    assert resolution.fallback_count == 1


def test_consistent_duplicate_key_is_allowed(tmp_path):
    row = {"image_id": "image_01", "bounding_box_id": 0, "species_id": "ABUTH"}
    csv_path = _write_csv(tmp_path, [row, row], include_cultivar=False)

    index = build_class_id_index(csv_path, _catalog())

    assert index.get("image_01", 0) == 11


def test_conflicting_duplicate_key_is_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        [
            {"image_id": "image_01", "bounding_box_id": 0, "species_id": "ABUTH"},
            {"image_id": "IMAGE_01", "bounding_box_id": 0, "species_id": "BETVU"},
        ],
        include_cultivar=False,
    )

    with pytest.raises(DuplicateDetectionKeyError, match="duplicate detection key"):
        build_class_id_index(csv_path, _catalog())


@pytest.mark.parametrize("value", ["1.5", "", "not-an-id", "-1"])
def test_malformed_bounding_box_id_is_rejected(tmp_path, value):
    csv_path = _write_csv(
        tmp_path,
        [{"image_id": "image_01", "bounding_box_id": value, "species_id": "ABUTH"}],
        include_cultivar=False,
    )

    with pytest.raises(ClassIdResolutionError, match="bounding_box_id"):
        build_class_id_index(csv_path, _catalog())


def test_unknown_species_is_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        [{"image_id": "image_01", "bounding_box_id": 0, "species_id": "UNKNOWN"}],
        include_cultivar=False,
    )

    with pytest.raises(UnknownSpeciesError, match="UNKNOWN"):
        build_class_id_index(csv_path, _catalog())


@pytest.mark.parametrize("class_id", [-1, 0, 256])
def test_species_class_outside_foreground_mask_range_is_rejected(tmp_path, class_id):
    csv_path = _write_csv(
        tmp_path,
        [{"image_id": "image_01", "bounding_box_id": 0, "species_id": "ABUTH"}],
        include_cultivar=False,
    )
    catalog = {"species": {"ABUTH": {"class_id": class_id}}}

    with pytest.raises(InvalidClassIdError, match="uint8"):
        build_class_id_index(csv_path, catalog)


def test_cultivar_class_outside_uint8_is_rejected(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        [
            {
                "image_id": "image_01",
                "bounding_box_id": 0,
                "species_id": "ABUTH",
                "cultivar_id": 999,
            }
        ],
    )

    with pytest.raises(InvalidClassIdError, match="uint8"):
        build_class_id_index(csv_path, _catalog())


def test_load_rejects_malformed_catalog_structure(tmp_path):
    csv_path = _write_csv(tmp_path, [], include_cultivar=False)
    catalog_path = tmp_path / "species.json"
    catalog_path.write_text(json.dumps({"species": []}), encoding="utf-8")

    with pytest.raises(ClassIdResolutionError, match="'species' object"):
        load_class_id_index(csv_path, catalog_path)


def test_missing_required_csv_column_is_rejected(tmp_path):
    csv_path = tmp_path / "batch_georeferenced.csv"
    csv_path.write_text("image_id,bounding_box_id\nimage_01,0\n", encoding="utf-8")

    with pytest.raises(ClassIdResolutionError, match="species_id"):
        build_class_id_index(csv_path, _catalog())
