CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS logs;

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

CREATE OR REPLACE FUNCTION ops.claim_stage_lease(
    p_batch_id TEXT,
    p_stage TEXT,
    p_orchestrator_id TEXT,
    p_ttl_seconds INTEGER,
    p_attempt INTEGER DEFAULT NULL
)
RETURNS TABLE (
    claimed BOOLEAN,
    lease_id UUID,
    batch_id TEXT,
    stage TEXT,
    expires_at TIMESTAMPTZ,
    attempt INTEGER,
    job_workdir_policy JSONB
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_ttl_seconds IS NULL OR p_ttl_seconds <= 0 THEN
        RAISE EXCEPTION 'ttl_seconds must be > 0';
    END IF;

    RETURN QUERY
    WITH upserted AS (
        INSERT INTO logs.stage_leases AS sl (
            lease_id, batch_id, stage, orchestrator_id,
            leased_at, expires_at, attempt, state,
            released_at, release_reason, updated_at
        )
        VALUES (
            ops.uuid_v4(), p_batch_id, p_stage, p_orchestrator_id,
            now(), now() + make_interval(secs => p_ttl_seconds), 1, 'active',
            NULL, NULL, now()
        )
        ON CONFLICT ON CONSTRAINT stage_leases_batch_stage_key DO UPDATE
        SET
            lease_id = ops.uuid_v4(),
            orchestrator_id = EXCLUDED.orchestrator_id,
            leased_at = now(),
            expires_at = now() + make_interval(secs => p_ttl_seconds),
            attempt = sl.attempt + 1,
            state = 'active',
            released_at = NULL,
            release_reason = NULL,
            updated_at = now()
        WHERE NOT (
            sl.state = 'active'
            AND sl.expires_at > now()
        )
        RETURNING
            TRUE AS claimed,
            sl.lease_id,
            sl.batch_id,
            sl.stage,
            sl.expires_at,
            sl.attempt
    )
    SELECT
        u.claimed,
        u.lease_id,
        u.batch_id,
        u.stage,
        u.expires_at,
        u.attempt,
        jsonb_build_object('use_tmp', true, 'tmp_root', '/tmp/agir') AS job_workdir_policy
    FROM upserted u;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            FALSE AS claimed,
            NULL::UUID AS lease_id,
            p_batch_id AS batch_id,
            p_stage AS stage,
            NULL::TIMESTAMPTZ AS expires_at,
            NULL::INTEGER AS attempt,
            jsonb_build_object('use_tmp', true, 'tmp_root', '/tmp/agir') AS job_workdir_policy;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ops.release_stage_lease(
    p_lease_id UUID,
    p_orchestrator_id TEXT,
    p_release_reason TEXT,
    p_released_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS TABLE (
    released BOOLEAN,
    lease_id UUID,
    released_at TIMESTAMPTZ,
    release_reason TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_release_reason IS NULL OR btrim(p_release_reason) = '' THEN
        RAISE EXCEPTION 'release_reason is required';
    END IF;

    RETURN QUERY
    WITH updated AS (
        UPDATE logs.stage_leases l
        SET
            state = 'released',
            released_at = COALESCE(p_released_at, now()),
            release_reason = p_release_reason,
            updated_at = now()
        WHERE l.lease_id = p_lease_id
          AND l.state = 'active'
          AND l.orchestrator_id = p_orchestrator_id
        RETURNING l.lease_id, l.released_at, l.release_reason
    )
    SELECT TRUE, u.lease_id, u.released_at, u.release_reason
    FROM updated u;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            FALSE AS released,
            p_lease_id AS lease_id,
            COALESCE(
                (SELECT l.released_at FROM logs.stage_leases l WHERE l.lease_id = p_lease_id),
                p_released_at
            ) AS released_at,
            COALESCE(
                (SELECT l.release_reason FROM logs.stage_leases l WHERE l.lease_id = p_lease_id),
                p_release_reason
            ) AS release_reason;
    END IF;
END;
$$;
