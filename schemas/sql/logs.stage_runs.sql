CREATE SCHEMA IF NOT EXISTS logs;

CREATE TABLE IF NOT EXISTS logs.stage_runs (
    run_id           UUID PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    stage            TEXT NOT NULL,
    window_key       TEXT NOT NULL DEFAULT '',
    attempt          INTEGER NOT NULL CHECK (attempt >= 1),
    status           TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    exit_code        INTEGER NULL,
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ NOT NULL,
    run_report_ref   TEXT NOT NULL,
    output_ref       TEXT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_stage_runs_batch_stage_time
    ON logs.stage_runs (batch_id, stage, ended_at DESC);

CREATE INDEX IF NOT EXISTS idx_stage_runs_stage_status_time
    ON logs.stage_runs (stage, status, ended_at DESC);

CREATE INDEX IF NOT EXISTS idx_stage_runs_batch_stage_window
    ON logs.stage_runs (batch_id, stage, window_key);