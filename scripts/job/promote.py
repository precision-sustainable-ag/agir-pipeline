#!/usr/bin/env python3
"""
scripts/job/promote.py
===================

Promote stage outputs to the final destination.

Works for any stage whose manifest follows the standard contract:
  - run_report.json with exit_code, stage, and batch_id fields
  - manifest.json with items, each having an "artifacts" dict and
    optional "checksum" dict

The script is fully generic — it reads artifact keys, file paths, and
checksums directly from the manifest so no changes are needed when adding
new stages. Examples of what it handles without modification:

  raw_to_jpg   → artifacts: {jpg_path: "TX_123.jpg"}
  jpg_to_det   → artifacts: {det_txt_path: "TX_123.txt"}
                 + batch-level {batch_id}.csv
  det_to_seg   → artifacts: {mask_png_path: "TX_123_mask.png"}
  seg_to_cutouts → artifacts: {cutout_path: "TX_123_crop_0.jpg",
                                meta_path:  "TX_123_crop_0.json"}

Multi-artifact items (multiple keys in "artifacts") are fully supported —
all keys are verified and copied.

The only stage-specific behaviour is the optional batch-level CSV copy,
which is non-fatal if no CSV is found, so it is harmless for stages that
don't produce one.

Promotion rules:
  - run_report.json must show exit_code == 0 (100% success required),
    except for det_to_world, which also accepts exit_code == 1
    (EXIT_PARTIAL): its artifact is one batch-level CSV shared by every
    item, so a failed item (e.g. a straggler frame with no ASFM grid) just
    means that image's row is absent from the CSV, not a corrupted
    artifact — only the successful items are verified/copied.
  - For every other stage, manifest.json must have zero failed items
  - Every artifact file must exist on disk and its SHA-256 checksum must
    match the manifest (if a checksum is recorded)
  - Only after all checks pass are files copied to --dest
  - run_report.json, manifest.json, run.log, and any inputs_manifest are
    copied to dest.parent/<stage>/ so each stage's metadata is isolated

Exit codes:
  0  all checks passed and files copied
  1  promotion skipped or failed (details printed to stdout)

Usage
-----
python scripts/job/promote.py --run-dir /path/to/run_dir --dest /path/to/dest
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _rewrite_run_report(report: dict, dest: Path, meta_dest: Path) -> dict:
    """Return a copy of report with all output paths rewritten to the promoted location."""
    r = copy.deepcopy(report)

    outputs = r.get("outputs", {})
    old_artifacts_dir = outputs.get("artifacts_dir", "")
    outputs["output_root"]   = str(meta_dest.parent)
    outputs["run_root"]      = str(meta_dest)
    outputs["artifacts_dir"] = str(dest)
    outputs["report_path"]   = str(meta_dest / "run_report.json")
    outputs["manifest_path"] = str(meta_dest / "manifest.json")

    for entry in outputs.get("artifacts", []):
        p = entry.get("path", "")
        if p == old_artifacts_dir:
            entry["path"] = str(dest)
        elif old_artifacts_dir and p.startswith(old_artifacts_dir):
            rel = Path(p).relative_to(old_artifacts_dir)
            entry["path"] = str(dest / rel)

    inputs = r.get("inputs", {})
    old_manifest = inputs.get("inputs_manifest_path")
    if old_manifest:
        inputs["inputs_manifest_path"] = str(meta_dest / Path(old_manifest).name)

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


# ---------------------------------------------------------------------------
# Core promote logic
# ---------------------------------------------------------------------------

def promote(run_dir: Path, dest: Path) -> int:
    run_report_path = run_dir / "run_report.json"
    manifest_path   = run_dir / "manifest.json"

    # ── Validate run_report ───────────────────────────────────────────────────
    if not run_report_path.exists():
        print(f"PROMOTE FAIL: run_report.json not found in {run_dir}")
        return 1

    run_report = json.loads(run_report_path.read_text())
    exit_code  = run_report.get("exit_code")
    stage      = run_report.get("stage", "unknown_stage")
    batch_id   = run_report.get("batch_id", "")

    # det_to_world's artifact is one batch-level CSV shared by every item,
    # not a per-item file — a failed item just means that image's row is
    # absent from the CSV (e.g. a straggler frame with no ASFM grid), not a
    # corrupted/missing artifact. So unlike per-item-file stages
    # (raw_to_jpg, jpg_to_det, det_to_seg, ...), it's safe to promote on
    # EXIT_PARTIAL as long as at least one item succeeded — see
    # stages/__init__.py: EXIT_SUCCESS=0, EXIT_PARTIAL=1, EXIT_FAILURE=2.
    allow_partial = stage == "det_to_world"
    ok_exit_codes = {0, 1} if allow_partial else {0}
    if exit_code not in ok_exit_codes:
        print(f"PROMOTE SKIP: run_report exit_code={exit_code} (need {sorted(ok_exit_codes)})")
        return 1

    # ── Validate manifest ─────────────────────────────────────────────────────
    if not manifest_path.exists():
        print(f"PROMOTE FAIL: manifest.json not found in {run_dir}")
        return 1

    manifest = json.loads(manifest_path.read_text())
    items    = manifest.get("items", [])
    if not items:
        print("PROMOTE FAIL: manifest has no items")
        return 1

    failed_items = [i for i in items if i.get("status") != "ok"]
    if failed_items and not allow_partial:
        print(
            f"PROMOTE SKIP: {len(failed_items)}/{len(items)} items failed "
            f"— 100% success required"
        )
        for item in failed_items:
            print(f"  FAILED: {item.get('image_id')} — {item.get('error', {}).get('message', '')}")
        return 1

    ok_items = [i for i in items if i.get("status") == "ok"]
    if not ok_items:
        print("PROMOTE FAIL: no successful items to promote")
        return 1

    if failed_items:
        print(
            f"PROMOTE PARTIAL: {len(failed_items)}/{len(items)} items failed "
            f"— promoting the {len(ok_items)} successful item(s)"
        )
        for item in failed_items:
            print(f"  SKIPPED (failed): {item.get('image_id')} — {item.get('error', {}).get('message', '')}")

    artifacts_root = Path(manifest.get("artifacts_root", run_dir / "artifacts"))

    # ── Verify every artifact exists and checksum matches ─────────────────────
    # All artifact keys per item are checked so multi-artifact stages
    # (e.g. mask PNG + JSON sidecar per image) are fully verified before
    # anything is copied. Only successful items are checked/copied — failed
    # items (when allow_partial) never had artifacts written for them.
    for item in ok_items:
        image_id  = item.get("image_id", "<unknown>")
        artifacts = item.get("artifacts") or {}
        checksums = item.get("checksum") or {}

        if not artifacts:
            print(f"PROMOTE FAIL: item {image_id} has no artifacts in manifest")
            return 1

        for artifact_key, artifact_rel in artifacts.items():
            if not artifact_rel:
                print(f"PROMOTE FAIL: item {image_id} key '{artifact_key}' has no path")
                return 1

            artifact_path = artifacts_root / artifact_rel
            if not artifact_path.exists():
                print(f"PROMOTE FAIL: artifact missing on disk: {artifact_path}")
                return 1

            expected_checksum = checksums.get(artifact_key)
            if expected_checksum:
                actual = _sha256(artifact_path)
                if actual != expected_checksum:
                    print(
                        f"PROMOTE FAIL: checksum mismatch for {image_id} [{artifact_key}]\n"
                        f"  expected: {expected_checksum}\n"
                        f"  actual:   {actual}"
                    )
                    return 1

    # ── All checks passed — copy artifacts to destination ─────────────────────
    dest.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for item in ok_items:
        for artifact_rel in (item.get("artifacts") or {}).values():
            if not artifact_rel:
                continue
            src = artifacts_root / artifact_rel
            if src.exists():
                shutil.copy2(src, dest / src.name)
                promoted += 1

    print(f"PROMOTE OK: {promoted} artifact file(s) copied to {dest}")

    # ── Optional batch-level CSV ───────────────────────────────────────────────
    # jpg_to_det (and potentially other stages) produce a {batch_id}.csv
    # alongside per-image artifacts. Non-fatal if not present.
    if batch_id:
        csv_src = artifacts_root / f"{batch_id}.csv"
        if csv_src.exists():
            shutil.copy2(csv_src, dest / csv_src.name)
            print(f"PROMOTE OK: batch CSV copied to {dest / csv_src.name}")

    # ── Copy metadata to stage-namespaced dir ─────────────────────────────────
    # dest      e.g. .../semifield-developed-images/<batch_id>/images/
    # meta_dest e.g. .../semifield-developed-images/<batch_id>/raw_to_jpg/
    # Stage name comes from run_report so this is always correct.
    meta_dest = dest.parent / stage
    meta_dest.mkdir(parents=True, exist_ok=True)

    (meta_dest / "run_report.json").write_text(
        json.dumps(_rewrite_run_report(run_report, dest, meta_dest), indent=2)
    )
    (meta_dest / "manifest.json").write_text(
        json.dumps(_rewrite_manifest(manifest, dest), indent=2)
    )

    logs_path_str = run_report.get("pointers", {}).get("logs_path")
    if logs_path_str:
        logs_path = Path(logs_path_str)
        if logs_path.exists():
            shutil.copy2(logs_path, meta_dest / logs_path.name)

    input_manifest_str = run_report.get("inputs", {}).get("inputs_manifest_path")
    if input_manifest_str:
        input_manifest = Path(input_manifest_str)
        if input_manifest.exists():
            shutil.copy2(input_manifest, meta_dest / input_manifest.name)

    print(f"PROMOTE OK: metadata copied to {meta_dest}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote stage artifacts to the final destination."
    )
    parser.add_argument(
        "--run-dir", required=True, type=Path,
        help="Directory containing run_report.json, manifest.json, and artifacts/",
    )
    parser.add_argument(
        "--dest", required=True, type=Path,
        help="Destination directory (e.g. .../semifield-developed-images/<batch_id>/images)",
    )
    args = parser.parse_args()
    return promote(args.run_dir, args.dest)


if __name__ == "__main__":
    raise SystemExit(main())