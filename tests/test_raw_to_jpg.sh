#!/usr/bin/env bash
#SBATCH --job-name=raw2jpg
#SBATCH --account=dash_agir
#SBATCH --partition=compute          # change for atlas/ceres as needed
#SBATCH --time=06:00:00              # start here; adjust after a pilot
#SBATCH --cpus-per-task=16           # match your --t (see below)
#SBATCH --mem=32G                    # conservative; tune down after pilot
#SBATCH --array=1-200%20             # 200 batches, max 20 running at once
#SBATCH --output=/project/dash_agir/logs/raw2jpg/%x_%A_%a.out
#SBATCH --error=/project/dash_agir/logs/raw2jpg/%x_%A_%a.err

set -euo pipefail

# ---- Thread controls (important for RawTherapee / OpenMP) ----
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK}"
export OMP_DYNAMIC="FALSE"
export OMP_NESTED="FALSE"
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

# ---- Your per-task batch id list ----
BATCH_LIST="/project/dash_agir/matthew.kutugata/repos/agir-pipeline/batch_list.txt"
BATCH_ID="$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$BATCH_LIST")"
[[ -n "${BATCH_ID:-}" ]] || { echo "No BATCH_ID for task ${SLURM_ARRAY_TASK_ID}"; exit 2; }

IN_DIR="/tmp/${BATCH_ID}/uploads"
OUT_DIR="/tmp/${BATCH_ID}/images"
CFG="my_config.yaml"

mkdir -p "$OUT_DIR"

# ---- Run your module ----
python -m stages.raw_to_jpg.cli \
  --c "$CFG" \
  --i "$IN_DIR" \
  --o "$OUT_DIR" \
  --t "${SLURM_CPUS_PER_TASK}" \
  --fs
