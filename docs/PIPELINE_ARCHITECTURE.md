# AgIR Pipeline Architecture

## Purpose

The AgIR pipeline transforms batch-organized agricultural imagery into
developed images and derived analysis products. The repository separates image
processing from orchestration: stage modules transform data, while the SQLite
orchestrator decides what should run, stages inputs, submits jobs, and records
results.

This document describes the repository-wide processing architecture:

- the stage dependency graph;
- stage inputs and outputs;
- shared execution contracts;
- data movement between storage tiers;
- the boundary between stage code and orchestration; and
- which parts of the graph are currently orchestrated.

Related documents:

- [`SQLITE_ORCHESTRATOR_ARCHITECTURE.md`](SQLITE_ORCHESTRATOR_ARCHITECTURE.md)
  describes the control plane.
- [`SQLITE_DB_SCHEMA.md`](SQLITE_DB_SCHEMA.md) describes persistent state.
- [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) describes operator commands.

## Pipeline at a Glance

```text
Camera RAW files
       |
       v
+----------------+
|   raw_to_jpg   |
+----------------+
       |
       | developed JPG images
       v
+----------------+
|   jpg_to_det   |
+----------------+
       |
       | per-image detections + batch detection CSV
       |
       +-----------------------------+
       |                             |
       v                             v
+----------------+          +----------------+
|   det_to_seg   |          |  det_to_world  |
+----------------+          +----------------+
       |                             |
       | binary masks                | georeferenced detections
       v                             v
segmentation products          world-coordinate CSV
```

`det_to_seg` also reads the developed JPG images. `det_to_world` also reads
precomputed pixel-to-world grid files.

## Current Integration Status

| Stage | Stage CLI implemented | Shared run contracts | SQLite readiness view | Input-staging route | Slurm submit configuration |
| --- | --- | --- | --- | --- | --- |
| `raw_to_jpg` | Yes | Yes | Yes | Yes | Yes |
| `jpg_to_det` | Yes | Yes | Yes | Yes | Yes |
| `det_to_seg` | Yes | Yes | No | No | No |
| `det_to_world` | Yes | Yes | Yes | Yes | Yes |

The first three stages form the currently orchestrated path. `det_to_seg`
can be invoked directly, but does not yet participate in the SQLite
readiness, prerequisite-staging, lease, or generated Slurm-job flow.

`det_to_world` differs from `raw_to_jpg`/`jpg_to_det` in several ways:

- It needs three independent pieces of data — images, detections, and ASFM
  pixel-to-world NPZ grids — rather than one. Images and detections are
  resolved against the nearest site that already has them (destination
  cluster, then ATLAS/CERES, then JUNO LTS) instead of a single fixed JUNO
  route; grids are resolved the same way but from a separate root/data
  state. See `orchestrator/input_staging_planner.py`'s
  `_plan_multi_site_requests()` and `_plan_grid_request()`.
- Only a small random sample of images is staged (`transfer.routes
  .det_to_world.image_sample_size`, default 8) rather than the whole
  directory — the stage CLI never reads image pixels; the sample exists
  solely to support an optional visualization step (see below).
- Submission readiness is gated on `staged_inputs` — all three pieces must
  show `status='completed'` — the same mechanism `raw_to_jpg`/`jpg_to_det`
  use, so it never depends on a fresh `globus_file_index` rescan after a
  transfer finishes. Pieces already resident at the destination when planned
  are recorded as immediately-completed `staged_inputs` rows too (see
  `StagingRequest.already_satisfied`). See `scripts/job/submit.py`'s
  `filter_det_to_world_staged_ready()`.
- It's CPU-only (scipy interpolation, no GPU) and runs on CERES by default,
  unlike GPU-bound `jpg_to_det` on ATLAS — `transfer.routes.det_to_world
  .destination_site` drives both which cluster's presence skips staging and
  which Globus endpoint is used as the destination.
- It can optionally render a QC visualization: georeferenced boxes drawn
  back onto the sampled images, each labeled with its world-space area in
  cm² (computed via the shoelace formula over the box's four remapped world
  corners). See `scripts/job/visualize.py`'s `det_to_world` mode.

This distinction matters operationally: the presence of a stage package does
not by itself mean `scripts/job/submit.py` can schedule that stage.

## Architectural Layers

```text
+------------------------------------------------------------------+
| Operator commands                                                |
| inventory | stage inputs | poll transfers | submit compute       |
+------------------------------------------------------------------+
                              |
+------------------------------------------------------------------+
| SQLite control plane                                             |
| inventory | readiness | staged inputs | leases | stage runs      |
+------------------------------------------------------------------+
                              |
+------------------------------------------------------------------+
| Execution layer                                                  |
| Jinja Slurm job | scratch copy | stage CLI | promote | ingest    |
+------------------------------------------------------------------+
                              |
+------------------------------------------------------------------+
| Stage layer                                                      |
| raw_to_jpg | jpg_to_det | det_to_seg | det_to_world              |
+------------------------------------------------------------------+
                              |
+------------------------------------------------------------------+
| Storage layer                                                    |
| long-term source | compute staging | job scratch | final outputs |
+------------------------------------------------------------------+
```

Each layer has a narrow responsibility:

- Operators advance the lifecycle through explicit commands.
- SQLite stores durable control state and calculates readiness.
- The execution layer prepares a batch and invokes one stage.
- Stage modules process data and produce standardized run artifacts.
- Storage paths hold source data, staged inputs, run bundles, and promoted
  products.

## Unit of Work

The orchestrator's unit of work is:

```text
(batch_id, stage)
```

A batch ID identifies a collection event and location, for example:

```text
MD_2025-04-25
```

Stages generally process the images belonging to one batch in a single run.
Within a run, an image ID is the normal per-item unit recorded in the manifest
and error lists.

Every stage execution creates a unique `run_id`. Reprocessing the same batch
therefore produces a new isolated run directory rather than overwriting the
previous run bundle.

## Stage Model

Every stage follows the same broad pattern:

```text
validate configuration and inputs
              |
              v
discover batch work items
              |
              v
process each item
              |
              v
write stage artifacts
              |
              v
write manifest.json and run_report.json
              |
              v
return a standardized exit code
```

Stage implementations live under `stages/<stage_name>/`. A typical stage
package contains:

```text
__init__.py       stage identity, version, and error constants
cli.py            command-line boundary and contract generation
processor.py      batch and per-item processing coordination
<algorithm>.py    stage-specific transformation logic
configs/          default stage configuration
tests/            focused stage tests
```

The CLI is the stable execution boundary used by direct runs and generated
compute jobs. Processing modules do not query orchestration state.

## Stage Details

### `raw_to_jpg`

Purpose:

Convert proprietary camera RAW images into developed JPG images through an
intermediate DNG representation.

Inputs:

- directory of RAW image files;
- camera-processing YAML configuration;
- color calibration data and camera metadata profiles; and
- optional input manifest.

Processing:

```text
RAW image -> DNG conversion -> JPG development -> metadata update
```

Outputs:

- one JPG artifact for each successful input image;
- `manifest.json`; and
- `run_report.json`.

Run layout:

```text
<output-root>/
  raw_to_jpg/
    <run-id>/
      artifacts/
        <image-id>.jpg
      manifest.json
      run_report.json
      run.log
```

Downstream consumer:

```text
jpg_to_det
```

Detailed reference: [`RAW_TO_JPG.md`](RAW_TO_JPG.md).

### `jpg_to_det`

Purpose:

Run multiscale object detection over developed JPG images.

Inputs:

- directory of JPG images;
- detection YAML configuration; and
- model weights.

Processing:

```text
JPG
  -> multiscale inference
  -> confidence filtering
  -> weighted box fusion
  -> optional post-fusion suppression
  -> artifact export
```

Outputs:

- one YOLO-format detection text file per successfully processed image;
- one batch-level detection CSV;
- `manifest.json`; and
- `run_report.json`.

Run layout:

```text
<output-root>/
  jpg_to_det/
    <run-id>/
      artifacts/
        <image-id>.txt
        <batch-id>.csv
      manifest.json
      run_report.json
      run.log
```

Downstream consumers:

```text
det_to_seg
det_to_world
```

Detailed reference: [`JPG_TO_DET.md`](JPG_TO_DET.md).

### `det_to_seg`

Purpose:

Run segmentation inference inside detection boxes and composite the results
into one full-image binary mask per JPG.

Inputs:

- per-image YOLO detection text files from `jpg_to_det`;
- the corresponding developed JPG images;
- segmentation YAML configuration; and
- segmentation model weights selected by the configuration.

Detection and image files are paired by case-insensitive filename stem.

Processing:

```text
detection boxes + JPG
  -> crop or tile inference regions
  -> predict masks
  -> threshold masks
  -> composite into full-image mask
```

Outputs:

- one binary mask PNG per successfully paired image;
- `manifest.json`; and
- `run_report.json`.

Run layout:

```text
<output-root>/
  det_to_seg/
    <run-id>/
      artifacts/
        masks/
          <image-id>_mask.png
      manifest.json
      run_report.json
      run.log
```

Detailed reference: [`DET_TO_SEG.md`](DET_TO_SEG.md).

### `det_to_world`

Purpose:

Map image-space detection bounding boxes into real-world coordinates using
precomputed per-image pixel-to-world grids.

Inputs:

- batch detection CSV from `jpg_to_det`;
- directory of per-image NPZ coordinate grids; and
- batch ID, either explicit or inferred from the input path.

Processing:

```text
detection CSV + pixel-to-world grids
  -> map bounding-box corners
  -> calculate world centroid
  -> attach coordinate reference system
  -> write georeferenced rows
```

Outputs:

- one batch-level georeferenced detection CSV;
- `manifest.json`; and
- `run_report.json`.

Run layout:

```text
<output-root>/
  det_to_world/
    <run-id>/
      artifacts/
        <batch-id>_georeferenced.csv
      manifest.json
      run_report.json
      run.log
```

The stage supports `--skip-remap` for batches where remapping is intentionally
not applicable. The run remains auditable and records the skip reason.

Detailed reference: [`DET_TO_WORLD.md`](DET_TO_WORLD.md).

## Shared Stage Contracts

Shared builders in `stages/common/contracts.py` produce two JSON files for each
run:

```text
run_report.json
manifest.json
```

These files separate batch-level execution information from per-item artifact
detail.

### `run_report.json`

The run report is the batch-level execution summary. It contains:

- stage name and version;
- run, pipeline, batch, and orchestrator identifiers;
- start time, end time, duration, exit code, and status;
- code, configuration, model, dependency, and container provenance;
- input root and discovered-unit count;
- output root, run directory, artifact directory, and aggregate counts;
- artifact-type summaries;
- stage-level and item-level errors and warnings; and
- pointers to logs and other detailed files.

The SQLite orchestrator ingests selected fields into `stage_runs` and retains
the complete report as JSON text.

### `manifest.json`

The manifest is the per-item artifact record. It identifies the stage, run,
batch, artifact root, and one result entry per image or work item. Each item can
record:

- success, failure, or skip state;
- relative artifact paths;
- checksums and sizes;
- error information; and
- item-specific metadata.

Promotion validates artifact paths and checksums from the manifest before
copying outputs to their final destination.

Example contracts:

- [`examples/run_report_example.json`](examples/run_report_example.json)
- [`examples/manifest_example.json`](examples/manifest_example.json)

## Exit Codes and Run Status

Stages use a common exit-code model:

| Exit code | Meaning | Run status |
| --- | --- | --- |
| `0` | Every required item succeeded | `success` |
| `1` | Some items succeeded and some failed | `partial` |
| `2` | Processing failed | `failed` |
| `3` | Configuration or input validation failed | `failed` |

Per-item failures are recorded in both the manifest and run report. A stage can
therefore finish with partial success while preserving the successful
artifacts and detailed failure information.

The SQLite ingestion layer normalizes `partial` to `partial_success` when it
writes `stage_runs`.

## Run Directory Contract

All stage CLIs write isolated run bundles using this shape:

```text
<output-root>/
  <stage>/
    <run-id>/
      artifacts/
        <stage-specific files>
      manifest.json
      run_report.json
      run.log
```

This layout provides:

- retry isolation;
- a stable unit for promotion and archival;
- deterministic report and manifest locations;
- a clear relationship between metadata and artifacts; and
- preservation of failed or partial execution evidence.

The compute wrapper locates the newest run directory created under the selected
stage output path and copies that bundle to durable run storage.

## Data Storage and Movement

The orchestrated execution path uses three storage roles:

| Storage role | Lifetime | Purpose |
| --- | --- | --- |
| Source storage | Long-term | Authoritative upstream inputs |
| Compute staging | Persistent across jobs | Inputs made available to the compute cluster and promoted outputs |
| Job scratch | One scheduled job | Fast local processing workspace |

### Input movement

```text
source storage
    |
    | Globus transfer tracked in staged_inputs
    v
compute staging
    |
    | rsync at job start
    v
$TMPDIR/input
```

`stage_inputs.py` plans and submits the persistent transfer. The compute job
does not begin until `staged_inputs.status = 'completed'`, and it independently
checks that files exist before copying them into scratch.

### Output movement

```text
$TMPDIR/output/<stage>/<run-id>/
    |
    +--> promote validated artifacts to final batch destination
    |
    +--> copy complete run bundle to paths.output_stage_runs
```

Promotion copies usable data products into their stage-specific final
directory. The durable run bundle preserves the report, manifest, logs, and
artifacts associated with the exact execution attempt.

## Promotion

`scripts/job/promote.py` is the generic promotion boundary used by generated
compute jobs. It reads the run report and manifest instead of embedding
stage-specific file lists.

Before promotion, it verifies:

- required report fields;
- stage and batch identity;
- manifest structure;
- existence of every referenced artifact; and
- SHA-256 checksums when present.

After validation, it:

- copies item artifacts into the final destination;
- copies a batch-level CSV when the stage produced one;
- rewrites promoted metadata paths;
- preserves the report and manifest alongside promoted metadata; and
- retains an input manifest when one exists.

Promotion is separate from stage processing so stage CLIs remain usable in
local and test environments without cluster-specific destination logic.

## Orchestration Boundary

The processing stages do not decide whether they should run. They receive
explicit input, output, configuration, model, and batch arguments and return
artifacts plus structured status.

For the integrated stages, the SQLite orchestrator owns:

- storage inventory;
- readiness calculation;
- prerequisite transfer planning and tracking;
- completed-input gating;
- exclusive submission leases;
- Slurm script rendering and submission; and
- run-report ingestion.

The stage owns:

- configuration validation;
- input discovery within its assigned directory;
- batch and per-item processing;
- stage-specific error classification;
- artifact creation;
- manifest creation; and
- run-report creation.

This boundary keeps image-processing code independent of storage inventory,
Globus task state, scheduler state, and SQLite transaction behavior.

## Configuration Boundary

There are two related configuration layers:

### Orchestrator stage configuration

The YAML passed to `scripts/job/submit.py` describes how a stage runs in the
cluster environment:

- stage name and Python module;
- rendered CLI arguments;
- repository and environment paths;
- input staging and output destinations;
- model and stage-config paths;
- transfer endpoints and routes;
- Slurm resources;
- rendering template; and
- optional visualization settings.

### Processing configuration

The YAML passed into a stage CLI controls its processing algorithm. Examples
include camera calibration, inference thresholds, model architecture, tiling,
and worker behavior.

The orchestrator treats the processing configuration as an external file. It
passes the path into the stage command and records provenance; it does not
interpret the algorithm-specific settings.

## Repository Map

```text
agir-pipeline/
  configs/                       cluster and stage configuration examples
  docs/                          architecture, operations, and stage references
  orchestrator/                  SQLite, staging, transfer, and submission logic
    templates/                   generated Slurm job templates
    tests/                       focused orchestrator tests
  schemas/sqlite/                canonical SQLite schema and readiness views
  scripts/admin/                 inventory administration commands
  scripts/job/                   staging, polling, submission, promotion, ingestion
  stages/                        processing stage packages
    common/                      shared contracts, configuration, and logging
    raw_to_jpg/
    jpg_to_det/
    det_to_seg/
    det_to_world/
  tests/                         repository-level tests
```

## Adding a Stage to the Processing Graph

A new processing stage should first establish a standalone stage contract:

1. Create `stages/<stage_name>/`.
2. Expose a CLI with explicit input, output, configuration, and batch options.
3. Use the shared report and manifest builders.
4. Write artifacts beneath `<output>/<stage>/<run-id>/artifacts/`.
5. Use the common exit-code model.
6. Record checksums and relative artifact paths in the manifest.
7. Add focused processing and contract tests.
8. Add a stage reference under `docs/`.

Integrating that stage into the orchestrator is a separate step:

1. Define how inventory identifies its inputs and outputs.
2. Add a SQLite readiness view.
3. Add a query helper in `orchestrator/sqlite_db.py`.
4. Add an input specification in `orchestrator/input_staging_planner.py`.
5. Add a transfer route and compute configuration.
6. Define stage CLI arguments for the Slurm render context.
7. Add readiness, staging, render, and submission tests.
8. Update the architecture, schema, and operator documents.

Keeping standalone stage implementation separate from orchestration integration
allows processing behavior to be validated before it is enabled for automated
submission.

## Source Map

| Responsibility | Source |
| --- | --- |
| Shared report and manifest contracts | `stages/common/contracts.py` |
| Common configuration helpers | `stages/common/config.py` |
| Common stage logging | `stages/common/loggers.py` |
| Exit-code definitions | `stages/__init__.py` |
| RAW development | `stages/raw_to_jpg/` |
| Plant detection | `stages/jpg_to_det/` |
| Segmentation | `stages/det_to_seg/` |
| World-coordinate mapping | `stages/det_to_world/` |
| Generic output promotion | `scripts/job/promote.py` |
| Generated compute lifecycle | `orchestrator/templates/slurm_job.sh.j2` |
| Pipeline state and readiness | `schemas/sqlite/pipeline.sql` |
| Inventory scanner | `scripts/admin/globus_index.py` |
| Input staging and transfer polling | `scripts/job/stage_inputs.py`, `scripts/job/poll_stage_inputs.py` |
| Compute submission | `scripts/job/submit.py`, `orchestrator/submit_jobs.py` |
