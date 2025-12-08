CREATE SCHEMA IF NOT EXISTS logs;

CREATE TABLE IF NOT EXISTS logs.juno_transfers (
    id                BIGSERIAL PRIMARY KEY,
    batch_id          TEXT NOT NULL,
    endpoint          TEXT NOT NULL,
    location          TEXT,
    lts_root          TEXT,
    root_path         TEXT NOT NULL,
    data_state        TEXT NOT NULL,
    source_dir        TEXT NOT NULL,
    destination_dir   TEXT NOT NULL,
    transfer_time     TIMESTAMPTZ DEFAULT now(),
    status            TEXT,               -- e.g. submitted, dry_run, failed
    error_message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_juno_transfers_batch_id
    ON logs.juno_transfers(batch_id);

CREATE INDEX IF NOT EXISTS idx_juno_transfers_status
    ON logs.juno_transfers(status);
