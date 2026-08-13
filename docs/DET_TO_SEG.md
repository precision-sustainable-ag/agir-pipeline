# Detection to Segmentation Stage

Runs segmentation inference inside YOLO detection boxes for each jpg image and generates one full-image binary mask PNG per source JPG.

---

## Output Structure

```
{output_dir}/
  det_to_seg/
    {run_id}/
      artifacts/
        masks/
          image1_mask.png
          image2_mask.png
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

Image error codes are recorded in `run_report.json` and `manifest.json` when processing fails.

| Code | Meaning |
|------|---------|
| `DET_READ_FAILED` | Detection `.txt` file missing, unreadable, malformed, or unmatched to a JPG |
| `IMAGE_READ_FAILED` | Failed to read input JPG image |
| `INFERENCE_FAILED` | Segmentation inference or compositing failed |
| `EXPORT_FAILED` | Failed to write output mask PNG to disk |
| `MODEL_LOAD_FAILED` | Segmentation model weights could not be loaded |
| `CFG_VALIDATION_FAILED` | Segmentation config missing required keys or invalid values |
| `UNKNOWN` | Error that doesn't match any category |

---


## `segmentor.py`

### Function: `load_weights_flex(model, path, strict=False) -> None`

Loads segmentation state dict weights and applies them to the inference model.

---

### Function: `build_seg_model(arch, encoder, weights_path, device) -> torch.nn.Module`

Constructs a model, moves it to the target device, and loads checkpoint weights.


---

### Function: `predict_mask_single(...) -> np.ndarray`

Runs segmentation on one crop directly. Pads the tensor, to a number divisible for infernce, performs inference, applies a threshold to values in the mask, and removes padding if needed.

---

### Function: `predict_mask_tiled(...) -> np.ndarray`

Splits a large crop into a set of overlapping tiles, runs segmentation inference over tiles individually, and outputs a blend of the outputs.
---

### Function: `predict_mask(...) -> np.ndarray`

Chooses between single-pass and tiled inference for one crop, based on crop size and config flags. Not used by `composite_bbox_masks` for the common (non-tiled) case anymore — kept as a general-purpose single-crop utility; see `predict_masks_batch` below for the batched path `composite_bbox_masks` actually uses.

---

### Function: `predict_masks_batch(model, crops_rgb, thr, divisor, device) -> list[np.ndarray]`

Runs one forward pass for multiple same-pass (non-tiled) crops instead of one pass per crop. Crops may differ in size — each is padded to `divisor` individually, then all are further zero-padded up to the batch's max padded height/width so they can be stacked into one tensor. Callers should group similarly sized crops together first (see `composite_bbox_masks`) to keep that extra padding small.

---

### Function: `composite_bbox_masks(model, image_rgb, boxes_xyxy, config, device) -> np.ndarray`

Creates a full-image binary mask by combining individual per-box masks. For each bounding box, the box is clipped to valid image bounds and its RGB crop extracted. From there:
- **Large crops** (bigger than 1024px on a side, or over ~1MP — same threshold `predict_mask` uses) still run individually through `predict_mask_tiled`, which does its own tile-by-tile work.
- **Everything else** is grouped, sorted by crop size, and chunked into groups of up to `batch_size` (config key, default 16) — each chunk runs through `predict_masks_batch` as a single forward pass, instead of one model call per box. Sorting by size before chunking keeps crops of similar dimensions together, so the batch-level padding `predict_masks_batch` adds doesn't waste compute pairing a tiny box with a near-tile-sized one.

Each box's resulting mask is composited into the full-image mask with logical OR. Returns a `uint8` mask with values `0` or `1`.

---

### Function: `write_mask_png(mask01, out_path) -> None`

Saves the binary mask as a PNG by writing background pixels as 0 and foreground pixels as 255.

---

## `processor.py`

### Dataclass: `SegmentationResult`

Result of processing a single detection/JPG pair.

| Field | Type | Description |
|-------|------|-------------|
| `image_id` | `str` | Image stem |
| `status` | `str` | `"ok"` or `"failed"` |
| `mask_path` | `Path \| None` | Path to output mask PNG |
| `n_detections` | `int` | Number of parsed detection boxes |
| `error_code` | `str` | Error code if failed |
| `error_type` | `str \| None` | Exception class name if failed |
| `error_message` | `str \| None` | Exception message if failed |
| `retryable` | `bool` | Whether the error is retryable |

---

### Function: `validate_config(config: dict) -> None`

Validates that config has all required keys and basic value constraints.

**Required keys:**
- `weights`
- `arch`
- `encoder`
- `threshold`
- `pad_divisor`
- `tile_size`
- `overlap`
- `tiling`

**Validation checks:**
- config is not empty
- all required keys are present
- `threshold` is in `[0, 1]`
- `pad_divisor` and `tile_size` are `> 0`
- `overlap` is `>= 0`

**Optional keys:**
- `batch_size` (int, default 16) — max number of non-tiled crops grouped into one forward pass by `composite_bbox_masks`/`predict_masks_batch`. Not validated here; unset falls back to `segmentor.DEFAULT_INFERENCE_BATCH_SIZE`.

---

### Function: `load_config(config_path: Path) -> dict`

Loads segmentation config from YAML, resolves the weights path, and validates the result.

---

### Function: `parse_yolo_detections(txt_path, width, height) -> list[tuple[int, int, int, int]]`

Parses YOLO rows in the form:

```text
cls xc yc w h [conf]
```
where 
  - cls = class id
  - xc = x-coordinate of the box center
  - yc = y-coordinate of the box center
  - w = box width
  - h = box height
  - [conf] = optional confidence score
The bbox coordinates normalized.

---

### Class: `Processor`

High-level interface for detection-to-segmentation processing.

**Constructor: `__init__(self, config_path, device="cpu")`**
- loads YAML config
- loads segmentation model from checkpoint
- stores config and device

**Method: `process_image(self, txt_path, jpg_path, output_dir) -> SegmentationResult`**
- reads JPG
- parses detection txt
- combines bbox masks for each bounding box into one final mask
- writes `{image_id}_mask.png`
- returns a `SegmentationResult`

Each stage is wrapped in its own `try/except`, so failure type maps to a stable stage error code.

**Method: `process_batch(self, image_pairs, output_dir, fail_stop=True) -> list[SegmentationResult]`**
Processes the image batch sequentially in one process — each image's own detection boxes are already batched into a handful of model forward passes internally (see `composite_bbox_masks`), so there's no separate multi-worker path. An earlier `ProcessPoolExecutor`-based worker-pool mode was removed: on a single-GPU job it added no real parallelism (N processes sharing one GPU) and crashed under CUDA, since `torch.cuda` cannot survive `fork()` (Python multiprocessing's default start method on Linux).

---

## `cli.py`

Command-line entry point for the `det_to_seg` stage. Outputs `run_report.json` and `manifest.json`.

### Arguments

| Flag | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `--i` | Path | Yes | — | Detection artifacts directory containing per-image `.txt` files |
| `--j` | Path | Yes | — | Directory containing original JPG images |
| `--c` | Path | Yes | — | Path to segmentation YAML config file |
| `--o` | Path | Yes | — | Output directory |
| `--fs` | flag | No | false | Stop on first failure |
| `--batch-id` | str | No | auto | Batch ID. Auto-inferred from input paths if omitted |
| `--device` | str | No | cpu | Torch device (`cpu`, `cuda`, `cuda:0`, etc.) |

### Matching Behavior

- Detection files are discovered from `--i` using `*.txt` and `*.TXT`
- JPGs are indexed from `--j` using `.jpg` and `.jpeg`
- Matching between txt and image files is by lowercase stem
- Missing JPGs are recorded as failures in the report/manifest

### Manifest Artifact Shape

Successful items are written with:

```json
{
  "image_id": "TX_1687344534",
  "status": "ok",
  "artifacts": { "mask_path": "masks/TX_1687344534_mask.png" },
  "checksum": { "mask_path": "sha256:..." },
  "size_bytes": { "mask_path": 182334 }
}
```

### Run Report Artifact Type

The stage records:

```python
report.add_artifact_type(
    artifact_type="segmentation_mask",
    path=str(masks_dir),
    n_files=num_succeeded,
)
```

---

## Config

Example config:

```yaml
weights: /project/dash_agir/matthew.kutugata/repos/AgIR-CVToolkit/data/plant_segmentation_model/epoch=47-step=37200-val_loss=0.00.ckpt
arch: Unet
encoder: mit_b4
threshold: 0.5
pad_divisor: 32
tile_size: 1024
overlap: 128
tiling: true
batch_size: 16  # optional, see validate_config's "Optional keys" above
```


---

## Local Example

```bash
python3 -m stages.det_to_seg.cli \
  --i /path/to/detections/artifacts \
  --j /path/to/jpgs \
  --c stages/det_to_seg/configs/default.yaml \
  --o /path/to/output \
  --batch-id TX_2025-08-18 \
  --device cpu
```

---

## Atlas GPU Test

```bash
sbatch det_to_seg_a100.sh
```

- uses `gpu-a100`
- resolves the newest `jpg_to_det` artifacts directory under the configured detection root
- validates that the run directory, `manifest.json`, `run_report.json`, and at least one mask PNG were produced

---

## Testing

Unit tests live in:

```bash
stages/det_to_seg/tests/test_processor.py
```

Current test coverage:
- config loading and validation
- detection parsing
- successful image processing
- zero-detection behavior
- image read, detection read, inference, export, and model-load failure handling
- idempotent skip behavior
- batch fail-stop vs continue behavior
