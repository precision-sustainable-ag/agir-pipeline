#!/usr/bin/env bash
#SBATCH --job-name=jpg_to_det_a100_mig7
#SBATCH --account=dash_agir
#SBATCH --partition=gpu-a100-mig7
#SBATCH --gres=gpu:nvidia_a100_1g.10gb:1
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
#SBATCH --chdir=/project/dash_agir/brennen.farrell
#SBATCH -o /project/dash_agir/brennen.farrell/logs-a100-mig7/%x-%j.out
#SBATCH -e /project/dash_agir/brennen.farrell/logs-a100-mig7/%x-%j.err

# Exit on command errors, unset variables, and pipeline failures.
set -euo pipefail

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

# repo and environment setup
REPO_DIR="${REPO_DIR:-/project/dash_agir/brennen.farrell/agir-pipeline}"
UV_ENV="${UV_ENV:-/project/dash_agir/brennen.farrell/uv/venvs/agir_pipeline/bin/activate}"

# input/output paths
JPG_INPUT_DIR="${JPG_INPUT_DIR:-/90daydata/dash_agir/tmp/semifield-developed-images/new/images2}"
FINAL_DET_DIR="${FINAL_DET_DIR:-/90daydata/dash_agir/tmp/semifield-developed-images/new/a100-mig7/detections}"
FINAL_VIZ_DIR="${FINAL_VIZ_DIR:-/90daydata/dash_agir/tmp/semifield-developed-images/new/a100-mig7/visualizations}"

# model, config, and script paths
DET_CFG_PATH="${DET_CFG_PATH:-$REPO_DIR/stages/jpg_to_det/configs/mig7.yaml}"
MODEL_PATH="${MODEL_PATH:-/project/dash_agir/matthew.kutugata/repos/AgIR-CVToolkit/data/plant_detection_model/last.pt}"
VIZ_SCRIPT="${VIZ_SCRIPT:-$REPO_DIR/scripts/visualize_detections.py}"

# viz sampling and rendering parameters
VIZ_SAMPLE_SIZE="${VIZ_SAMPLE_SIZE:-24}"
VIZ_MAX_WIDTH="${VIZ_MAX_WIDTH:-1800}"
DET_THREADS="${DET_THREADS:-1}"
DET_DEVICE="${DET_DEVICE:-cuda}"
BATCH_ID="${BATCH_ID:-new-a100-mig7}"

mkdir -p /project/dash_agir/brennen.farrell/logs-a100-mig7
mkdir -p "$FINAL_DET_DIR" "$FINAL_VIZ_DIR"

echo "Sourcing environment..."
source "$UV_ENV"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

echo "Running JPG -> DET on A100 MIG 1g.10gb with model $MODEL_PATH..."
python3 -m stages.jpg_to_det.cli \
  --c "$DET_CFG_PATH" \
  --m "$MODEL_PATH" \
  --i "$JPG_INPUT_DIR" \
  --o "$FINAL_DET_DIR" \
  --t "$DET_THREADS" \
  --fs \
  --batch-id "$BATCH_ID" \
  --device "$DET_DEVICE"

# Resolve the most recently updated detection artifacts directory.
DET_ARTIFACT_DIR="$(ls -td "$FINAL_DET_DIR"/jpg_to_det/*/artifacts 2>/dev/null | head -n 1)"
if [[ -z "$DET_ARTIFACT_DIR" || ! -d "$DET_ARTIFACT_DIR" ]]; then
  echo "Could not resolve detection artifacts directory under $FINAL_DET_DIR/jpg_to_det" >&2
  exit 1
fi

echo "Rendering random overlay sample to $FINAL_VIZ_DIR..."
python3 "$VIZ_SCRIPT" \
  --images "$JPG_INPUT_DIR" \
  --detections "$DET_ARTIFACT_DIR" \
  --output "$FINAL_VIZ_DIR" \
  --sample-size "$VIZ_SAMPLE_SIZE" \
  --max-width "$VIZ_MAX_WIDTH"

echo "Job finished at $(date)"
echo "Detections:       $FINAL_DET_DIR"
echo "Artifacts:        $DET_ARTIFACT_DIR"
echo "Visualizations:   $FINAL_VIZ_DIR"
