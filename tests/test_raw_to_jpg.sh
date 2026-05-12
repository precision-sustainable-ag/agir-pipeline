#!/usr/bin/env bash
#SBATCH --job-name=raw2jpg
#SBATCH --account=dash_agir
#SBATCH --partition=compute          # change for atlas/ceres as needed
#SBATCH --time=06:00:00              # start here; adjust after a pilot
#SBATCH --cpus-per-task=16           # match your --t (see below)
#SBATCH --mem=128G                    # conservative; tune down after pilot
#SBATCH --output=/project/dash_agir/logs/raw2jpg/%x_%A_%a.out
#SBATCH --error=/project/dash_agir/logs/raw2jpg/%x_%A_%a.err

set -euo pipefail

CPUS_PER_TASK="${SLURM_CPUS_PER_TASK:-16}"

# ---- Thread controls (important for RawTherapee / OpenMP) ----
export OMP_NUM_THREADS="${CPUS_PER_TASK}"
export OMP_DYNAMIC="FALSE"
export OMP_NESTED="FALSE"
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

# ---- Your per-task batch id list ----
BATCH_ID="MD_2025-04-25"
IN_DIR="/90daydata/dash_agir/semifield-upload/${BATCH_ID}"
OUT_DIR="/90daydata/dash_agir/tmp/semifield-developed-images/${BATCH_ID}/images"

CFG="configs/myconfig.yaml"

mkdir -p "$OUT_DIR"


echo "Running raw_to_jpg"
echo "BATCH_ID:       $BATCH_ID"
echo "IN_DIR:         $IN_DIR"
echo "OUT_DIR:        $OUT_DIR"
echo "CFG:            $CFG"
echo "CPUS_PER_TASK:  $CPUS_PER_TASK"
echo "OMP_NUM_THREADS:$OMP_NUM_THREADS"

# ---- Run your module ----
python -m stages.raw_to_jpg.cli \
  --c "$CFG" \
  --i "$IN_DIR" \
  --o "$OUT_DIR" \
  --t "${CPUS_PER_TASK}" \
  --fs
