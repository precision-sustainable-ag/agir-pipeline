# Assign Species

Assigns each detection to a species. Supports both spatical joins for multiple species usng a shapfile, and a monoculture config which assigns one species to all.


## Input Structure

| Input | Source Stage | Description |
|---|---|---|
| `<batch_id>_georeferenced.csv` | `det_to_world` | Detections with world coordinates - used for spatial join |
| `<batch_id>.csv` | `jpg_to_det` | Original detection CSV without world coordinates - used for monoculture |
| `run_report.json` | `det_to_world` | Determines assignment mode — `skipped: true` means monoculture |
| `species_zones.shp` | External | Shapefile of species zone |


## Output Structure

```
<output_dir>/
  assign_species/
    <run_id>/
      run_report.json
      manifest.json
      logs/
      artifacts/
        <batch_id>_species_assigned.csv
```

* run_report.json --> summarize the run, including timing, git commit, inputs, assignment (spatial_join vs monoculture_config), any errors, etc
* manifest.json --> outputs metadata about each output image file (status, artifacts, byte size, checksum used) 
* logs/ --> info/error messages from the run
* <batch_id>_species_assigned.csv --> input columns plus the following appended columns:

| Column | Description |
|---|---|
| `species_id` | Species code assigned to the detection |
| `assignment_method` | How the species was assigned: `spatial_join`, `nearest_polygon`, or `monoculture_config` |


## Exit Codes

| Code | Value | Meaning |
|---|---|---|
| `EXIT_SUCCESS` | `0` | Species assigned successfully |
| `EXIT_FAILURE` | `2` | Assignment failed (CSV unreadable or spatial join error) |
| `EXIT_CONFIG_ERROR` | `3` | Invalid inputs or configuration |


## Error Codes

| Code | Scope | Meaning |
|---|---|---|
| `E_SHAPEFILE_UNREADABLE` | Stage | Shapefile not found or could not be read |
| `E_SPATIAL_JOIN_FAILED` | Stage | Spatial join raised an unexpected error |
| `E_CSV_INVALID` | Stage | Detection or georeferenced CSV could not be read |


## CLI

| Flag | Required | Description |
|------|----------|-------------|
| `--geo` |  Yes (spatial join) | Georeferenced CSV from `det_to_world` containing world coordinates for detections |
| `--det` | Yes (monoculture) | Original detection CSV from `jpg_to_det` |
| `--geo-report` | Yes | `run_report.json` from `det_to_world`; used to determine if batch is monoculture or spatial join |
| `--shp` | Yes | Species zone shapefile with polygon geometries and species codes |
| `--bbot-version` | Yes | BBot version string  (must be <= 3.1) |
| `--o` | Yes | Output directory where results and reports are written |
| `--species` | No | Species code to assign to all detections (required when batch is monoculture) |
| `--batch-id` | No | Batch ID; auto-inferred from input path if omitted |

### Batch ID Resolution

`batch_id` is resolved in the following steps:
1. Explicit `--batch-id` flag
2. Auto-parsed from the input path (looks for `XX_YYYY-MM-DD` pattern in path segments)
3. Exits with `EXIT_CONFIG_ERROR` if neither resolves


## Call Flow

***[cli.py]***
1. `parse_batch_id()` — resolve batch ID from `--batch-id` or input path
2. Validate `bbot_version` — reject if > 3.1
3. Read `geo_report.json` — `skipped: true` → monoculture, else spatial join

**[`assigner.py` - monoculture]** `assign_monoculture(det_csv, species_code)`
- Read detection CSV from `jpg_to_det` (no world columns)
- Append `species_id` and `assignment_method="monoculture_config"` to every row

**[`assigner.py` - spatial join]** `assign_spatial(geo_csv, shapefile)`
- Read georeferenced CSV from `det_to_world`; build GeoDataFrame from `world_centroid_x`/`world_centroid_y`
- Reproject detection points to match shapefile CRS
- `gpd.sjoin()` — assign species where centroid falls inside a zone polygon
- `gpd.sjoin_nearest()` — nearest-polygon fallback for any unmatched detections

***[cli.py]***
4. Write `<batch_id>_species_assigned.csv` + `run_report.json` + `manifest.json`



## Tests

| Test | Description |
|---|---|
| `test_assign_spatial_within` | Centroids inside a zone polygon receive that zone's species code and `assignment_method="spatial_join"` |
| `test_assign_spatial_nearest_fallback` | A centroid outside all zone polygons falls back to the nearest polygon and gets `assignment_method="nearest_polygon"` |
| `test_assign_monoculture_sets_species_and_method` | All rows receive the given species code and `assignment_method="monoculture_config"` |
| `test_assign_monoculture_no_world_columns` | Monoculture output contains no `world_*` columns |


## Sample Runs

### Spatial Join

```bash
python3 -m stages.assign_species.cli \
  --geo        data/NC_2026-01-07/NC_2026-01-07_georeferenced.csv \
  --det        data/NC_2026-01-07/NC_2026-01-07.csv \
  --geo-report data/NC_2026-01-07/run_report.json \
  --shp        shapefiles/species_zones.shp \
  --bbot-version 3.1 \
  --o          output/
```

### Monoculture

```bash
python3 -m stages.assign_species.cli \
  --geo        data/NC_2026-01-07/NC_2026-01-07_georeferenced.csv \
  --det        data/NC_2026-01-07/NC_2026-01-07.csv \
  --geo-report data/NC_2026-01-07/run_report.json \
  --shp        shapefiles/species_zones.shp \
  --bbot-version 3.1 \
  --species    ABUTH \
  --o          output/
```
