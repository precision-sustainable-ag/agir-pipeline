# Detection Stage to World Coordinates
Turns local image bounding box coordinates into real world geographic cooridnates using precalculated pixel-to-world grids, then assigns a species to each detection — either via a spatial join against a species-zone shapefile, or a single configured species code for monoculture batches.

Species assignment was originally a separate `assign_species` stage; it was folded into `det_to_world` so the pipeline produces one final CSV, one run_report/manifest, and one exit code per batch instead of two stages handing a file off between them. See `stages/det_to_world/species.py` for `assign_spatial()`/`assign_monoculture()`.


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
| `species_id` | Species code assigned to the detection |
| `assignment_method` | How the species was assigned: `spatial_join`, `nearest_polygon`, or `monoculture_config` |

Zone shapefiles carry at most one of two optional human-readable attributes (never both, in practice):

| Shapefile has... | Appended column(s) | Example |
|---|---|---|
| `comm_name` (ordinary species zones, e.g. `cover_crops_2025_2026`) | `species_name` | `"Velvetleaf"` |
| `cultc_id` + `disp_name` (cultivar seasons, e.g. `peanuts_2026` — every zone is the same species, a different cultivar) | `cultivar_id`, `cultivar_name` | `107`, `"Peanut - EXP-OLEIC-001"` |

Whether these columns appear at all is decided per batch by what's actually in the resolved `--shp` shapefile's columns (see `species.assign_spatial()`), not by any config flag — a shapefile with neither attribute produces neither column, unlike `world_*`/`crs`, which are always present (blank for monoculture) since georeferencing conceptually applies to every batch.

Detections whose bounding box corners cannot be mapped to world coordinates after inward nudging are dropped and reported as warnings. For monoculture batches (`--skip-remap`), the `world_*`/`crs` columns are present but blank — the schema stays the same across every batch regardless of mode.


## Exit Codes

| Code | Value | Meaning |
|---|---|---|
| `EXIT_SUCCESS` | `0` | All images processed successfully |
| `EXIT_PARTIAL` | `1` | Some images failed to remap, some succeeded (species assignment still ran on the successful rows) |
| `EXIT_FAILURE` | `2` | All images failed, or species assignment itself failed |
| `EXIT_CONFIG_ERROR` | `3` | Invalid inputs or configuration (bad CSV, missing grid directory, missing/invalid `--shp`, `--species`, or `--bbot-version`, etc.) |

A species-assignment failure (bad/missing shapefile, spatial join error) always forces `EXIT_FAILURE`, even if remapping itself succeeded for every image — the single shared CSV isn't usable without species columns, so partial credit doesn't apply the way it does for remap-only failures (e.g. a straggler frame with no ASFM grid).


## Error Codes

| Code | Scope | Meaning |
|---|---|---|
| `E_CSV_INVALID` | Stage | Input detection CSV is missing required columns or cannot be read |
| `E_GRID_NOT_FOUND` | Per-image | No NPZ grid file found for the image |
| `E_REMAP_FAILED` | Per-image | Unexpected failure while remapping bounding boxes for an image |
| `E_SHAPEFILE_UNREADABLE` | Stage | Shapefile not found or could not be read |
| `E_SPATIAL_JOIN_FAILED` | Stage | Spatial join raised an unexpected error |


## Sample Run Command

Spatial join (multiple species via shapefile zones):

```bash
python3 -m stages.det_to_world.cli \
  --i /path/to/combined_detections.csv \
  --g /path/to/pixel_world_grids \
  --shp /path/to/species_zones.shp \
  --bbot-version 3.1 \
  --o /path/to/output \
  --batch-id NC_2026-01-07
```

Monoculture (single species, no remap):

```bash
python3 -m stages.det_to_world.cli \
  --i /path/to/combined_detections.csv \
  --g /path/to/pixel_world_grids \
  --skip-remap \
  --species ABUTH \
  --bbot-version 3.1 \
  --o /path/to/output \
  --batch-id NC_2026-01-07
```

| Flag | Description |
|---|---|
| `--i` | Input detection CSV with `image_id` and normalized bounding box columns |
| `--g` | Directory containing per-image NPZ pixel-to-world grid files |
| `--shp` | Species zone shapefile with polygon geometries and species codes. Required unless `--skip-remap` |
| `--species` | Species code to assign to all detections. Required when `--skip-remap` |
| `--bbot-version` | BBot version string (must be <= 3.1) |
| `--o` | Output directory where run artifacts are written |
| `--batch-id` | Batch identifier; auto-inferred from the input path if omitted |
| `--skip-remap` | Skip remapping for monoculture batches — assigns `--species` directly to the original detection rows instead of georeferenced ones |


## Call Flow

```
cli.main()
  |
  |-- load_detection_rows()        read + validate the combined detection CSV (image_id + bbox coords)
  |
  |-- [--skip-remap]  assign_monoculture()    assign --species to every loaded row directly
  |
  |-- [else]  remap_rows()         loop over images
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
  |     `-- assign_spatial()       spatial-join the successfully remapped rows against --shp
  |
  `-- write_georeferenced_csv()    write the (species-assigned) rows to the output CSV
```


## Method Descriptions

### `cli.main()`
Entry point. Parses arguments, validates inputs, coordinates the full run consisting of loading the CSV, remapping rows, assigning species, writing output, and writing the run report and manifest.

### Remapper
### `load_detection_rows(csv_path)`
Reads the input detection CSV and validates that all required columns are present (`image_id`, `bounding_box_id`, `xmin`, `ymin`, `xmax`, `ymax`). Returns the fieldnames and rows.

### `remap_rows(rows, grid_dir)`
Groups rows by `image_id` and processes each image. Loads the grid as `GridCache` object, calls `map_bbox` (below) for each detection, and stores each image's results and warnings.

### Species
### `assign_spatial(dets, shapefile)`
Takes a DataFrame of remapped rows (must have `world_centroid_x`/`world_centroid_y`/`crs`) and a species-zone shapefile path. Builds a GeoDataFrame from the centroids, reprojects to the shapefile's CRS, and spatial-joins (`predicate="within"`) to assign each point's `species_id` from whichever zone polygon contains it. Any unmatched points (outside every zone) fall back to `sjoin_nearest()`; ties (a point exactly equidistant from two zones) are resolved by keeping the first match per point. If the shapefile has `comm_name`, it's joined in the same pass and renamed to `species_name`; if it has `cultc_id`/`disp_name` instead, those are renamed to `cultivar_id`/`cultivar_name` — a shapefile with neither produces neither column.

### `assign_monoculture(dets, species_code)`
Takes a DataFrame of the original (non-georeferenced) detection rows and assigns `species_code` to every row with `assignment_method="monoculture_config"` — no spatial computation.

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
| `test_assign_spatial_within` | Centroids inside a zone polygon receive that zone's species code and `assignment_method="spatial_join"` |
| `test_assign_spatial_nearest_fallback` | A centroid outside all zone polygons falls back to the nearest polygon and gets `assignment_method="nearest_polygon"` |
| `test_assign_spatial_no_cultivar_columns_when_shapefile_lacks_them` | Shapefiles without `cultc_id` produce no `cultivar_*` columns at all |
| `test_assign_spatial_within_assigns_species_name` | Zones with a `comm_name` attribute get `species_name` alongside `species_id`, and no cultivar columns |
| `test_assign_spatial_within_assigns_cultivar` | Zones with a `cultc_id` attribute get `cultivar_id`/`cultivar_name` alongside `species_id`, and no `species_name` |
| `test_assign_spatial_nearest_fallback_assigns_cultivar` | Nearest-polygon fallback carries cultivar columns too, not just species |
| `test_assign_monoculture_sets_species_and_method` | All rows receive the given species code and `assignment_method="monoculture_config"` |
| `test_assign_monoculture_no_world_columns` | Monoculture output contains no `world_*` columns |


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
top-left corner with its species/cultivar name (whichever of
`cultivar_name`/`species_name` the CSV has, if either — see
`_detection_name_label()`) and the box's world-space area in cm², joined as
`"<name> | <area> cm^2"` when both are available. The area is computed via
the shoelace formula over the box's four remapped world corners
(`world_tl/tr/bl/br_x/y`), not just width × height, since a bbox's world
footprint can be a rotated/skewed quad rather than an axis-aligned
rectangle. The area half of the label is omitted when it can't be computed
(missing corner columns, or a geographic CRS like EPSG:4326 where raw-unit
shoelace area isn't meaningful as cm² without a geodetic reprojection — see
`_DEGREE_CRS_CODES`); no label is drawn at all when neither the name nor the
area is available.

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
**every** box in the batch — not just the sampled images rendered above,
since the georeferenced CSV already has all of them regardless of which
images got staged for the JPG sample. Each box's geometry is the real-world
polygon from its four remapped corners (`world_tl/tr/bl/br_x/y`, same
tl→tr→br→bl order as the area calculation above), not just a centroid point.

Attributes use the *zone* shapefile's own short column names
(`species`, `comm_name`, `cultc_id`, `disp_name`, `class_id`; see
`_BBOX_SHP_FIELDS`), not det_to_world's more readable CSV headers
(`species_id`, `species_name`, `cultivar_id`, `cultivar_name`) — both
because shapefile DBF fields are capped at 10 characters, and so this
output overlays cleanly in GIS software against the same zone shapefile a
batch's boxes were assigned from. Only whichever columns are actually
present in the CSV are included (see `write_bbox_shapefile()`); rows with
no world corners at all (monoculture batches, which skip remapping) are
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
  (`--skip-remap --species ...`, no `$SHAPEFILE_PATH`) is never blocked by
  a missing shapefile. Since `cli_args` is one static string applied to
  every batch submitted with a given config file, a batch set spanning
  both monoculture and spatial-join seasons needs two separate config
  files — which one to use for a given batch is a manual/operator choice,
  not derived automatically from whether the season has a shapefile.
- A resolution failure (no season window, or shapefile not indexed on
  CERES) is caught in `submit_jobs()` *before* claiming a lease, so it
  never burns a lease slot — it surfaces as a `JobResult` with
  `status="config_error"`.