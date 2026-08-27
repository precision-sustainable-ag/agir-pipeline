#!/usr/bin/env bash
#SBATCH --job-name=globus_index_sqlite
#SBATCH --account=dash_agir
#SBATCH --time=20:00:00
#SBATCH --cpus-per-task=12
#SBATCH --mem=128G
#SBATCH --output=/project/dash_agir/logs/weekly_globus_sqlite/agir_%x_%j.out.log
#SBATCH --error=/project/dash_agir/logs/weekly_globus_sqlite/agir_%x_%j.err.log

set -Euo pipefail

# SQLite Globus inventory wrapper.
# Uses a YAML endpoint configuration file instead of a long Bash array.
# The Python script now tracks inventory runs, marks stale rows, and rebuilds summaries.

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/project/dash_agir/logs/weekly_globus_sqlite"
mkdir -p "${LOG_DIR}"
MAIN_LOG="${LOG_DIR}/agir_${TIMESTAMP}.log"
exec > >(tee -a "${MAIN_LOG}") 2>&1

log_error() {
    local exit_code=$?
    echo "[ERROR] Exit=${exit_code} at line ${BASH_LINENO[0]}: ${BASH_COMMAND}"
}
trap log_error ERR

RUNNING_COUNT=$(squeue -u "$USER" -n globus_index_sqlite -h | wc -l)

if [ "${RUNNING_COUNT}" -gt 1 ]; then
    echo "[WARN] Another globus_index_sqlite job is already running. Exiting to avoid overlap."
    exit 0
fi

on_exit() {
    local exit_code=$?
    echo "[INFO] Script exiting with code ${exit_code} at $(date)"
}
trap on_exit EXIT

echo "[INFO] SQLite Globus Indexing Job started on $(hostname) at $(date)"
echo "[INFO] Main log: ${MAIN_LOG}"

# ---- Environment ----
# Load whatever your system requires for globus CLI and Python.
# module load globus-cli
source /project/dash_agir/matthew.kutugata/repos/agir-pipeline/.venv/bin/activate

# ---- Paths ----
REPO_DIR="/project/dash_agir/matthew.kutugata/repos/agir-pipeline"
PYTHON_SCRIPT="${REPO_DIR}/scripts/admin/globus_index.py"
SPECIES_REFERENCE_SCRIPT="${REPO_DIR}/scripts/admin/load_species_reference.py"
ENDPOINT_CONFIG_YAML="${REPO_DIR}/configs/globus_endpoint_config.example.yaml"
SQLITE_DB="/project/dash_agir/globus_index/globus_file_index.sqlite3"

# ---- Tuning ----
MAX_WORKERS=12
BATCH_SIZE=10000

mkdir -p "$(dirname "${SQLITE_DB}")"

if [ ! -f "${PYTHON_SCRIPT}" ]; then
    echo "[ERROR] Python script not found: ${PYTHON_SCRIPT}"
    exit 1
fi

if [ ! -f "${ENDPOINT_CONFIG_YAML}" ]; then
    echo "[ERROR] Endpoint config YAML not found: ${ENDPOINT_CONFIG_YAML}"
    echo "[ERROR] Copy globus_endpoint_config.example.yaml to this path and enable the entries you want."
    exit 1
fi

if ! command -v globus >/dev/null 2>&1; then
    echo "[ERROR] globus CLI not found in PATH. Load the correct module/environment first."
    exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "[WARN] sqlite3 CLI not found in PATH. Python can still write the DB, but final CLI checks will be skipped."
fi

echo "========================================"
echo "SQLite Globus Indexing Started"
echo "Timestamp: $(date)"
echo "SQLite DB: ${SQLITE_DB}"
echo "Endpoint config: ${ENDPOINT_CONFIG_YAML}"
echo "========================================"

python3 "${PYTHON_SCRIPT}" \
    --db "${SQLITE_DB}" \
    --endpoint-config-yaml "${ENDPOINT_CONFIG_YAML}" \
    --batch-size "${BATCH_SIZE}" \
    --max-workers "${MAX_WORKERS}" \
    --optimize-at-end \
    --log-file "${LOG_DIR}/globus_index_sqlite_${TIMESTAMP}.log"

if command -v sqlite3 >/dev/null 2>&1; then
    echo "[INFO] Latest inventory runs (most recent run per site/scope):"
    sqlite3 -header -column "${SQLITE_DB}" \
        "SELECT ir.run_id, ir.site, ir.namespace, ir.storage_root, ir.data_state, ir.status, ir.total_seen, ir.total_marked_stale, ir.started_at_ts_iso, ir.ended_at_ts_iso
         FROM inventory_runs ir
         JOIN (
             SELECT site, storage_domain, namespace, storage_root, data_state, MAX(run_id) AS run_id
             FROM inventory_runs
             GROUP BY site, storage_domain, namespace, storage_root, data_state
         ) latest USING (site, storage_domain, namespace, storage_root, data_state, run_id)
         ORDER BY ir.site, ir.namespace, ir.storage_root, ir.data_state;"

    echo "[INFO] Current file counts by site/root/state:"
    sqlite3 -header -column "${SQLITE_DB}" \
        "SELECT site, namespace, storage_root, data_state, COUNT(*) AS current_rows FROM globus_file_index WHERE is_current = 1 GROUP BY site, namespace, storage_root, data_state ORDER BY site, namespace, storage_root, data_state;"
fi

echo "========================================"
echo "SQLite Globus Indexing Complete"
echo "Timestamp: $(date)"
echo "SQLite DB: ${SQLITE_DB}"
echo "========================================"

# species/cultivar reference data
echo "========================================"
echo "Species Reference Reload Started"
echo "Timestamp: $(date)"
echo "========================================"

if [ -f "${SPECIES_REFERENCE_SCRIPT}" ]; then
    python3 "${SPECIES_REFERENCE_SCRIPT}" --db "${SQLITE_DB}"

    if command -v sqlite3 >/dev/null 2>&1; then
        echo "[INFO] Current reference-table counts:"
        sqlite3 -header -column "${SQLITE_DB}" \
            "SELECT (SELECT COUNT(*) FROM species) AS species, (SELECT COUNT(*) FROM cultivars) AS cultivars, (SELECT COUNT(*) FROM color_palette) AS color_palette;"
    fi
else
    echo "[WARN] Species reference script not found: ${SPECIES_REFERENCE_SCRIPT}. Skipping."
fi

echo "========================================"
echo "Species Reference Reload Complete"
echo "Timestamp: $(date)"
echo "========================================"

find "${LOG_DIR}" -name "*.log" -type f -mtime +30 -delete

# Resubmit this job for tomorrow at midnight.
SCRIPT_PATH="$(readlink -f "$0")"
NEXT_MIDNIGHT=$(date -d "tomorrow 00:00" +"%Y-%m-%dT%H:%M:%S")

echo "[INFO] Resubmitting ${SCRIPT_PATH} for ${NEXT_MIDNIGHT}."
sbatch --begin="${NEXT_MIDNIGHT}" "${SCRIPT_PATH}"

exit 0
