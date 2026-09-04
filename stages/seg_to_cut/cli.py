#!/usr/bin/env python3
"""Validate ``seg_to_cut`` inputs without writing cutout artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stages import EXIT_CONFIG_ERROR, EXIT_SUCCESS

from .config import load_config
from .errors import SegToCutError
from .processor import discover_and_validate_inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--segmentations", type=Path, required=True)
    parser.add_argument("--georeferenced-csv", type=Path, required=True)
    parser.add_argument("--species-catalog", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        result = discover_and_validate_inputs(
            images_dir=args.images,
            masks_dir=args.segmentations,
            georeferenced_csv=args.georeferenced_csv,
            species_catalog=args.species_catalog,
            config=config,
        )
    except SegToCutError as exc:
        print(json.dumps({"status": "failed", "error_code": exc.code, "message": str(exc)}))
        return EXIT_CONFIG_ERROR

    print(
        json.dumps(
            {
                "status": "validated",
                "images": result.image_count,
                "detections": result.detection_count,
            },
            sort_keys=True,
        )
    )
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())

