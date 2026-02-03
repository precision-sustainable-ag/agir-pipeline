#!/usr/bin/env bash
#
# AGIR Pipeline Setup Script (uv edition)
# Automates common setup tasks for development and HPC environments
#
# Usage:
#   ./setup.sh --dev          # Development setup (creates/reuses env; auto-detects HPC)
#   ./setup.sh --all          # Full install (creates/reuses env; auto-detects HPC)
#   ./setup.sh --hpc          # Force HPC/SciNet mode (HPC env + cache on /project)
#   ./setup.sh --local        # Force local mode (repo-local env)
#   ./setup.sh --schema-only  # Only apply database schemas
#   ./setup.sh --test-only    # Only run verification tests
#
# Recreate environment (non-interactive):
#   AGIR_RECREATE_ENV=1 ./setup.sh --dev
#   AGIR_RECREATE_ENV=1 ./setup.sh --hpc
#
# Notes:
# - Uses uv + pyproject.toml (no conda/mamba).
# - Auto-detects HPC and adjusts venv + UV cache locations.
# - On HPC, prefers /project/dash_agir/$USER for venv + UV_CACHE_DIR to avoid $HOME quotas.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -----------------------------
# Global defaults
# -----------------------------
ENV_NAME="agir_pipeline"

# uv binary install location (user-local, no sudo)
UV_BIN_DIR="${HOME}/.local/bin"
UV_BIN="${UV_BIN_DIR}/uv"

# Local (repo-local) env
LOCAL_VENV_PATH="${SCRIPT_DIR}/.venv"
LOCAL_CACHE_DIR="${SCRIPT_DIR}/.uv-cache"

# HPC (SciNet) env defaults (preferred)
HPC_PROJECT_ROOT="/project/dash_agir/${USER}"
HPC_VENV_ROOT="${HPC_PROJECT_ROOT}/software/uv/venvs"
HPC_VENV_PATH="${HPC_VENV_ROOT}/${ENV_NAME}"
HPC_CACHE_DIR="${HPC_PROJECT_ROOT}/uv-cache"

# Non-interactive recreate toggle
AGIR_RECREATE_ENV="${AGIR_RECREATE_ENV:-0}"

# Mode override: auto | hpc | local
AGIR_MODE="${AGIR_MODE:-auto}"

# Desired Python version for venvs
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PYTHON_SPEC="python${PYTHON_VERSION}"

# -----------------------------
# Logging helpers
# -----------------------------
log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# -----------------------------
# HPC detection + configuration
# -----------------------------
detect_hpc() {
    # Heuristics:
    # - SCINet often provides /project
    # - Your AGIR project root: /project/dash_agir
    # - HPC has module command
    # - Hostnames sometimes include ceres/atlas (not relied upon)
    if [ -d "/project/dash_agir" ]; then
        return 0
    fi
    if command -v module &>/dev/null && [ -d "/project" ]; then
        return 0
    fi
    return 1
}

configure_mode() {
    local mode="${1:-auto}"

    case "${mode}" in
        auto)
            if detect_hpc; then
                AGIR_MODE="hpc"
            else
                AGIR_MODE="local"
            fi
            ;;
        hpc|local)
            AGIR_MODE="${mode}"
            ;;
        *)
            log_error "Invalid AGIR_MODE: ${mode} (expected auto|hpc|local)"
            return 1
            ;;
    esac

    if [ "${AGIR_MODE}" = "hpc" ]; then
        VENV_PATH="${HPC_VENV_PATH}"
        CACHE_DIR="${HPC_CACHE_DIR}"
    else
        VENV_PATH="${LOCAL_VENV_PATH}"
        CACHE_DIR="${LOCAL_CACHE_DIR}"
    fi

    log_info "Mode: ${AGIR_MODE}"
    log_info "Venv path: ${VENV_PATH}"
    log_info "UV cache: ${CACHE_DIR}"
}

# -----------------------------
# Checks / utilities
# -----------------------------
ensure_python_for_uv() {
    # On local machines, we want the venv to be 3.12 even if system python isn't.
    # Prefer an existing python3.12, otherwise ask uv to install it.
    if command -v "${PYTHON_SPEC}" >/dev/null 2>&1; then
        log_info "Found ${PYTHON_SPEC} on PATH: $(command -v "${PYTHON_SPEC}")"
        return 0
    fi

    # If uv can manage Python, install it.
    log_warn "${PYTHON_SPEC} not found on PATH; attempting to install via uv..."
    uv python install "${PYTHON_VERSION}" || {
        log_error "Failed to install Python ${PYTHON_VERSION} via uv."
        log_error "Options:"
        log_error "  - Install python3.12 system-wide (brew/apt/pyenv), or"
        log_error "  - Ensure uv can download Python (network access), then retry."
        return 1
    }

    log_info "uv installed Python ${PYTHON_VERSION}"
}

ensure_uv() {
    # Ensure uv exists; install user-local if missing.
    if [ -x "${UV_BIN}" ]; then
        export PATH="${UV_BIN_DIR}:${PATH}"
        log_info "uv found: ${UV_BIN} ($( ${UV_BIN} --version 2>/dev/null || true ))"
        return 0
    fi

    if command -v uv >/dev/null 2>&1; then
        log_info "uv found on PATH: $(command -v uv) ($(uv --version))"
        return 0
    fi

    log_warn "uv not found; installing to ${UV_BIN_DIR} (no sudo)..."
    mkdir -p "${UV_BIN_DIR}"

    if ! command -v curl >/dev/null 2>&1; then
        log_error "curl not found; cannot install uv automatically."
        log_error "Install uv manually or ensure curl is available."
        return 1
    fi

    curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${UV_BIN_DIR}" UV_NO_MODIFY_PATH=1 sh

    if [ ! -x "${UV_BIN}" ]; then
        log_error "uv installation did not produce ${UV_BIN}"
        return 1
    fi

    export PATH="${UV_BIN_DIR}:${PATH}"
    log_info "uv installed: ${UV_BIN} ($(uv --version))"
}

set_uv_cache_dir() {
    local cache_dir="${1:-}"
    if [ -n "${cache_dir}" ]; then
        mkdir -p "${cache_dir}"
        export UV_CACHE_DIR="${cache_dir}"
        log_info "UV_CACHE_DIR set: ${UV_CACHE_DIR}"
    fi
}

maybe_load_hpc_modules() {
    # Only attempt module loads if in HPC mode.
    if [ "${AGIR_MODE}" != "hpc" ]; then
        return 0
    fi

    if ! command -v module &>/dev/null; then
        log_warn "module command not found (HPC mode). Continuing without module loads."
        return 0
    fi

    log_info "HPC mode: attempting module loads..."

    # PostgreSQL client
    module load postgresql || log_warn "Could not load postgresql module"
}

check_postgres() {
    log_info "Checking PostgreSQL access..."

    if [ -z "${PGHOST:-}" ]; then
        log_info "PGHOST not set."

        if [ "${AGIR_MODE}" = "hpc" ] && [ -f "/project/dash_agir/postgres/pg_coords.env" ]; then
            log_info "HPC mode; sourcing pg_coords.env for PGHOST..."
            # shellcheck disable=SC1091
            source /project/dash_agir/postgres/pg_coords.env
            if [ -n "${PGHOST:-}" ]; then
                log_info "PGHOST set from pg_coords.env: ${PGHOST}"
                # do not return 0 here; still need psql + connectivity
            fi
        else
            log_warn "PGHOST must be set manually."
        fi
    fi

    if ! command -v psql &>/dev/null; then
        log_warn "psql not found."
        if [ "${AGIR_MODE}" = "hpc" ]; then
            log_info "Attempting to load PostgreSQL module..."
            maybe_load_hpc_modules
        fi
    fi

    if ! command -v psql &>/dev/null; then
        log_warn "psql still not available. Install PostgreSQL client tools."
        return 1
    fi

    if [ -z "${PGHOST:-}" ]; then
        log_warn "PGHOST is still not set; skipping DB connect test."
        return 1
    fi

    if psql -c "SELECT 1;" &>/dev/null; then
        log_info "PostgreSQL connection OK"
        return 0
    else
        log_warn "Cannot connect to PostgreSQL. Check connection settings."
        return 1
    fi
}

# -----------------------------
# Environment setup (uv venv)
# -----------------------------
create_or_recreate_venv() {
    local venv_path="$1"

    ensure_uv

    # For local mode, make sure uv has Python 3.12 available (even if system python is older)
    if [ "${AGIR_MODE}" = "local" ]; then
        ensure_python_for_uv
        PYTHON_ARG="${PYTHON_VERSION}"
    else
        # On HPC you usually have python modules; still prefer 3.12 if available
        ensure_python_for_uv
        PYTHON_ARG="${PYTHON_VERSION}"
        if ! command -v "${PYTHON_ARG}" >/dev/null 2>&1; then
            # fallback to python3 if site provides it (but we'll still validate)
            PYTHON_ARG="python3"
        fi
    fi

    # If env exists, reuse unless forced recreate
    if [ -d "${venv_path}" ]; then
        log_info "venv already exists at: ${venv_path}"
        if [ "${AGIR_RECREATE_ENV}" = "1" ]; then
            log_warn "AGIR_RECREATE_ENV=1 set; recreating venv..."
            rm -rf "${venv_path}"
        fi
    fi

    if [ ! -d "${venv_path}" ]; then
        log_info "Creating uv venv at: ${venv_path} (Python: ${PYTHON_ARG})"
        uv venv --python "${PYTHON_ARG}" "${venv_path}" || {
            log_error "Failed to create venv with uv"
            return 1
        }
    fi

    # Activate for the rest of the script
    # shellcheck disable=SC1091
    source "${venv_path}/bin/activate"

    log_info "Activated venv: ${venv_path}"
    log_info "Python: $(which python)"
    log_info "Python version: $(python --version 2>&1)"

    # Hard assert the venv python is 3.12+
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" || {
        log_error "Venv Python is not 3.12+. Something went wrong."
        return 1
    }
}


setup_env_auto() {
    configure_mode "${AGIR_MODE}"
    maybe_load_hpc_modules
    set_uv_cache_dir "${CACHE_DIR}"
    create_or_recreate_venv "${VENV_PATH}" "python3"

    # Convenience: source pg_coords.env if present and in HPC mode
    if [ "${AGIR_MODE}" = "hpc" ] && [ -f "/project/dash_agir/postgres/pg_coords.env" ]; then
        log_info "HPC mode; sourcing pg_coords.env (if needed)..."
        # shellcheck disable=SC1091
        source /project/dash_agir/postgres/pg_coords.env
    fi
}

setup_dev_env() {
    log_info "Setting up development environment..."
    local LOG_DIR="${HOME}/.agir/logs"
    mkdir -p "${LOG_DIR}"
    log_info "Created log directory: ${LOG_DIR}"
}

# -----------------------------
# Install / verify (pyproject.toml)
# -----------------------------
is_agir_installed() {
    python - <<'EOF'
import importlib.util
import sys
spec = importlib.util.find_spec("agir_db")
sys.exit(0 if spec is not None else 1)
EOF
}

install_package() {
    log_info "Installing agir-pipeline (uv)..."
    cd "${SCRIPT_DIR}"

    if is_agir_installed; then
        log_info "agir-pipeline already importable in this environment"
        log_info "Reinstalling to ensure dependencies and entry points are up to date"
    else
        log_info "agir-pipeline not detected; performing fresh install"
    fi

    if [ "${1:-}" = "--dev" ]; then
        uv pip install -e ".[dev]"
    elif [ "${1:-}" = "--all" ]; then
        uv pip install -e ".[all]"
    else
        uv pip install -e .
    fi

    log_info "Installation step complete"
}

verify_installation() {
    log_info "Verifying installation..."

    if python -c "from agir_db import AgirDB; print('✓ AgirDB imported successfully')" 2>/dev/null; then
        log_info "Package verification OK"
        return 0
    else
        log_error "Package verification failed"
        return 1
    fi
}

# -----------------------------
# DB schemas / tests
# -----------------------------
apply_schemas() {
    log_info "Applying database schemas..."

    if ! check_postgres; then
        log_error "Cannot connect to database. Set PGHOST, PGPORT, PGDATABASE, PGUSER (or source pg_coords.env on HPC)."
        exit 1
    fi

    local SCHEMA_DIR="${SCRIPT_DIR}/schemas"

    log_info "Applying source schema..."
    psql -f "${SCHEMA_DIR}/sql/source.globus_file_index.sql" || {
        log_error "Failed to apply source schema"
        exit 1
    }

    log_info "Applying logs schemas..."
    psql -f "${SCHEMA_DIR}/sql/logs.transfer_requests.sql" || true

    if [ -f "${SCHEMA_DIR}/sql/logs.transfer_runs.sql" ]; then
        psql -f "${SCHEMA_DIR}/sql/logs.transfer_runs.sql" || true
    fi

    log_info "Applying report views..."
    psql -f "${SCHEMA_DIR}/views/report.missing_on_juno.sql" || {
        log_error "Failed to apply report views"
        exit 1
    }

    log_info "Schema application complete"
}

run_tests() {
    log_info "Running verification tests..."
    cd "${SCRIPT_DIR}"

    if [ -f "tests/test_p1.py" ]; then
        log_info "Running Phase 1 tests..."
        python tests/test_p1.py || {
            log_warn "Phase 1 tests failed"
            return 1
        }
    fi

    log_info "Tests complete"
}

# -----------------------------
# CLI / help
# -----------------------------
print_usage() {
    cat << EOF
AGIR Pipeline Setup Script (uv)

Usage:
    $0 [OPTIONS]

Options:
    --dev           Dev install (auto-detects HPC vs local unless overridden)
    --all           Full install (auto-detects HPC vs local unless overridden)
    --hpc           Force HPC/SciNet mode (venv + UV cache under /project/dash_agir/\$USER)
    --local         Force local mode (repo-local venv + cache)
    --schema-only   Only apply database schemas (requires DB connectivity)
    --test-only     Only run verification tests
    --help          Show this help message

Environment recreation:
    AGIR_RECREATE_ENV=1 $0 --dev
    AGIR_RECREATE_ENV=1 $0 --hpc

Mode override via env var:
    AGIR_MODE=auto|hpc|local

DB env vars:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

EOF
}

# -----------------------------
# Main
# -----------------------------
main() {
    echo "=========================================="
    echo "AGIR Pipeline Setup (uv)"
    echo "=========================================="
    echo ""

    case "${1:-}" in
        --hpc)
            AGIR_MODE="hpc"
            setup_dev_env
            setup_env_auto
            install_package
            verify_installation
            if check_postgres; then
                apply_schemas
            else
                log_warn "Database not running or not reachable."
                log_warn "If on SciNet, start with: sbatch server/db_server.sh"
            fi
            ;;

        --local)
            AGIR_MODE="local"
            setup_dev_env
            setup_env_auto
            install_package
            verify_installation
            if check_postgres; then
                apply_schemas
                run_tests
            else
                log_warn "Database not available. Skipping schema and tests."
            fi
            ;;

        --dev)
            log_info "Starting DEVELOPMENT setup..."
            setup_dev_env
            setup_env_auto
            install_package --dev
            verify_installation
            if check_postgres; then
                apply_schemas
                run_tests
            else
                log_warn "Database not available. Skipping schema and tests."
            fi
            ;;

        --all)
            log_info "Starting FULL installation (all dependencies)..."
            setup_dev_env
            setup_env_auto
            install_package --all
            verify_installation
            if check_postgres; then
                apply_schemas
                run_tests
            else
                log_warn "Database not available. Skipping schema and tests."
            fi
            ;;

        --schema-only)
            log_info "Applying database schemas only..."
            # Do NOT set up env here; assume user has psql + env vars
            apply_schemas
            ;;

        --test-only)
            log_info "Running tests only..."
            run_tests
            ;;

        --help)
            print_usage
            exit 0
            ;;

        *)
            log_error "Invalid option: ${1:-none}"
            echo ""
            print_usage
            exit 1
            ;;
    esac

    echo ""
    echo "=========================================="
    log_info "Setup complete!"
    echo "=========================================="
    echo ""
    echo "Next steps:"
    echo "  1. Activate your environment manually if needed:"
    echo "     # Current mode (${AGIR_MODE}):"
    echo "     source ${VENV_PATH}/bin/activate"
    echo "  2. Test DB connection (if configured):"
    echo "     python -c \"from agir_db import AgirDB; db=AgirDB(); db.connect(); print(db.is_connected)\""
    echo ""
}

main "$@"
