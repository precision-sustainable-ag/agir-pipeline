from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2
import numpy as np


def make_input_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "images": tmp_path / "images",
        "masks": tmp_path / "segmentations",
        "csv": tmp_path / "batch_georeferenced.csv",
        "catalog": tmp_path / "species_catalog.generated.json",
    }
    paths["images"].mkdir()
    paths["masks"].mkdir()
    paths["catalog"].write_text(
        json.dumps(
            {
                "species": {
                    "ABUTH": {"class_id": 11},
                    "BETVU": {"class_id": 19},
                },
                "cultivars": {"107": {"display_name": "Cultivar 107"}},
            }
        ),
        encoding="utf-8",
    )
    return paths


def write_image_and_mask(
    paths: dict[str, Path],
    image_id: str,
    *,
    image_shape: tuple[int, int] = (10, 20),
    mask_shape: tuple[int, int] | None = None,
    mask_dtype=np.uint8,
    mask_value: int = 11,
    color_mask: bool = False,
) -> None:
    height, width = image_shape
    image = np.full((height, width, 3), 127, dtype=np.uint8)
    assert cv2.imwrite(str(paths["images"] / f"{image_id}.jpg"), image)

    mask_height, mask_width = mask_shape or image_shape
    shape = (mask_height, mask_width, 3) if color_mask else (mask_height, mask_width)
    mask = np.full(shape, mask_value, dtype=mask_dtype)
    assert cv2.imwrite(str(paths["masks"] / f"{image_id}.png"), mask)


def write_csv(paths: dict[str, Path], rows: list[dict[str, object]]) -> None:
    columns = [
        "image_id",
        "bounding_box_id",
        "xmin",
        "ymin",
        "xmax",
        "ymax",
        "species_id",
        "cultivar_id",
    ]
    with paths["csv"].open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def detection_row(
    image_id: str,
    bounding_box_id: int,
    *,
    bbox: tuple[float, float, float, float] = (0.1, 0.2, 0.8, 0.9),
    species_id: str = "ABUTH",
    cultivar_id: str = "",
) -> dict[str, object]:
    return {
        "image_id": image_id,
        "bounding_box_id": bounding_box_id,
        "xmin": bbox[0],
        "ymin": bbox[1],
        "xmax": bbox[2],
        "ymax": bbox[3],
        "species_id": species_id,
        "cultivar_id": cultivar_id,
    }

