# AgIR Pipeline Operator Runbook

## Purpose

This runbook describes how an operator moves batches through the SQLite-backed
AgIR pipeline. It covers the normal workflow from inventory refresh through
input staging, compute submission, and result verification.

For the design and component boundaries behind these commands, see
[`SQLITE_ORCHESTRATOR_ARCHITECTURE.md`](SQLITE_ORCHESTRATOR_ARCHITECTURE.md).

The supported stages are:

- `raw_to_jpg`
- `jpg_to_det`

Run commands from the repository root unless a command says otherwise.

## Operator Workflow at a Glance

```text
1. Validate the environment and configuration
2. Refresh storage inventory
3. Preview input-staging requests
4. Submit input-staging transfers
5. Poll until transfers finish
6. Preview compute-eligible batches
7. Render or submit compute jobs
8. Monitor Slurm and pipeline logs
9. Verify run records and outputs
10. Synchronize Atlas result requests and run bundles to Ceres
```

Do not submit compute jobs until input polling shows that the required
transfers completed.

## Command Placeholders

Examples use the following placeholders:

| Placeholder | Meaning |
| --- | --- |
| `<stage>` | `raw_to_jpg` or `jpg_to_det` |
| `<stage-config.yaml>` | Cluster-specific stage configuration |
| `<result-sync-config.yaml>` | Ceres result-sync configuration |
| `<endpoint-config.yaml>` | Inventory scanner endpoint configuration |
| `<database.sqlite3>` | Shared SQLite database path |
| `<count>` | Maximum number of batches or rows to process |
| `<batch-file.txt>` | Text file containing one batch ID per line |
| `<seconds>` | Polling interval or timeout in seconds |

## Before Each Run

### 1. Enter the repository and activate the environment

```bash
cd <agir-pipeline-directory>
source <virtual-environment>/bin/activate
```

Confirm that the Python environment can import the orchestrator:

```bash
python -c "import orchestrator; print('orchestrator import succeeded')"
```

### 2. Confirm external commands

Input inventory and staging require an authenticated Globus CLI session:

```bash
globus whoami
```

Compute submission requires Slurm:

```bash
sbatch --version
```

Only the command needed for the current lifecycle step must be available. For
example, inventory and staging can be performed without `sbatch`.

### 3. Review the stage configuration

At minimum, confirm these values point to the intended environment:

```text
stage.name
paths.db
paths.db_read_mode
paths.db_temp_dir (only when db_read_mode is snapshot)
paths.input_staging_root
paths.output_stage_runs
paths.final_dest_root
paths.log_dir
transfer routes for the selected stage
slurm.account
slurm.partition
slurm.time
```

The `stage.name` value must match the `--stage` argument used on the command
line.

### 4. Choose a safe batch limit

Start with a small limit when validating a new configuration or environment:

```text
--limit 1
```

Increase the limit only after the dry-run output, transfer paths, rendered job
script, and final destinations have been checked.

## One-Time Database Setup

Apply the schema when creating a database or updating its schema and readiness
views:

```bash
sqlite3 <database.sqlite3> < schemas/sqlite/pipeline.sql
```

Verify the installed schema version:

```bash
sqlite3 <database.sqlite3> 'PRAGMA user_version;'
```

The current schema declares version `7`.

Schema application changes database objects. Coordinate it with other
operators and do not run it during active inventory, staging, polling, or
submission writes.

## Step 1: Refresh Storage Inventory

Readiness is calculated from the SQLite inventory. Refresh every storage scope
needed to determine whether inputs and outputs currently exist.

For a configured set of endpoints:

```bash
python scripts/admin/globus_index.py \
  --db <database.sqlite3> \
  --endpoint-config-yaml <endpoint-config.yaml> \
  --log-file <inventory-log-file>
```

For a single endpoint and storage root:

```bash
python scripts/admin/globus_index.py \
  --db <database.sqlite3> \
  --endpoint <globus-endpoint-id> \
  --site <site> \
  --storage-domain <storage-domain> \
  --namespace <namespace> \
  --storage-root <storage-root> \
  --state <data-state> \
  --log-file <inventory-log-file>
```

Normal operation should not require `--clean-slate`. That option deletes the
existing rows for the selected scope before rebuilding them and should be used
only when a full scope reset is intentional.

### Inventory checkpoint

Continue only when:

- the scanner exits successfully;
- the scan log reports that rows were indexed;
- the expected endpoint scopes were scanned; and
- there are no unresolved Globus listing errors.

If a later command finds no work unexpectedly, first verify that the source and
output storage scopes were both refreshed. The readiness commands query the
database; they do not scan storage themselves.

## Step 2: Preview Input Staging

Preview the transfer plan before creating database rows or contacting Globus:

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --dry-run \
  --limit <count>
```

Review every planned request for:

- the expected batch ID;
- the correct source endpoint and path;
- the correct destination endpoint and path; and
- the expected stage.

For a targeted set of batches, create a file containing one batch ID per line:

```text
<batch-id-1>
<batch-id-2>
```

Then preview only those batches:

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --dry-run \
  --limit <count>
```

### Preview checkpoint

Do not submit transfers if a path, endpoint, site, or stage is incorrect. Fix
the configuration or inventory scope and repeat the dry run.

If a requested batch is not planned, the command logs a warning. Check:

- that the batch appears in current inventory;
- that `--site` matches the indexed source site;
- that the readiness view considers the stage necessary;
- that the stage has a configured transfer route; and
- that the batch ID in the batch file is exact.

## Step 3: Submit Input Transfers

After validating the preview, rerun the same command without `--dry-run`:

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit <count>
```

For targeted batches:

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --limit <count>
```

A new successful request is recorded in `staged_inputs` with a Globus task ID
and a `submitted` status. A matching active or completed request is not
submitted again.

Command logs are written beneath `paths.log_dir`, or beneath the directory
given with `--log-dir`. Review the terminal summary and command logs before
polling.

### Submission checkpoint

Expected successful output includes:

```text
status=submitted
task=<globus-task-id>
staging_id=<staging-id>
```

If submission fails, the row is marked `failed` with an error summary. Resolve
the authentication, endpoint, or path problem before rerunning the same staging
command.

## Step 4: Poll Input Transfers

To refresh transfer state once:

```bash
python scripts/job/poll_stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit <count>
```

For an interactive run, wait until all currently pollable transfers finish:

```bash
python scripts/job/poll_stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --wait \
  --interval 30 \
  --timeout 7200 \
  --limit <count>
```

Use `--forever` only when the command is managed by an operator or supervisor
that will stop it:

```bash
python scripts/job/poll_stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --forever \
  --interval 60 \
  --limit <count>
```

The relevant state changes are:

```text
submitted -> active -> completed
                    -> failed
                    -> canceled
```

### Polling checkpoint

Continue to compute submission only after the intended batches are reported as
`completed`.

Investigate any `failed` or `canceled` result. Rerunning `stage_inputs.py` for
the same batch and paths reopens a terminal unsuccessful request and submits a
new transfer attempt.

`--wait` exits with code `124` when its timeout is reached. A timeout does not
cancel the Globus tasks; run the poller again to refresh them.

## Step 5: Preview Compute-Eligible Batches

Ask the compute command which batches pass both readiness and completed-input
checks:

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --find-only \
  --limit <count>
```

To save the eligible batch IDs:

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --find-only \
  --out <batch-file.txt> \
  --limit <count>
```

This is the safest compute preview because `--find-only` does not enter the
submission loop and does not claim stage leases.

### Eligibility checkpoint

The printed list contains only batches that:

- still need the selected stage;
- are not excluded by a filesystem lock;
- have no active stage lease; and
- have completed input staging recorded in SQLite.

If the command reports that the staging gate skipped a batch, return to the
polling step and confirm that its transfer reached `completed`.

## Step 6: Render a Trial Job Script

For a new or changed configuration, render one script without calling
`sbatch`:

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --dry-run \
  --limit 1
```

Inspect the generated script under:

```text
<paths.script_dir>/scripts/
```

If `paths.script_dir` is not configured, it is written under:

```text
<paths.log_dir>/scripts/
```

Check the Slurm directives, input and output paths, stage command, model path,
environment activation, and final destination.

Current behavior claims a stage lease before rendering a dry-run script. Do
not use repeated dry runs as a harmless preview mechanism; use `--find-only`
for batch discovery and reserve `--dry-run` for deliberate render validation.

## Step 7: Submit Compute Jobs

Submit batches discovered directly from SQLite:

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --limit <count>
```

Or submit an reviewed batch file:

```bash
python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --limit <count>
```

Successful output reports:

```text
<batch-id>  submitted  job=<slurm-job-id>
```

The submission process claims a database lease before rendering and submitting
each job. The lease prevents a second operator from submitting the same batch
and stage concurrently.

### Submission checkpoint

Record or retain:

- the stage and batch IDs;
- the Slurm job IDs;
- the configuration path;
- the command log location; and
- the generated script location.

If `sbatch` fails, do not repeatedly resubmit immediately. The lease remains
until it expires or is deliberately released through an approved recovery
procedure.

## Step 8: Monitor Compute Jobs

Use standard Slurm commands for scheduler state:

```bash
squeue --jobs <slurm-job-id>
```

For completed-job accounting when available:

```bash
sacct --jobs <slurm-job-id> --format=JobID,State,ExitCode,Elapsed
```

The generated job writes logs beneath `paths.log_dir`:

```text
<batch-id>-<slurm-job-id>.out
<batch-id>-<slurm-job-id>.err
<batch-id>-<slurm-job-id>.log
```

The job log is organized into these lifecycle sections:

1. staged-input validation;
2. input copy to `$TMPDIR`;
3. stage execution;
4. visualization;
5. output promotion; and
6. artifact copy, report ingestion, and lease release.

The visualization step is nonfatal. Input validation, stage execution,
promotion, artifact handling, and ingestion messages should be reviewed when a
job does not produce the expected result.

## Step 9: Verify Completion

A successful operational result has four forms of evidence:

1. Slurm reports that the job completed.
2. Promoted outputs exist beneath `paths.final_dest_root`.
3. The ingested `stage_runs` row reports `success`.
4. The stage lease was released.

Check the durable artifact location configured by `paths.output_stage_runs` for
the run's `run_report.json` and related artifacts.

The next inventory refresh provides the final end-to-end confirmation. Once
the promoted outputs are indexed as current, the corresponding readiness view
should no longer return the successfully completed batch.

## Step 10: Synchronize Atlas Results to Ceres

Run this step on Ceres after Atlas GPU jobs have written result-sync request
files to the Atlas outbox.

The command performs two Globus transfers:

1. Atlas result-sync outbox to the Ceres inbox.
2. Each registered Atlas run bundle to its Ceres run-bundle directory.

It validates each request, records it in `result_syncs`, submits the run-bundle
transfer, and polls the Globus task until it reaches a terminal state or the
configured timeout expires.

### Prepare the Ceres configuration

Create a local configuration from the example:

```bash
cp configs/config.result_sync.ceres.example.yaml \
  configs/config.result_sync.ceres.local.yaml
```

Review these values before running:

- `paths.db`: canonical Ceres SQLite database
- `paths.log_dir`: Ceres result-sync log directory
- `result_sync.atlas_endpoint` and `result_sync.ceres_endpoint`
- `result_sync.atlas_outbox` and `result_sync.ceres_inbox`
- `result_sync.atlas_run_root` and `result_sync.ceres_run_root`
- `result_sync.promotion.root` and its stage suffixes
- `result_sync.inventory` Ceres developed-images scope and worker settings
- polling interval and timeout

Confirm that schema version `7` and the `result_syncs` table are installed,
and that Globus authentication is available:

```bash
sqlite3 <database.sqlite3> 'PRAGMA user_version;'
sqlite3 <database.sqlite3> 'PRAGMA table_info(result_syncs);'
globus whoami
```

If the schema is missing, follow the coordinated procedure in
[One-Time Database Setup](#one-time-database-setup) before continuing.

### Preview the synchronization

```bash
python scripts/admin/sync_atlas_results.py \
  --config configs/config.result_sync.ceres.local.yaml \
  --dry-run \
  --bundle-limit 1
```

Dry-run mode does not start Globus transfers or write to the database. It
validates requests already present in the Ceres inbox and plans bundle
transfers for previously registered rows in `requested` state. It also
validates local bundles already in `transferred` state and previews promotion
and ingestion for `verified` rows without advancing them. If a promotion would
occur, it also reports the planned inventory refresh. Because dry-run does not
register new rows, a newly discovered request will not also appear as a planned
bundle transfer during the same invocation.

### Run the synchronization

```bash
python scripts/admin/sync_atlas_results.py \
  --config configs/config.result_sync.ceres.local.yaml \
  --bundle-limit 10
```

`--bundle-limit` limits the number of requested run bundles submitted during
one invocation. The normal state progression is:

```text
requested -> transferring -> transferred -> verified -> ingested
```

Failed or canceled Globus tasks are recorded as `failed` or `canceled`.
Bundles that fail post-transfer validation are recorded as `failed`.

### Verify synchronization state

```bash
sqlite3 -header -column <database.sqlite3> "
SELECT
    run_id,
    batch_id,
    stage,
    run_status,
    status,
    attempt_count,
    globus_task_id,
    error_summary,
    registered_at,
    transfer_started_at,
    transferred_at
FROM result_syncs
ORDER BY registered_at DESC;
"
```

Summarize outstanding work:

```bash
sqlite3 -header -column <database.sqlite3> "
SELECT status, COUNT(*) AS runs
FROM result_syncs
GROUP BY status
ORDER BY status;
"
```

Inspect a specific Globus task when needed:

```bash
globus task show <globus-task-id> --format json
```

Logs are written below:

```text
<paths.log_dir>/result_sync/sync_atlas_results/YYYY-MM-DD/
```

### Timeout and retry behavior

If the command exits with status `124`, the local polling timeout expired but
the transfer remains recorded as `transferring`. Run the same synchronization
command again. It resumes polling the recorded Globus task instead of
submitting a duplicate transfer.

If a task reaches `failed` or `canceled`, resolve the underlying Globus or path
problem before reopening it. Do not update `result_syncs.status` manually.
Reopen an explicitly selected run and resume the normal workflow with:

```bash
python scripts/admin/sync_atlas_results.py \
  --config configs/config.result_sync.ceres.local.yaml \
  --reopen-run-id <run-id>
```

Repeat `--reopen-run-id` to reopen multiple failed or canceled runs. Other
states are rejected so active or already-ingested work cannot be reset.

### Current completion boundary

A `transferred` result means that Globus successfully copied the run bundle to
Ceres. The command then validates request-to-bundle identity, required files,
declared artifact sizes, and any checksums present in the manifest. A bundle
that passes advances to `verified`; a bundle that fails validation is marked
`failed` with an error summary.

Checksums are currently verified when present but are not yet required for
every artifact.

After verification, the command reproduces successful Atlas promotions on
Ceres and ingests each run report into canonical `stage_runs`. Failed and
partial run reports are ingested for history without promotion. An older
successful run cannot replace a newer Atlas promotion for the same batch and
stage. A completed row advances to `ingested`.

When at least one promotion succeeds, the command performs one complete Ceres
`semifield-developed-images` inventory refresh, rebuilds the inventory summary
tables, and confirms every newly promoted artifact is current in
`globus_file_index`. The inventory refresh runs once per command, not once per
bundle.

Force a recovery refresh when no new promotion occurred:

```bash
python scripts/admin/sync_atlas_results.py \
  --config configs/config.result_sync.ceres.local.yaml \
  --refresh-inventory
```

An inventory or confirmation failure makes the command exit unsuccessfully but
does not reverse completed promotion or ingestion. Correct the inventory issue
and rerun with `--refresh-inventory`. Do not publish a fresh database snapshot
to Atlas until result synchronization and inventory are reconciled.

### Publish the reconciled Ceres database snapshot

After Atlas jobs are no longer creating requests for the nightly window, run
the result synchronization command through completion. Then preview the final
snapshot check:

```bash
python scripts/admin/publish_ceres_db_snapshot.py \
  --config configs/config.result_sync.ceres.local.yaml \
  --dry-run
```

Final reconciliation blocks publication when a result sync is unfinished,
failed, or canceled; an ingested sync is absent or inconsistent in
`stage_runs`; or the promoted-output inventory is missing, unsuccessful,
stale, or does not contain the expected files.

Publish a consistent snapshot on Ceres without transferring it:

```bash
python scripts/admin/publish_ceres_db_snapshot.py \
  --config configs/config.result_sync.ceres.local.yaml
```

When `snapshot.atlas_destination_path` has been confirmed for the Atlas
deployment, publish and transfer the snapshot with Globus:

```bash
python scripts/admin/publish_ceres_db_snapshot.py \
  --config configs/config.result_sync.ceres.local.yaml \
  --transfer
```

The publisher uses SQLite's backup API, checks the exact candidate snapshot,
and atomically replaces `snapshot.ceres_publish_path` only when reconciliation
passes. `--transfer` waits for Globus to report successful checksum-based
delivery. It does not discover requests that are still only in the Atlas
outbox, so Atlas jobs must be quiesced or allowed to finish before the final
sync and publication window. Coordinate or disable the pre-existing external
nightly copy before enabling `--transfer`; two independent publishers must not
write the Atlas database destination.

## Common Recovery Procedures

### No input-staging requests were found

Check, in order:

1. The command uses the intended database and stage configuration.
2. The source inventory scope was refreshed successfully.
3. The `--site` value matches the indexed source site.
4. The batch ID is present and current in `globus_file_index`.
5. The readiness view still considers the stage necessary.
6. The transfer route exists for the selected stage.

Do not infer readiness only from files visible in a shell. The planner uses the
SQLite inventory and readiness views.

### A transfer is stuck in `submitted` or `active`

Inspect the task directly:

```bash
globus task show <globus-task-id> --format json
```

Then rerun `poll_stage_inputs.py`. Confirm endpoint activation,
authentication, permissions, and source/destination paths if the task is not
progressing.

### A transfer failed or was canceled

Resolve the cause shown by Globus, then rerun `stage_inputs.py` with the same
stage and targeted batch. The existing terminal request is reopened for a new
attempt.

### `submit.py` skips a batch after staging

Confirm that polling wrote `completed`, not merely `submitted` or `active`.
Also confirm that the completed row uses the same stage and batch ID as the
compute command.

### A lease conflict is reported

Check whether a job for the batch and stage is already queued or running. Do
not remove the lease while its job may still be active. If no job exists, wait
for the lease to expire or follow the project's approved lease-recovery
procedure.

### `sbatch` failed

Correct the scheduler, account, partition, resource, or script problem. Because
the lease remains after an `sbatch` failure, coordinate lease recovery before
another submission attempt.

### The compute job found no staged files

Compare the transfer destination recorded during staging with
`paths.input_staging_root` and the stage route's `input_subdir`. Confirm that
the Globus task completed and that files are visible on the compute cluster.

### The job ran but readiness still returns the batch

Check:

- whether `run_report.json` was ingested successfully;
- whether the run status is `success`;
- whether outputs were promoted to the configured final destination; and
- whether the output storage scope has been inventoried since promotion.

## Targeted Batch Workflow

For a cautious one-batch operation:

```bash
python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --dry-run \
  --limit 1

python scripts/job/stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --limit 1

python scripts/job/poll_stage_inputs.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --wait \
  --interval 30 \
  --timeout 7200

python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --find-only \
  --limit 1

python scripts/job/submit.py \
  --stage <stage> \
  --config <stage-config.yaml> \
  --batches <batch-file.txt> \
  --limit 1
```

## End-of-Run Checklist

- [ ] Inventory completed for every required storage scope.
- [ ] Staging paths and endpoints were reviewed in dry-run output.
- [ ] Intended transfers reached `completed`.
- [ ] Compute-eligible batches were reviewed with `--find-only`.
- [ ] Submitted batch IDs and Slurm job IDs were retained.
- [ ] Slurm and pipeline logs were checked.
- [ ] Promoted outputs exist at the expected destination.
- [ ] Run artifacts include `run_report.json`.
- [ ] The run was ingested with `success` status and its lease released.
- [ ] Atlas result-sync requests were received by Ceres.
- [ ] Expected run bundles reached `ingested` in `result_syncs`.
- [ ] Output inventory was refreshed after completion.
- [ ] Successfully completed batches disappeared from the readiness view.

## Related Documentation

- [`SQLITE_ORCHESTRATOR_ARCHITECTURE.md`](SQLITE_ORCHESTRATOR_ARCHITECTURE.md)
- [`INPUT_STAGING_RUNBOOK.md`](INPUT_STAGING_RUNBOOK.md)
- [`GLOBUS_TRANSFER_NOTES.md`](GLOBUS_TRANSFER_NOTES.md)
- [`SLURM_JINJA_RENDERING_NOTES.md`](SLURM_JINJA_RENDERING_NOTES.md)
- [`SQLITE_TEMP_COPY_CONFIGURATION.md`](SQLITE_TEMP_COPY_CONFIGURATION.md)
