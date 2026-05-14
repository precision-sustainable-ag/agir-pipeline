#!/usr/bin/env bash
set -euo pipefail

SCHEMA_DIR="schemas"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

run_sql() {
  local file="$1"
  echo "Running: $file"
  psql -v ON_ERROR_STOP=1 -f "$file" || die "Failed: $file"
}

drop_dev_objects() {
  echo "Dropping dev objects..."

  psql -v ON_ERROR_STOP=1 <<'SQL' || die "Failed dropping dev objects"

-- Drop views first
DROP VIEW IF EXISTS report.ready_work;
DROP VIEW IF EXISTS report.batches_needing_input_staging;
DROP VIEW IF EXISTS report.missing_on_juno;

-- Drop functions next if they may block table changes
DROP FUNCTION IF EXISTS ops.claim_stage_lease(text, text, integer);
DROP FUNCTION IF EXISTS ops.release_stage_lease(uuid, text, text);
DROP FUNCTION IF EXISTS ops.heartbeat_stage_lease(uuid);
DROP FUNCTION IF EXISTS ops.start_stage_run(uuid, text, text, jsonb);
DROP FUNCTION IF EXISTS ops.finish_stage_run(uuid, text, jsonb);

-- Drop tables
DROP TABLE IF EXISTS logs.stage_runs;
DROP TABLE IF EXISTS logs.stage_leases;
DROP TABLE IF EXISTS logs.transfer_runs;
DROP TABLE IF EXISTS logs.transfer_requests;

SQL
}

create_dev_objects() {
  echo "Recreating dev objects..."

  run_sql "${SCHEMA_DIR}/sql/logs.transfer_requests.sql"
  run_sql "${SCHEMA_DIR}/sql/logs.transfer_runs.sql"
  run_sql "${SCHEMA_DIR}/sql/logs.stage_leases.sql"
  run_sql "${SCHEMA_DIR}/sql/logs.stage_runs.sql"
  run_sql "${SCHEMA_DIR}/sql/ops.orchestrator.sql"
  run_sql "${SCHEMA_DIR}/views/report.missing_on_juno.sql"
  run_sql "${SCHEMA_DIR}/views/report.batches_needing_input_staging.sql"
  run_sql "${SCHEMA_DIR}/views/report.ready_work.sql"
}

main() {
  drop_dev_objects
  create_dev_objects
  echo "Done."
}

main "$@"