# JPG to Detection Stage

Runs multiscale YOLO plant detection on JPG images, producing per-image YOLO `.txt` label files and a batch-level CSV of all detections.

---

## Output Structure

```
{output_dir}/
  jpg_to_det/
    {run_id}/
      artifacts/
        image1.txt
        image2.txt
        TX_2024-06-01.csv
      run_report.json
      manifest.json
```

## Exit Codes

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `EXIT_SUCCESS` | All images processed |
| 1 | `EXIT_PARTIAL` | Some images failed |
| 2 | `EXIT_FAILURE` | All images failed |
| 3 | `EXIT_CONFIG_ERROR` | Setup/config error |

---

## Error Codes

Per-image error codes are recorded in `run_report.json` and `manifest.json` when processing fails. Error classification is determined by which processing step failed (image read, inference, or export), not by string matching.

| Code | Meaning |
|------|---------|
| `IMAGE_READ_FAILED` | Failed to read input JPG (cv2.imread returned None) |
| `INFERENCE_FAILED` | YOLO model inference or multiscale pipeline failed |
| `EXPORT_FAILED` | Failed to write detection .txt or build detection rows |
| `MODEL_LOAD_FAILED` | YOLO model weights could not be loaded |
| `CFG_VALIDATION_FAILED` | Detection config missing required keys or invalid values |
| `UNKNOWN` | Error that doesn't match any category |

---

## Detection Pipeline Overview

```
JPG image
  |
  v
run_multiscale()
  |-- YOLO predict at each scale (e.g. 0.5x, 1.0x, 1.5x)
  |-- Edge-aware confidence filtering (optional)
  |-- Weighted Box Fusion (WBF) across scales
  |-- Post-fusion Non-Maximum Suppression (optional)
  |-- Final confidence threshold + NMS
  |
  v
Nx6 tensor [x1, y1, x2, y2, conf, cls]  (absolute pixel coordinates)
  |
  v
export_predictions()
  |-- Per-image YOLO .txt (normalized coordinates)
  |-- Detection rows for batch CSV
```

---

## `detector.py`

### Function: `iou_xyxy(a, b) -> float`

Computes intersection-over-union between two `[x1, y1, x2, y2]` bounding boxes.

---

### Function: `edge_aware_filter(boxes_xyxy, scores, img_wh, ...) -> (keep_mask, dyn_thr)`

Dynamic confidence threshold based on distance to the nearest image edge. Boxes near the frame border get a lower threshold (down to `base_conf * min_factor`) so partial plants at edges are not discarded too aggressively. The threshold ramps linearly back to `base_conf` between `edge_band_rel` and `taper_rel`.

**Parameters:**
- `base_conf` — Normal confidence threshold for non-edge boxes
- `edge_band_rel` — Fraction of shorter side defining the hard edge zone
- `min_factor` — Multiplier on `base_conf` at the very edge (e.g. 0.60)
- `taper_rel` — Relative distance at which the threshold returns to `base_conf`

---

### Function: `weighted_box_fusion_single_class(boxes, scores, iou_thr, score_thr) -> (fused_boxes, fused_scores)`

Clusters overlapping boxes by IoU (internal overlap allowed), then fuses into a single box, weighting each box by its confidence. Operates on normalized coordinates per class.

**Parameters:**
- `boxes` — Nx4 array of normalized `[x1, y1, x2, y2]` coordinates
- `scores` — N array of confidence scores
- `iou_thr` — IoU threshold for clustering boxes together
- `score_thr` — Minimum confidence to include a box

**Returns:**
- `fused_boxes` — Mx4 array of fused coordinates (normalized)
- `fused_scores` — M array of fused confidence scores

---

### Function: `weighted_box_fusion_all_classes(boxes_xyxy_norm, scores, classes, iou_thr, score_thr) -> Tensor`

Runs single-class WBF independently per class ID, then concatenates and sorts by confidence descending.

**Parameters:**
- `boxes_xyxy_norm` — Nx4 array of normalized `[x1, y1, x2, y2]` coordinates
- `scores` — N array of confidence scores
- `classes` — N array of integer class IDs
- `iou_thr` — IoU threshold for clustering
- `score_thr` — Minimum confidence to include a box

**Returns:** Mx6 tensor `[x1, y1, x2, y2, conf, cls]` in normalized coordinates.

---

### Function: `nms_xyxy_abs(dets_xyxy_conf_cls, iou_thr, max_det) -> Tensor`

Class-aware non-maximum suppression (box removal). Boxes of different classes do affect the suppression of each other.

**Parameters:**
- `dets_xyxy_conf_cls` — Nx6 tensor `[x1, y1, x2, y2, conf, cls]` in absolute pixel coordinates
- `iou_thr` — IoU threshold above which the lower-confidence box is removed
- `max_det` — Maximum detections to keep

**Returns:** Mx6 tensor of surviving detections in absolute pixel coordinates.

---

### Function: `export_predictions(results_raw_xyxy_abs, save_dir, filename, names, im0) -> (txt_path, detection_rows)`

Writes per-image YOLO `.txt` label file and returns detection rows for the batch CSV.

**Parameters:**
- `results_raw_xyxy_abs` — Nx6 tensor `[x1, y1, x2, y2, conf, cls]` to represent box results, or None/empty
- `save_dir` — Directory where the `.txt` file is written
- `filename` — Original image filename (stem used for output name)
- `names` — Dict mapping class IDs to names (e.g. `{0: "plant"}`)
- `im0` — Original image array (HxWx3) for coordinate normalization

**Returns:**
- `txt_path` — Path to YOLO `.txt` file: `cls x_center y_center width height conf` (normalized)
- `detection_rows` — `list[dict]` with keys: `image_id`, `bounding_box_id`, `xmin`, `ymin`, `xmax`, `ymax`, `conf`, `class`, `classname` (normalized)

---

### Function: `run_multiscale(model, im0_bgr, config, device) -> Tensor`

Main detection pipeline. Runs YOLO inference at multiple image scales, fuses overlapping detections, and applies final filtering.

**Parameters:**
- `model` — Loaded YOLO model instance
- `im0_bgr` — Original BGR image as numpy array (HxWx3)
- `config` — Detection config dict
- `device` — Torch device string (e.g. `"cpu"`, `"cuda:0"`)

**Flow:**
1. Compute scaled image sizes from `base_imgsz * scales[]`
2. Run `model.predict()` at each scale with per-scale conf/iou/max_det thresholds
3. Normalize all boxes to [0,1] and concatenate across scales
4. Apply edge-aware filtering (if `edge_aware.enabled`)
5. Run weighted box fusion across all scales
6. Optional post-fusion NMS (if `post_fusion_nms.enabled`)
7. Apply final confidence threshold and final NMS

**Returns:** Nx6 tensor `[x1, y1, x2, y2, conf, cls]` in absolute pixel coordinates, or empty `(0, 6)` tensor if no detections survive.

---

## `processor.py`

### Dataclass: `DetectionResult`

Result of processing a single image.

| Field | Type | Description |
|-------|------|-------------|
| `image_id` | `str` | Image stem (filename without extension) |
| `status` | `str` | `"ok"` or `"failed"` |
| `txt_path` | `Path \| None` | Path to YOLO .txt output |
| `detection_rows` | `list[dict]` | Detection metadata for batch CSV |
| `n_detections` | `int` | Number of detections |
| `error_code` | `str` | Error code if failed |
| `error_type` | `str \| None` | Exception class name if failed |
| `error_message` | `str \| None` | Exception message if failed |
| `retryable` | `bool` | Whether the error is retryable |

---

### Function: `validate_config(config: dict) -> None`

Validates that config has all required keys with valid values.

**Required keys:** `base_imgsz`, `scales`, `per_scale_conf`, `per_scale_iou`, `per_scale_max_det`, `conf`, `iou`, `final_max_det`

**Validation checks:**
- Config is not empty/None
- All required keys present
- `scales` is a non-empty list
- `conf` is a float in [0, 1]

---

### Function: `load_config(config_path: Path) -> dict`

Loads detection config from YAML file and validates it.

---

### Class: `Processor`

Detection processor — loads YOLO model and runs multiscale detection on each JPG image.

**Constructor: `__init__(self, config_path, model_path, device="cpu")`**
- Loads and validates config from YAML
- Loads YOLO model from `.pt` weights file
- Extracts class name mapping from model

**Method: `process_image(self, jpg_path, output_dir) -> DetectionResult`**
- Processes a single JPG through read -> inference -> export
- Each step is wrapped in its own try/except, so the error code is determined by which step failed
- Always returns a `DetectionResult`

**Method: `process_batch(self, jpg_images, output_dir, fail_stop=True, max_workers=0) -> List[DetectionResult]`**
- Processes multiple JPG images with optional parallelization
- **Sequential mode** (`max_workers <= 1`, default): iterates and calls `process_image()`
- **Parallel mode** (`max_workers > 1`): uses `ThreadPoolExecutor` for threaded image processing
- If `fail_stop=True`, raises `RuntimeError` on first failure

---

## `cli.py`

Command-line entry point for the jpg_to_det stage. Outputs `run_report.json`, `manifest.json`, and a batch CSV.

### Arguments

| Flag | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `--c` | Path | Yes | — | Path to detection YAML config file |
| `--m` | Path | Yes | — | Path to YOLO model weights (.pt) |
| `--i` | Path | Yes | — | Input directory containing JPG images |
| `--o` | Path | Yes | — | Output directory for processed results |
| `--t` | int | No | 0 | Number of parallel threads (0/1 = sequential) |
| `--fs` | flag | No | false | Stop on first failure |
| `--batch-id` | str | No | auto | Batch ID (e.g. `TX_2024-06-01`). Auto-inferred from input path if omitted. |
| `--device` | str | No | cpu | Torch device (cpu, cuda, cuda:0, etc.) |

### Batch ID Resolution

`batch_id` is resolved in order:
1. Explicit `--batch-id` flag
2. Auto-parsed from the input path (looks for `XX_YYYY-MM-DD` pattern in path segments)
3. Exits with `EXIT_CONFIG_ERROR` if neither works

### Flow

1. Parse args, resolve `batch_id`
2. Validate input directory and load config
3. Initialize `Processor` with config, model, and device
4. Discover `*.jpg` / `*.JPG` files in input directory
5. Initialize `RunReportBuilder` and `ManifestBuilder` (from `stages.common`)
6. Process batch via `Processor.process_batch()`
7. Populate manifest items (ok/failed per image) and aggregate detection rows with metadata for detected objects
8. Write batch CSV with all detection rows (`{batch_id}.csv`)
9. Determine exit code from success/fail counts
10. Write `run_report.json` and `manifest.json` to `{output}/jpg_to_det/{run_id}/`

---

## Manifest & Report Details

### Manifest (`manifest.json`)

For each successfully processed image:

```json
{
  "image_id": "MD_1764960482",
  "status": "ok",
  "artifacts": {
    "det_txt_path": "MD_1764960482.txt",
    "det_csv_path": "TX_2024-06-01.csv"
  },
  "checksum": {
    "det_txt_path": "sha256:a1b2c3d4..."
  },
  "size_bytes": {
    "det_txt_path": 1024
  }
}
```

For failed images:
```json
{
  "image_id": "MD_corrupted",
  "status": "failed",
  "error": {
    "error_type": "RuntimeError",
    "message": "cv2.imread returned None for MD_corrupted.jpg",
    "retryable": false
  }
}
```

### Run Report (`run_report.json`)

High-level summary including:
- **Timing:** `started_at`, `ended_at`, `duration_ms`
- **Provenance:** `code_commit` (git hash), `config_path`, `config_hash` (SHA256), `model_id`
- **Inputs:** `input_root`, `n_units_discovered`
- **Outputs:** `artifacts_dir`, counts (`n_units_succeeded`, `n_units_failed`)
- **Artifacts:** `detection_txt` (per-image), `detection_csv` (batch-level)
- **Errors:** List of errors with `unit_id`, `code`, `type`, `message`, `retryable`
- **Logs:** Pointer to `logs_path`

---

## Orchestration

`jpg_to_det` is wired into `scripts/job/submit.py` (`SUPPORTED_STAGES`) and
`orchestrator/input_staging_planner.py` (`STAGE_INPUT_SPECS`), with its own
`v_batches_needing_jpg_to_det` SQLite readiness view. Unlike `raw_to_jpg`,
it resolves its one input (`images/`) through the same multi-site priority
resolver `det_to_world` uses, rather than one fixed Juno route.

### Destination ATLAS, then CERES, then JUNO LTS

`jpg_to_det` needs GPU (A100) and runs on ATLAS, but its images are usually
written by `raw_to_jpg`, which runs on CERES
(`configs/config.raw_to_jpg.example.yaml`'s `final_dest_root`). For each
batch, `orchestrator/input_staging_planner.py`'s `_plan_multi_site_requests()`
checks, in order:

1. **ATLAS** (`destination_site`) — if already indexed there (e.g. a rerun),
   nothing is transferred; the request is recorded as immediately
   `already_satisfied` in `staged_inputs`.
2. **CERES** (`transfer.routes.jpg_to_det.source_root_ceres`) — the common
   case, since `raw_to_jpg` just wrote the images there.
3. **JUNO LTS** (`transfer.routes.jpg_to_det.source_root_juno`) — final
   fallback when neither cluster has current images indexed.

`v_batches_needing_jpg_to_det` (see `schemas/sqlite/pipeline.sql`) does not
restrict which site a batch's JPGs must be indexed at — a batch is a
candidate regardless of whether its images currently live on ATLAS, CERES,
or JUNO, since the resolver above figures out where to pull from (or
whether staging is even needed).

### Full directory transfer, not sampled

`det_to_world`'s `images/` subdir is only a small random sample for an
optional visualization step — the stage CLI itself never reads image
pixels. `jpg_to_det` is the opposite: the detector reads every JPG, so its
`images/` subdir is always transferred as a whole recursive directory
(`StagingRequest.file_names` stays `None`), never sampled.

### Readiness gating

Because `jpg_to_det` has a single input piece, readiness is a straight
"any `staged_inputs` row completed" check
(`scripts/job/submit.py`'s `filter_completed_staged_inputs()`) — it does not
need `det_to_world`'s multi-piece "ALL pieces completed" gate
(`filter_det_to_world_staged_ready()`), since there's only one piece to wait
on.

---

## Sample Run Command

```sh
python3 -m stages.jpg_to_det.cli \
  --c configs/detection.yaml \
  --m /path/to/yolo_weights.pt \
  --i /mnt/data/NC_2025-08-25/jpgs/ \
  --o ./processed_detections \
  --t 4 \
  --device cuda:0
```

`batch_id` will be auto-inferred as `NC_2025-08-25` from the input path.

---

## Sample Config YAML

```yaml
base_imgsz: 4096
scales: [0.5, 1.0, 1.5]

# Per-scale thresholds (lenient — let WBF handle fusion)
per_scale_conf: 0.15
per_scale_iou: 0.5
per_scale_max_det: 1000

# Final thresholds (strict)
conf: 0.70
iou: 0.5
final_max_det: 1000

# Weighted box fusion
wbf_iou: 0.55
wbf_score_thr: 0.001

# Edge-aware filtering (optional)
edge_aware:
  enabled: true
  edge_band_rel: 0.08
  min_factor: 0.60
  taper_rel: 0.20

# Post-fusion NMS (optional)
post_fusion_nms:
  enabled: false
  iou: 0.5
```


## High Performance Running

Atlas test scripts are under [tests/gpu](/Users/brennenfarrell/Random/psa/agir-pipeline/tests/gpu), and current GPU timing/performance notes are in [performance.md](/Users/brennenfarrell/Random/psa/agir-pipeline/docs/performance.md).



As the inference in this task can be very compute intensive, it is recommended to run using Atlas. Example scrips can be found in tests/gpu/. Currently, the A100 is highly recommended. 


Atlas test scripts are under [tests/gpu](/Users/brennenfarrell/Random/psa/agir-pipeline/tests/gpu), and current GPU timing/performance notes are in [performance.md](/Users/brennenfarrell/Random/psa/agir-pipeline/docs/performance.md).


The scrips are described below

```bash
tests/gpu/
```



### A100

```bash
sbatch tests/gpu/jpg_to_det/jpg_to_det_a100.sh
```

### L40S

```bash
sbatch tests/gpu/jpg_to_det/jpg_to_det_l40s.sh
```

### A100 MIG7

```bash
sbatch tests/gpu/jpg_to_det/jpg_to_det_a100_mig7.sh
```

### Run All Three

```bash
bash tests/gpu/jpg_to_det/run_all_jpg_to_det.sh
```

### MIG7 Config

```yaml
base_imgsz: 2048
scales: [0.5, 0.75, 1.0]
per_scale_conf: 0.15
per_scale_iou: 0.5
per_scale_max_det: 300
conf: 0.70
iou: 0.5
final_max_det: 300
wbf_iou: 0.55
wbf_score_thr: 0.001
edge_aware:
  enabled: true
  edge_band_rel: 0.08
  min_factor: 0.60
  taper_rel: 0.20
post_fusion_nms:
  enabled: false
  iou: 0.5
```
