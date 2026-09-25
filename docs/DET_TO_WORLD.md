# Detection Stage to World Coordinates
Turns local image bounding box coordinates into real world geographic coordinates using precalculated ASFM pixel-to-world grids, marks the primary detection among overlapping views, then assigns a species to each detection — either via a spatial join against a species-zone shapefile, or a single configured species code for monoculture batches.

Species assignment was originally a separate `assign_species` stage; it was folded into `det_to_world` so the pipeline produces one final CSV, one run_report/manifest, and one exit code per batch instead of two stages handing a file off between them. See `stages/det_to_world/species.py` for `assign_spatial()`/`assign_monoculture()`.


## Output Structure

```
<output_dir>/
  det_to_world/
    <run_id>/
      run.log
      run_report.json
      manifest.json
      artifacts/
        <batch_id>_georeferenced.csv
        <batch_id>_camera_reference.csv    (georeferenced batches only)
        <batch_id>_fov.csv                 (georeferenced batches only)
```

The georeferenced CSV contains all input detection columns plus the following appended columns, in this order:

| Column | Description |
|---|---|
| `world_tl_x` / `world_tl_y` | Top-left corner in world coordinates |
| `world_tr_x` / `world_tr_y` | Top-right corner in world coordinates |
| `world_bl_x` / `world_bl_y` | Bottom-left corner in world coordinates |
| `world_br_x` / `world_br_y` | Bottom-right corner in world coordinates |
| `world_centroid_x` / `world_centroid_y` | Midpoint of TL and BR corners in world coordinates |
| `crs` | Coordinate reference system of the world coordinates (e.g. `EPSG:32617`) |
| `is_primary` | Whether this is the selected detection among overlapping views; always False for detections that weren't georeferenced |
| `species_id` | Species code assigned to the detection; `PLANT` for an unknown plant, `COLORCHECKER` for a color checker |
| `assignment_method` | How the species was assigned: `spatial_join`, `nearest_polygon`, or `monoculture_config`; `color_checker_class` for color-checker detections (`COLORCHECKER`, regardless of zone); `unknown_too_far` for detections more than `--max-nearest-distance-m` from every zone and `unknown_not_georeferenced` for detections with no world coordinates in a spatial batch (both `PLANT`); `no_detections` identifies an image-only placeholder row |

The other two artifacts combine every paired `camera_reference.csv` and
`fov.csv` found in the batch's ASFM tree. They use a flat batch-level schema
and retain the source row order. When an image label occurs in more than one
reconstruction, the first complete camera/FOV occurrence in sorted source-path
order is retained. Camera and FOV rows are selected together from the same
source directory.

### Zone attribute columns

Zone shapefiles carry at most one of two optional human-readable attributes (never both, in practice), plus an optional `class_id`:

| Shapefile has... | Appended column(s) | Example |
|---|---|---|
| `comm_name` (ordinary species zones, e.g. `cover_crops_2025_2026`) | `species_name` | `"Velvetleaf"` |
| `cultc_id` + `disp_name` (cultivar seasons, e.g. `peanuts_2026` — every zone is the same species, a different cultivar) | `cultivar_id`, `cultivar_name` | `107`, `"Peanut - EXP-OLEIC-001"` |
| `class_id` (present in every zone shapefile checked so far, but not assumed) | `class_id` | `29` |

Whether these columns appear at all is decided per batch by what's actually in the resolved `--shp` shapefile's columns (see `species.assign_spatial()`), not by any config flag — a shapefile with none of these attributes produces none of these columns, unlike `world_*`/`crs`, which are always present (blank for detections that weren't georeferenced) since georeferencing conceptually applies to every batch.

`PLANT` and `COLORCHECKER` rows don't take these values from the zone: their `species_name` and `class_id` (when those columns exist) come from the species catalog's `PLANT`/`COLORCHECKER` entries, and their cultivar columns are cleared.

### Catalog columns

When `--species-catalog` loads, every detection row also gets reference columns from the catalog entry for its `species_id`: `species_common_name`, `species_family`, `species_genus`, `species_growth_habit`, `species_category`, `species_hex`, `species_r`, `species_g`, `species_b`. If the CSV has a `cultivar_id` column, rows also get `cultivar_display_name`, `cultivar_line_name`, `cultivar_registered`, `cultivar_hex`, `cultivar_r`, `cultivar_g`, `cultivar_b` (blank on rows without a cultivar). These sit after the zone attribute columns.

A missing or unreadable catalog skips these columns with a logged warning and leaves `PLANT`/`COLORCHECKER` rows' `species_name`/`class_id` blank. A `species_id` or `cultivar_id` with no catalog entry fails the run (`E_UNKNOWN_SPECIES_CODE`).

### Rows that aren't georeferenced

Every image and every detection is written to the CSV. Detections that can't be georeferenced — the image has no ASFM grid (`W_GRID_NOT_FOUND`), its grid has no ASFM camera/FOV reference row (`W_NO_CAMERA_REFERENCE`), a corner can't be mapped after inward nudging (`W_SURFACE_MISS`), or the image's remap failed (`E_REMAP_FAILED`) — keep their pixel bbox with blank `world_*`/`crs` and `is_primary=False`. Images in the batch's `raw_to_jpg`/`jpg_to_det` manifests with no detection rows or file at all get a `no_detections` placeholder row and a `W_NO_DETECTION_FILE` warning. For monoculture batches that aren't georeferenced (`--skip-remap`, or `--species` with no ASFM camera/FOV references under `--g` or no `--g`), every detection's `world_*`/`crs` columns are blank, `is_primary` is False, and no combined camera/FOV CSVs are written — the schema stays the same across every batch regardless of mode. A `--species` batch that does have ASFM references is georeferenced and primary-selected like a spatial batch; only the species assignment differs.

The manifests are read from `raw_to_jpg/manifest.json` and `jpg_to_det/manifest.json` in the directory above the detections directory (failed `raw_to_jpg` items are ignored), and any `images/*.jpg` there is included too. If neither manifest exists, a warning is logged and images with no detection file can't be identified.


## Exit Codes

| Code | Value | Meaning |
|---|---|---|
| `EXIT_SUCCESS` | `0` | All images processed successfully |
| `EXIT_PARTIAL` | `1` | Some images had remap errors (`E_REMAP_FAILED`), some succeeded (species assignment still ran on every row) |
| `EXIT_FAILURE` | `2` | No image succeeded, or a stage-level step failed: combining references/remapping, primary selection, species assignment, or catalog enrichment |
| `EXIT_CONFIG_ERROR` | `3` | Invalid inputs or configuration (see below) |

`EXIT_CONFIG_ERROR` is returned before a run directory exists — so no run report or manifest — when:
- `--skip-remap` is passed without `--species`
- the batch ID can't be determined
- `--i` doesn't exist
- there are no ASFM camera/FOV references under `--g` (or no `--g`) and no `--species`
- neither `--species` nor an existing `--shp` is given
- `--bbot-version` is invalid or newer than `3.1`

An unusable detection input (`E_CSV_INVALID`) also returns `EXIT_CONFIG_ERROR`, but after the run directory is created, so `run_report.json` and `manifest.json` are written.

A stage-level failure after loading (references/remapping, primary selection, species assignment, catalog enrichment) always forces `EXIT_FAILURE` and writes no CSV, even if remapping itself succeeded for every image — the single shared CSV isn't usable without species columns, so partial credit doesn't apply the way it does for remap-only failures. Images with no grid or no camera/FOV reference are OK items with a warning, not failures, so they don't affect the exit code.


## Error Codes

| Code | Scope | Meaning |
|---|---|---|
| `E_CSV_INVALID` | Stage | Detection input is unusable: CSV missing required columns or unreadable, a malformed line in a `.txt` file, or a directory with neither a batch CSV nor `.txt` files |
| `E_REMAP_FAILED` | Per-image | Unexpected failure while remapping an image (e.g. unreadable NPZ); its detections are kept un-georeferenced |
| `E_REMAP_FAILED` | Stage | Combining camera/FOV references or remapping the batch raised (e.g. no paired `camera_reference.csv`/`fov.csv`) |
| `E_PRIMARY_SELECTION_FAILED` | Stage | Primary selection raised: mapped rows don't share one populated CRS, or camera/FOV/bbox coordinates are missing or invalid |
| `E_SHAPEFILE_UNREADABLE` | Stage | Species assignment failed with an error message that mentions "shapefile" |
| `E_SPATIAL_JOIN_FAILED` | Stage | Any other species-assignment failure |
| `E_UNKNOWN_SPECIES_CODE` | Stage | An assigned `species_id` or `cultivar_id` has no entry in the species catalog |

| Warning | Scope | Meaning |
|---|---|---|
| `W_GRID_NOT_FOUND` | Per-image | No NPZ grid file for the image; its detections are kept un-georeferenced (the image is still an OK item) |
| `W_NO_CAMERA_REFERENCE` | Per-image | The image has an NPZ grid but no row in any ASFM `camera_reference.csv`/`fov.csv` (ASFM has written grids for cameras it didn't align); its detections are kept un-georeferenced (the image is still an OK item) |
| `W_SURFACE_MISS` | Per-detection | A bbox corner couldn't be mapped after inward nudges; the detection is kept un-georeferenced |
| `W_ZONE_TOO_FAR` | Per-detection | Detection is more than `--max-nearest-distance-m` from every zone polygon; assigned `PLANT` |
| `W_NO_DETECTION_FILE` | Per-image | Image is in the batch manifests but has no detections input; added as a `no_detections` row |


## Run Report

Besides the standard fields, `run_report.json` has these top-level keys:

| Key | Description |
|---|---|
| `assignment_mode` | `monoculture_config` when `--species` is set, else `spatial_join` |
| `detection_source` | `csv` or `txt_dir` |
| `georeferenced` | Whether the batch was georeferenced |
| `n_not_georeferenced_detections` | Detections written without world coordinates |
| `n_missing_detection_files` | Images added as `no_detections` rows because they had no detections input |
| `bbot_version` | Value of `--bbot-version` |
| `primary_reference_paths` | Source `camera_reference.csv`/`fov.csv` paths combined (georeferenced batches only) |
| `combined_camera_reference_rows` / `combined_fov_rows` | Rows in the combined reference CSVs (georeferenced batches only) |
| `n_primary_detections` / `n_non_primary_detections` | Primary-selection counts over georeferenced detections (georeferenced batches only) |

Artifact types are `georeferenced_csv`, plus `camera_reference_csv` and `fov_csv` for georeferenced batches. Because the outputs are batch-level, every OK manifest item lists the same artifact paths, checksums, and sizes.


## Sample Run Command

Spatial join (multiple species via shapefile zones):

```bash
python3 -m stages.det_to_world.cli \
  --i /path/to/combined_detections.csv \
  --g /path/to/asfm_batch_dir \
  --shp /path/to/species_zones.shp \
  --bbot-version 3.1 \
  --o /path/to/output \
  --batch-id NC_2026-01-07
```

Monoculture, georeferenced (single species; primary selection still runs):

```bash
python3 -m stages.det_to_world.cli \
  --i /path/to/combined_detections.csv \
  --g /path/to/asfm_batch_dir \
  --species ABUTH \
  --bbot-version 3.1 \
  --o /path/to/output \
  --batch-id NC_2026-01-07
```

Monoculture (single species, no remap):

```bash
python3 -m stages.det_to_world.cli \
  --i /path/to/combined_detections.csv \
  --skip-remap \
  --species ABUTH \
  --bbot-version 3.1 \
  --o /path/to/output \
  --batch-id NC_2026-01-07
```

| Flag | Description |
|---|---|
| `--i` | Detection CSV with `image_id` and normalized bounding box columns, **or** a detections directory. For a directory, `<batch_id>.csv` inside it is used if present; otherwise the per-image YOLO `.txt` files are compiled into the same row shape (older batches that predate the batch CSV). A CSV path that doesn't exist is an error — it doesn't fall back to its parent directory |
| `--g` | ASFM batch directory: per-image NPZ pixel-to-world grids plus `camera_reference.csv`/`fov.csv`. Required unless `--species`; a `--species` batch without ASFM references is written un-georeferenced |
| `--shp` | Species zone shapefile with polygon geometries and species codes. Required unless `--species`; ignored when `--species` is set |
| `--species` | Species code to assign to all detections, georeferenced or not. Required when `--skip-remap` or when there are no ASFM references |
| `--max-nearest-distance-m` | How far (in the zone shapefile's CRS units, meters for UTM) a detection outside every zone may be and still fall back to its nearest zone; farther ones get `PLANT`. Default `5` |
| `--species-catalog` | Generated species catalog JSON used for fallback labels and catalog columns. Default `/project/dash_agir/semifield-utils/species_information/species_catalog.generated.json` |
| `--bbot-version` | BBot version string (required, must be <= 3.1) |
| `--o` | Output directory where run artifacts are written |
| `--batch-id` | Batch identifier; auto-inferred from the input path if omitted |
| `--skip-remap` | Don't georeference even if ASFM output exists (requires `--species`) — assigns `--species` directly to the original detection rows, all with `is_primary=False` |


## Call Flow

```
cli.main()
  |
  |-- validate flags               --skip-remap/--species/--g/--shp/--bbot-version (exit 3, no run dir)
  |-- load_catalog()               --species-catalog; missing/unreadable -> warning, no catalog columns
  |-- resolve_detection_source()   directory -> its <batch_id>.csv if present, else the directory
  |-- load_detection_rows()        read + validate the detection CSV, or compile per-image .txt files into CSV-shaped rows
  |-- _expected_image_ids()        manifest images with no detection row -> placeholder rows (W_NO_DETECTION_FILE)
  |
  |-- [ASFM references under --g, no --skip-remap]
  |     |
  |     |-- combine_reference_pairs()   merge paired camera/FOV tables from the ASFM batch tree
  |     |-- remap_rows()                loop over images; images with no reference row stay un-georeferenced
  |     |     |
  |     |     |-- GridCache.get()        load the per-image NPZ grid (cached; NPZ holds a sparse grid of
  |     |     |     |                   pixel (u,v) -> world (x,y) sample points produced by ASFM)
  |     |     |     `-- _load_grid()     build RegularGridInterpolators from NPZ arrays
  |     |     |
  |     |     `-- map_bbox()             map one detection row to world coords
  |     |           |
  |     |           |-- _map_point()     resolve one bbox corner to pixel coords + nudge if out of bounds
  |     |           |     |
  |     |           |     `-- _interpolate_point()   query X/Y interpolators at (u, v)
  |     |           |
  |     |           `-- _construct_global_coords()   assemble output row with all corners + centroid
  |     |
  |     `-- select_with_references()    mark the preferred detection among overlapping views
  |
  |-- [else]  _unmapped_image_results()   keep every detection un-georeferenced
  |
  |-- species assignment
  |     |-- georeferenced rows:     assign_monoculture() with --species, else assign_spatial() against --shp
  |     |-- un-georeferenced rows:  assign_monoculture() with --species, else PLANT / unknown_not_georeferenced
  |     `-- assign_fallback_labels()    COLORCHECKER for color checkers, catalog labels for PLANT rows
  |-- enrich_with_catalog()        add species_*/cultivar_* catalog columns (when the catalog loaded)
  |
  |-- _append_no_detection_rows()  one no_detections placeholder row per image with no detections
  |-- write_georeferenced_csv()    write the species-assigned rows
  `-- write_combined_references()  write the two flat batch reference CSVs (georeferenced batches only)
```


## Method Descriptions

### `cli.main()`
Entry point. Parses and validates arguments, loads the species catalog and detection input, adds placeholder rows for images with no detections, georeferences and primary-selects the batch when ASFM references are available, assigns species, writes the CSV and reference CSVs, and writes the run report and manifest.

### Remapper
### `resolve_detection_source(path, batch_id)`
Picks what to read from `--i`. A file is returned as given. A directory resolves to its `<batch_id>.csv` when that exists (CSV preferred), otherwise to the directory itself.

### `load_detection_rows(source)`
Given a CSV, validates that all required columns are present (`image_id`, `bounding_box_id`, `xmin`, `ymin`, `xmax`, `ymax`) and returns the fieldnames and rows. Bbox values within `1e-6` outside `[0, 1]` (float32 rounding at the image edge) are clamped. Empty sibling `.txt` files missing from the batch CSV are added as image-only placeholders.

Given a directory, compiles every `*.txt` file into rows with the same columns as jpg_to_det's batch CSV (`image_id`, `bounding_box_id`, `xmin`, `ymin`, `xmax`, `ymax`, `conf`, `class`, `classname`), so nothing downstream depends on which input was used. Each line is YOLO `cls xc yc w h [conf]` (normalized): `image_id` is the file stem, `bounding_box_id` the line index within the file, corners are `xc ± w/2`, `yc ± h/2` clamped to [0, 1] (the files store center/size at 6 decimals, so edge-touching boxes can round ~5e-7 outside the image), and `conf` is blank for the older 5-column form. `classname` is always blank because the files don't record class names, and the `class` values of older batches are that model's own ids, not jpg_to_det's `0 = plant`, `1 = color_checker`. Each empty file contributes one output row containing its `image_id` and `assignment_method=no_detections`; every detection and georeferencing field is blank. The run report records which was used as `detection_source` (`csv` or `txt_dir`).

### `remap_rows(rows, grid_dir, *, cache=None, referenced_image_ids=None)`
Groups rows by `image_id` and processes each image, returning the mapped rows and one `ImageRemapResult` per image. Each result carries the image's warnings and its `unmapped_rows` (detections that weren't georeferenced, which the CLI still writes to the CSV).
- An image that is only a `no_detections` placeholder is OK with nothing to map.
- No grid file: OK with `W_GRID_NOT_FOUND`; all its detections are unmapped.
- The grid fails to load: failed with `E_REMAP_FAILED`; all its detections are unmapped.
- `referenced_image_ids` is given and the image isn't in it: OK with `W_NO_CAMERA_REFERENCE`; all its detections are unmapped.
- Otherwise `map_bbox` runs for each detection. A surface miss adds a `W_SURFACE_MISS` warning and leaves that detection unmapped. An unexpected exception fails the image with `E_REMAP_FAILED`, and its remaining detections are unmapped.

### `GridCache.get(image_id)`
Maintains a dict of `image_id → GridData` populated lazily as each image is encountered. On first access it finds the image's NPZ by stem in a recursive index of `*.npz` under the grid directory, so both a flat directory and ASFM's nested per-time-range layout (`<start>_<end>/pixel_world_grids/`) work. If two NPZ files share a stem, the first in sorted path order is kept and a warning is logged. Subsequent accesses return the cached result. `GridCache.path_for(image_id)` returns the same indexed path.

`GridData` is a dataclass holding:
- **Two scipy interpolators** (one for world X / easting (west–east), one for world Y / northing (south–north)) — given a pixel coordinate `(u, v)`, each returns the corresponding real-world coordinate
- **Sensor dimensions** — the pixel width and height of the image, used to convert normalized bbox coordinates (0–1) back to absolute pixel coordinates before querying the interpolators
- **CRS string** — the coordinate reference system of the world coordinates (e.g. `EPSG:32617` = WGS 84 / UTM zone 17N), passed through to the output CSV

### `GridCache._load_grid(image_id, data)`
Reads the NPZ arrays (`u_pixels`, `v_pixels`, `world_x`, `world_y`, `sensor_width`, `sensor_height`, `crs`), validates their shape, and constructs two `RegularGridInterpolator` instances (one for world X, one for world Y) that map pixel `(u, v)` coordinates to real-world coordinates. ASFM writes `crs` as the string form of its own CRS object (e.g. `<CoordinateSystem 'WGS 84 / UTM zone 18N (EPSG::32618)'>`), so `_normalize_crs()` pulls out a plain `EPSG:<code>`.

### `map_bbox(row, grid)`
Maps all four corners of a bounding box to world coordinates. Returns `(mapped_row, None)`, or `(None, RemapWarning)` with `W_SURFACE_MISS` if any corner cannot be resolved.

### `_map_point(x, y, grid)`
Converts a single normalized bbox corner to pixel coordinates, then attempts to interpolate its world position. If the point is outside the grid bounds or interpolates to NaN, it nudges the coordinate toward the image center by each of `NUDGE_PX` (`0, 1, 2, 3, 5, 10` px) in turn, returning the first valid result, or `None` if none work.

### `_interpolate_point(grid, u, v)`
Queries the X and Y interpolators at pixel position `(u, v)` and returns the corresponding world coordinates.

### `_construct_global_coords(row, coords, crs)`
Assembles the output row dict from the four mapped corner coordinates, computing the centroid as the midpoint of the top-left and bottom-right corners.

### `write_georeferenced_csv(rows, fieldnames, csv_path)`
Writes the mapped rows to a CSV, preserving original input columns and appending the geo columns. Supports writing a header-only file when there are no rows.

### Primary selection
### `combine_reference_pairs(root)`
Finds every `camera_reference.csv` under the ASFM batch directory that has a sibling `fov.csv`, visiting pairs in sorted path order. Unpaired files, and labels present in only one file of a pair, are ignored with a warning. For each image `label`, the first complete camera + FOV pair is kept. Raises `ValueError` if there are no pairs or no complete rows. Returns a `CombinedReferences` with both tables and the source paths.

### `select_with_references(rows, cache, references)`
Requires every georeferenced row to share one populated CRS, looks up each image's sensor dimensions from its grid, and calls `select_primary_rows()` with the combined camera and FOV rows.

### `select_primary_rows(rows, camera_rows, fov_rows, image_dimensions)`
Legacy `BBoxFilter` selection adapted from SemiF-AnnotationPipeline, with its thresholds and traversal kept as-is for old batches:
- Images are compared when their FOV rectangles overlap with IoU > `0.1`.
- Boxes in compared images whose world polygons overlap with IoU > `0.3` are grouped. In each group, the box whose centroid is closest to its camera's `(Estimated_X, Estimated_Y)` is marked primary.
- Cleanup unmarks primaries whose pixel centroid is outside the middle half of the image. When two remaining primaries overlap with IoU > `0.3`, only the one closer to its camera stays primary.

A group can end up with no primary, and comparisons aren't split by species. Returns every input row, in order, with a fresh boolean `is_primary`. Raises `ValueError` on missing references, duplicate detection IDs, or missing/invalid coordinates or normalized bboxes.

### `write_combined_references(references, artifacts_dir, batch_id)`
Writes `<batch_id>_camera_reference.csv` and `<batch_id>_fov.csv` into the artifacts directory.

### Species
### `assign_spatial(dets, shapefile, max_nearest_distance_m=5.0)`
Takes a DataFrame of remapped rows (must have `world_centroid_x`/`world_centroid_y`/`crs`) and a species-zone shapefile path. Builds a GeoDataFrame from the centroids, reprojects to the shapefile's CRS, and spatial-joins (`predicate="within"`) to assign each point's `species_id` from whichever zone polygon contains it. Any unmatched points (outside every zone) fall back to `sjoin_nearest()` if the nearest zone is within `max_nearest_distance_m` (default 5), otherwise they get `PLANT` / `unknown_too_far`, blank zone attributes, and a logged warning; ties (a point exactly equidistant from two zones) are resolved by keeping the first match per point. If the shapefile has `comm_name`, it's joined in the same pass and renamed to `species_name`; if it has `cultc_id`/`disp_name` instead, those are renamed to `cultivar_id`/`cultivar_name` — a shapefile with neither produces neither column. A `class_id` attribute is carried through the same way when present.

### `assign_monoculture(dets, species_code)`
Takes a DataFrame of detection rows (georeferenced or not) and assigns `species_code` to every row with `assignment_method="monoculture_config"` — no spatial computation.

### `assign_fallback_labels(dets, catalog)`
Applies the labels that don't come from the zone or monoculture assignment. Color-checker detections (`classname == "color_checker"`, or `class == "1"` since TXT-sourced rows have a blank `classname`) get `COLORCHECKER` / `color_checker_class`, whatever zone they fall in and whether or not they were georeferenced. Rows with `species_id == "PLANT"` get the catalog's `PLANT` labels. Both take `species_name`/`class_id` from their catalog entry (blank without a catalog) and have cultivar columns cleared.

### `load_catalog(path)`
Reads `species_catalog.generated.json` as plain JSON. Stages never open a DB connection; this generated file is the only reference data they consume.

### `enrich_with_catalog(dets, catalog)`
Adds the [catalog columns](#catalog-columns) by looking up `species_id` in the catalog's `species` and, when the column exists, `cultivar_id` in its `cultivars`. Raises `UnknownSpeciesCodeError` for any code with no entry, since that means the zone data and the catalog have drifted apart and need a person to reconcile them.


## Tests

Tests live in `stages/det_to_world/tests/`. There are no unit tests yet for primary selection, reference combining, or the CLI.

| Test | Description |
|---|---|
| `test_normalize_crs_extracts_epsg_from_asfm_repr` | Confirms ASFM's CRS string (`<CoordinateSystem '... (EPSG::32618)'>`) normalizes to `EPSG:32618` |
| `test_normalize_crs_passes_through_clean_epsg_strings` | Confirms plain `EPSG:` and `EPSG::` strings normalize to `EPSG:<code>` |
| `test_grid_cache_normalizes_asfm_style_crs` | Confirms `GridCache` returns the normalized CRS for an NPZ with an ASFM-style `crs` |
| `test_load_detection_rows_validates_required_columns` | Confirms that a CSV missing required columns raises a `ValueError` with a descriptive message |
| `test_load_detection_rows_compiles_txt_dir_like_batch_csv` | Checks a directory of YOLO `.txt` files compiles to CSV-shaped rows (corners, per-image box ids, 5- vs 6-column `conf`, empty-file placeholders) |
| `test_load_detection_rows_txt_dir_clamps_rounding_at_image_edges` | Confirms corners that round just outside [0, 1] from 6-decimal center/size are clamped |
| `test_load_detection_rows_txt_dir_reports_bad_lines` | Confirms a malformed `.txt` line raises a `ValueError` naming the file and line |
| `test_load_detection_rows_dir_without_csv_or_txt_raises` | Confirms a directory with neither a batch CSV nor `.txt` files is an error |
| `test_resolve_detection_source_prefers_batch_csv` | Confirms a directory containing `<batch_id>.csv` resolves to it, and to the directory itself when it doesn't |
| `test_remap_rows_from_txt_dir_matches_csv_input` | Runs txt-compiled rows through `remap_rows` and checks the world coordinates |
| `test_map_bbox_maps_all_corners` | Verifies that all four corners and the centroid are correctly interpolated for a simple linear grid |
| `test_remap_rows_handles_warnings_and_missing_grids` | Checks that out-of-bounds corners produce a `W_SURFACE_MISS` warning and that images with no grid file are OK with a `W_GRID_NOT_FOUND` warning, with the un-georeferenced detections kept in `unmapped_rows` |
| `test_remap_rows_leaves_unreferenced_grid_images_unmapped` | Checks that an image with a grid but no camera/FOV reference row is OK with `W_NO_CAMERA_REFERENCE` and all its detections unmapped |
| `test_map_bbox_applies_inward_nudges` | Confirms that a bbox corner falling outside the grid boundary is nudged inward and snapped to the nearest valid grid point |
| `test_map_bbox_uses_tl_br_midpoint_for_centroid` | Confirms the centroid is the TL/BR midpoint, not the average of all four corners |
| `test_write_georeferenced_csv_supports_header_only` | Confirms that writing an empty row list still produces a valid CSV with the correct header |
| `test_assign_spatial_within` | Centroids inside a zone polygon receive that zone's species code and `assignment_method="spatial_join"` |
| `test_assign_spatial_nearest_fallback` | A centroid outside all zone polygons falls back to the nearest polygon and gets `assignment_method="nearest_polygon"` |
| `test_assign_spatial_unknown_plant_when_nearest_zone_beyond_default_threshold` | A detection ~99 m from every zone exceeds the 5 m default and gets `PLANT` / `unknown_too_far` |
| `test_assign_spatial_unknown_plant_when_nearest_zone_beyond_custom_threshold` | A stricter `max_nearest_distance_m` flags a point that would otherwise fall back to its nearest zone |
| `test_assign_spatial_nearest_fallback_within_custom_threshold` | A larger `max_nearest_distance_m` lets a far point still fall back to `nearest_polygon` |
| `test_assign_spatial_no_cultivar_columns_when_shapefile_lacks_them` | Shapefiles without `cultc_id` produce no `cultivar_*` columns at all |
| `test_assign_spatial_within_assigns_species_name` | Zones with a `comm_name` attribute get `species_name` alongside `species_id`, and no cultivar columns |
| `test_assign_spatial_carries_class_id_when_present` | Zones with a `class_id` attribute carry it through |
| `test_assign_spatial_no_class_id_column_when_shapefile_lacks_it` | Shapefiles without `class_id` produce no `class_id` column |
| `test_assign_spatial_within_assigns_cultivar` | Zones with a `cultc_id` attribute get `cultivar_id`/`cultivar_name` alongside `species_id`, and no `species_name` |
| `test_assign_spatial_nearest_fallback_assigns_cultivar` | Nearest-polygon fallback carries cultivar columns too, not just species |
| `test_assign_monoculture_sets_species_and_method` | All rows receive the given species code and `assignment_method="monoculture_config"` |
| `test_assign_monoculture_no_world_columns` | Monoculture output contains no `world_*` columns |
| `test_load_catalog_reads_json_file` | Confirms `load_catalog` reads the catalog JSON |
| `test_enrich_with_catalog_adds_species_columns` | Species catalog fields are added as `species_*` columns |
| `test_enrich_with_catalog_raises_on_unmatched_species` | A `species_id` with no catalog entry raises `UnknownSpeciesCodeError` |
| `test_enrich_with_catalog_raises_on_unmatched_cultivar` | A `cultivar_id` with no catalog entry raises `UnknownSpeciesCodeError` |
| `test_enrich_with_catalog_adds_cultivar_columns_only_when_present` | Cultivar catalog fields are filled only on rows that have a `cultivar_id` |
| `test_enrich_with_catalog_no_cultivar_columns_when_shapefile_lacked_them` | No `cultivar_*` columns are added when the input has no `cultivar_id` column |
| `test_enrich_with_catalog_after_assign_spatial` | `assign_spatial`'s `cultivar_id` feeds straight into enrichment |
| `test_assign_fallback_labels` | Color checkers (by `classname`, or `class` when `classname` is blank) get `COLORCHECKER` whatever their zone or georeferencing; `PLANT` rows get the catalog's labels; cultivar is cleared on both |
| `test_assign_fallback_labels_with_text_class_id_column` | The catalog's integer `class_id` can be assigned into a text `class_id` column from the shapefile |


## Orchestration

`det_to_world` is wired into the SQLite orchestrator like `raw_to_jpg` and
`jpg_to_det` (`scripts/job/submit.py`'s `SUPPORTED_STAGES`), but its input
staging differs enough from the single-route stages to warrant its own
mechanics, described below. Example config:
[`configs/config.det_to_world.example.yaml`](../configs/config.det_to_world.example.yaml).

### Compute placement: CERES, not ATLAS

The stage is CPU-only (scipy `RegularGridInterpolator`, no GPU), so it runs
on CERES by default — unlike GPU-bound `jpg_to_det`, which runs on ATLAS.
`transfer.routes.det_to_world.destination_site` in the stage config drives
both which cluster's presence skips staging for a piece of data, and which
Globus endpoint (`atlas_endpoint`/`ceres_endpoint`/`juno_endpoint`) is used
as the destination — see `_endpoint_for_site()` in
`orchestrator/input_staging_planner.py`.

### Three staged input pieces

Unlike `raw_to_jpg`/`jpg_to_det` (one fixed JUNO → compute route),
`det_to_world` needs images, detections, and one ASFM bundle. Images and
detections are resolved independently. The ASFM bundle keeps each
reconstruction's NPZ grids, `camera_reference.csv`, and `fov.csv` together:

| Piece | Source root(s) | Destination layout |
|---|---|---|
| Images | `source_root_atlas`/`source_root_ceres`/`source_root_juno` | `<input_staging_root>/<batch_id>/images/` |
| Detections | same roots as images | `<input_staging_root>/<batch_id>/detections/<batch_id>.csv` |
| ASFM grid and reference bundle | `source_root_grids_ceres`/`source_root_grids_juno` | `<grid_root>/<batch_id>/` (nested reconstruction paths preserved) |

For each piece, the resolver checks (in order): the destination cluster
itself, then the other non-destination compute cluster (ATLAS/CERES,
whichever isn't the destination), then JUNO LTS — see
`_resolve_fallback_source()` / `_INTERMEDIATE_FALLBACK_SITES` in
`orchestrator/input_staging_planner.py`. Images/detections are planned by
`_plan_multi_site_requests()`; the ASFM bundle is planned separately by
`_plan_grid_request()`, since they live under a different `data_state`
(`semifield-asfm`) and are read directly off shared storage at job time
(`stage.cli_args`' `--g` argument points at `paths.grid_root` directly, not
`$TMPDIR`) rather than being copied into job scratch. A site satisfies this
piece only when it has at least one NPZ grid and at least one directory with a
paired `camera_reference.csv` and `fov.csv`; there is no separate
`primary_references/` transfer.

### Image sampling

The stage CLI never reads image pixels — only the detections CSV (`--i`)
and the ASFM bundle (`--g`), plus the file names under `images/` when looking
for images with no detection row. Images are staged only to support the optional
visualization step below, so instead of transferring the whole `images/`
directory, only a small random sample is fetched
(`transfer.routes.det_to_world.image_sample_size`, default `DEFAULT_IMAGE_SAMPLE_SIZE`
in `orchestrator/input_staging_planner.py`), as individual files in one
Globus batch-mode transfer (`globus transfer ... --batch -`, see
`orchestrator/globus_transfer.py`) rather than a recursive directory sync.
The sample is deterministic per `(batch_id, site)` so replanning doesn't
churn which files were picked.

If a batch's images already reside at the destination in full (e.g.
promoted there by `jpg_to_det`), staging is skipped (`already_satisfied`),
and the generated Slurm job's own copy-to-`$TMPDIR` step re-applies the same
cap — `IMAGE_SAMPLE_SIZE` in `orchestrator/templates/slurm_job.sh.j2`
copies only a random subset from `images/` when more than
`image_sample_size` files are already there, so a job never rsyncs an
entire batch of images it has no use for.

### Readiness and staging-completion gating

- `v_batches_needing_det_to_world` (in `schemas/sqlite/pipeline.sql`)
  answers "does this batch's data exist somewhere" — it requires current
  images, detections, and grids (anywhere, any site) and excludes batches
  that already have georeferenced output. It does **not** check locality.
- The staging planner additionally requires paired camera/FOV references at
  the same source site as the grids. Missing references produce a clear
  planning error before submission.
- `scripts/job/submit.py`'s `filter_det_to_world_staged_ready()` is the
  locality/submission gate: it requires all three pieces'
  `staged_inputs` rows to show `status='completed'` for a batch
  (`orchestrator/sqlite_db.py`'s `get_det_to_world_staged_batch_ids()`,
  compared against `orchestrator/input_staging_planner.py`'s
  `det_to_world_expected_dst_paths()`). This mirrors how
  `raw_to_jpg`/`jpg_to_det` gate on `staged_inputs`, so — unlike an
  earlier, now-removed live-inventory-based gate — it never requires a
  fresh `globus_file_index` rescan after a Globus transfer completes.
  Pieces already resident at the destination when planned are recorded as
  immediately-`completed` `staged_inputs` rows too
  (`StagingRequest.already_satisfied`), so this reflects readiness the
  moment `stage_inputs.py` runs, not after some later rescan.

### Visualization

`scripts/job/visualize.py`'s `det_to_world` mode (enabled via
`visualization.enabled: true` / `mode: det_to_world` in the stage config)
draws each detection box back onto its sampled image, labeled at the
top-left corner with its species/cultivar name (whichever of
`cultivar_name`/`species_name` the CSV has, if either — see
`_detection_name_label()`) and the box's world-space area in cm², joined as
`"<name> | <area> cm^2"` when both are available. The area is computed via
the shoelace formula over the box's four remapped world corners
(`world_tl/tr/bl/br_x/y`), not just width × height, since a bbox's world
footprint can be a rotated/skewed quad rather than an axis-aligned
rectangle. The area half of the label is omitted when it can't be computed
(missing corner columns — including detections that weren't georeferenced —
or a geographic CRS like EPSG:4326 where raw-unit
shoelace area isn't meaningful as cm² without a geodetic reprojection — see
`_DEGREE_CRS_CODES`); no label is drawn at all when neither the name nor the
area is available. `no_detections` placeholder rows have no bbox and are skipped.

`--images` is expected to already be the pre-sampled subset staged above
(`$TMPINPUT/images`), so this mode renders every image found there rather
than sampling a second time — there is deliberately no `sample_size` in
`visualization.args` for `det_to_world` in the example config, since a
second independent cap could silently drift out of sync with
`image_sample_size`.

`--detections` points at the single `<batch_id>_georeferenced.csv` file
(`$RUN_DIR/artifacts/${BATCH_ID}_georeferenced.csv`), not a directory of
per-image files like `jpg_to_det`'s mode — see
`load_georeferenced_rows_by_image()`, which groups the CSV's rows by
`image_id` before rendering.

### Bbox shapefile

When `--bbox-shp-output <dir>` is passed (`visualization.args.bbox_shp_output`
in the example config, rendered as `$VIZ_DIR` — the same directory the JPG
sample writes to), `visualize.py` also writes one shapefile covering
**every** georeferenced box in the batch — not just the sampled images rendered above,
since the georeferenced CSV already has all of them regardless of which
images got staged for the JPG sample. Each box's geometry is the real-world
polygon from its four remapped corners (`world_tl/tr/bl/br_x/y`, same
tl→tr→br→bl order as the area calculation above), not just a centroid point.

Attributes are `image_id` and `bbox_id`, plus the *zone* shapefile's own short column names
(`species`, `comm_name`, `cultc_id`, `disp_name`, `class_id`; see
`_BBOX_SHP_FIELDS`), not det_to_world's more readable CSV headers
(`species_id`, `species_name`, `cultivar_id`, `cultivar_name`) — both
because shapefile DBF fields are capped at 10 characters, and so this
output overlays cleanly in GIS software against the same zone shapefile a
batch's boxes were assigned from. Only whichever columns are actually
present in the CSV are included (see `write_bbox_shapefile()`); rows with
no world corners (detections that weren't georeferenced, `no_detections`
placeholders, and every row of a batch that wasn't georeferenced) are
excluded, since there's no real-world geometry for them.

Writing it into `$VIZ_DIR` means the shapefile's files (`.shp`/`.shx`/
`.dbf`/`.prj`/`.cpg`) sit alongside the sample JPGs and get picked up by the
existing `zip -j "$VIZ_ZIP" "$VIZ_DIR"/*` step in `slurm_job.sh.j2` —
one `det_to_world_sample_<batch_id>.zip`, promoted to
`<final_dest_root>/<batch_id>/det_to_world/`, ships both. No separate
promotion path or destination.

Boxes/labels are drawn *after* downscaling to `--max-width`, not before —
source images can be very large (e.g. 13368×9520), so text/box strokes sized
for that resolution would shrink to illegible slivers once downscaled for
the sample. `xmin`/`ymin`/`xmax`/`ymax` are normalized `[0, 1]`, so they map
directly onto the resized image's pixel dimensions either way.

### `--bbot-version` / `--shp` resolution

Neither is hardcoded in the stage config — both are resolved per batch by
`orchestrator/submit_jobs.py` and threaded into the generated Slurm script
as `$BBOT_VERSION`/`$SHAPEFILE_PATH` shell variables, which
`stage.cli_args` references directly (see
[`configs/config.det_to_world.example.yaml`](../configs/config.det_to_world.example.yaml)):

- `orchestrator/sqlite_db.py`'s `resolve_season_for_batch()` looks up the
  `season_date_ranges` row (loaded from `configs/date_ranges.yaml` by
  `scripts/admin/load_date_ranges.py`) whose `[start_date, end_date]` window
  covers the batch's date, for its site — giving `bbot_version` directly.
- The shapefile's relative path is *derived* from that row's
  `pipeline_season` using the naming convention ASFM already writes to —
  `semifield-utils/autosfm/ShapeFiles/<pipeline_season>/<pipeline_season>.shp`
  — not read from any manually-typed field, since the authoritative
  question is "does this file currently exist," which only
  `globus_file_index` can answer.
- `resolve_file_path_with_priority()` then checks `globus_file_index` for a
  current row at that relative path, in priority order across a list of
  `(site, namespace)` pairs — configurable per stage config as
  `transfer.file_source_priority` (a deployment-topology fact, same
  category as `transfer.ceres_endpoint`/`juno_endpoint`), defaulting to
  `orchestrator/sqlite_db.py`'s `DEFAULT_FILE_SOURCE_PRIORITY`
  (`CERES/90daydata` before `CERES/project`) for configs that don't set it.
  Both default tiers are direct filesystem reads on CERES, so no Globus
  transfer is needed for either; there's no JUNO fallback tier by default.
  If nothing in the configured (or default) priority list has the file
  indexed, resolution fails for that batch.
- This resolution only runs when `stage.cli_args` actually contains
  `$BBOT_VERSION` or `$SHAPEFILE_PATH` — not just because `stage.name ==
  "det_to_world"` — because most real seasons are monoculture (no
  shapefile at all). A config written for monoculture batches
  (`--species ...` with or without `--skip-remap`, no `$SHAPEFILE_PATH`) is never blocked by
  a missing shapefile. Since `cli_args` is one static string applied to
  every batch submitted with a given config file, a batch set spanning
  both monoculture and spatial-join seasons needs two separate config
  files — which one to use for a given batch is a manual/operator choice,
  not derived automatically from whether the season has a shapefile.
- A resolution failure (no season window, or shapefile not indexed on
  CERES) is caught in `submit_jobs()` *before* claiming a lease, so it
  never burns a lease slot — it surfaces as a `JobResult` with
  `status="config_error"`.
