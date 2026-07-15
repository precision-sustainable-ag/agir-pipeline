# SQLite Orchestrator Architecture

## Purpose

The AgIR orchestrator coordinates data movement and batch processing through a
shared SQLite database. SQLite is the durable control plane for inventory,
readiness, input-staging state, submission leases, and stage-run history.

The orchestrator is a collection of focused Python commands rather than one
long-running service. Operators run each command for a specific lifecycle
step, and the commands coordinate through database state.

The currently supported stages are:

- `raw_to_jpg`
- `jpg_to_det`

## Design Principles

- SQLite is the source of truth for orchestration state.
- Storage inventory determines which batches are ready for work.
- Input movement finishes before compute submission begins.
- Each operator command owns one part of the lifecycle.
- Database leases prevent duplicate compute submissions.
- Generated Slurm jobs are self-contained and record their results when they
  finish.
- Repeated requests, report ingestion, and lease release are designed to be
  safe where practical.

## High-Level Flow

```text
Globus storage endpoints
          |
          v
scripts/admin/globus_index.py
          |
          v
globus_file_index + inventory_runs
          |
          v
SQLite readiness views
          |
          v
scripts/job/stage_inputs.py
          |
          +--> staged_inputs --> Globus transfer
                                  |
                                  v
scripts/job/poll_stage_inputs.py updates transfer status
          |
          v
staged_inputs.status = 'completed'
          |
          v
scripts/job/submit.py
          |
          +--> stage_leases --> rendered Slurm script --> sbatch
                                                       |
                                                       v
                                                 stage compute
                                                       |
                                                       v
                                           promote outputs and artifacts
                                                       |
                                                       v
                                      stage_runs + stage lease release
```

## Component Responsibilities

### Inventory scanner

`scripts/admin/globus_index.py` scans configured Globus storage roots and
updates the SQLite inventory. It records scan history, upserts files and
directories, marks missing entries as stale, and rebuilds inventory summaries.

The scanner writes the state used by the readiness views. The staging and
compute commands do not independently crawl storage.

### SQLite schema

`schemas/sqlite/pipeline.sql` defines the orchestration tables, indexes, and
readiness views. The schema is designed to be reapplied: tables and indexes use
`IF NOT EXISTS`, while readiness views are dropped and recreated so their query
logic stays current.

### Input-staging planner

`orchestrator/input_staging_planner.py` is a pure planning layer. It combines:

- rows returned by a stage's readiness query;
- transfer endpoints and routes from the stage configuration;
- the input directory convention for the selected stage; and
- optional batch, site, and limit filters.

It returns `StagingRequest` values describing what should move and where. It
does not write database state or invoke Globus.

### Input-staging command

`scripts/job/stage_inputs.py` is the operator-facing transfer submission
command. It:

1. Loads the stage configuration.
2. Queries the appropriate readiness view through the planner.
3. Records an idempotent request in `staged_inputs`.
4. Submits accepted requests through the Globus CLI.
5. Records the Globus task ID or a submission failure.

`--dry-run` performs planning only. It does not write SQLite state or contact
Globus.

### Input-staging poller

`scripts/job/poll_stage_inputs.py` refreshes existing transfer requests. It
does not plan work or submit new transfers. It selects `submitted` and `active`
rows that have a Globus task ID, polls each task, maps the remote state to an
orchestrator state, and updates `staged_inputs`.

The command supports three modes:

- one polling pass;
- `--wait`, which stops when no pollable rows remain; and
- `--forever`, which continues until interrupted.

### Compute-submission command

`scripts/job/submit.py` is the login-node entry point for compute submission.
It:

1. Finds candidate batches from a SQLite readiness view, or loads a supplied
   batch file.
2. Skips candidates with a filesystem lock file.
3. Keeps only batches whose matching `staged_inputs` row is `completed`.
4. Prints the eligible batch list or delegates to `submit_jobs()`.

The completed-input check is a database gate. The command does not call the
staging command and does not poll Globus.

Read-heavy discovery and input-gating queries can use a temporary local copy of
the database. The copy directory is configured by `paths.db_temp_dir`, and the
copy is removed when its connection closes.

### Submission and rendering

`orchestrator/submit_jobs.py` owns the submission loop. For each batch it:

1. Claims a `stage_leases` row.
2. Builds stage-specific paths and rendering context from configuration.
3. Renders `orchestrator/templates/slurm_job.sh.j2` with Jinja and
   `StrictUndefined`.
4. Writes an executable per-batch script.
5. Calls `sbatch` and records the returned Slurm job ID on the lease.

The Python renderer assembles the context. The Jinja template does not read
SQLite or YAML directly.

### Compute job

The rendered Slurm job performs the downstream batch lifecycle:

1. Validate that staged input files exist.
2. Copy staged inputs into `$TMPDIR/input`.
3. Run the configured stage CLI.
4. Generate an optional visualization sample.
5. Validate and promote final outputs.
6. Copy run artifacts to durable storage.
7. Ingest `run_report.json` and release the stage lease.

If input validation or run-directory resolution fails, the job attempts to
release its lease before exiting.

### Report ingestion and lease release

`scripts/job/ingest_and_release.py` is called at the end of the Slurm job. It
uses helpers from `orchestrator/sqlite_db.py` to:

- upsert `run_report.json` into `stage_runs`; and
- release the lease owned by the submitting orchestrator process.

Lease release is attempted even when report ingestion fails.

## Database State Model

### `inventory_runs`

Records each inventory scan, including its scope, timing, status, and scan
statistics.

### `globus_file_index`

Stores the current and historical file inventory across indexed endpoints.
Important orchestration fields include:

- storage endpoint and site;
- storage root and data state;
- relative and full paths;
- batch ID and batch date;
- file extension and parent directory;
- current/stale state; and
- first-seen and last-seen metadata.

### `stage_runs`

Stores one row per stage execution, keyed by `run_id`. It contains the batch,
stage, terminal status, timings, provenance, unit counts, artifact locations,
and the complete run-report JSON.

Report ingestion uses an upsert on `run_id`, so ingesting the same report again
updates the existing run instead of creating a duplicate.

### `stage_leases`

Stores one active submission lease per `(batch_id, stage)`. A lease includes:

- a unique lease ID;
- the owning orchestrator ID;
- claim and expiration timestamps; and
- the Slurm job ID after successful submission.

### `staged_inputs`

Stores the input-transfer lifecycle for a batch and stage. A request is unique
by `(batch_id, stage, src_path, dst_path)`.

Valid states are:

```text
planned -> requested -> submitted -> active -> completed
                                      |            
                                      +-> failed
                                      +-> canceled
```

`planned` is primarily a dry-run/result concept; persisted requests are created
as `requested`. A failed or canceled request can be reopened. An active or
completed matching request is returned without creating a duplicate transfer.

## Readiness Model

The readiness views answer which batches should run next from current inventory
and orchestration state.

### `v_batches_needing_raw_to_jpg`

A batch is ready when current RAW files exist and current JPG outputs do not.
The view excludes batches with an active `raw_to_jpg` lease or a successful
`raw_to_jpg` run.

### `v_batches_needing_jpg_to_det`

A batch is ready when current JPG files exist and current detection outputs do
not. The view excludes batches with an active `jpg_to_det` lease or a successful
`jpg_to_det` run.

The views use all current inventory rows rather than tying readiness to one
specific scan. Accurate readiness therefore depends on refreshing every
relevant storage scope and maintaining `is_current` correctly.

Readiness and input availability are separate decisions:

- readiness views decide whether a stage's output is needed;
- `staged_inputs.status = 'completed'` decides whether compute may be
  submitted; and
- the compute job verifies that files are physically present before processing.

## Concurrency and Idempotency

### Lease claiming

`claim_stage_lease()` uses `BEGIN IMMEDIATE` to serialize competing writers.
The unique `(batch_id, stage)` constraint prevents two active leases for the
same work. An unexpired lease causes a lease conflict; an expired lease is
replaced.

The lease time-to-live is derived from the configured Slurm time limit with an
additional buffer.

### Input-staging requests

`request_input_staging()` also uses `BEGIN IMMEDIATE`. Repeating an identical
request returns its existing state when it is active or completed. Failed and
canceled requests are reset for another submission attempt.

### Report ingestion

`ingest_run_report()` uses `INSERT ... ON CONFLICT (run_id) DO UPDATE`. This
allows ingestion to be retried after an interruption without duplicating a run.

### Lease release

Lease release is owner-checked and idempotent. Releasing an already removed
lease returns a false result instead of raising an error.

## Configuration Contract

The staging and compute commands share a stage YAML configuration. Its main
sections are:

### `stage`

Defines the stage name, Python CLI module, CLI arguments, output subdirectory,
and promoted-output suffix.

### `paths`

Defines the database, repository, environment, staging, artifact, final-output,
log, script, lock, model, and stage-specific configuration paths.

Important database paths are:

- `paths.db`: shared SQLite database;
- `paths.db_temp_dir`: temporary directory for read-only login-node copies.

### `transfer`

Defines the source and destination Globus endpoints and a route for each stage.
A route supplies its source root, optional destination root, input subdirectory,
source subdirectory, and priority.

### `slurm`

Defines account, partition, CPU, memory, time, and optional generic resources.

### `render`

Selects the Jinja job template. The default is `slurm_job.sh.j2`.

### `visualization`

Controls optional stage-specific visualization generation and its arguments.

## Operator Workflow

Placeholders below must be replaced with environment-specific paths.

### 1. Initialize or update the schema

```bash
sqlite3 <database.sqlite3> < schemas/sqlite/pipeline.sql
```

### 2. Refresh inventory

```bash
python scripts/admin/globus_index.py \
  --db <database.sqlite3> \
  --endpoint-config-yaml <endpoint-config.yaml>
```

Every storage scope needed by the readiness views should be refreshed.

### 3. Preview input staging

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --dry-run \
  --limit <count>
```

### 4. Submit input transfers

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit <count>
```

### 5. Wait for transfer completion

```bash
python scripts/job/poll_stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --wait \
  --interval <seconds> \
  --timeout <seconds>
```

### 6. Preview eligible compute batches

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --find-only \
  --limit <count>
```

### 7. Submit compute jobs

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit <count>
```

## Failure Behavior

### Transfer submission failure

The matching `staged_inputs` row becomes `failed` and records an error summary.
The same request can later be reopened and resubmitted.

### Transfer polling failure

Terminal remote states are recorded as `failed` or `canceled`. A temporary or
unrecognized remote state remains nonterminal so a later polling pass can
refresh it.

### Input-staging gate failure

`submit.py` skips batches without a completed staging record. It does not treat
the skipped batch as a compute-submission error.

### Lease conflict

Submission skips a batch when another active lease owns the same stage and
batch. An expired lease can be replaced by a later attempt.

### `sbatch` failure

The result is reported as `sbatch_failed`. The claimed lease remains until it
expires or is explicitly released.

### Stage or promotion failure

The compute script preserves available run artifacts, attempts report
ingestion, and releases the lease through `ingest_and_release.py`. Visualization
failure is nonfatal.

## Current Operational Constraints

- Inventory must be refreshed before readiness queries can reflect storage
  changes.
- Input staging currently supports only `raw_to_jpg` and `jpg_to_det`.
- Transfer polling requires the Globus CLI and an authenticated environment.
- Compute submission requires a Slurm login node with `sbatch` available.
- The shared SQLite database permits multiple readers but serializes writers.
- Read-only local copies are ordinary file copies, so operators should avoid
  copying while a large write transaction is in progress.
- `submit.py --dry-run` renders scripts without calling `sbatch`, but the
  submission layer currently claims leases before rendering them.
- An `sbatch` failure does not immediately release the associated lease.
- Lease expiration is time-based; there is no automatic lease-renewal loop.
- The orchestration commands are intentionally separate and require an
  operator or external scheduler to run them in sequence.

## Source Map

| Responsibility | Source |
| --- | --- |
| Inventory scanning | `scripts/admin/globus_index.py` |
| Database schema and readiness views | `schemas/sqlite/pipeline.sql` |
| SQLite operations | `orchestrator/sqlite_db.py` |
| Stage configuration loading | `orchestrator/config.py` |
| Input-staging planning | `orchestrator/input_staging_planner.py` |
| Globus command construction and polling | `orchestrator/globus_transfer.py` |
| Input-transfer submission CLI | `scripts/job/stage_inputs.py` |
| Input-transfer polling CLI | `scripts/job/poll_stage_inputs.py` |
| Compute-submission CLI | `scripts/job/submit.py` |
| Lease, render, and `sbatch` loop | `orchestrator/submit_jobs.py` |
| Slurm job lifecycle | `orchestrator/templates/slurm_job.sh.j2` |
| Run ingestion and lease release | `scripts/job/ingest_and_release.py` |
| Output promotion | `scripts/job/promote.py` |
| Visualization generation | `scripts/job/visualize.py` |
