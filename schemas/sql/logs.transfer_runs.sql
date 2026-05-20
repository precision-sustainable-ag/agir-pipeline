CREATE SCHEMA IF NOT EXISTS logs;

-- Dev-safe reset; remove if need to preserve existing transfer_runs data
DROP TABLE IF EXISTS logs.transfer_runs CASCADE;

CREATE TABLE logs.transfer_runs (
  transfer_run_pk      BIGSERIAL PRIMARY KEY,
  transfer_id          UUID NOT NULL UNIQUE,

  direction            TEXT NOT NULL CHECK (direction IN ('input_stage', 'promotion')),
  batch_id             TEXT NOT NULL,
  stage                TEXT NOT NULL,
  window_key           TEXT NOT NULL,

  transfer_profile_id  TEXT NOT NULL,
  src_lts_ref          TEXT NOT NULL,
  dst_staging_ref      TEXT NOT NULL,

  priority             INTEGER NOT NULL DEFAULT 100,
  requested_by         TEXT NULL,
  dedupe_key           TEXT NULL,

  status               TEXT NOT NULL CHECK (status IN ('requested','active','completed','failed')),
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at           TIMESTAMPTZ NULL,
  ended_at             TIMESTAMPTZ NULL,
  error_summary        TEXT NULL,
  globus_task_id       TEXT NULL,
  globus_src_endpoint  TEXT NULL,
  globus_dst_endpoint  TEXT NULL,
  globus_label         TEXT NULL,
  submission_details   TEXT NULL,
  input_manifest_ref   TEXT NULL,
  staging_report_ref   TEXT NULL,
  globus_status_text   TEXT NULL,
  last_polled_at       TIMESTAMPTZ NULL,
  poll_attempts        INTEGER NOT NULL DEFAULT 0,

  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_transfer_runs_active_input
  ON logs.transfer_runs (direction, batch_id, stage, window_key, dst_staging_ref)
  WHERE status IN ('requested', 'active')
  AND dedupe_key IS NULL;

CREATE UNIQUE INDEX idx_transfer_runs_dedupe_key
  ON logs.transfer_runs (dedupe_key)
  WHERE dedupe_key IS NOT NULL
    AND status <> 'failed';

CREATE INDEX idx_transfer_runs_status_time
  ON logs.transfer_runs (status, requested_at DESC);

CREATE INDEX idx_transfer_runs_globus_task_id
  ON logs.transfer_runs (globus_task_id)
  WHERE globus_task_id IS NOT NULL;

CREATE INDEX idx_transfer_runs_completed_manifest
  ON logs.transfer_runs (batch_id, stage, window_key, requested_at DESC)
  WHERE direction = 'input_stage'
    AND status = 'completed'
    AND input_manifest_ref IS NOT NULL;
