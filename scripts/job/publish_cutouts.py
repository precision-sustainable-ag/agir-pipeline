#!/usr/bin/env python3
"""Atomically publish a validated ``seg_to_cut`` batch and refresh inventory."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.artifact_validation import ArtifactValidationError
from orchestrator.cutout_publication import publish_cutout_batch
from orchestrator.sqlite_db import open_db
from scripts.admin.globus_index import EndpointConfig, refresh_inventory_scope


logger = logging.getLogger(__name__)


def _verify_inventory(
    db_path: Path,
    *,
    batch_id: str,
    site: str,
    published_paths: tuple[Path, ...],
) -> None:
    expected = {str(path) for path in published_paths}
    conn = open_db(db_path, readonly=True)
    try:
        rows = conn.execute(
            """
            SELECT full_path
            FROM globus_file_index
            WHERE data_state = 'semifield-cutouts'
              AND entry_type = 'file'
              AND is_current = 1
              AND site = ?
              AND batch_id = ?
            """,
            (site, batch_id),
        ).fetchall()
    finally:
        conn.close()
    indexed = {row["full_path"] for row in rows}
    missing = sorted(expected - indexed)
    if missing:
        raise RuntimeError(
            f"published inventory is missing {len(missing)} file(s): {missing[:3]}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--refresh-inventory", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--site", default="CERES")
    parser.add_argument("--storage-domain", default="dash_agir")
    parser.add_argument("--namespace", default="90daydata")
    parser.add_argument("--storage-root", default="/90daydata/dash_agir")
    parser.add_argument("--inventory-workers", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        result = publish_cutout_batch(args.run_dir, args.dest)
    except ArtifactValidationError as exc:
        print(f"PUBLISH FAIL: {exc}")
        return 1

    print(
        f"PUBLISH OK: {result.cutout_count} cutout(s), "
        f"{result.artifact_count} files at {result.destination}"
    )
    if not args.refresh_inventory:
        return 0
    if args.db is None or not args.endpoint:
        print("INVENTORY FAIL: --db and --endpoint are required with --refresh-inventory")
        return 1

    inventory_run_id, status = refresh_inventory_scope(
        db_path=args.db,
        config=EndpointConfig(
            endpoint=args.endpoint,
            site=args.site,
            storage_domain=args.storage_domain,
            namespace=args.namespace,
            storage_root=args.storage_root,
            data_state="semifield-cutouts",
        ),
        max_workers=args.inventory_workers,
        logger=logger,
    )
    if status != "success":
        print(f"INVENTORY FAIL: inventory run {inventory_run_id} ended with {status}")
        return 1
    try:
        _verify_inventory(
            args.db,
            batch_id=result.destination.name,
            site=args.site,
            published_paths=result.published_paths,
        )
    except RuntimeError as exc:
        print(f"INVENTORY FAIL: {exc}")
        return 1
    print(f"INVENTORY OK: inventory run {inventory_run_id} reconciled the batch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
