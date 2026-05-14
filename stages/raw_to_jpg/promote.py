"""
Promote raw_to_jpg outputs to the final semifield-developed-images destination.

Promotion rules:
  - run_report.json must show exit_code == 0
  - manifest.json must have zero failed items (100 % success required)
  - Every listed JPG must exist at artifacts_root/jpg_path and its SHA-256
    checksum must match the value recorded in the manifest
  - Only after all checks pass are the JPGs copied to --dest

Exit codes:
  0  all checks passed and images copied
  1  promotion skipped or failed (details printed to stdout)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def promote(run_dir: Path, dest: Path) -> int:
    run_report_path = run_dir / "run_report.json"
    manifest_path = run_dir / "manifest.json"

    # ── Validate run_report ───────────────────────────────────────────────────
    if not run_report_path.exists():
        print(f"PROMOTE FAIL: run_report.json not found in {run_dir}")
        return 1

    run_report = json.loads(run_report_path.read_text())
    exit_code = run_report.get("exit_code")
    if exit_code != 0:
        print(f"PROMOTE SKIP: run_report exit_code={exit_code} (need 0)")
        return 1

    # ── Validate manifest ─────────────────────────────────────────────────────
    if not manifest_path.exists():
        print(f"PROMOTE FAIL: manifest.json not found in {run_dir}")
        return 1

    manifest = json.loads(manifest_path.read_text())
    items = manifest.get("items", [])
    if not items:
        print("PROMOTE FAIL: manifest has no items")
        return 1

    failed_items = [i for i in items if i.get("status") != "ok"]
    if failed_items:
        print(
            f"PROMOTE SKIP: {len(failed_items)}/{len(items)} items failed "
            f"— 100% success required, not promoting"
        )
        for item in failed_items:
            print(f"  FAILED: {item.get('image_id')} — {item.get('error', {}).get('message', '')}")
        return 1

    artifacts_root = Path(manifest.get("artifacts_root", run_dir / "artifacts"))

    # ── Verify every JPG exists and checksum matches ──────────────────────────
    for item in items:
        image_id = item.get("image_id", "<unknown>")
        jpg_rel = (item.get("artifacts") or {}).get("jpg_path")
        if not jpg_rel:
            print(f"PROMOTE FAIL: item {image_id} has no jpg_path in artifacts")
            return 1

        jpg_path = artifacts_root / jpg_rel
        if not jpg_path.exists():
            print(f"PROMOTE FAIL: JPG missing on disk: {jpg_path}")
            return 1

        expected = (item.get("checksum") or {}).get("jpg_path")
        if expected:
            actual = _sha256(jpg_path)
            if actual != expected:
                print(
                    f"PROMOTE FAIL: checksum mismatch for {image_id}\n"
                    f"  expected: {expected}\n"
                    f"  actual:   {actual}"
                )
                return 1

    # ── All checks passed — copy to destination ───────────────────────────────
    dest.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for item in items:
        jpg_rel = item["artifacts"]["jpg_path"]
        src = artifacts_root / jpg_rel
        dst = dest / src.name
        shutil.copy2(src, dst)
        promoted += 1

    print(f"PROMOTE OK: {promoted} images copied to {dest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote raw_to_jpg artifacts to the final destination."
    )
    parser.add_argument(
        "--run-dir", required=True, type=Path,
        help="Directory containing run_report.json, manifest.json, and artifacts/",
    )
    parser.add_argument(
        "--dest", required=True, type=Path,
        help="Destination directory (e.g. /90daydata/dash_agir/semifield-developed-images/<batch_id>/images)",
    )
    args = parser.parse_args()
    return promote(args.run_dir, args.dest)


if __name__ == "__main__":
    raise SystemExit(main())