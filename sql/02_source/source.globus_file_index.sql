CREATE SCHEMA IF NOT EXISTS source;

CREATE TABLE IF NOT EXISTS source.globus_file_index (
    file_id           BIGSERIAL PRIMARY KEY,

    endpoint          TEXT NOT NULL,
    location          TEXT NOT NULL,
    lts_root          TEXT NOT NULL,
    root_path         TEXT NOT NULL,
    rel_path          TEXT NOT NULL,
    file_name         TEXT NOT NULL,

    entry_type        TEXT NOT NULL,
    file_ext          TEXT,
    size_bytes        BIGINT,
    checksum          TEXT,

    batch_id          TEXT,
    batch_state       TEXT,
    batch_date        DATE,

    data_state        TEXT NOT NULL,

    mtime_iso         TIMESTAMPTZ,
    fname_ts_epoch    BIGINT,
    fname_ts_iso      TIMESTAMPTZ,
    created_at_ts_iso  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_source_globus_unique
ON source.globus_file_index(endpoint, data_state, root_path, rel_path);