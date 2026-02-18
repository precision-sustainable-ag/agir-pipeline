CREATE SCHEMA IF NOT EXISTS logs;
CREATE SCHEMA IF NOT EXISTS agir_db;

-- Needed for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS logs.stage_leases (
    lease_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id         TEXT NOT NULL,
    stage            TEXT NOT NULL,
    orchestrator_id  TEXT NOT NULL,
    leased_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state            TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released')),
    released_at      TIMESTAMPTZ NULL,
    release_reason   TEXT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- one mutable lease row per (batch_id, stage) for minimal Phase 1
    CONSTRAINT stage_leases_batch_stage_key UNIQUE (batch_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_stage_leases_active_expiry
    ON logs.stage_leases (stage, state, expires_at);

CREATE INDEX IF NOT EXISTS idx_stage_leases_batch_stage
    ON logs.stage_leases (batch_id, stage);
