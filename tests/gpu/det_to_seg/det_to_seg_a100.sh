#!/usr/bin/env bash
#SBATCH --job-name=det_to_seg_a100
#SBATCH --account=dash_agir
#SBATCH --partition=gpu-a100
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
#SBATCH --chdir=/project/dash_agir/brennen.farrell
#SBATCH -o /project/dash_agir/brennen.farrell/logs/%x-%j.out
#SBATCH -e /project/dash_agir/brennen.farrell/logs/%x-%j.err

set -euo pipefail

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

REPO_DIR="${REPO_DIR:-/project/dash_agir/brennen.farrell/agir-pipeline}"
UV_ENV="${UV_ENV:-/project/dash_agir/brennen.farrell/uv/venvs/agir_pipeline/bin/activate}"

JPG_INPUT_DIR="${JPG_INPUT_DIR:-/90daydata/dash_agir/tmp/semifield-developed-images/new/images2}"
FINAL_DET_DIR="${FINAL_DET_DIR:-/90daydata/dash_agir/tmp/semifield-developed-images/new/a100/detections}"
FINAL_SEG_DIR="${FINAL_SEG_DIR:-/90daydata/dash_agir/tmp/semifield-developed-images/new/a100/segmentations}"

SEG_CFG_PATH="${SEG_CFG_PATH:-$REPO_DIR/stages/det_to_seg/configs/default.yaml}"
SEG_THREADS="${SEG_THREADS:-1}"
SEG_DEVICE="${SEG_DEVICE:-cuda:0}"
BATCH_ID="${BATCH_ID:-new-test-sample}"

mkdir -p /project/dash_agir/brennen.farrell/logs
mkdir -p "$FINAL_SEG_DIR"

echo "Sourcing environment..."
source "$UV_ENV"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

DET_ARTIFACT_DIR="$(ls -td "$FINAL_DET_DIR"/jpg_to_det/*/artifacts 2>/dev/null | head -n 1)"
if [[ -z "$DET_ARTIFACT_DIR" || ! -d "$DET_ARTIFACT_DIR" ]]; then
  echo "Could not resolve detection artifacts directory under $FINAL_DET_DIR/jpg_to_det" >&2
  exit 1
fi

echo "Running DET -> SEG from detections in $DET_ARTIFACT_DIR..."
python3 -m stages.det_to_seg.cli \
  --i "$DET_ARTIFACT_DIR" \
  --j "$JPG_INPUT_DIR" \
  --c "$SEG_CFG_PATH" \
  --o "$FINAL_SEG_DIR" \
  --t "$SEG_THREADS" \
  --fs \
  --batch-id "$BATCH_ID" \
  --device "$SEG_DEVICE"

echo "Job finished at $(date)"
echo "Detections:           $DET_ARTIFACT_DIR"
echo "Segmentation outputs: $FINAL_SEG_DIR"
