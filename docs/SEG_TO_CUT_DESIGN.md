# Segmentation to Cutouts (`seg_to_cut`)

`seg_to_cut` turns each detected plant into a small image crop, a transparent plant cutout, a mask, and a JSON file describing it. The stage is implemented and runs on CPUs, normally on Ceres.

## What it needs

| Input | What it provides |
|---|---|
| Original JPG images | The plant's image pixels. |
| Segmentation PNG masks | Which pixels belong to each species or cultivar. |
| Georeferenced CSV | Detection boxes, detection IDs, species/cultivar assignments, and world coordinates when available. |
| Generated species catalog | Numeric class IDs, plant names, and other category information. |

The usual batch layout is:

```text
semifield-developed-images/<batch_id>/
  images/<image_id>.jpg
  segmentations/<image_id>.png
  georeferenced/<batch_id>_georeferenced.csv
```

Images and masks are matched by filename without the extension. Each image with detections needs a matching mask with the same width and height. Masks must be 8-bit grayscale.

## How the stage works

1. Read the detections and look up each plant's species or cultivar class.
2. Crop each detection box, keeping the crop within the image boundaries.
3. Keep mask pixels for the target class and remove small regions confined to the crop's border band.
4. Skip detections that have no target pixels left.
5. Calculate plant properties, edge flags, and size comparisons within the batch.
6. Write the four output files and record successes, skips, and errors.

Processing happens in two passes: first, check which detections survive cleanup and calculate batch size averages; then, create their files. Images can be processed in parallel.

When a cultivar is assigned, its numeric ID is the mask value. Otherwise, the species catalog's `class_id` is used. `0` is background; plant values are between `1` and `255`. Pixels with any other class value are ignored for that cutout.

### Cleaning the border

The stage removes separate target-class regions that lie entirely within a band around the crop's edges. A region reaching the crop's interior is kept. The default band covers 10% of the height at the top/bottom and 10% of the width at the left/right, rounded up to at least one pixel.

This helps remove pieces of neighboring plants. It cannot separate plants that touch each other or remove neighboring regions that reach the interior. Very small crops with no interior left after applying the band are skipped. The original JPG crop is unchanged.

### Checking whether the plant is cut off

After cleanup, the stage checks the outermost row or column on each side. A side is flagged when more than 5% of those pixels belong to the plant, using the default threshold.

| Flagged sides | Placement guidance |
|---|---|
| None | Can be placed anywhere. |
| One | Place against that edge. |
| Two adjacent | Place in that corner. |
| Two opposite, or three/four | Unsuitable for synthetic placement. |

This guidance is saved in `edge_cut.synthetic_placement`; unsuitable cutouts are still written. The metadata also distinguishes an edge at the full image boundary from an edge created by the detection box.

`extends_border` simply means at least one plant pixel touches the crop edge, even if there are too few pixels to flag that side.

## Output files

Each cutout is named `<image_id>_<bounding_box_id>`. For example, detection 0 in image `MD_1784297213` becomes `MD_1784297213_0`.

| File | Contents |
|---|---|
| `<cutout_id>.jpg` | Original rectangular RGB crop, saved at JPEG quality 100. |
| `<cutout_id>.png` | Plant cutout with a transparent background. Background RGB pixels are black. |
| `<cutout_id>_mask.png` | Grayscale mask: zero for background and the target class ID for plant pixels. |
| `<cutout_id>.json` | Identity, dimensions, plant category, and calculated properties. |

All three image files have the same dimensions. `cutout_num` is the detection's bounding-box ID, so skipped detections can leave gaps in numbering.

The stage writes into a run directory:

```text
<output>/seg_to_cut/<run_id>/
  artifacts/cutouts/<cutout files>
  logs/
  manifest.json
  run_report.json
```

The manifest lists each cutout's result and output files. The run report summarizes counts and errors. Files are checked before final names are created, and handled write errors trigger cleanup. Existing cutout files are never overwritten.

## JSON structure

This example shows the current output fields. Values are illustrative. `category` may include more names, taxonomy, colors, links, or cultivar details from the catalog.

```json
{
  "season": "summer_weeds_2026",
  "datetime": "2026:07:17 15:00:13",
  "bbot_version": "3.1",
  "batch_id": "MD_2026-07-17",
  "image_id": "MD_1784297213",
  "cutout_id": "MD_1784297213_0",
  "cutout_num": 0,
  "cutout_height": 400,
  "cutout_width": 600,
  "lens_model": "Linos Inspect XL",
  "validated": false,
  "cutout_version": "2.0",
  "cutout_props": {
    "is_primary": true,
    "intruder_cleanup": {
      "method": "border_sweep",
      "border_band_fraction": 0.1,
      "border_width_px": {
        "top_bottom": 40,
        "left_right": 60
      },
      "removed_components": 2,
      "remaining_components": 1,
      "removed_pixels": 231
    },
    "extends_border": true,
    "edge_cut": {
      "flagged": true,
      "threshold": 0.05,
      "band_width_px": {
        "top_bottom": 1,
        "left_right": 1
      },
      "flagged_sides": [
        "right"
      ],
      "source_image_sides": [
        "right"
      ],
      "detection_box_truncation_sides": [],
      "synthetic_placement": "right_edge_only",
      "plant_fraction": {
        "top": 0.0,
        "bottom": 0.02,
        "left": 0.0,
        "right": 0.34
      }
    },
    "bbox_area_cm2": 120.0,
    "species_mean_bbox_area_cm2": 100.0,
    "species_bbox_sample_size": 10,
    "species_bbox_area_ratio": 1.2,
    "abnormal_bbox_size": false,
    "solidity": 0.73,
    "blur_effect": 0.404,
    "num_components": 1,
    "cropout_rgb_mean": [
      0.366,
      0.339,
      0.283
    ],
    "cropout_rgb_std": [
      0.148,
      0.13,
      0.095
    ],
    "non_target_weed": null,
    "non_target_weed_pred_conf": null
  },
  "category": {
    "species_id": "URRE2",
    "class_id": 42,
    "USDA_symbol": "URRE2",
    "common_name": "Sprawling Signalgrass",
    "family": "Poaceae",
    "genus": "Urochloa",
    "cultivar_id": null,
    "cultivar_class_id": null,
    "cultivar_name": null
  }
}
```

### What the fields mean

| Field or group | Meaning |
|---|---|
| `batch_id`, `image_id`, `cutout_id`, `cutout_num` | Identify the batch, source image, and detection. |
| `cutout_height`, `cutout_width` | Crop dimensions in pixels. |
| `season`, `bbot_version` | Values supplied when running the stage. |
| `datetime`, `lens_model` | Capture time and lens from the original image's EXIF metadata. `--lens-model` supplies a fallback lens. |
| `validated` | Starts as false; stage completion does not mean the cutout has been reviewed. |
| `cutout_version` | Metadata version from the config, currently `"2.0"` by default. |
| `is_primary` | Primary-detection flag copied from the CSV; both primary and non-primary detections are processed. |
| `intruder_cleanup` | Border-band settings and counts of removed/remaining regions and removed pixels. |
| `extends_border`, `edge_cut` | Whether the plant touches the crop edges and where it can be placed. |
| `bbox_area_cm2` | Physical area of the detection box, calculated from valid projected world coordinates. This measures the box, not the plant silhouette. |
| `species_mean_bbox_area_cm2`, `species_bbox_sample_size` | Average box area and number of valid area samples in the same batch/category. Groups use cultivar when present, otherwise species. |
| `species_bbox_area_ratio`, `abnormal_bbox_size` | Current area divided by the group average, and whether the difference exceeds the configured limit. |
| `solidity` | How much of the plant's enclosing convex shape is filled by plant pixels. |
| `blur_effect` | Blur score measured on the cutout with a black background. Higher means blurrier. |
| `num_components` | Number of separate plant regions remaining after cleanup. |
| `cropout_rgb_mean`, `cropout_rgb_std` | Average color and color variation across the original unmasked crop, in RGB order on a 0–1 scale. |
| `non_target_weed`, `non_target_weed_pred_conf` | Currently null; the stage does not run a weed classifier. |
| `category` | Species identity and catalog information. Cultivar fields are null for species-only cutouts. |

Unavailable values are written as `null`. This includes missing capture metadata and physical-area measurements that cannot be calculated. Missing optional catalog fields are omitted.

For cultivar cutouts, use `category.cultivar_class_id` as the mask value. For species-only cutouts, use `category.class_id`.

## Configuration

Stage settings live in [`stages/seg_to_cut/configs/default.yaml`](../stages/seg_to_cut/configs/default.yaml). Pass another settings file with `--config` to override them.

| Setting | Default | What changing it does |
|---|---|---|
| `image_extensions` | `[.jpg, .jpeg]` | Chooses which input image files are found. |
| `mask_extension` | `.png` | Chooses which input mask files are found. |
| `border_band_fraction` | `0.10` | Larger values widen the cleanup band and can remove more border regions. Must be greater than 0 and less than 0.5. |
| `edge_threshold` | `0.05` | Larger values require more plant coverage before an edge is flagged. Allowed range: 0–1. |
| `cutout_version` | `"2.0"` | Sets the version label in the JSON. |
| `bbox_area.source` | `georeferenced_csv` | Uses world coordinates from the CSV to calculate physical area. |
| `species_bbox_min_sample_size` | `5` | Requires this many valid areas before calculating a group average. Must be a positive integer. |
| `abnormal_bbox_size_threshold` | `0.50` | Flags an area more than 50% above or below its group average. Must be nonnegative. |

For example, if the group average is 100 cm², the default size threshold flags areas below 50 cm² or above 150 cm². If the group has fewer than five valid areas, its average, area ratio, and abnormal-size flag are null; the sample count is still recorded.

`bbox_area.source: camera` is accepted but currently produces null areas. The YAML's camera specifications are descriptive; they do not yet calculate area or supply the metadata's lens model.

The separate [`Ceres job configuration`](../configs/config.seg_to_cut.ceres.example.yaml) controls input staging, CPU/memory allocation, paths, and publication. Its example uses 8 CPUs, 64 GB of memory, and four hours. `publication_mode: cutout_batch` publishes complete batches, while `result_sync.enabled: false` keeps this stage's outputs on Ceres.

## Running it

Replace these example paths with the actual batch and catalog locations:

```bash
python -m stages.seg_to_cut.cli \
  --images /data/batch/images \
  --segmentations /data/batch/segmentations \
  --georeferenced-csv /data/batch/MD_2026-07-17_georeferenced.csv \
  --species-catalog /data/reference/species_catalog.generated.json \
  --config stages/seg_to_cut/configs/default.yaml \
  --batch-id MD_2026-07-17 \
  --season summer_weeds_2026 \
  --bbot-version 3.1 \
  --output /data/stage_runs \
  --t 8
```

`--t` sets the number of parallel image workers; the default is 8. Use 0 or 1 to process sequentially. Add `--fail-stop` with sequential processing to stop creating outputs after a failure.

Omit `--output` for a quick input check. This checks file presence, image headers, detection boxes, and class assignments; it does not decode masks or create cutouts.

Exit codes are `0` for no failures, `1` for partial success, `2` for processing failure, and `3` for input/configuration errors. A run with no detections or only skipped detections can return zero, but has nothing to publish.

## Publishing on Ceres

The normal job workflow gathers the required inputs on Ceres, runs the stage, and publishes successful cutouts to:

```text
semifield-cutouts/<batch_id>/
```

Publication requires an exit-zero run with at least one successful cutout and no failed cutouts. Skipped detections are allowed. The publisher checks file sets, dimensions, class values, transparency, and checksums, then makes the completed batch directory visible in one rename. It refuses to replace an existing batch.

The Ceres job also refreshes the file inventory. Readiness checks exclude batches with an active job lease or a previous successful stage run. Running the stage CLI directly creates local outputs and reports; publication and database updates are handled separately by the job workflow.
