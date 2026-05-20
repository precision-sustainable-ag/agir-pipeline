"""
Promote raw_to_jpg outputs to the final semifield-developed-images destination.

Promotion rules:
  - run_report.json must show exit_code == 0
  - manifest.json must have zero failed items (100 % success required)
  - Every listed JPG must exist at artifacts_root/jpg_path and its SHA-256
    checksum must match the value recorded in the manifest
  - Only after all checks pass are the JPGs copied to --dest and
    run_report.json, manifest.json, run.log, and any inputs_manifest copied
    to dest.parent/{stage}/ so each stage's metadata is isolated

Exit codes: 
  0  all checks passed and files copied
  1  promotion skipped or failed (details printed to stdout)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import copy
import sys
from pathlib import Path

def _rewrite_run_report(report: dict, dest: Path, meta_dest: Path) -> dict:
    """Return a copy of report with all output and input paths updated to the promoted location."""
    r = copy.deepcopy(report)

    outputs = r.get("outputs", {})
    outputs["output_root"] = str(meta_dest.parent)
    outputs["run_root"] = str(meta_dest)
    outputs["artifacts_dir"] = str(dest)
    outputs["report_path"] = str(meta_dest / "run_report.json")
    outputs["manifest_path"] = str(meta_dest / "manifest.json")

    old_artifacts_dir = report.get("outputs", {}).get("artifacts_dir", "")
    for entry in outputs.get("artifacts", []):
        if entry.get("path") == old_artifacts_dir:
            entry["path"] = str(dest)

    inputs = r.get("inputs", {})
    old_input_manifest = inputs.get("inputs_manifest_path")
    if old_input_manifest:
        inputs["inputs_manifest_path"] = str(meta_dest / Path(old_input_manifest).name)

    pointers = r.get("pointers", {})
    old_logs = pointers.get("logs_path")
    if old_logs:
        pointers["logs_path"] = str(meta_dest / Path(old_logs).name)

    return r


def _rewrite_manifest(manifest: dict, dest: Path) -> dict:
    """Return a copy of manifest with artifacts_root updated to the promoted location."""
    m = copy.deepcopy(manifest)
    m["artifacts_root"] = str(dest)
    return m

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

    # ── All checks passed — copy images and metadata to destination ──────────
    dest.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for item in items:
        jpg_rel = item["artifacts"]["jpg_path"]
        src = artifacts_root / jpg_rel
        dst = dest / src.name
        shutil.copy2(src, dst)
        promoted += 1


    # Write rewritten run_report.json, manifest.json, and run.log into a
    # stage-namespaced subdirectory under the batch root so each stage's
    # metadata lives separately and cannot overwrite another stage's files.
    stage = run_report.get("stage", "unknown_stage")
    meta_dest = dest.parent / stage
    meta_dest.mkdir(parents=True, exist_ok=True)
    
    promoted_report = _rewrite_run_report(run_report, dest, meta_dest)
    (meta_dest / "run_report.json").write_text(json.dumps(promoted_report, indent=2))

    promoted_manifest = _rewrite_manifest(manifest, dest)
    (meta_dest / "manifest.json").write_text(json.dumps(promoted_manifest, indent=2))

    logs_path_str = run_report.get("pointers", {}).get("logs_path")
    if logs_path_str:
        logs_path = Path(logs_path_str)
        if logs_path.exists():
            shutil.copy2(logs_path, meta_dest / logs_path.name)
    
    input_manifest_path_str = run_report.get("inputs", {}).get("inputs_manifest_path")
    if input_manifest_path_str:
        input_manifest_path = Path(input_manifest_path_str)
        if input_manifest_path.exists():
            shutil.copy2(input_manifest_path, meta_dest / input_manifest_path.name)

    print(f"PROMOTE OK: {promoted} images copied to {dest}")
    print(f"PROMOTE OK: run_report.json, manifest.json, and run.log copied to {meta_dest}")
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