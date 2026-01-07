CREATE SCHEMA IF NOT EXISTS logs;

CREATE TABLE IF NOT EXISTS logs.transfer_runs (
  transfer_run_pk  BIGSERIAL PRIMARY KEY,
  transfer_run_id  UUID NOT NULL UNIQUE,

  transfer_request_id BIGINT NOT NULL REFERENCES ops.transfer_requests(transfer_request_id),

  batch_id      TEXT NOT NULL,
  data_state    TEXT NOT NULL,
  storage_domain TEXT NOT NULL,
  namespace     TEXT NOT NULL,

  from_site     TEXT NOT NULL,
  to_site       TEXT NOT NULL,

  -- resolved plan (what worker actually used)
  source_root   TEXT NOT NULL,
  dest_root     TEXT NOT NULL,

  status        TEXT NOT NULL CHECK (status IN ('created','submitted','active','succeeded','failed','canceled')),
  started_at    TIMESTAMPTZ NOT NULL,
  ended_at      TIMESTAMPTZ NULL,

  globus_task_id TEXT NULL,

  n_files_expected BIGINT NULL,
  bytes_expected   BIGINT NULL,
  n_files_done     BIGINT NULL,
  bytes_done       BIGINT NULL,

  error_summary TEXT NULL,

  -- keep the full report for forward-compat
  transfer_report_json JSONB NOT NULL,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transfer_runs_request_time
  ON logs.transfer_runs (transfer_request_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_transfer_runs_status_time
  ON logs.transfer_runs (status, started_at DESC);
