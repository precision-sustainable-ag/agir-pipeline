# Detection to Segmentation Stage

Runs segmentation inference inside YOLO detection boxes for each jpg image and generates one full-image class-coded mask PNG per source JPG.

---

## Output Structure

```
{output_dir}/
  det_to_seg/
    {run_id}/
      artifacts/
        masks/
          image1.png
          image2.png
      run.log
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

`EXIT_CONFIG_ERROR` happens at two points:
- before a run directory exists: `--i` or `--j` doesn't exist, the batch ID can't be determined, or `--t` isn't `0` or `1`. Nothing is written besides the console log.
- after the run directory is created: no detection `.txt` files, invalid class-assignment inputs, or processor init failure. `run_report.json` and `manifest.json` are still written, with a `CFG_VALIDATION_FAILED` stage error.

---

## Error Codes

Image error codes are recorded in `run_report.json` and `manifest.json` when processing fails. Stage-level errors are recorded under unit ID `__stage__`.

| Code | Meaning |
|------|---------|
| `DET_READ_FAILED` | Detection `.txt` file missing, unreadable, or malformed, or no JPG matches it |
| `IMAGE_READ_FAILED` | Failed to read input JPG image, or an unexpected error in the prefetch thread |
| `INFERENCE_FAILED` | Segmentation inference or compositing failed |
| `EXPORT_FAILED` | Output mask failed validation or couldn't be written to disk |
| `CFG_VALIDATION_FAILED` | Stage-level setup failure: no detection `.txt` files, invalid georeferenced CSV or species catalog, or processor init failure (bad config or model load) |
| `UNKNOWN` | Unexpected exception from batch processing (stage-level) |

`MODEL_LOAD_FAILED` is defined in `stages/det_to_seg/__init__.py` but is never recorded as its own code. A model load failure is reported as `CFG_VALIDATION_FAILED`, with an error message that starts with `MODEL_LOAD_FAILED:`.

---

## Class IDs

Mask pixel values are class IDs: `0` is background, and each detection's foreground pixels take that detection's class ID (`1`-`255`). Class IDs come from det_to_world's georeferenced CSV and the generated species catalog. The logic lives in `stages/common/class_ids.py`; `stages/det_to_seg/class_ids.py` re-exports it.

### Function: `load_class_id_index(georeferenced_csv_path, species_catalog_path) -> ClassIdIndex`

Loads the species catalog and georeferenced CSV and builds a lookup keyed by `(image_id, bounding_box_id)`. Image IDs match case-insensitively.

The catalog must be a JSON object with a `species` object whose entries each have a `class_id`. The CSV must have `image_id` and `species_id` columns. For each CSV row:
- rows with `assignment_method` = `no_detections` (det_to_world's placeholder for images with zero detections) are skipped
- `bounding_box_id` is read from its column when present; otherwise IDs are assigned per image in CSV row order starting at `0`
- a non-empty `cultivar_id` is used as the class ID; otherwise the class ID is the catalog `class_id` for `species_id`

Raises `ClassIdResolutionError` (or a subclass) when:
- the CSV or catalog can't be read, or the catalog is malformed
- a required CSV column is missing
- `bounding_box_id` isn't a non-negative integer
- `species_id` has no catalog entry (`UnknownSpeciesError`)
- a class ID isn't an integer in `1`-`255` (`InvalidClassIdError`)
- two rows give the same detection different class IDs (`DuplicateDetectionKeyError`); duplicates with the same class ID are allowed

The CLI calls this before loading the model, so any of these fails the whole run with `CFG_VALIDATION_FAILED` and exit code `3`.

---

### Function: `resolve_detection_class_ids(image_id, detections, class_ids, *, fallback_class_id=27) -> DetectionClassResolution`

Assigns each `DetectionBox` its class ID from the index. Detections with no matching CSV row get `FALLBACK_CLASS_ID` (`27`, the generic `PLANT` class) and are counted in `fallback_count`.

---

## `segmentor.py`

### Function: `load_weights_flex(model, path, strict=False) -> None`

Loads segmentation state dict weights and applies them to the inference model. Strips `model.`/`module.` key prefixes and logs missing or unexpected keys.

---

### Function: `build_seg_model(arch, encoder, weights_path, device) -> torch.nn.Module`

Constructs a `segmentation_models_pytorch` model (3 input channels, 1 output class), moves it to the target device, and loads checkpoint weights.

---

### Function: `predict_masks_batched(model, crops_rgb, thr, divisor, device) -> list[np.ndarray]`

Runs one forward call over crops of different sizes. Each crop is padded to the batch's largest height and width, rounded up to a multiple of `divisor` (reflect padding when possible, constant otherwise). Each output is sliced back to its own crop's size and thresholded into a binary `0`/`1` mask.

---

### Function: `predict_mask_single(model, crop_rgb, thr, divisor, device) -> np.ndarray`

Runs segmentation on one crop: `predict_masks_batched` with a batch of one.

---

### Function: `predict_mask_tiled(model, crop_rgb, thr, divisor, device, tile_size=1024, overlap=128, batch_size=16) -> np.ndarray`

Splits a large crop into overlapping tiles (step `tile_size - overlap`) and runs them through the model in batches of `batch_size`. Raw tile probabilities are blended with a Hann window, and the threshold is applied once to the blended result so tile seams aren't thresholded separately.

---

### Function: `predict_mask(model, crop_rgb, *, thr, divisor, device, use_tiling, tile_size, overlap, batch_size=16, ...) -> np.ndarray`

Uses tiled inference when `use_tiling` is true and the crop's longest side is over `1024` px or its area is over `1024 * 1024` px; otherwise uses single-pass inference.

---

### Function: `composite_bbox_masks(model, image_rgb, detections, config, device) -> np.ndarray`

Creates a full-image class-coded mask by combining individual crop masks. For each detection:
- clip bbox to valid image bounds
- extract RGB crop
- run segmentation on that crop
- composite the crop's foreground pixels into the full-image mask as the detection's class ID, but only where the full-image mask is still background — detections are composited in original TXT order, so the first detection to claim a pixel wins any overlap

Crops aren't inferred one at a time. Crops over the tiling threshold go through `predict_mask` individually; the rest are sorted by longest side and inferred in chunks of `batch_size` with `predict_masks_batched`. Compositing still happens in TXT order.

Returns a `uint8` mask with values `0` (background) or the detection's class ID (`1`-`255`). With no detections, returns an all-background mask without running the model. A class ID outside `1`-`255` raises `ValueError`.

---

### Function: `write_mask_png(class_mask, out_path, *, expected_shape=None) -> None`

Validates the class-coded mask with `validate_class_mask` (single-channel `uint8`, and matches `expected_shape` when given) and writes it directly as a PNG — pixel values are class IDs, not a 0/255 binary remap.

---

### Functions: `reset_forward_call_count()` / `get_forward_call_count() -> int`

Module-level counter of model forward calls. The processor resets it before each image and records the count as `n_forward_calls` for perf baselining.

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
| `n_fallback_detections` | `int` | Detections that used fallback class `27` |
| `error_code` | `str` | Error code if failed |
| `error_type` | `str \| None` | Exception class name if failed |
| `error_message` | `str \| None` | Exception message if failed |
| `retryable` | `bool` | Whether the error is retryable |
| `decode_seconds` | `float` | JPG decode time |
| `inference_seconds` | `float` | Segmentation and compositing time |
| `write_seconds` | `float` | Mask PNG write time |
| `n_forward_calls` | `int` | Model forward calls for this image |

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

**Optional keys:**
- `batch_size` (default `16`)

**Validation checks:**
- config is not empty
- all required keys are present
- `threshold` is in `[0, 1]`
- `pad_divisor` and `tile_size` are `> 0`
- `overlap` is `>= 0`
- `batch_size` is a positive integer

---

### Function: `load_config(config_path: Path) -> dict`

Loads segmentation config from YAML, resolves the `weights` path relative to the config file's directory, and validates the result.

---

### Function: `parse_yolo_detections(txt_path, width, height) -> list[DetectionBox]`

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

The bbox coordinates are normalized to `[0, 1]`.

Each row becomes a `DetectionBox` with pixel `xyxy` coordinates clamped to the image, and a `bounding_box_id` set to the row's zero-based index among non-blank lines. That ID is how detections join to the georeferenced CSV. `cls` and `conf` are parsed but not used; class IDs come from [Class IDs](#class-ids). Boxes with zero width or height after clamping are dropped, but they still use up their row index. Raises on a missing file, or a row with fewer than five values or non-numeric values.

---

### Class: `Processor`

High-level interface for detection-to-segmentation processing.

**Constructor: `__init__(self, config_path, device="cpu", class_id_index=None)`**
- loads YAML config
- loads segmentation model from checkpoint (failures raise `RuntimeError` prefixed with `MODEL_LOAD_FAILED`)
- stores config, device, and class ID index. Without a `class_id_index`, every detection keeps fallback class `27`.

**Method: `process_image(self, txt_path, jpg_path, output_dir) -> SegmentationResult`**
- returns `ok` without doing any work if `{image_id}.png` already exists
- reads JPG
- parses detection txt and resolves class IDs
- combines bbox masks for each bounding box into one final class-coded mask
- writes `{image_id}.png`
- returns a `SegmentationResult` with per-phase timings

Each step is wrapped in its own `try/except`, so failure type maps to a stable stage error code.

**Method: `process_batch(self, image_pairs, output_dir, fail_stop=True) -> list[SegmentationResult]`**

Processes images sequentially with one model copy. A background thread prepares the next image (skip check, JPG decode, detection parsing, class-ID resolution) while the main thread runs GPU inference and writes the mask for the current one. The thread never touches the model, and the queue between them holds one image. With `fail_stop`, returns at the first failed image and stops the prefetch thread.

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
| `--georeferenced-csv` | Path | Yes | — | det_to_world georeferenced CSV with species/cultivar assignments per detection |
| `--species-catalog` | Path | Yes | — | Generated species catalog JSON used to resolve class IDs |
| `--fs` | flag | No | false | Stop on first failure |
| `--batch-id` | str | No | auto | Batch ID. Auto-inferred from `--i`, then `--j`, if omitted (the path must contain `XX_YYYY-MM-DD`) |
| `--device` | str | No | cpu | Torch device (`cpu`, `cuda`, `cuda:0`, etc.) |

`--t` is hidden from `--help`. The stage is single-process, so only `0` or `1` is accepted; any other value exits with code `3`.

### Matching Behavior

- Detection files are discovered from `--i` using `*.txt` and `*.TXT`
- JPGs are indexed from `--j` using `.jpg` and `.jpeg`
- Matching between txt and image files is by lowercase stem
- Missing JPGs are recorded as `DET_READ_FAILED` failures (`MissingImageError`, not retryable) in the report/manifest
- With `--fs`, the first missing JPG stops matching; images already matched are still processed

### Manifest Artifact Shape

Successful items are written with:

```json
{
  "image_id": "TX_1687344534",
  "status": "ok",
  "artifacts": { "mask_path": "masks/TX_1687344534.png" },
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

### Run Report Extras

Besides the standard fields, `run_report.json` has these top-level keys:

| Key | Description |
|-----|-------------|
| `georeferenced_csv_path` | Value of `--georeferenced-csv` |
| `species_catalog_path` | Value of `--species-catalog` |
| `fallback_detection_count` | Detections across the run that used fallback class `27` |
| `timing` | Per-image timing summary; only written when the run gets past setup |

`timing` has `decode_seconds`, `inference_seconds`, and `write_seconds`, each summarized as `n`, `sum`, `mean`, `p50`, `p90`, and `max` over images with a nonzero value, plus `forward_calls_total`. On CUDA it also has `peak_gpu_memory_mb`.

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
batch_size: 16
```

The checked-in `stages/det_to_seg/configs/default.yaml` has a placeholder `weights` path; set it to a real checkpoint before running. `batch_size` sets how many crops or tiles go into each forward call.

---

## Local Example

```bash
python3 -m stages.det_to_seg.cli \
  --i /path/to/detections/artifacts \
  --j /path/to/jpgs \
  --c stages/det_to_seg/configs/default.yaml \
  --o /path/to/output \
  --georeferenced-csv /path/to/batch_georeferenced.csv \
  --species-catalog /path/to/species_catalog.generated.json \
  --batch-id TX_2025-08-18 \
  --device cpu
```

`--georeferenced-csv` and `--species-catalog` are required (see CLI Arguments above) -- omitting them fails argument parsing before any processing starts.

---

## Atlas GPU Test

```bash
sbatch tests/gpu/det_to_seg/det_to_seg_a100.sh
```

- uses `gpu-a100`
- defaults to batch `MD_2025-04-25`; override `BATCH_ID`, input/output paths, or `SEG_DEVICE` with `sbatch --export=ALL,...`
- requires the batch's georeferenced CSV (default `$BATCH_ROOT/georeferenced/${BATCH_ID}_georeferenced.csv`) and the species catalog (default `/project/dash_agir/semifield-utils/species_information/species_catalog.generated.json`)
- resolves the newest `jpg_to_det` artifacts directory under the configured detection root
- runs the CLI with `--fs`
- validates that the run directory, `manifest.json`, `run_report.json`, and at least one mask PNG were produced
- renders a random sample of mask overlays into `{run_dir}/visualizations/` with `tests/gpu/det_to_seg/visualize_segmentation.py`

---

## Testing

Unit tests live in:

```bash
stages/det_to_seg/tests/test_processor.py
stages/det_to_seg/tests/test_segmentor.py
stages/det_to_seg/tests/test_class_ids.py
```

Current test coverage:
- `test_processor.py`
  - config loading and validation
  - detection parsing
  - successful image processing
  - zero-detection behavior
  - image read, detection read, inference, export, and model-load failure handling
  - idempotent skip behavior, including skipping JPG decode
  - batch fail-stop vs continue behavior
  - prefetch thread shutdown on fail-stop, and prepare failures surfaced from the prefetch thread
- `test_segmentor.py`
  - batched inference: one forward call per chunk, mixed crop sizes, no padding leakage across crops
  - size-sorted chunking and `batch_size`
  - large crops inferred individually
  - tiled inference batching, and probability blending before thresholding
  - class compositing with first detection winning overlaps
  - zero detections, and class `0` rejected
  - mask validation, and class values preserved in the written PNG
- `test_class_ids.py`
  - species and cultivar resolution, case-insensitive image IDs
  - fallback class for detections with no CSV row
  - duplicate detection keys
  - malformed `bounding_box_id`, unknown species, out-of-range class IDs
  - malformed catalog, missing CSV columns
