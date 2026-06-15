#!/usr/bin/env bash
#SBATCH --job-name=assign_species
#SBATCH --account=dash_agir
#SBATCH --partition=atlas
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=01:00:00
#SBATCH --output=/project/dash_agir/logs/assign_species/%x-%j.out
#SBATCH --error=/project/dash_agir/logs/assign_species/%x-%j.err

# override default variables
set -euo pipefail

# user — set this to your SCINet username
USERNAME="your.username"

# path to repo where /agir-pipeline lives
REPO_DIR=
# virtual environment activate script
UV_ENV=


# batch to test
BATCH_ID="${BATCH_ID:-NC_2026-01-07}"
BATCH_ROOT="/90daydata/dash_agir/semifield-developed-images/${BATCH_ID}"

# inputs
GEO_CSV="${GEO_CSV:-${BATCH_ROOT}/det_to_world/artifacts/${BATCH_ID}_georeferenced.csv}"
DET_CSV="${DET_CSV:-${BATCH_ROOT}/plant-detections/${BATCH_ID}.csv}"
GEO_REPORT="${GEO_REPORT:-${BATCH_ROOT}/det_to_world/run_report.json}"
SHP="${SHP:-${BATCH_ROOT}/reference/species_zones.shp}"
BBOT_VERSION="${BBOT_VERSION:-3.1}"
MONO_SPECIES="${MONO_SPECIES:-BETVU}"

# Create log dir if it doesn't exist
mkdir -p /project/dash_agir/logs/assign_species

# Validate inputs
if [[ ! -d "${REPO_DIR}" ]]; then
  echo "Repo directory does not exist: ${REPO_DIR}" >&2
  exit 1
fi

if [[ ! -f "${UV_ENV}" ]]; then
  echo "Virtual environment activate script does not exist: ${UV_ENV}" >&2
  exit 1
fi

if [[ ! -f "${GEO_CSV}" ]]; then
  echo "Georeferenced CSV does not exist: ${GEO_CSV}" >&2
  exit 1
fi

if [[ ! -f "${DET_CSV}" ]]; then
  echo "Detection CSV does not exist: ${DET_CSV}" >&2
  exit 1
fi

if [[ ! -f "${GEO_REPORT}" ]]; then
  echo "det_to_world run_report.json does not exist: ${GEO_REPORT}" >&2
  exit 1
fi
# =============== SPATIAL JOIN TEST ===============
SJOIN_OUTPUT_DIR="/project/dash_agir/${USERNAME}/outputs/${BATCH_ID}/assign_species_test/spatial_join"

# create output directory
mkdir -p "${SJOIN_OUTPUT_DIR}"

echo "Connecting to env"
source "${UV_ENV}"

cd "${REPO_DIR}"
export PYTHONPATH="${REPO_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Running pytest"
python3 -m pytest stages/assign_species/tests

echo "Starting CLI with:"
echo "Batch:      ${BATCH_ID}"
echo "Geo CSV:    ${GEO_CSV}"
echo "Det CSV:    ${DET_CSV}"
echo "Geo report: ${GEO_REPORT}"
echo "Shapefile:  ${SHP}"
echo "Output:     ${SJOIN_OUTPUT_DIR}"

python3 -m stages.assign_species.cli \
  --geo        "${GEO_CSV}" \
  --det        "${DET_CSV}" \
  --geo-report "${GEO_REPORT}" \
  --shp        "${SHP}" \
  --bbot-version "${BBOT_VERSION}" \
  --o          "${SJOIN_OUTPUT_DIR}" \
  --batch-id   "${BATCH_ID}"

# Show artifact directory if it exists
set +o pipefail
ARTIFACT_DIR="$(ls -td "${SJOIN_OUTPUT_DIR}"/assign_species/*/artifacts 2>/dev/null | head -n 1)"
set -o pipefail

# print artifact directory
if [[ -n "${ARTIFACT_DIR}" && -d "${ARTIFACT_DIR}" ]]; then
  echo "Artifacts: ${ARTIFACT_DIR}"
fi

# =============== MONOCULTURE TEST ===============
MONO_OUTPUT_DIR="/project/dash_agir/${USERNAME}/outputs/${BATCH_ID}/assign_species_test/monoculture"
MONO_REPORT="${MONO_OUTPUT_DIR}/fake_skipped_report.json"
mkdir -p "${MONO_OUTPUT_DIR}"
echo '{"skipped": true}' > "${MONO_REPORT}"

echo "Starting monoculture CLI with:"
echo "Batch:      ${BATCH_ID}"
echo "Det CSV:    ${DET_CSV}"
echo "Geo report: ${MONO_REPORT} (synthetic)"
echo "Species:    ${MONO_SPECIES}"
echo "Output:     ${MONO_OUTPUT_DIR}"

python3 -m stages.assign_species.cli \
  --geo        "${GEO_CSV}" \
  --det        "${DET_CSV}" \
  --geo-report "${MONO_REPORT}" \
  --shp        "${SHP}" \
  --bbot-version "${BBOT_VERSION}" \
  --species    "${MONO_SPECIES}" \
  --o          "${MONO_OUTPUT_DIR}" \
  --batch-id   "${BATCH_ID}"

set +o pipefail
MONO_ARTIFACT_DIR="$(ls -td "${MONO_OUTPUT_DIR}"/assign_species/*/artifacts 2>/dev/null | head -n 1)"
set -o pipefail

if [[ -n "${MONO_ARTIFACT_DIR}" && -d "${MONO_ARTIFACT_DIR}" ]]; then
  echo "Monoculture artifacts: ${MONO_ARTIFACT_DIR}"
fi

echo "Job finished at $(date)"
