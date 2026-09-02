# Segmentation to Cutouts Design (`seg_to_cut`)

## Purpose

`seg_to_cut` turns full-sized images and their segmentation masks into small, self-contained records for individual plants. Each record is a set of four files: the original image crop, the masked cutout, the cutout mask, and JSON metadata.

The stage should preserve the cutout format already used by the SemiField dataset while using the new AgIR pipeline for species and cultivar identity, job tracking, storage discovery, and output publication.

## Inputs

The batch data lives below:

```text
semifield-developed-images/<batch_id>/
```

The stage needs:

| Input | Why it is needed |
|---|---|
| `images/<image_id>.jpg` | Supplies the original RGB pixels. |
| `segmentations/<image_id>.png` | Supplies the full-sized class mask. |
| `georeferenced/<batch_id>_georeferenced.csv` | Supplies detection boxes, detection IDs, and species/cultivar assignments. |
| Generated species catalog | Resolves IDs into names and other category metadata. |

The image and segmentation must have the same width and height. Files are matched by `image_id`, not by directory order.

The georeferenced CSV is included because images and masks alone do not retain reliable instance boundaries or stable detection IDs. It is already an input to `det_to_seg`, so `seg_to_cut` can reuse the same published batch artifact.

## Species and Cultivar Values

The new pipeline is the source of truth for class identity.

`det_to_seg` already applies this rule when writing the full-sized mask:

1. Use `cultivar_id` as the mask value when a cultivar is present.
2. Otherwise, resolve `species_id` through the species catalog and use its
   numeric `class_id`.
3. Reserve `0` for background.
4. Keep foreground values in the 8-bit range `1` through `255`.

`seg_to_cut` should preserve that nonzero value in the local cutout mask. It must not translate values through the legacy database.

Metadata should retain both kinds of identity where available:

- the human-facing `species_id`, such as a USDA symbol;
- the numeric species `class_id` used in masks;
- the `cultivar_id` / `cultivar_class_id` used in cultivar batches;
- catalog names and taxonomy useful to downstream tasks.

## Outputs

Published cutouts use this layout:

```text
semifield-cutouts/<batch_id>/
  <cutout_id>.jpg
  <cutout_id>.png
  <cutout_id>_mask.png
  <cutout_id>.json
```

For example:

```text
semifield-cutouts/MD_2024-05-06/
  MD_1715022013_0.jpg
  MD_1715022013_0.png
  MD_1715022013_0_mask.png
  MD_1715022013_0.json
```

All four files form one unit. The stage should publish either the complete set or none of it. A failed write must not leave an apparently valid partial cutout.

### Cropout JPG

`<cutout_id>.jpg` is the rectangular crop from the original image using the detection bounding box.

- RGB image
- same width and height as the bounding box
- JPEG quality 100
- no masking or background replacement

### Cutout PNG

`<cutout_id>.png` contains the same RGB crop with pixels outside the target mask set to black.

The cutout PNG should use:

- 8-bit RGBA PNG;
- black RGB values outside the target;
- alpha `255` for target-plant pixels;
- alpha `0` for non-plant pixels.

This intentionally departs from the historical format, which used alpha `255` for every pixel. Encoding the binary plant mask in the alpha channel makes the background transparent and allows future consumers to recover the plant silhouette directly from the cutout PNG. The separate mask PNG remains the source of the numeric species or cultivar class value.

### Mask PNG

`<cutout_id>_mask.png` is the segmentation mask cropped to the same bounding
box.

- 8-bit, single-channel grayscale PNG
- `0` for background
- the resolved species `class_id` or cultivar class ID for target pixels
- exactly the same width and height as the JPG and cutout PNG

For a normal single-class cutout, the only values should be `0` and the target class value.

### Intruder Removal

The bounding-box crop can contain leaves or stems from neighboring plants. These unwanted plant regions are intruders. The cropout JPG should preserve them because it is the untouched source crop, but the cutout mask and masked cutout PNG should remove them.

The stage should use a border sweep that removes free-floating components confined to a configurable band along the crop border while keeping components that extend into the crop interior. It should preserve the expected class value for retained target pixels, set removed pixels to `0`, record the numbers of removed and remaining components, and run before edge-cut analysis so border intruders do not create false edge flags.

### Edge Truncation Flag

A detection box can cut off part of the target plant. These cutouts cannot be placed freely in a synthetic recreation because the missing part of the plant would be visible. They may still be useful when placed against the matching edge or corner of the synthetic image.

The stage should measure how much of each cutout edge is occupied by the target plant. This measurement must use the cleaned target mask after intruder removal so that neighboring plants do not create false edge flags.

For each side, calculate the fraction of edge pixels occupied by the target:

```text
top_fraction    = target pixels on top edge / cutout width
bottom_fraction = target pixels on bottom edge / cutout width
left_fraction   = target pixels on left edge / cutout height
right_fraction  = target pixels on right edge / cutout height
```

The implementation may inspect a small band of pixels along each edge instead of a single pixel-wide line to reduce sensitivity to minor mask noise. The band width and edge threshold should be configurable and recorded in the metadata.

A side is flagged when its plant fraction is greater than the configured threshold. The resulting placement guidance is:

- no flagged sides: the plant can be placed anywhere;
- one flagged side: the plant should be placed against that image edge;
- two adjacent flagged sides: the plant should be placed in the matching corner;
- opposing sides or three or more flagged sides: the cutout is likely unsuitable for synthetic recreation.

The stage should also compare the detection box with the full-sized image boundary. A flagged side that also touches the original image boundary is a source-image edge. A flagged side inside the full image suggests that the detection box was too small and truncated an otherwise visible plant. The latter should be clearly labeled so a downstream pipeline can reject it.

### JSON Metadata

`<cutout_id>.json` describes the output and records enough identity and provenance to understand it without opening the database.

A practical first schema is:

```json
{
  "season": "cool_season_covers_2023_2024_MD_pos_2",
  "datetime": "2024:05:06 15:00:13",
  "bbot_version": "v2.0",
  "batch_id": "MD_2024-05-06",
  "image_id": "MD_1715022013",
  "cutout_id": "MD_1715022013_0",
  "cutout_num": 0,
  "cutout_height": 4059,
  "cutout_width": 6740,
  "lens_model": "FE 55mm F1.8 ZA",
  "validated": false,
  "cutout_version": "<configured schema version>",
  "cutout_props": {
    "is_primary": true,
    "extends_border": false,
    "edge_cut": {
      "flagged": true,
      "threshold": 0.1,
      "band_width_px": 3,
      "flagged_sides": ["right"],
      "source_image_sides": ["right"],
      "detection_box_truncation_sides": [],
      "synthetic_placement": "right_edge_only",
      "plant_fraction": {
        "top": 0.0,
        "bottom": 0.02,
        "left": 0.0,
        "right": 0.34
      }
    },
    "bbox_area_cm2": 2750.47,
    "estimated_bbox_area_cm2": 2701.18,
    "species_mean_bbox_area_cm2": 2401.05,
    "species_bbox_sample_size": 128,
    "species_bbox_area_ratio": 1.125,
    "abnormal_bbox_size": false,
    "solidity": 0.73,
    "blur_effect": 0.404,
    "num_components": 46,
    "cropout_rgb_mean": [0.366, 0.339, 0.283],
    "cropout_rgb_std": [0.148, 0.130, 0.095],
    "non_target_weed": null,
    "non_target_weed_pred_conf": null
  },
  "category": {
    "species_id": "TRIN3",
    "class_id": 31,
    "common_name": "Crimson clover",
    "cultivar_id": null,
    "cultivar_name": null
  }
}
```

The catalog may add taxonomy, display color, aliases, and links below `category`. Those values should be copied from the current catalog rather than reconstructed from the legacy database.

New outputs start with `validated: false`. Validation is a separate action and must not be implied simply because the stage completed successfully.

Values that cannot be supported by a current input should be written as `null`, not guessed. The field names should remain stable so readers can handle old and new cutouts consistently.

## Properties Calculated by the Stage

The stage can calculate the following directly:

| Property | Practical calculation |
|---|---|
| `cutout_height`, `cutout_width` | Dimensions of the bounding-box crop. |
| `extends_border` | True when target pixels touch an edge of the local mask. |
| `edge_cut` | Per-side plant fractions, flagged sides, source-image edge information, and synthetic-placement guidance. |
| `blur_effect` | The same normalized blur measure selected for the historical cutouts. |
| `num_components` | Number of connected foreground regions in the local target mask. |
| `cropout_rgb_mean` | Mean red, green, and blue values from the unmasked crop, scaled consistently with historical data. |
| `cropout_rgb_std` | Red, green, and blue standard deviations from the unmasked crop. |
| `bbox_area_cm2` | Physical area derived from world coordinates when the batch has usable georeferencing. Otherwise `null`. |
| `estimated_bbox_area_cm2` | Estimated physical bounding-box area from the camera intrinsics, camera height, and pixel dimensions. The camera model and ground-plane assumptions must be defined; otherwise this is `null`. |
| `species_mean_bbox_area_cm2` | Mean `estimated_bbox_area_cm2` for valid detections in the same batch and species or configured category group. |
| `species_bbox_sample_size` | Number of valid detections used to calculate `species_mean_bbox_area_cm2`. |
| `species_bbox_area_ratio` | Current `estimated_bbox_area_cm2` divided by `species_mean_bbox_area_cm2`. Otherwise `null` when the mean is unavailable or zero. |
| `abnormal_bbox_size` | True when `species_bbox_area_ratio` differs from `1.0` by more than the configured percentage threshold. |
| `solidity` | Area of the cleaned target mask divided by the area of its convex hull. |

Identity, season, camera, and classifier fields come from upstream data or the current catalog. They should not be inferred from image appearance.

The species-level area metrics require a batch aggregation step. The stage should first calculate valid per-cutout area estimates, group them by species or the configured category group, and then finalize each cutout's mean, sample size, ratio, and abnormal-size flag. The abnormal-size percentage threshold and grouping rule must be configurable and recorded in the metadata or run report.

## Processing Flow

For each image, the stage should:

1. Load the JPG and matching grayscale segmentation mask.
2. Validate dimensions, mask type, and mask values.
3. Read that image's detections in stable `bounding_box_id` order.
4. Resolve the expected species or cultivar class for each detection.
5. Clip its bounding box to the image boundary.
6. Crop the image and select pixels matching the expected class value.
7. Remove intruders from the local mask using the connected-component cleanup.
8. Skip the detection with a recorded reason if no target pixels remain.
9. Measure plant coverage on each edge and assign edge-placement flags.
10. Calculate the remaining cutout properties.
11. Write the four outputs to temporary names and move them into place only
   after all four writes succeed.
12. Record success or failure in the manifest and run report.

The stage should process one image at a time so memory use depends on image size rather than batch size. This is expected to be a CPU stage; it does not run a segmentation model.

## Execution Site

`seg_to_cut` should run on Ceres by default.

The work is mostly reading images and masks, cropping arrays, calculating simple image properties, and writing many files. It does not need a GPU, so it is a better fit for the CPU-oriented Ceres workflow than the GPU-oriented Atlas workflow.

The expected sequence is:

1. `det_to_seg` runs on Atlas because it needs a GPU.
2. Its run bundle and segmentation masks are synchronized and promoted on
   Ceres through the existing result-sync process.
3. The Ceres inventory sees the promoted masks and marks the batch ready for
   `seg_to_cut`.
4. `seg_to_cut` reads the Ceres copies of the images, masks, and
   georeferenced CSV, then publishes `semifield-cutouts` on Ceres.

This avoids using an Atlas GPU allocation for CPU work and avoids sending a large directory of four-file cutout sets from Atlas back to Ceres. If an input is not already present on Ceres, the normal staging planner can fetch that input before submission.

## Stage and Run Structure

The code should follow the existing stage pattern:

```text
stages/seg_to_cut/
  __init__.py
  cli.py
  processor.py
  metadata.py
  writers.py
  configs/default.yaml
  tests/
```

Responsibilities should stay simple:

- `cli.py` validates arguments, builds the run directory, and writes the run
  report and manifest.
- `processor.py` matches inputs and creates cutouts image by image.
- `metadata.py` resolves catalog records and calculates properties.
- `writers.py` owns the exact JPG, PNG, mask, and JSON formats.

A run should use the standard pipeline bundle:

```text
seg_to_cut/<run_id>/
  artifacts/
    cutouts/
      <cutout files>
  logs/
  manifest.json
  run_report.json
```

The manifest should record all four artifact paths, sizes, and checksums for each successful cutout.

## Operational Database and Orchestration

`globus_file_index.sqlite3` remains the operational database. No row needs to be inserted to “register” a stage because its stage columns accept names as text.

Implementation work should add:

- `seg_to_cut` to `STAGE_INPUT_SPECS`;
- `seg_to_cut` to the supported submission stages;
- a `v_batches_needing_seg_to_cut` readiness view in the maintained SQLite
  schema and migration path;
- a stage configuration with its input routes and CPU job resources;
- result-sync and promotion support for the `semifield-cutouts` data state;
- tests for staging, readiness, leases, run ingestion, and publication.

The readiness view should select a batch only when its required images, segmentations, and georeferenced CSV are available, no successful `seg_to_cut` run has already completed, and no active lease exists.

Normal operation then creates `seg_to_cut` records in:

- `staged_inputs` while required batch inputs are moved;
- `stage_leases` while a job owns the batch;
- `stage_runs` after the run report is ingested.

Published files are indexed in `globus_file_index` under the `semifield-cutouts` data state during the normal inventory refresh.

## Validation and Acceptance Checks

The first implementation is ready for a batch when it can demonstrate:

- every selected image has a matching mask;
- image and mask dimensions match;
- masks are 8-bit grayscale and all foreground values are known to the current
  species/cultivar catalog;
- every successful cutout has exactly four files;
- the JPG, cutout PNG, and mask have identical dimensions;
- every local mask contains only background and its expected target value;
- cutout PNG background pixels are black, target alpha values are `255`, and non-target alpha values are `0`;
- JSON identifiers, dimensions, and class values agree with the files;
- output names are unique and deterministic across reruns;
- a failed or empty detection has a clear recorded reason and no partial file
  set;
- run counts agree with manifest successes, failures, and skips.

Tests should include species and cultivar examples, bounding boxes touching image edges, cutouts with each single edge and each corner flagged, detection-box truncation away from the source image edge, empty masks, separate same-class and different-class intruders, overlapping detections, unknown mask values, malformed input files, interrupted writes, and repeat runs.

## Implementation Plan

1. **Freeze examples and contracts.** Add a few small legacy-style four-file
   fixtures and define the expected JSON and image properties in tests.
2. **Build the core processor.** Match images, masks, and detection rows;
   create local target masks; calculate properties; and write complete output
   sets.
3. **Add the stage command.** Produce the standard run directory, manifest,
   logs, stable exit codes, and run report.
4. **Wire orchestration.** Add readiness, input staging, submission config,
   leases, promotion, and result sync.
5. **Validate on representative batches.** Compare file formats and selected
   properties with historical cutouts, including at least one species batch
   and one cultivar batch.
6. **Run a controlled publication test.** Publish one batch to
   `semifield-cutouts`, refresh the inventory, and verify database visibility
   before enabling normal batch discovery.
