#!/usr/bin/env bash
#
# Weekly Globus Indexer - Wrapper Script
# Run this via cron for automated weekly indexing
#
# Usage: ./weekly_globus_index.sh

#!/usr/bin/env bash
#SBATCH --job-name=globus_index
#SBATCH --account=dash_agir
#SBATCH --partition=compute          # adjust if needed
#SBATCH --time=6:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --output=/project/dash_agir/logs/weekly_globus/agir_%x_%j.out.log
#SBATCH --error=/project/dash_agir/logs/weekly_globus/agir_%x_%j.err.log

set -euo pipefail
echo "[INFO] Weekly Globus Indexing Job started on $(hostname) at $(date)"

# ============================================================
#                   IMPORTS & SETUP
# ============================================================

module load postgresql
echo "[INIT] Loading database connection parameters..."
source /project/dash_agir/postgres/pg_coords.env
module load miniconda
source activate /project/dash_agir/matthew.kutugata/software/miniforge3/envs/semif_prep
pip install psycopg2-binary
echo "[INIT] Database connection parameters loaded."

PSQL="psql -v ON_ERROR_STOP=1 -h $PGHOST -p $PGPORT -d $PGDATABASE -U $PGUSER"

# ============================================================
#                ENDPOINT AND PATH DEFINITIONS
# ============================================================
JUNO_EP="904c2108-90cf-11e8-9672-0a6d4e044368"
CERES_EP="f45a24f8-09ba-11ec-b342-1feaf93e3729"
# NCSU_EP="f5897c0b-97a9-4340-abbe-800343b79b02"
NCSU_EP="2f7f6170-8d5c-11e9-8e6a-029d279f7e24"

# Base root for NCSU
NCSU_BASE="/rsstu/users/s/screberg"

# Logical LTS root labels (your internal tags)
NCSU_ROOT_1="longterm_images2"
NCSU_ROOT_2="longterm_images"
NCSU_ROOT_3="GROW_DATA"
JUNO_NPIR_ROOT="npir"
JUNO_ROOT="dash_agir"
CERES_ROOT="dash_agir"

UPLOAD_ds="upload_raw"
DEVELOPED_ds="developed_jpg"
CUTOUT_ds="cutouts"

# ============================================================
#                   CONFIGURATION
# ============================================================

# Globus endpoints to index
# Format: "endpoint_id|location|root_path|lts_root|data_state"
# Includes: CERES (90-day + LTS), JUNO (LTS), NCSU (various LTS)
ENDPOINTS=(
    "${CERES_EP}|CERES|/project/${CERES_ROOT}/semifield-upload|${CERES_ROOT}|${UPLOAD_ds}"
    "${CERES_EP}|CERES|/project/${CERES_ROOT}/semifield-developed-images|${CERES_ROOT}|${DEVELOPED_ds}"
    "${CERES_EP}|CERES|/project/${CERES_ROOT}/semifield-cutouts|${CERES_ROOT}|${CUTOUT_ds}"

    "${CERES_EP}|CERES|/90daydata/${CERES_ROOT}/semifield-upload|${CERES_ROOT}|${UPLOAD_ds}"
    "${CERES_EP}|CERES|/90daydata/${CERES_ROOT}/semifield-developed-images|${CERES_ROOT}|${DEVELOPED_ds}"
    "${CERES_EP}|CERES|/90daydata/${CERES_ROOT}/semifield-cutouts|${CERES_ROOT}|${CUTOUT_ds}"

    "${JUNO_EP}|JUNO|/LTS/project/${JUNO_ROOT}/semifield-upload|${JUNO_ROOT}|${UPLOAD_ds}"
    "${JUNO_EP}|JUNO|/LTS/project/${JUNO_ROOT}/semifield-developed-images|${JUNO_ROOT}|${DEVELOPED_ds}"
    "${JUNO_EP}|JUNO|/LTS/project/${JUNO_ROOT}/semifield-cutouts|${JUNO_ROOT}|${CUTOUT_ds}"

    "${JUNO_EP}|JUNO|/LTS/project/national_plant_image_repository/semifield-upload|${JUNO_NPIR_ROOT}|${UPLOAD_ds}"
    "${JUNO_EP}|JUNO|/LTS/project/national_plant_image_repository/semifield-developed-images|${JUNO_NPIR_ROOT}|${DEVELOPED_ds}" 
    "${JUNO_EP}|JUNO|/LTS/project/national_plant_image_repository/semifield-cutouts|${JUNO_NPIR_ROOT}|${CUTOUT_ds}" 
    
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_1}/semifield-upload|${NCSU_ROOT_1}|${UPLOAD_ds}"
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_2}/semifield-upload|${NCSU_ROOT_2}|${UPLOAD_ds}"
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_3}/semifield-upload|${NCSU_ROOT_3}|${UPLOAD_ds}"

    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_1}/semifield-developed-images|${NCSU_ROOT_1}|${DEVELOPED_ds}"
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_2}/semifield-developed-images|${NCSU_ROOT_2}|${DEVELOPED_ds}"
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_3}/semifield-developed-images|${NCSU_ROOT_3}|${DEVELOPED_ds}"

    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_1}/semifield-cutouts|${NCSU_ROOT_1}|${CUTOUT_ds}"
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_2}/semifield-cutouts|${NCSU_ROOT_2}|${CUTOUT_ds}"
    "${NCSU_EP}|NCSU|${NCSU_BASE}/${NCSU_ROOT_3}/semifield-cutouts|${NCSU_ROOT_3}|${CUTOUT_ds}"
)

# Script settings
REPO_DIR="/project/dash_agir/matthew.kutugata/repos/agir-db"
PYTHON_SCRIPT="${REPO_DIR}/scripts/globus_index.py"
SCHEMA="${REPO_DIR}/sql/schemas/02_source/source.globus_file_index.sql"

# check of pyhton script exists
if [ ! -f "${PYTHON_SCRIPT}" ]; then
    echo "[ERROR] Python script not found: ${PYTHON_SCRIPT}"
    exit 1
fi

LOG_DIR="/project/dash_agir/logs/weekly_globus"
MAX_WORKERS=8
BATCH_SIZE=5000

# Update behavior: set to "--update-existing" to update modified files,
# or leave empty to only insert new files

# ============================================================
#                    SETUP
# ============================================================

mkdir -p "${LOG_DIR}"

# Log with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
MAIN_LOG="${LOG_DIR}/weekly_run_${TIMESTAMP}.log"

echo "========================================" | tee -a "${MAIN_LOG}"
echo "Weekly Globus Indexing Started" | tee -a "${MAIN_LOG}"
echo "Timestamp: $(date)" | tee -a "${MAIN_LOG}"
echo "========================================" | tee -a "${MAIN_LOG}"
echo "" | tee -a "${MAIN_LOG}"

# ============================================================
#                    INDEX EACH ENDPOINT
# ============================================================

TOTAL_SUCCESS=0
TOTAL_FAILED=0

for endpoint_config in "${ENDPOINTS[@]}"; do
    IFS='|' read -r endpoint location root lts_root state <<< "$endpoint_config"
    
    echo "Processing: $location / $state / $root" | tee -a "${MAIN_LOG}"
    
    # Individual log for this endpoint
    ENDPOINT_LOG="${LOG_DIR}/${location}_${state}_${TIMESTAMP}.log"
    
    # Run the indexer
    if python3 "${PYTHON_SCRIPT}" \
        --schema "${SCHEMA}" \
        --host "${PGHOST}" \
        --port "${PGPORT}" \
        --dbname "${PGDATABASE}" \
        --user "${PGUSER}" \
        --endpoint "${endpoint}" \
        --location "${location}" \
        --root "${root}" \
        --lts-root "${lts_root}" \
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