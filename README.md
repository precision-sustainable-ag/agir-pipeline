# AgIR Pipeline

AgIR is a batch-oriented agricultural image-processing pipeline. It converts
camera RAW imagery into developed JPGs and derived products including plant
detections, segmentation masks, and georeferenced detections.

The repository contains:

- standalone image-processing stages;
- a SQLite-backed orchestration control plane;
- Globus input-staging commands;
- Slurm job rendering and submission;
- structured run reports and artifact manifests; and
- operator and architecture documentation.

## Pipeline

```text
RAW images
    |
    v
raw_to_jpg
    |
    v
developed JPG images
    |
    v
jpg_to_det
    |
    +-------------------------+
    |                         |
    v                         v
det_to_seg                det_to_world
    |                         |
    v                         v
binary masks        georeferenced detections
```

| Stage | Purpose | Orchestrated |
| --- | --- | --- |
| `raw_to_jpg` | Convert camera RAW files into developed JPG images | Yes |
| `jpg_to_det` | Detect plants and export per-image labels and a batch CSV | Yes |
| `det_to_seg` | Produce full-image binary masks within detection regions | Not yet |
| `det_to_world` | Map image detections into real-world coordinates | Yes |

`raw_to_jpg`, `jpg_to_det`, and `det_to_world` currently participate in
SQLite readiness, input staging, lease management, and Slurm submission.
`det_to_seg`'s CLI is implemented and can be run directly, but is not yet
wired into the orchestrator.

See [Pipeline Architecture](docs/PIPELINE_ARCHITECTURE.md) for the complete
stage and data-flow design.

## Orchestration Flow

The orchestrator is a collection of explicit operator commands coordinated
through SQLite:

```text
refresh storage inventory
        |
        v
calculate ready batches
        |
        v
stage required inputs with Globus
        |
        v
poll transfers to completion
        |
        v
claim a batch/stage lease
        |
        v
render and submit a Slurm job
        |
        v
promote outputs, ingest run report, release lease
```

SQLite stores:

- storage inventory and scan history;
- readiness state derived through views;
- input-transfer requests and status;
- active submission leases; and
- completed stage-run history.

See [SQLite Orchestrator Architecture](docs/SQLITE_ORCHESTRATOR_ARCHITECTURE.md)
and [SQLite Database Schema](docs/SQLITE_DB_SCHEMA.md) for details.

## Requirements

Core development:

- Python 3.12 or newer
- `uv` or another Python environment manager
- system libraries required by the selected imaging or ML stage

Cluster orchestration additionally requires:

- an authenticated Globus CLI environment;
- access to the configured storage endpoints;
- a Slurm login node with `sbatch`; and
- shared paths configured for SQLite, staging, logs, models, and outputs.

GPU stages require a compatible PyTorch, CUDA, and model environment.

## Development Setup

Create and activate an environment:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
```

Install the repository with development, imaging, and ML dependencies:

```bash
uv pip install -e '.[dev,cv,ml]'
```

For a smaller environment, select only the extras needed for the stage under
development:

```bash
uv pip install -e '.[dev,cv]'
```

Verify the primary packages import:

```bash
python -c "import orchestrator, stages; print('imports succeeded')"
```

## SQLite Setup

Create or update the pipeline database:

```bash
sqlite3 <database.sqlite3> < schemas/sqlite/pipeline.sql
```

Check the schema version:

```bash
sqlite3 <database.sqlite3> 'PRAGMA user_version;'
```

The stage configuration supplied to the operator commands must point
`paths.db` at this database.

## Operator Quickstart

The normal operator sequence is inventory, input staging, transfer polling,
compute preview, and submission.

### 1. Refresh inventory

```bash
python scripts/admin/globus_index.py \
  --db <database.sqlite3> \
  --endpoint-config-yaml <endpoint-config.yaml>
```

### 2. Preview input staging

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --dry-run \
  --limit 10
```

### 3. Submit input staging

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit 10
```

### 4. Wait for transfers

```bash
python scripts/job/poll_stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --wait \
  --interval 30 \
  --timeout 7200
```

### 5. Preview compute-eligible batches

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --find-only \
  --limit 10
```

### 6. Submit compute jobs

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit 10
```

Use the [Operator Runbook](docs/OPERATOR_RUNBOOK.md) for prerequisites,
checkpoints, targeted batch runs, monitoring, and recovery procedures.

## Running Stages Directly

Each stage exposes a Python module CLI. Direct execution is useful for local
development and focused validation without the orchestration layer.

### RAW to JPG

```bash
python -m stages.raw_to_jpg.cli \
  --c <camera-config.yaml> \
  --i <raw-input-directory> \
  --o <output-directory> \
  --batch-id <batch-id>
```

### JPG to detections

```bash
python -m stages.jpg_to_det.cli \
  --c <detection-config.yaml> \
  --m <model-weights.pt> \
  --i <jpg-input-directory> \
  --o <output-directory> \
  --batch-id <batch-id> \
  --device <device>
```

### Detections to segmentation

```bash
python -m stages.det_to_seg.cli \
  --i <detection-artifacts-directory> \
  --j <jpg-input-directory> \
  --c <segmentation-config.yaml> \
  --o <output-directory> \
  --batch-id <batch-id> \
  --device <device>
```

### Detections to world coordinates

```bash
python -m stages.det_to_world.cli \
  --i <batch-detection.csv> \
  --g <pixel-world-grid-directory> \
  --o <output-directory> \
  --batch-id <batch-id>
```

Each stage writes an isolated run directory containing artifacts,
`manifest.json`, `run_report.json`, and a run log.

## Testing

Run the complete available test suite:

```bash
python -m pytest
```

Run focused orchestrator tests:

```bash
python -m pytest orchestrator/tests
```

Run a specific stage suite:

```bash
python -m pytest stages/<stage>
```

Some tests or stage runs require optional imaging libraries, model weights,
GPU resources, Globus authentication, or a Slurm environment. Use focused
tests when those external capabilities are unavailable.

## Repository Layout

```text
agir-pipeline/
  configs/               stage and cluster configuration examples
  docs/                  architecture, operations, and stage documentation
  orchestrator/          SQLite, transfer, rendering, and submission logic
  schemas/sqlite/        canonical SQLite schema and readiness views
  scripts/admin/         storage inventory commands
  scripts/job/           staging, polling, submission, promotion, ingestion
  stages/                standalone image-processing stage packages
  tests/                 repository-level tests
```

Important entry points:

| Task | Entry point |
| --- | --- |
| Refresh storage inventory | `scripts/admin/globus_index.py` |
| Plan and submit input transfers | `scripts/job/stage_inputs.py` |
| Poll input transfers | `scripts/job/poll_stage_inputs.py` |
| Find and submit compute work | `scripts/job/submit.py` |
| Promote validated artifacts | `scripts/job/promote.py` |
| Ingest reports and release leases | `scripts/job/ingest_and_release.py` |

## Documentation

### Architecture and operations

- [Pipeline Architecture](docs/PIPELINE_ARCHITECTURE.md)
- [SQLite Orchestrator Architecture](docs/SQLITE_ORCHESTRATOR_ARCHITECTURE.md)
- [SQLite Database Schema](docs/SQLITE_DB_SCHEMA.md)
- [Operator Runbook](docs/OPERATOR_RUNBOOK.md)
- [Input Staging Runbook](docs/INPUT_STAGING_RUNBOOK.md)

### Stage references

- [RAW to JPG](docs/RAW_TO_JPG.md)
- [JPG to Detection](docs/JPG_TO_DET.md)
- [Detection to Segmentation](docs/DET_TO_SEG.md)
- [Detection to World Coordinates](docs/DET_TO_WORLD.md)

### Implementation notes

- [Globus Transfer Notes](docs/GLOBUS_TRANSFER_NOTES.md)
- [SQLite Input Staging Notes](docs/SQLITE_INPUT_STAGING_NOTES.md)
- [Stage Inputs Notes](docs/STAGE_INPUTS_NOTES.md)
- [Slurm Jinja Rendering Notes](docs/SLURM_JINJA_RENDERING_NOTES.md)
- [SQLite Temporary Copy Configuration](docs/SQLITE_TEMP_COPY_CONFIGURATION.md)

## Execution Contracts

Every stage follows the shared contracts implemented in
`stages/common/contracts.py`:

- a standardized exit-code model;
- one `run_report.json` per run;
- one `manifest.json` per run;
- per-item success, failure, and skip information;
- artifact paths and checksums; and
- provenance for code, configuration, dependencies, and models.

Reference examples:

- [Run report example](docs/examples/run_report_example.json)
- [Manifest example](docs/examples/manifest_example.json)

These contracts allow stages to run independently while still integrating with
promotion, audit history, and orchestration state.
