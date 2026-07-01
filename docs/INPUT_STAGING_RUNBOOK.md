# Input Staging Runbook

This runbook covers the operator-facing commands for SQLite-backed input
staging:

- `scripts/job/stage_inputs.py` plans and submits input-staging transfers.
- `scripts/job/poll_stage_inputs.py` refreshes existing transfer status in
  SQLite.

The normal flow is:

```text
stage_inputs.py
  -> staged_inputs.status = submitted

poll_stage_inputs.py
  -> staged_inputs.status = active/completed/failed/canceled

submit.py
  -> later consumes rows where staged_inputs.status = completed
```

## Preview Staging Requests

Use `--dry-run` to inspect planned staging requests without writing SQLite and
without contacting Globus:

```bash
python scripts/job/stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --dry-run \
  --limit 10
```

For `jpg_to_det`:

```bash
python scripts/job/stage_inputs.py \
  --stage jpg_to_det \
  --config <jpg_to_det_config.yaml> \
  --dry-run \
  --limit 10
```

## Submit Staging Transfers

Run without `--dry-run` to record rows in `staged_inputs` and submit Globus
transfers:

```bash
python scripts/job/stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --limit 10
```

Successful submissions are stored as:

```text
staged_inputs.status = submitted
staged_inputs.globus_task_id = <task id>
```

`stage_inputs.py` does not poll transfer completion. Use
`poll_stage_inputs.py` for that.

## Stage Inputs Flags

`stage_inputs.py` supports:

```text
--stage
  Required. One of: raw_to_jpg, jpg_to_det.

--config
  Required. Stage config YAML.

--batches
  Optional path to a batch list file. Limits planning to those batch_ids.

--dry-run
  Print planned requests only. Does not write SQLite and does not call Globus.

--limit
  Maximum readiness rows to plan. Default: 50.

--site
  Site filter for the readiness query. Default: JUNO.

--requested-by
  Value stored in staged_inputs.requested_by. Default: stage_inputs.py.

--log-level
  One of: DEBUG, INFO, WARNING, ERROR. Default: INFO.
```

## Poll Once

Run polling once to refresh currently submitted or active transfer rows:

```bash
python scripts/job/poll_stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --limit 50
```

The poller queries rows like:

```sql
status IN ('submitted', 'active')
AND globus_task_id IS NOT NULL
```

For each row, it runs:

```bash
globus task show <task_id> --format json
```

Then it updates `staged_inputs`:

```text
SUCCEEDED -> completed
ACTIVE    -> active
FAILED    -> failed
CANCELED  -> canceled
```

## Wait Until Current Transfers Finish

Use `--wait` when you want the command to keep polling until there are no
submitted or active task-backed rows left for the stage:

```bash
python scripts/job/poll_stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --wait \
  --interval 30 \
  --timeout 7200
```

This exits when every pollable row has become terminal:

```text
completed / failed / canceled
```

If `--timeout` is reached, the command exits with status code `124`.

## Poll Forever

Use `--forever` for cron-like, service-like, or supervised operation:

```bash
python scripts/job/poll_stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --forever \
  --interval 60
```

This keeps polling until interrupted.

## Polling Flags

`poll_stage_inputs.py` supports:

```text
--stage
  Required. One of: raw_to_jpg, jpg_to_det.

--config
  Required. Stage config YAML.

--limit
  Maximum staged_inputs rows to poll per pass. Default: 50.

--wait
  Poll repeatedly until no submitted/active task-backed rows remain.

--forever
  Poll repeatedly until interrupted.

--interval
  Seconds between polling passes for --wait or --forever. Default: 60.

--timeout
  Maximum seconds to wait. Only valid with --wait.

--log-level
  One of: DEBUG, INFO, WARNING, ERROR. Default: INFO.
```

`--wait` and `--forever` are mutually exclusive.

## Typical Operator Sequence

```bash
python scripts/job/stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --dry-run \
  --limit 10

python scripts/job/stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --limit 10

python scripts/job/poll_stage_inputs.py \
  --stage raw_to_jpg \
  --config <raw_to_jpg_config.yaml> \
  --wait \
  --interval 30 \
  --timeout 7200
```
