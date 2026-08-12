#!/usr/bin/env python3
"""Promote a successful, manifest-backed stage run to its final destination."""

from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator.artifact_validation import ArtifactValidationError
from orchestrator.promotion import promote_run_bundle


def promote(run_dir: Path, dest: Path) -> int:
    """CLI-compatible wrapper around the shared promotion service."""
    try:
        result = promote_run_bundle(run_dir, dest)
    except ArtifactValidationError as exc:
        print(f"PROMOTE {exc.outcome.upper()}: {exc}")
        return 1

    print(
        f"PROMOTE OK: {result.artifact_count} artifact file(s) copied to "
        f"{result.destination}"
    )
    print(f"PROMOTE OK: metadata copied to {result.metadata_destination}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote stage artifacts to the final destination."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Directory containing run_report.json, manifest.json, and artifacts/.",
    )
    parser.add_argument(
        "--dest",
        required=True,
        type=Path,
        help=(
            "Final artifact directory, for example "
            ".../semifield-developed-images/<batch_id>/images."
        ),
    )
    args = parser.parse_args()
    return promote(args.run_dir, args.dest)


if __name__ == "__main__":
    raise SystemExit(main())
