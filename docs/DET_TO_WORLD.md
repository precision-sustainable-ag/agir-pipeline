# Detection Stage to World Coordinates
Turns local image bounding box coordinates into real world geographic cooridnates using a precalculated pixel-to-world grids


## Output Structure

```
<output_dir>/
  det_to_world/
    <run_id>/
      run_report.json
      manifest.json
      logs/
      artifacts/
        <batch_id>_georeferenced.csv
```

The georeferenced CSV contains all input detection columns plus the following appended columns:

| Column | Description |
|---|---|
| `world_tl_x` / `world_tl_y` | Top-left corner in world coordinates |
| `world_tr_x` / `world_tr_y` | Top-right corner in world coordinates |
| `world_bl_x` / `world_bl_y` | Bottom-left corner in world coordinates |
| `world_br_x` / `world_br_y` | Bottom-right corner in world coordinates |
| `world_centroid_x` / `world_centroid_y` | Midpoint of TL and BR corners in world coordinates |
| `crs` | Coordinate reference system of the world coordinates (e.g. `EPSG:32617`) |

Detections whose bounding box corners cannot be mapped to world coordinates after inward nudging are dropped and reported as warnings.


## Exit Codes

| Code | Value | Meaning |
|---|---|---|
| `EXIT_SUCCESS` | `0` | All images processed successfully |
| `EXIT_PARTIAL` | `1` | Some images failed, some succeeded |
| `EXIT_FAILURE` | `2` | All images failed |
| `EXIT_CONFIG_ERROR` | `3` | Invalid inputs or configuration (bad CSV, missing grid directory, etc.) |


## Error Codes

| Code | Scope | Meaning |
|---|---|---|
| `E_CSV_INVALID` | Stage | Input detection CSV is missing required columns or cannot be read |
| `E_GRID_NOT_FOUND` | Per-image | No NPZ grid file found for the image |
| `E_REMAP_FAILED` | Per-image | Unexpected failure while remapping bounding boxes for an image |


## Sample Run Command

```bash
python3 -m stages.det_to_world.cli \
  --i /path/to/combined_detections.csv \
  --g /path/to/pixel_world_grids \
  --o /path/to/output \
  --batch-id NC_2026-01-07
```

| Flag | Description |
|---|---|
| `--i` | Input detection CSV with `image_id` and normalized bounding box columns |
| `--g` | Directory containing per-image NPZ pixel-to-world grid files |
| `--o` | Output directory where run artifacts are written |
| `--batch-id` | Batch identifier; auto-inferred from the input path if omitted |
| `--skip-remap` | Skip remapping and write an empty output CSV (for monoculture batches with no grid) |


## Call Flow

```
cli.main()
  |
  |-- load_detection_rows()        read + validate the combined detection CSV (image_id + bbox coords)
  |
  |-- remap_rows()                 loop over images
  |     |
  |     |-- GridCache.get()        load the per-image NPZ grid (cached; NPZ holds a sparse grid of
  |     |     |                   pixel (u,v) -> world (x,y) sample points produced by ASFM)
  |     |     `-- _load_grid()     build RegularGridInterpolators from NPZ arrays
  |     |
  |     `-- map_bbox()             map one detection row to world coords
  |           |
  |           |-- _map_point()     resolve one bbox corner to pixel coords + nudge if out of bounds
  |           |     |
  |           |     `-- _interpolate_point()   query X/Y interpolators at (u, v)
  |           |
  |           `-- _construct_global_coords()   assemble output row with all corners + centroid
  |
  `-- write_georeferenced_csv()    write mapped rows to output CSV
```


## Method Descriptions

### `cli.main()`
Entry point. Parses arguments, validates inputs, coordinates the full run consisting of loading the CSV, remapping rows, writing output, and writing the run report and manifest.

### Remapper
### `load_detection_rows(csv_path)`
Reads the input detection CSV and validates that all required columns are present (`image_id`, `bounding_box_id`, `xmin`, `ymin`, `xmax`, `ymax`). Returns the fieldnames and rows.

### `remap_rows(rows, grid_dir)`
Groups rows by `image_id` and processes each image. Loads the grid as `GridCache` object, calls `map_bbox` (below) for each detection, and stores each image's results and warnings.


### `GridCache.get(image_id)`
Maintains a dict of `image_id → GridData` populated lazily as each image is encountered. On first access for a given image it loads the corresponding NPZ from disk; on subsequent accesses it returns the cached result. By the end of a run it holds one loaded grid per unique image that appeared in the detection CSV.

`GridData` is a dataclass holding:
- **Two scipy interpolators** (one for world X / easting (west–east), one for world Y / northing (south–north)) — given a pixel coordinate `(u, v)`, each returns the corresponding real-world coordinate 
- **Sensor dimensions** — the pixel width and height of the image, used to convert normalized bbox coordinates (0–1) back to absolute pixel coordinates before querying the interpolators
- **CRS string** — the coordinate reference system of the world coordinates (e.g. `EPSG:32617` = WGS 84 / UTM zone 17N), passed through to the output CSV

### `GridCache._load_grid(image_id, data)`
Reads the NPZ arrays, validates their shape, and constructs two `RegularGridInterpolator` instances (one for world X, one for world Y) that map pixel `(u, v)` coordinates to real-world coordinates.

### `map_bbox(row, grid)`
Maps all four corners of a bounding box to world coordinates. Returns the mapped row or a `RemapWarning` if any corner cannot be resolved.

### `_map_point(x, y, grid)`
Converts a single normalized bbox corner to pixel coordinates, then attempts to interpolate its world position. If the point is outside the grid bounds, it nudges the coordinate inward toward the image center in steps (`NUDGE_PX`) until a valid in-bounds point is found.

### `_interpolate_point(grid, u, v)`
Queries the X and Y interpolators at pixel position `(u, v)` and returns the corresponding world coordinates.

### `_construct_global_coords(row, coords, crs)`
Assembles the output row dict from the four mapped corner coordinates, computing the centroid as the midpoint of the top-left and bottom-right corners.

### `write_georeferenced_csv(rows, fieldnames, csv_path)`
Writes the mapped rows to a CSV, preserving original input columns and appending the geo columns. Supports writing a header-only file when there are no rows.


## Tests

| Test | Description |
|---|---|
| `test_load_detection_rows_validates_required_columns` | Confirms that a CSV missing required columns raises a `ValueError` with a descriptive message |
| `test_map_bbox_maps_all_corners` | Verifies that all four corners and the centroid are correctly interpolated for a simple linear grid |
| `test_remap_rows_handles_warnings_and_missing_grids` | Checks that out-of-bounds corners produce a `W_SURFACE_MISS` warning and that images with no grid file produce an `E_GRID_NOT_FOUND` failure |
| `test_map_bbox_applies_inward_nudges` | Confirms that a bbox corner falling outside the grid boundary is nudged inward and snapped to the nearest valid grid point |
| `test_map_bbox_uses_tl_br_midpoint_for_centroid` | Confirms the centroid is the TL/BR midpoint, not the average of all four corners |
| `test_write_georeferenced_csv_supports_header_only` | Confirms that writing an empty row list still produces a valid CSV with the correct header |


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

### Three independent input pieces

Unlike `raw_to_jpg`/`jpg_to_det` (one fixed JUNO → compute route),
`det_to_world` needs three pieces that are each resolved independently
against whichever site already has them:

| Piece | Source root(s) | Destination layout |
|---|---|---|
| Images | `source_root_atlas`/`source_root_ceres`/`source_root_juno` | `<input_staging_root>/<batch_id>/images/` |
| Detections | same roots as images | `<input_staging_root>/<batch_id>/detections/<batch_id>.csv` |
| Pixel-to-world grids | `source_root_grids_ceres`/`source_root_grids_juno` | `<grid_root>/<batch_id>/` (nested per-time-range sub-batch dirs preserved) |

For each piece, the resolver checks (in order): the destination cluster
itself, then the other non-destination compute cluster (ATLAS/CERES,
whichever isn't the destination), then JUNO LTS — see
`_resolve_fallback_source()` / `_INTERMEDIATE_FALLBACK_SITES` in
`orchestrator/input_staging_planner.py`. Images/detections are planned by
`_plan_multi_site_requests()`; grids are planned separately by
`_plan_grid_request()`, since they live under a different `data_state`
(`semifield-asfm`) and are read directly off shared storage at job time
(`stage.cli_args`' `--g` argument points at `paths.grid_root` directly, not
`$TMPDIR`) rather than being copied into job scratch.

### Image sampling

The stage CLI never reads image pixels — only the detections CSV (`--i`)
and NPZ grids (`--g`). Images are staged only to support the optional
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
draws each georeferenced box back onto its sampled image, labeled at the
top-left corner with the box's world-space area in cm² — computed via the
shoelace formula over the box's four remapped world corners
(`world_tl/tr/bl/br_x/y`), not just width × height, since a bbox's world
footprint can be a rotated/skewed quad rather than an axis-aligned
rectangle. No label is drawn when the area can't be computed (missing
corner columns, or a geographic CRS like EPSG:4326 where raw-unit shoelace
area isn't meaningful as cm² without a geodetic reprojection — see
`_DEGREE_CRS_CODES`).

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

Boxes/labels are drawn *after* downscaling to `--max-width`, not before —
source images can be very large (e.g. 13368×9520), so text/box strokes sized
for that resolution would shrink to illegible slivers once downscaled for
the sample. `xmin`/`ymin`/`xmax`/`ymax` are normalized `[0, 1]`, so they map
directly onto the resized image's pixel dimensions either way.