#!/usr/bin/env bash
#SBATCH --job-name=globus_index
#SBATCH --account=dash_agir
#SBATCH --partition=short             # TODO: set to the right partition
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=12               # match --max-workers
#SBATCH --mem=16G
#SBATCH --output=/project/dash_agir/logs/globus_index_%x_%j.out.log
#SBATCH --error=/project/dash_agir/logs/globus_index_%x_%j.err.log

set -euo pipefail

echo "[INFO] Job started on $(hostname) at $(date)"

# ----------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------

# Make sure log directory exists (for both Slurm and Python logs)
mkdir -p /project/dash_agir/logs

# Activate your environment (adjust for your cluster)
# module load miniconda3
# source activate /project/dash_agir/matthew.kutugata/software/miniforge3/envs/semif_prep

# Path to index DB
DB_PATH="/project/dash_agir/matthew.kutugata/semifield-database/globus_file_index.db"

# Python script
INDEX_SCRIPT="scripts/globus_index.py"

# Globus endpoints
JUNO_EP="904c2108-90cf-11e8-9672-0a6d4e044368"
NCSU_EP="2f7f6170-8d5c-11e9-8e6a-029d279f7e24"

# Base root for NCSU
NCSU_BASE="/rsstu/users/s/screberg"

# Logical LTS root labels (your internal tags)
NCSU_ROOT_1="longterm_images2"
NCSU_ROOT_2="longterm_images"
NCSU_ROOT_3="GROW_DATA"
JUNO_ROOT="dash_agir"

# ----------------------------------------------------------------------
# Table-driven config: one row per tree to index
# Columns: endpoint|root|lts_root|state
# ----------------------------------------------------------------------

CONFIGS=(
  # JUNO RAW + DEV
  "${JUNO_EP}|/LTS/project/dash_agir/semifield-upload|${JUNO_ROOT}|upload_raw"
  "${JUNO_EP}|/LTS/project/dash_agir/semifield-developed-images|${JUNO_ROOT}|developed_jpg"
  "${JUNO_EP}|/LTS/project/dash_agir/semifield-cutouts|${JUNO_ROOT}|cutouts"


  # NCSU RAW
  "${NCSU_EP}|${NCSU_BASE}/longterm_images2/semifield-upload|${NCSU_ROOT_1}|upload_raw"
  "${NCSU_EP}|${NCSU_BASE}/longterm_images/semifield-upload|${NCSU_ROOT_2}|upload_raw"
  "${NCSU_EP}|${NCSU_BASE}/GROW_DATA/semifield-upload|${NCSU_ROOT_3}|upload_raw"

  # NCSU DEV
  "${NCSU_EP}|${NCSU_BASE}/longterm_images2/semifield-developed-images|${NCSU_ROOT_1}|developed_jpg"
  "${NCSU_EP}|${NCSU_BASE}/longterm_images/semifield-developed-images|${NCSU_ROOT_2}|developed_jpg"
  "${NCSU_EP}|${NCSU_BASE}/GROW_DATA/semifield-developed-images|${NCSU_ROOT_3}|developed_jpg"

  #CUTOUTS
  # "${NCSU_EP}|${NCSU_BASE}/longterm_images2/semifield-cutouts|${NCSU_ROOT_1}|cutouts"
  # "${NCSU_EP}|${NCSU_BASE}/longterm_images/semifield-cutouts|${NCSU_ROOT_2}|cutouts"
  # "${NCSU_EP}|${NCSU_BASE}/GROW_DATA/semifield-cutouts|${NCSU_ROOT_3}|cutouts"
)

# ----------------------------------------------------------------------
# Helper function
# ----------------------------------------------------------------------

run_index() {
  local endpoint="$1"
  local root="$2"
  local lts_root="$3"
  local state="$4"

  echo "[INFO] Running index for:"
  echo "       endpoint = ${endpoint}"
  echo "       root     = ${root}"
  echo "       lts_root = ${lts_root}"
  echo "       state    = ${state}"

  python "${INDEX_SCRIPT}" \
    --db "${DB_PATH}" \
    --endpoint "${endpoint}" \
    --root "${root}" \
    --lts-root "${lts_root}" \
    --state "${state}" \
    --batch-size 2000 \
    --max-workers "${SLURM_CPUS_PER_TASK}"
}

# ----------------------------------------------------------------------
# Main loop over all configs
# ----------------------------------------------------------------------

for cfg in "${CONFIGS[@]}"; do
  IFS="|" read -r endpoint root lts_root state <<< "${cfg}"
  run_index "${endpoint}" "${root}" "${lts_root}" "${state}"
done

echo "[INFO] Job finished at $(date)"
