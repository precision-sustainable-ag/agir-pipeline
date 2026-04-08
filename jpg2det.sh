#!/usr/bin/env bash
#SBATCH --job-name=jpg_to_det
#SBATCH --account=dash_agir
#SBATCH -p ceres
#SBATCH -N 1
#SBATCH -n 64
#SBATCH --mem=1024G
#SBATCH -t 01:00:00
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err

# Prevent each worker from spawning extra CPU threads
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

# Exit on command errors, unset variables, and pipeline failures.
set -euo pipefail

echo "Setting up paths..."

STAGING_IMAGE_DIR=/90daydata/dash_agir/semifield-developed-images/NC_2026-03-13/images/raw_to_jpg/ce1864da-a09a-4145-8698-c0bc04e490a5/artifacts/

BATCH_ID=NC_2026-03-13
CFG_PATH=/project/dash_agir/matthew.kutugata/repos/agir-pipeline/configs/myconfig.yaml
MODEL_PATH=/project/dash_agir/semifield-tools/models/plant_detector/Augmentation/weights/last.pt
INPUT_PATH=/tmp/semifield-developed-images/NC_2026-03-13/images/
TMP_MODEL_PATH=/tmp/semifield-developed-images/last.pt
OUTPUT_PATH=/tmp/semifield-developed-images/NC_2026-03-13/detections/
UV_ENV=/project/dash_agir/matthew.kutugata/software/uv/venvs/agir_pipeline/bin/activate
PARALLELISM=8
DEVICE=cpu


mkdir -p "$INPUT_PATH"
mkdir -p "$OUTPUT_PATH"

echo "Sourcing environment..."
source "$UV_ENV"

echo "Copying files and model to local storage..."
find "$STAGING_IMAGE_DIR" -maxdepth 1 -type f -name "*.jpg" -print0 \
  | shuf -z -n 200 \
  | xargs -0 -I {} cp "{}" "$INPUT_PATH"/
wait
rsync -av --size-only "$MODEL_PATH" "$TMP_MODEL_PATH"
wait

echo "Starting JPG to DET conversion..."
python3 -m stages.jpg_to_det.cli \
    --c "$CFG_PATH" \
    --m "$TMP_MODEL_PATH" \
    --i "$INPUT_PATH" \
    --o "$OUTPUT_PATH" \
    --t "$PARALLELISM" \
    --fs \
    --device "$DEVICE"

echo "JPG to DET conversion complete."

echo "Copying results back to project directory..."
mkdir -p "results/$BATCH_ID/detections/"
rsync -av --size-only "$OUTPUT_PATH"/* "results/$BATCH_ID/detections/"
