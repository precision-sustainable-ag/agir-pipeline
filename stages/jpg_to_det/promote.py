"""
Promote jpg_to_det outputs to the final semifield-detections destination.

Promotion rules:
  - run_report.json must show exit_code == 0
  - manifest.json must have zero failed items (100% success required)
  - Every listed .txt file must exist at artifacts_root/det_txt_path and its
    SHA-256 checksum must match the value recorded in the manifest
  - The batch-level CSV ({batch_id}.csv) is copied if present in artifacts_root
  - Only after all checks pass are the .txt files and CSV copied to --dest and
    run_report.json, manifest.json, run.log, and any inputs_manifest copied
    to dest.parent/{stage}/ so each stage's metadata is isolated

Exit codes:
  0  all checks passed and files copied
  1  promotion skipped or failed (details printed to stdout)
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


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
    old_artifacts_path = Path(old_artifacts_dir) if old_artifacts_dir else None
    for entry in outputs.get("artifacts", []):
        entry_path = entry.get("path")
        if not entry_path or not old_artifacts_path:
            continue
        p = Path(entry_path)
        if p == old_artifacts_path:
            # Directory-level artifact (e.g. detection_txt points at artifacts_dir)
            entry["path"] = str(dest)
        else:
            # File-level artifact inside artifacts_dir (e.g. detection_csv points at a specific file)
            try:
                rel = p.relative_to(old_artifacts_path)
                entry["path"] = str(dest / rel)
            except ValueError:
                pass  # path is outside artifacts_dir entirely — leave it unchanged

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


# ── Core promotion logic ──────────────────────────────────────────────────────

def promote(run_dir: Path, dest: Path, viz_dir: Path | None = None) -> int:
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

    # ── Verify every .txt exists and checksum matches ─────────────────────────
    for item in items:
        image_id = item.get("image_id", "<unknown>")
        txt_rel = (item.get("artifacts") or {}).get("det_txt_path")
        if not txt_rel:
            print(f"PROMOTE FAIL: item {image_id} has no det_txt_path in artifacts")
            return 1

        txt_path = artifacts_root / txt_rel
        if not txt_path.exists():
            print(f"PROMOTE FAIL: .txt missing on disk: {txt_path}")
            return 1

        expected = (item.get("checksum") or {}).get("det_txt_path")
        if expected:
            actual = _sha256(txt_path)
            if actual != expected:
                print(
                    f"PROMOTE FAIL: checksum mismatch for {image_id}\n"
                    f"  expected: {expected}\n"
                    f"  actual:   {actual}"
                )
                return 1

    # ── All checks passed — copy .txt files to destination ───────────────────
    dest.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for item in items:
        txt_rel = item["artifacts"]["det_txt_path"]
        src = artifacts_root / txt_rel
        dst = dest / src.name
        shutil.copy2(src, dst)
        promoted += 1

    # ── Copy batch CSV if present ─────────────────────────────────────────────
    # The CSV is not tracked per-item in the manifest; it lives directly in
    # artifacts_root as {batch_id}.csv and is referenced in the run_report.
    batch_id = run_report.get("batch_id", "")
    csv_src = artifacts_root / f"{batch_id}.csv"
    if csv_src.exists():
        shutil.copy2(csv_src, dest / csv_src.name)
        print(f"PROMOTE OK: batch CSV copied to {dest / csv_src.name}")
    else:
        # Non-fatal — zero detections is valid (empty batch produces no CSV)
        print(f"PROMOTE INFO: no batch CSV found at {csv_src} (zero detections?)")

    # ── Copy metadata to stage-namespaced subdirectory ────────────────────────
    stage = run_report.get("stage", "jpg_to_det")
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

    # ── Copy visualizations if provided ──────────────────────────────────────
    if viz_dir is not None:
        viz_dir = Path(viz_dir)
        if not viz_dir.is_dir():
            print(f"PROMOTE WARN: viz-dir {viz_dir} does not exist — skipping visualizations")
        else:
            viz_files = sorted(viz_dir.glob("*.jpg")) + sorted(viz_dir.glob("*.JPG"))
            if not viz_files:
                print(f"PROMOTE INFO: no visualization JPGs found in {viz_dir}")
            else:
                viz_dest = meta_dest / "visualizations"
                viz_dest.mkdir(parents=True, exist_ok=True)
                for vf in viz_files:
                    shutil.copy2(vf, viz_dest / vf.name)
                print(f"PROMOTE OK: {len(viz_files)} visualization JPGs copied to {viz_dest}")

    print(f"PROMOTE OK: {promoted} .txt files copied to {dest}")
    print(f"PROMOTE OK: run_report.json, manifest.json, and run.log copied to {meta_dest}")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote jpg_to_det artifacts to the final destination."
    )
    parser.add_argument(
        "--run-dir", required=True, type=Path,
        help="Directory containing run_report.json, manifest.json, and artifacts/",
    )
    parser.add_argument(
        "--dest", required=True, type=Path,
        help=(
            "Destination directory "
            "(e.g. /90daydata/dash_agir/semifield-detections/<batch_id>/detections)"
        ),
    )
    parser.add_argument(
        "--viz-dir", type=Path, default=None,
        help=(
            "Directory of rendered visualization JPGs to promote alongside artifacts. "
            "Defaults to <run-dir>/visualizations if not set."
        ),
    )
    args = parser.parse_args()

    # Default viz_dir to run_dir/visualizations if not explicitly passed
    viz_dir = args.viz_dir if args.viz_dir is not None else args.run_dir / "visualizations"

    return promote(args.run_dir, args.dest, viz_dir=viz_dir)


if __name__ == "__main__":
    raise SystemExit(main())