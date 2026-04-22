#!/usr/bin/env bash
#
# AGIR Pipeline Setup Script (uv edition)
#
# Usage:
#   ./setup.sh --dev | --all
#   ./setup.sh --hpc --dev
#   ./setup.sh --local --all
#   ./setup.sh --schema-only
#   ./setup.sh --test-only
#
# Recreate env (non-interactive):
#   AGIR_RECREATE_ENV=1 ./setup.sh --dev
#
# Exit on command errors, unset variables, and pipeline failures.
set -euo pipefail

# -----------------------------
# Defaults
# -----------------------------
ENV_NAME="${ENV_NAME:-agir_pipeline}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
AGIR_MODE="${AGIR_MODE:-auto}"            # auto|hpc|local
AGIR_RECREATE_ENV="${AGIR_RECREATE_ENV:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UV_BIN_DIR="${HOME}/.local/bin"
UV_BIN="${UV_BIN_DIR}/uv"

LOCAL_ROOT="/home/${USER}"
HPC_ROOT="/project/dash_agir/${USER}"

LOCAL_VENV="${LOCAL_ROOT}/software/uv/venvs/${ENV_NAME}"
HPC_VENV="${HPC_ROOT}/software/uv/venvs/${ENV_NAME}"

LOCAL_CACHE="${LOCAL_ROOT}/uv-cache"
HPC_CACHE="${HPC_ROOT}/uv-cache"

# -----------------------------
# Logging
# -----------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()      { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()     { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()      { echo -e "${RED}[ERROR]${NC} $*"; }

die() { err "$*"; exit 1; }

# -----------------------------
# Mode detection / config
# -----------------------------
detect_hpc() {
  [[ -d "/project/dash_agir" ]] && return 0
  command -v module &>/dev/null && [[ -d "/project" ]] && return 0
  return 1
}

configure_mode() {
  case "${AGIR_MODE}" in
    auto)
      if detect_hpc; then AGIR_MODE="hpc"; else AGIR_MODE="local"; fi
      ;;
    hpc|local) ;;
    *) die "Invalid AGIR_MODE=${AGIR_MODE} (expected auto|hpc|local)";;
  esac

  if [[ "${AGIR_MODE}" == "hpc" ]]; then
    VENV_PATH="${HPC_VENV}"
    CACHE_DIR="${HPC_CACHE}"
  else
    VENV_PATH="${LOCAL_VENV}"
    CACHE_DIR="${LOCAL_CACHE}"
  fi

  log "Mode: ${AGIR_MODE}"
  log "Venv: ${VENV_PATH}"
  log "UV cache: ${CACHE_DIR}"
}

# -----------------------------
# uv / python / venv
# -----------------------------
ensure_uv() {
  if [[ -x "${UV_BIN}" ]]; then
    export PATH="${UV_BIN_DIR}:${PATH}"
    return 0
  fi
  if command -v uv &>/dev/null; then
    return 0
  fi

  command -v curl &>/dev/null || die "curl not found; cannot install uv automatically."
  mkdir -p "${UV_BIN_DIR}"
  log "Installing uv -> ${UV_BIN_DIR} (no sudo)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="${UV_BIN_DIR}" UV_NO_MODIFY_PATH=1 sh
  [[ -x "${UV_BIN}" ]] || die "uv install failed: ${UV_BIN} not found"
  export PATH="${UV_BIN_DIR}:${PATH}"
}

ensure_uv_python() {
  # Prefer system python${PYTHON_VERSION}; otherwise let uv install it
  if command -v "python${PYTHON_VERSION}" &>/dev/null; then
    return 0
  fi
  warn "python${PYTHON_VERSION} not found; trying: uv python install ${PYTHON_VERSION}"
  uv python install "${PYTHON_VERSION}" || die "Failed to install Python ${PYTHON_VERSION} via uv"
}

activate_or_create_venv() {
  ensure_uv
  ensure_uv_python

  mkdir -p "${CACHE_DIR}"
  export UV_CACHE_DIR="${CACHE_DIR}"

  if [[ -d "${VENV_PATH}" && "${AGIR_RECREATE_ENV}" == "1" ]]; then
    warn "AGIR_RECREATE_ENV=1 -> removing ${VENV_PATH}"
    rm -rf "${VENV_PATH}"
  fi

  if [[ ! -d "${VENV_PATH}" ]]; then
    log "Creating venv (Python ${PYTHON_VERSION}) -> ${VENV_PATH}"
    uv venv --python "${PYTHON_VERSION}" "${VENV_PATH}" || die "uv venv failed"
  else
    log "Reusing existing venv -> ${VENV_PATH}"
  fi

  # shellcheck disable=SC1091
  source "${VENV_PATH}/bin/activate"
  python -c "import sys; assert sys.version_info >= (3,12), sys.version" \
    || die "Venv python is not 3.12+ (got: $(python --version 2>&1))"
}

# -----------------------------
# Install / verify
# -----------------------------
install_agir() {
  local extras="${1:-}"  # "", "dev", "all"

  cd "${SCRIPT_DIR}"

  if [[ -n "${extras}" ]]; then
    log "Installing editable: .[${extras}]"
    uv pip install -e ".[${extras}]"
  else
    log "Installing editable: ."
    uv pip install -e .
  fi
}

verify_agir() {
  python -c "from agir_db import AgirDB; print('✓ AgirDB import OK')" \
    || die "Package import failed (agir_db)"
}

# -----------------------------
# DB helpers
# -----------------------------
maybe_load_hpc_modules() {
  [[ "${AGIR_MODE}" == "hpc" ]] || return 0
  command -v module &>/dev/null || return 0
  module load postgresql || true
}

source_pg_coords_if_present() {
  if [[ "${AGIR_MODE}" == "hpc" && -f "/project/dash_agir/postgres/pg_coords.env" ]]; then
    # shellcheck disable=SC1091
    source /project/dash_agir/postgres/pg_coords.env
  elif [[ -f "${SCRIPT_DIR}/pg_coords.env" ]]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/pg_coords.env" || true
  fi
}

check_db() {
  source_pg_coords_if_present
  maybe_load_hpc_modules

  command -v psql &>/dev/null || return 1
  [[ -n "${PGHOST:-}" ]] || return 1
  psql -c "SELECT 1;" &>/dev/null
}

apply_schemas() {
  local SCHEMA_DIR="${SCRIPT_DIR}/schemas"
  [[ -d "${SCHEMA_DIR}" ]] || die "Missing schema dir: ${SCHEMA_DIR}"

  check_db || die "DB not reachable. Set PG* env vars or source pg_coords.env; ensure psql exists."

  log "Applying schemas..."
  psql -f "${SCHEMA_DIR}/sql/source.globus_file_index.sql" || die "Failed: source schema"
  psql -f "${SCHEMA_DIR}/sql/logs.transfer_requests.sql" || die "Failed: logs.transfer_requests"
  psql -f "${SCHEMA_DIR}/sql/logs.transfer_runs.sql" || die "Failed: logs.transfer_runs"
  psql -f "${SCHEMA_DIR}/sql/logs.stage_leases.sql" || die "Failed: logs.stage_leases"
  psql -f "${SCHEMA_DIR}/sql/logs.stage_runs.sql" || die "Failed: logs.stage_runs"
  psql -f "${SCHEMA_DIR}/sql/ops.orchestrator.sql" || die "Failed: ops orchestrator funcs"
  psql -f "${SCHEMA_DIR}/views/report.missing_on_juno.sql" || die "Failed: report views"
  psql -f "${SCHEMA_DIR}/views/report.batches_needing_input_staging.sql" || die "Failed: report.batches_needing_input_staging"
  psql -f "${SCHEMA_DIR}/views/report.ready_work.sql" || die "Failed: report.ready_work"
}

run_tests() {
  cd "${SCRIPT_DIR}"
  [[ -f "tests/test_p1.py" ]] || { warn "No tests/test_p1.py found; skipping"; return 0; }
  log "Running tests/test_p1.py"
  python tests/test_p1.py
}

# -----------------------------
# CLI
# -----------------------------
usage() {
  cat <<EOF
AGIR Pipeline Setup (uv)

Options:
  --dev            Install with dev extras
  --all            Install with all extras
  --hpc            Force HPC mode
  --local          Force local mode
  --schema-only    Apply DB schemas only (no venv/install)
  --test-only      Run tests only (assumes env already active)
  --help

Env vars:
  AGIR_MODE=auto|hpc|local
  AGIR_RECREATE_ENV=1
  PYTHON_VERSION=3.12
  PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
EOF
}

# -----------------------------
# Main
# -----------------------------
main() {
  local install_extras=""     # "", "dev", "all"
  local do_install=1
  local do_verify=1
  local do_schemas=1
  local do_tests=1
  local schema_only=0
  local test_only=0

  # Parse args (order-independent)
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dev)  install_extras="dev" ;;
      --all)  install_extras="all" ;;
      --hpc)  AGIR_MODE="hpc" ;;
      --local) AGIR_MODE="local" ;;
      --schema-only) schema_only=1; do_install=0; do_verify=0; do_tests=0 ;;
      --test-only)   test_only=1; do_install=0; do_verify=0; do_schemas=0 ;;
      --help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
    shift
  done

  echo "=========================================="
  echo "AGIR Pipeline Setup (uv)"
  echo "=========================================="

  configure_mode

  if [[ "${schema_only}" == "1" ]]; then
    apply_schemas
    log "Done."
    exit 0
  fi

  if [[ "${test_only}" == "1" ]]; then
    run_tests
    log "Done."
    exit 0
  fi

  # Normal path
  activate_or_create_venv

  if [[ "${do_install}" == "1" ]]; then
    install_agir "${install_extras}"
  fi
  if [[ "${do_verify}" == "1" ]]; then
    verify_agir
  fi

  if [[ "${do_schemas}" == "1" ]]; then
    if check_db; then
      apply_schemas
    else
      warn "DB not reachable; skipping schema apply."
      warn "If on SciNet: start DB with your sbatch workflow, then rerun --schema-only."
    fi
  fi

  if [[ "${do_tests}" == "1" ]]; then
    if check_db; then
      run_tests || warn "Tests failed"
    else
      warn "DB not reachable; skipping tests."
    fi
  fi

  echo ""
  log "Setup complete!"
  echo ""
  echo "Sanity check:"
  echo "python -c 'from agir_db import AgirDB; db=AgirDB(); db.connect(); print(db.is_connected)'"
  echo ""
  echo "Next:"
  echo "  source ${VENV_PATH}/bin/activate"
  
}

main "$@"
