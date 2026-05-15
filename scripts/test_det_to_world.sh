#!/usr/bin/env bash
#SBATCH --job-name=det_to_world
#SBATCH --account=dash_agir
#SBATCH --partition=atlas
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=02:00:00
#SBATCH --output=/project/dash_agir/logs/det_to_world/%x-%j.out
#SBATCH --error=/project/dash_agir/logs/det_to_world/%x-%j.err

set -euo pipefail

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

# user — set this to your SCINet username (e.g. first.last)
USERNAME="your.username"

# repo and environment setup
REPO_DIR="${REPO_DIR:-/project/dash_agir/${USERNAME}/agir-pipeline}"
UV_ENV="${UV_ENV:-/project/dash_agir/${USERNAME}/uv/venvs/agir_pipeline/bin/activate}"

# batch to test
BATCH_ID="${BATCH_ID:-NC_2026-01-07}"
BATCH_ROOT="/90daydata/dash_agir/semifield-developed-images/${BATCH_ID}"
GRID_DIR="${BATCH_ROOT}/reference/pixel_world_grids"
OUTPUT_DIR="/project/dash_agir/${USERNAME}/outputs/${BATCH_ID}/det_to_world_test"

# Create log dir if it doesn't exist
mkdir -p /project/dash_agir/logs/det_to_world

# Basic Validationls
if [[ ! -d "${REPO_DIR}" ]]; then
  echo "Repo directory does not exist: ${REPO_DIR}" >&2
  exit 1
fi

if [[ ! -f "${UV_ENV}" ]]; then
  echo "Virtual environment activate script does not exist: ${UV_ENV}" >&2
  exit 1
fi

if [[ ! -d "${GRID_DIR}" ]]; then
  echo "Grid directory does not exist: ${GRID_DIR}" >&2
  exit 1
fi

DET_DIR="${BATCH_ROOT}/plant-detections"

if [[ ! -d "${DET_DIR}" ]]; then
  echo "Plant detections directory does not exist: ${DET_DIR}" >&2
  exit 1
fi

# Find first CSV to extract header
set +o pipefail
FIRST_CSV="$(find "${DET_DIR}" -name 'NC_*.csv' -type f | sort | head -n 1)"
set -o pipefail

if [[ -z "${FIRST_CSV}" ]]; then
  echo "No detection CSVs found under ${DET_DIR}" >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"

# Aggregate all per-image CSVs into one combined CSV with image_id column
COMBINED_CSV="${OUTPUT_DIR}/combined_detections.csv"
echo "image_id,$(head -n 1 "${FIRST_CSV}")" > "${COMBINED_CSV}"
for csv in "${DET_DIR}"/NC_*.csv; do
  image_id="$(basename "${csv}" .csv)"
  tail -n +2 "${csv}" | awk -v id="${image_id}" '{print id "," $0}' >> "${COMBINED_CSV}"
done

echo "Sourcing environment..."
source "${UV_ENV}"

cd "${REPO_DIR}"
export PYTHONPATH="${REPO_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Running det_to_world for ${BATCH_ID}"
echo "Input CSV: ${COMBINED_CSV}"
echo "Grid dir:  ${GRID_DIR}"
echo "Output:    ${OUTPUT_DIR}"

echo "Running det_to_world tests..."
python3 -m pytest stages/det_to_world/tests

echo "Starting CLI execution..."
python3 -m stages.det_to_world.cli \
  --i "${COMBINED_CSV}" \
  --g "${GRID_DIR}" \
  --o "${OUTPUT_DIR}" \
  --batch-id "${BATCH_ID}"

# Another SIGPIPE safety for the artifact check
set +o pipefail
ARTIFACT_DIR="$(ls -td "${OUTPUT_DIR}"/det_to_world/*/artifacts 2>/dev/null | head -n 1)"
set -o pipefail

if [[ -n "${ARTIFACT_DIR}" && -d "${ARTIFACT_DIR}" ]]; then
  echo "Artifacts: ${ARTIFACT_DIR}"
fi

echo "Job finished at $(date)"