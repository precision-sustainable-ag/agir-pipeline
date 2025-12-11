#!/usr/bin/env bash
#
# Weekly Globus Indexer - Wrapper Script
# Run this via cron for automated weekly indexing
#
# Usage: ./globus_index.sh

#!/usr/bin/env bash
#SBATCH --job-name=globus_index
#SBATCH --account=dash_agir
#SBATCH --partition=compute          # adjust if needed
#SBATCH --time=6:00:00
#SBATCH --cpus-per-task=12
#SBATCH --mem=32G
#SBATCH --output=/project/dash_agir/logs/weekly_globus/agir_%x_%j.out.log
#SBATCH --error=/project/dash_agir/logs/weekly_globus/agir_%x_%j.err.log

set -Euo pipefail

# ----------------- Basic log setup -----------------
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/project/dash_agir/logs/weekly_globus"
mkdir -p "${LOG_DIR}"
MAIN_LOG="${LOG_DIR}/agir_${TIMESTAMP}.log"

# Send *all* stdout/stderr to MAIN_LOG (plus SLURM out/err)
exec > >(tee -a "${MAIN_LOG}") 2>&1

echo "[INFO] Weekly Globus Indexing Job started on $(hostname) at $(date)"
echo "[INFO] Main log: ${MAIN_LOG}"

# Error trap: log any failing command with line + exit code
log_error() {
    local exit_code=$?
    # BASH_COMMAND is the command that caused the error
    echo "[ERROR] Exit=${exit_code} at line ${BASH_LINENO[0]}: ${BASH_COMMAND}"
}
trap log_error ERR

# Exit trap: always log final status
on_exit() {
    local exit_code=$?
    echo "[INFO] Script exiting with code ${exit_code} at $(date)"
}
trap on_exit EXIT

echo "[INFO] Weekly Globus Indexing Job started on $(hostname) at $(date)"

# ============================================================
#                   IMPORTS & SETUP
# ============================================================

module load postgresql
echo "[INIT] Loading database connection parameters..."
source /project/dash_agir/postgres/pg_coords.env
module load miniconda
source activate /project/dash_agir/matthew.kutugata/software/miniforge3/envs/semif_prep
echo "[INIT] Database connection parameters loaded."

PSQL="psql -v ON_ERROR_STOP=1 -h $PGHOST -p $PGPORT -d $PGDATABASE -U $PGUSER"

# ============================================================
#                       ENDPOINTS
# ============================================================
JUNO_EP="904c2108-90cf-11e8-9672-0a6d4e044368"
CERES_EP="f45a24f8-09ba-11ec-b342-1feaf93e3729"
NCSU_EP="2f7f6170-8d5c-11e9-8e6a-029d279f7e24"
# ============================================================
#                     STORAGE ROOTS
# ============================================================
NCSU_NFS_LI_SROOT="/rsstu/users/s/screberg/longterm_images"
NCSU_NFS_LI2_SROOT="/rsstu/users/s/screberg/longterm_images2"
NCSU_NFS_GD_SROOT="/rsstu/users/s/screberg/GROW_DATA"

JUNO_LTS_DASH_SROOT="/LTS/project/dash_agir"
JUNO_LTS_NPIR_SROOT="/LTS/project/national_plant_image_repository"

CERES_90D_DASH_SROOT="/90daydata/dash_agir"
CERES_90D_NPIR_SROOT="/90daydata/national_plant_image_repository"

CERES_PROJECT_DASH_SROOT="/project/dash_agir"
CERES_PROJECT_NPIR_SROOT="/project/national_plant_image_repository"
# ============================================================
#                        SITE
# ============================================================
NCSU_LOC="NCSU"
JUNO_LOC="JUNO"
CERES_LOC="CERES"
# ============================================================
#                    STORAGE DOMAIN
# ============================================================
SCREB="screberg"
DASH="dash_agir"
NPIR="national_plant_image_repository"
# ============================================================
#                         NAMESPACE
# ============================================================
NCSU_LI="longterm_images"
NCSU_LI2="longterm_images2"
NCSU_LI3="GROW_DATA"
SCINET_90D="90daydata"
SCINET_PROJ="project"
SCINET_JUNO_PROJ="LTS"
# ============================================================
#                        DATA STATES
# ============================================================
DATA_STATE_UP="semifield-upload"
DATA_STATE_DEV="semifield-developed-images"
DATA_STATE_CUT="semifield-cutouts"
# ============================================================
#                   CONFIGURATION
# ============================================================
# Format: endpoint_id | site| storage_domain | namespace | storage_root| data_state
ENDPOINTS=(
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI}|${NCSU_NFS_LI_SROOT}|${DATA_STATE_UP}"
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI}|${NCSU_NFS_LI_SROOT}|${DATA_STATE_DEV}"
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI}|${NCSU_NFS_LI_SROOT}|${DATA_STATE_CUT}"

"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI2}|${NCSU_NFS_LI2_SROOT}|${DATA_STATE_UP}"
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI2}|${NCSU_NFS_LI2_SROOT}|${DATA_STATE_DEV}"
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI2}|${NCSU_NFS_LI2_SROOT}|${DATA_STATE_CUT}"

"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI3}|${NCSU_NFS_GD_SROOT}|${DATA_STATE_UP}"
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI3}|${NCSU_NFS_GD_SROOT}|${DATA_STATE_DEV}"
"${NCSU_EP}|${NCSU_LOC}|${SCREB}|${NCSU_LI3}|${NCSU_NFS_GD_SROOT}|${DATA_STATE_CUT}"

"${JUNO_EP}|${JUNO_LOC}|${DASH}|${SCINET_JUNO_PROJ}|${JUNO_LTS_DASH_SROOT}|${DATA_STATE_UP}"
"${JUNO_EP}|${JUNO_LOC}|${DASH}|${SCINET_JUNO_PROJ}|${JUNO_LTS_DASH_SROOT}|${DATA_STATE_DEV}"
"${JUNO_EP}|${JUNO_LOC}|${DASH}|${SCINET_JUNO_PROJ}|${JUNO_LTS_DASH_SROOT}|${DATA_STATE_CUT}"

"${JUNO_EP}|${JUNO_LOC}|${NPIR}|${SCINET_JUNO_PROJ}|${JUNO_LTS_NPIR_SROOT}|${DATA_STATE_UP}"
"${JUNO_EP}|${JUNO_LOC}|${NPIR}|${SCINET_JUNO_PROJ}|${JUNO_LTS_NPIR_SROOT}|${DATA_STATE_DEV}"
"${JUNO_EP}|${JUNO_LOC}|${NPIR}|${SCINET_JUNO_PROJ}|${JUNO_LTS_NPIR_SROOT}|${DATA_STATE_CUT}"
    
"${CERES_EP}|${CERES_LOC}|${DASH}|${SCINET_90D}|${CERES_90D_DASH_SROOT}|${DATA_STATE_UP}"
"${CERES_EP}|${CERES_LOC}|${DASH}|${SCINET_90D}|${CERES_90D_DASH_SROOT}|${DATA_STATE_DEV}"
"${CERES_EP}|${CERES_LOC}|${DASH}|${SCINET_90D}|${CERES_90D_DASH_SROOT}|${DATA_STATE_CUT}"

"${CERES_EP}|${CERES_LOC}|${DASH}|${SCINET_PROJ}|${CERES_PROJECT_DASH_SROOT}|${DATA_STATE_UP}"
"${CERES_EP}|${CERES_LOC}|${DASH}|${SCINET_PROJ}|${CERES_PROJECT_DASH_SROOT}|${DATA_STATE_DEV}"
"${CERES_EP}|${CERES_LOC}|${DASH}|${SCINET_PROJ}|${CERES_PROJECT_DASH_SROOT}|${DATA_STATE_CUT}"

"${CERES_EP}|${CERES_LOC}|${NPIR}|${SCINET_90D}|${CERES_90D_NPIR_SROOT}|${DATA_STATE_UP}"
"${CERES_EP}|${CERES_LOC}|${NPIR}|${SCINET_90D}|${CERES_90D_NPIR_SROOT}|${DATA_STATE_DEV}"
"${CERES_EP}|${CERES_LOC}|${NPIR}|${SCINET_90D}|${CERES_90D_NPIR_SROOT}|${DATA_STATE_CUT}"

"${CERES_EP}|${CERES_LOC}|${NPIR}|${SCINET_PROJ}|${CERES_PROJECT_NPIR_SROOT}|${DATA_STATE_UP}"
"${CERES_EP}|${CERES_LOC}|${NPIR}|${SCINET_PROJ}|${CERES_PROJECT_NPIR_SROOT}|${DATA_STATE_DEV}"
"${CERES_EP}|${CERES_LOC}|${NPIR}|${SCINET_PROJ}|${CERES_PROJECT_NPIR_SROOT}|${DATA_STATE_CUT}"
)

# ============================================================
#                       SETUP
# ============================================================
# Paths
REPO_DIR="/project/dash_agir/matthew.kutugata/repos/agir-db"
PYTHON_SCRIPT="${REPO_DIR}/scripts/globus_index.py"
SCHEMA="${REPO_DIR}/sql/schemas/02_source/source.globus_file_index.sql"
MAX_WORKERS=12
BATCH_SIZE=5000
# ============================================================
#                       PATH CHECKS
# ============================================================
# check that the python script exists
if [ ! -f "${PYTHON_SCRIPT}" ] || [ ! -f "${SCHEMA}" ]; then
    echo "[ERROR] Required file(s) not found:" | tee -a "${MAIN_LOG}"
    [ ! -f "${PYTHON_SCRIPT}" ] && echo "  - Python script: ${PYTHON_SCRIPT}" | tee -a "${MAIN_LOG}"
    [ ! -f "${SCHEMA}" ] && echo "  - Schema file: ${SCHEMA}" | tee -a "${MAIN_LOG}"
    exit 1
fi
# ============================================================
#                        INTRO
# ============================================================
echo "========================================" | tee -a "${MAIN_LOG}"
echo "Weekly Globus Indexing Started" | tee -a "${MAIN_LOG}"
echo "Timestamp: $(date)" | tee -a "${MAIN_LOG}"
echo "========================================" | tee -a "${MAIN_LOG}"
echo "" | tee -a "${MAIN_LOG}"

# ============================================================
#                    INDEX EACH ENDPOINT
# ============================================================
# Format: "endpoint_id | site | storage_domain | namespace | storage_root | data_state "
TOTAL_SUCCESS=0
TOTAL_FAILED=0

for endpoint_config in "${ENDPOINTS[@]}"; do
    IFS='|' read -r endpoint site storage_domain namespace storage_root state <<< "$endpoint_config"
    
    echo "Processing: $site / $state / $storage_root" | tee -a "${MAIN_LOG}"
    
    # Individual log for this endpoint
    ENDPOINT_LOG="${LOG_DIR}/${site}_${state}_${TIMESTAMP}.log"
    
    # Run the indexer
    if python3 "${PYTHON_SCRIPT}" \
        --schema "${SCHEMA}" \
        --host "${PGHOST}" \
        --port "${PGPORT}" \
        --dbname "${PGDATABASE}" \
        --user "${PGUSER}" \
        --endpoint "${endpoint}" \
        --site "${site}" \
        --storage-domain "${storage_domain}" \
        --namespace "${namespace}" \
        --storage-root "${storage_root}" \
        --state "${state}" \
        --batch-size "${BATCH_SIZE}" \
        --max-workers "${MAX_WORKERS}" \
        --clean-slate \
        --log-file "${ENDPOINT_LOG}" 2>&1 | tee -a "${MAIN_LOG}"; then
        
        echo "✓ Success: $location / $state" | tee -a "${MAIN_LOG}"
        TOTAL_SUCCESS=$((TOTAL_SUCCESS + 1))
    else
        echo "✗ Failed: $location / $state" | tee -a "${MAIN_LOG}"
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
    fi
    
    echo "" | tee -a "${MAIN_LOG}"
done

# ============================================================
#                    SUMMARY
# ============================================================

echo "========================================" | tee -a "${MAIN_LOG}"
echo "Weekly Globus Indexing Complete" | tee -a "${MAIN_LOG}"
echo "Timestamp: $(date)" | tee -a "${MAIN_LOG}"
echo "Success: ${TOTAL_SUCCESS}" | tee -a "${MAIN_LOG}"
echo "Failed: ${TOTAL_FAILED}" | tee -a "${MAIN_LOG}"
echo "========================================" | tee -a "${MAIN_LOG}"

# Optional: Send email notification
# echo "See attached log" | mail -s "Globus Index: $TOTAL_SUCCESS OK, $TOTAL_FAILED Failed" -a "${MAIN_LOG}" your-email@example.com

# Clean up old logs (keep last 30 days)
find "${LOG_DIR}" -name "*.log" -type f -mtime +30 -delete

# ------------------ Resubmit for next week -----
echo "[WEEKLY] Scheduling next run for ~7 days from now at the same time..."

sbatch --begin=now+7days "$0"

exit 0