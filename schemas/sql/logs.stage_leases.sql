CREATE SCHEMA IF NOT EXISTS logs;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE OR REPLACE FUNCTION ops.uuid_v4()
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_uuid UUID;
BEGIN
    IF to_regprocedure('gen_random_uuid()') IS NOT NULL THEN
        EXECUTE 'SELECT gen_random_uuid()' INTO v_uuid;
        RETURN v_uuid;
    END IF;
    IF to_regprocedure('uuid_generate_v4()') IS NOT NULL THEN
        EXECUTE 'SELECT uuid_generate_v4()' INTO v_uuid;
        RETURN v_uuid;
    END IF;
    RAISE EXCEPTION 'No UUID generator available (pgcrypto/uuid-ossp missing). Provide UUIDs from the application layer.';
END;
$$;

CREATE TABLE IF NOT EXISTS logs.stage_leases (
    lease_id         UUID PRIMARY KEY DEFAULT ops.uuid_v4(),
    batch_id         TEXT NOT NULL,
    stage            TEXT NOT NULL,
    window_key TEXT NOT NULL DEFAULT '',
    orchestrator_id  TEXT NOT NULL,
    leased_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state            TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released')),
    released_at      TIMESTAMPTZ NULL,
    release_reason   TEXT NULL,
    slurm_job_id     TEXT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- one mutable lease row per (batch_id, stage, window_key).
    CONSTRAINT stage_leases_batch_stage_key UNIQUE (batch_id, stage, window_key)
);

CREATE INDEX IF NOT EXISTS idx_stage_leases_active_expiry
    ON logs.stage_leases (stage, state, expires_at);

CREATE INDEX IF NOT EXISTS idx_stage_leases_batch_stage
    ON logs.stage_leases (batch_id, stage);
