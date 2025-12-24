CREATE SCHEMA IF NOT EXISTS logs;

CREATE TABLE IF NOT EXISTS logs.stage_runs (
    stage_run_pk      BIGSERIAL PRIMARY KEY,

    -- Identity
    run_id            UUID        NOT NULL UNIQUE,
    pipeline_run_id   UUID        NULL,
    stage             TEXT        NOT NULL,
    stage_version     TEXT        NOT NULL,
    run_report_version TEXT       NOT NULL DEFAULT '1.0',

    -- Work scope
    batch_id          TEXT        NOT NULL,
    scope             TEXT        NOT NULL CHECK (scope IN ('batch', 'image')),

    -- Timing / status
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ NOT NULL,
    duration_ms       BIGINT      NOT NULL CHECK (duration_ms >= 0),
    exit_code         INTEGER     NOT NULL CHECK (exit_code >= 0),
    status            TEXT        NOT NULL CHECK (status IN ('success','partial_success','failed','canceled','skipped')),

    -- Provenance (high-value for reproducibility)
    code_commit       TEXT        NOT NULL,
    build_id          TEXT        NULL,
    config_path       TEXT        NOT NULL,
    config_hash       TEXT        NOT NULL,
    model_id          TEXT        NOT NULL,
    deps_id           TEXT        NOT NULL,
    container_image   TEXT        NULL,

    -- Inputs summary
    input_root        TEXT        NOT NULL,
    n_units_discovered INTEGER    NOT NULL CHECK (n_units_discovered >= 0),
    unit_id_kind      TEXT        NOT NULL,
    inputs_manifest_path TEXT     NULL,

    -- Outputs summary
    output_root       TEXT        NOT NULL,
    run_root          TEXT        NOT NULL,
    artifacts_dir     TEXT        NOT NULL,
    manifest_path     TEXT        NULL,
    outputs_schema_version INTEGER NOT NULL CHECK (outputs_schema_version >= 1),

    n_units_succeeded INTEGER     NOT NULL CHECK (n_units_succeeded >= 0),
    n_units_failed    INTEGER     NOT NULL CHECK (n_units_failed >= 0),
    n_units_skipped   INTEGER     NOT NULL CHECK (n_units_skipped >= 0),

    -- Pointers for large payloads + optional raw retention
    errors_path       TEXT        NULL,
    warnings_path     TEXT        NULL,
    logs_path         TEXT        NULL,

    -- Debug / orchestration
    orchestrator_id   TEXT        NULL,

    -- Raw JSON for forward-compat (optional but I strongly recommend it)
    run_report_json   JSONB       NOT NULL,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Basic sanity
    CHECK (ended_at >= started_at)
);

-- Helpful query patterns
CREATE INDEX IF NOT EXISTS idx_stage_runs_batch_stage_time
  ON logs.stage_runs (batch_id, stage, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_stage_runs_stage_status_time
  ON logs.stage_runs (stage, status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_stage_runs_created_at
  ON logs.stage_runs (created_at DESC);
