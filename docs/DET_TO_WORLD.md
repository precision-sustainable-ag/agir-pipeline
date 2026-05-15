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