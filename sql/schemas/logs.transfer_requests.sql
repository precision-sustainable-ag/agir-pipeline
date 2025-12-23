CREATE SCHEMA IF NOT EXISTS logs;

CREATE TABLE IF NOT EXISTS logs.transfer_requests (
  transfer_request_id BIGSERIAL PRIMARY KEY,

  -- what we want transferred
  batch_id      TEXT NOT NULL,
  data_state    TEXT NOT NULL,
  storage_domain TEXT NOT NULL,
  namespace     TEXT NOT NULL,

  from_site     TEXT NOT NULL DEFAULT 'NCSU',
  to_site       TEXT NOT NULL DEFAULT 'JUNO',

  priority      INTEGER NOT NULL DEFAULT 100,
  is_enabled    BOOLEAN NOT NULL DEFAULT true,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- leasing (to prevent two workers doing same thing)
  leased_until  TIMESTAMPTZ NULL,
  leased_by     TEXT NULL,

  -- optional: human reason / ticket
  notes         TEXT NULL,

  UNIQUE (batch_id, data_state, storage_domain, namespace, from_site, to_site)
);

CREATE INDEX IF NOT EXISTS idx_transfer_requests_enabled_priority
  ON logs.transfer_requests (is_enabled, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_transfer_requests_lease
  ON logs.transfer_requests (leased_until);