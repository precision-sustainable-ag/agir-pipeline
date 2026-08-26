#!/usr/bin/env bash
#
# AGIR Pipeline Setup Script (uv edition)
#
# Usage:
#   ./setup.sh --dev | --all
#   ./setup.sh --hpc --dev
#   ./setup.sh --local --all
#   AGIR_SQLITE_DB=/path/to/pipeline.sqlite3 ./setup.sh --schema-only
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
install_pipeline() {
  local extras="${1:-}"  # "", "dev", "all"

  cd "${SCRIPT_DIR}"

  if [[ -n "${extras}" ]]; then
    log "Installing editable: .[${extras}]"
    uv pip install -e ".[${extras}]"
  else
    log "Installing editable: ."
    uv pip install -e .["all"]
  fi
}

verify_pipeline() {
  python -c "from orchestrator.config import load_stage_config; print('✓ orchestrator import OK')" \
    || die "Package import failed (orchestrator)"
  python -c "from orchestrator.sqlite_db import open_db; print('✓ SQLite orchestration import OK')" \
    || die "Package import failed (orchestrator.sqlite_db)"
}

# -----------------------------
# DB helpers
# -----------------------------
apply_schema() {
  local schema_file="${SCRIPT_DIR}/schemas/sqlite/pipeline.sql"
  local db_path="${AGIR_SQLITE_DB:-}"

  [[ -f "${schema_file}" ]] || die "Missing SQLite schema: ${schema_file}"
  [[ -n "${db_path}" ]] || die "Set AGIR_SQLITE_DB to the target SQLite database path."
  command -v python3 &>/dev/null || die "python3 is required to apply the SQLite schema."

  log "Applying SQLite schema: ${schema_file} -> ${db_path}"
  python3 -c '
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1]).expanduser()
schema_path = Path(sys.argv[2])
db_path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(db_path) as conn:
    conn.executescript(schema_path.read_text(encoding="utf-8"))
' "${db_path}" "${schema_file}" || die "Failed to apply SQLite schema."
}

run_tests() {
  cd "${SCRIPT_DIR}"
  log "Running environment and SQLite schema smoke tests"
  python tests/test_env.py
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
  --schema-only    Apply the SQLite schema only (no venv/install)
  --test-only      Run tests only (assumes env already active)
  --help

Env vars:
  AGIR_MODE=auto|hpc|local
  AGIR_RECREATE_ENV=1
  AGIR_SQLITE_DB=/path/to/pipeline.sqlite3
  PYTHON_VERSION=3.12
EOF
}

# -----------------------------
# Main
# -----------------------------
main() {
  local install_extras=""     # "", "dev", "all"
  local do_install=1
  local do_verify=1
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
      --test-only)   test_only=1; do_install=0; do_verify=0 ;;
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
    apply_schema
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
    install_pipeline "${install_extras}"
  fi
  if [[ "${do_verify}" == "1" ]]; then
    verify_pipeline
  fi

  if [[ "${do_tests}" == "1" ]]; then
    run_tests || warn "Tests failed"
  fi

  echo ""
  log "Setup complete!"
  echo ""
  echo "Sanity check:"
  echo "python tests/test_env.py"
  echo ""
  echo "Next:"
  echo "  source ${VENV_PATH}/bin/activate"
  echo "  python scripts/job/submit.py --help"
}

main "$@"
