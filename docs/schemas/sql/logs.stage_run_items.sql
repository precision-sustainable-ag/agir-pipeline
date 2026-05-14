CREATE TABLE IF NOT EXISTS logs.stage_run_items (
    stage_run_item_pk BIGSERIAL PRIMARY KEY,

    run_id            UUID        NOT NULL REFERENCES logs.stage_runs(run_id) ON DELETE CASCADE,
    batch_id          TEXT        NOT NULL,
    stage             TEXT        NOT NULL,

    unit_id           TEXT        NOT NULL,
    kind              TEXT        NOT NULL CHECK (kind IN ('error', 'warning')),
    code              TEXT        NOT NULL,
    type              TEXT        NOT NULL,
    message           TEXT        NOT NULL,

    retryable         BOOLEAN     NULL,      -- NULL for warnings
    meta              JSONB       NOT NULL DEFAULT '{}'::jsonb,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stage_run_items_run
  ON logs.stage_run_items (run_id);

CREATE INDEX IF NOT EXISTS idx_stage_run_items_batch_stage_kind
  ON logs.stage_run_items (batch_id, stage, kind);

CREATE INDEX IF NOT EXISTS idx_stage_run_items_unit
  ON logs.stage_run_items (unit_id);
