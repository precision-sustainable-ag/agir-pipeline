#!/usr/bin/env bash
#SBATCH --job-name=jpg_to_det_gpu
#SBATCH --account=dash_agir
#SBATCH --partition=gpu-a100
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
#SBATCH --chdir=/project/dash_agir
#SBATCH -o /project/dash_agir/logs/jpg_to_det_a100/%u-%x-%j.out
#SBATCH -e /project/dash_agir/logs/jpg_to_det_a100/%u-%x-%j.err

# Exit on command errors, unset variables, and pipeline failures.
set -euo pipefail

# Make files created during the job group-writable when possible.
# Note: Slurm creates the .out/.err files before this line runs, so this
# may not affect the initial Slurm log files depending on site config.
umask 002

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

# Shared/project setup

AGIR_PROJECT_ROOT="${AGIR_PROJECT_ROOT:-/project/dash_agir}"
AGIR_LOG_DIR="${AGIR_LOG_DIR:-$AGIR_PROJECT_ROOT/logs}"

# By default, use the running user for repo/env paths.
# Override with:
#   sbatch --export=ALL,AGIR_USER=matthew.kutugata jpg_to_det_gpu.sh
AGIR_USER="${AGIR_USER:-${USER:-$(whoami)}}"
AGIR_USER_ROOT="${AGIR_USER_ROOT:-$AGIR_PROJECT_ROOT/$AGIR_USER}"

# repo and environment setup

REPO_DIR="${REPO_DIR:-$AGIR_USER_ROOT/repos/agir-pipeline}"
UV_ENV="${UV_ENV:-$AGIR_USER_ROOT/software/uv/venvs/agir_pipeline/bin/activate}"

# input/output paths

BATCH_ID="${BATCH_ID:-MD_2025-04-25}"
BATCH_ROOT="${BATCH_ROOT:-/90daydata/dash_agir/tmp/semifield-developed-images/$BATCH_ID}"
JPG_INPUT_DIR="${JPG_INPUT_DIR:-$BATCH_ROOT/images}"
FINAL_DET_DIR="${FINAL_DET_DIR:-$BATCH_ROOT/a100/detections}"

# model, config, and script paths

DET_CFG_PATH="${DET_CFG_PATH:-$REPO_DIR/stages/jpg_to_det/configs/default.yaml}"
MODEL_PATH="${MODEL_PATH:-/90daydata/dash_agir/semifield-tools/models/plant_detector/With_Synthetic_Train_Data/weights/last.pt}"
VIZ_SCRIPT="${VIZ_SCRIPT:-$REPO_DIR/tests/gpu/jpg_to_det/visualize_detections.py}"

# viz sampling and rendering parameters

VIZ_SAMPLE_SIZE="${VIZ_SAMPLE_SIZE:-24}"
VIZ_MAX_WIDTH="${VIZ_MAX_WIDTH:-1800}"
DET_THREADS="${DET_THREADS:-1}"
DET_DEVICE="${DET_DEVICE:-cuda}"


# directory setup

mkdir -p "$AGIR_LOG_DIR"
mkdir -p "$FINAL_DET_DIR"

# only succeed if the user owns the shared log directory. Failures are ignored.
chmod u+rwx "$AGIR_LOG_DIR" 2>/dev/null || true
chmod g+rwx "$AGIR_LOG_DIR" 2>/dev/null || true
chmod g+s "$AGIR_LOG_DIR" 2>/dev/null || true
chmod -t "$AGIR_LOG_DIR" 2>/dev/null || true

echo "Job started at $(date)"
echo "Running user:           ${USER:-unknown}"
echo "AGIR_USER:              $AGIR_USER"
echo "AGIR_USER_ROOT:         $AGIR_USER_ROOT"
echo "Repo:                   $REPO_DIR"
echo "UV env:                 $UV_ENV"
echo "Batch ID:               $BATCH_ID"
echo "JPG input dir:          $JPG_INPUT_DIR"
echo "Detection output dir:   $FINAL_DET_DIR"
echo "Shared log dir:         $AGIR_LOG_DIR"

# Validation

if [[ ! -d "$REPO_DIR" ]]; then
  echo "Repository directory does not exist: $REPO_DIR" >&2
  exit 1
fi

if [[ ! -f "$UV_ENV" ]]; then
  echo "UV environment activate script does not exist: $UV_ENV" >&2
  exit 1
fi

if [[ ! -d "$JPG_INPUT_DIR" ]]; then
  echo "JPG input directory does not exist: $JPG_INPUT_DIR" >&2
  exit 1
fi

if [[ ! -f "$DET_CFG_PATH" ]]; then
  echo "Detection config does not exist: $DET_CFG_PATH" >&2
  exit 1
fi

if [[ ! -f "$MODEL_PATH" ]]; then
  echo "Model path does not exist: $MODEL_PATH" >&2
  exit 1
fi

if [[ ! -f "$VIZ_SCRIPT" ]]; then
  echo "Visualization script does not exist: $VIZ_SCRIPT" >&2
  exit 1
fi

# Environment
echo "Sourcing environment..."
source "$UV_ENV"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Run JPG -> DET

echo "Running JPG -> DET with model $MODEL_PATH..."

python3 -m stages.jpg_to_det.cli \
  --c "$DET_CFG_PATH" \
  --m "$MODEL_PATH" \
  --i "$JPG_INPUT_DIR" \
  --o "$FINAL_DET_DIR" \
  --t "$DET_THREADS" \
  --fs \
  --batch-id "$BATCH_ID" \
  --device "$DET_DEVICE"

# resolve the most recently updated detection artifacts directory.

DET_ARTIFACT_DIR="$(ls -td "$FINAL_DET_DIR"/jpg_to_det/*/artifacts 2>/dev/null | head -n 1)"

if [[ -z "$DET_ARTIFACT_DIR" || ! -d "$DET_ARTIFACT_DIR" ]]; then
  echo "Could not resolve detection artifacts directory under $FINAL_DET_DIR/jpg_to_det" >&2
  exit 1
fi

DET_RUN_DIR="$(dirname "$DET_ARTIFACT_DIR")"
FINAL_VIZ_DIR="${FINAL_VIZ_DIR:-$DET_RUN_DIR/visualizations}"

mkdir -p "$FINAL_VIZ_DIR"

# render visualization sample

echo "Rendering random overlay sample to $FINAL_VIZ_DIR..."

python3 "$VIZ_SCRIPT" \
  --images "$JPG_INPUT_DIR" \
  --detections "$DET_ARTIFACT_DIR" \
  --output "$FINAL_VIZ_DIR" \
  --sample-size "$VIZ_SAMPLE_SIZE" \
  --max-width "$VIZ_MAX_WIDTH"

# promote detection artifacts to final destination
FINAL_DEST="/90daydata/dash_agir/semifield-developed-images/${BATCH_ID}/detections"

echo "Promoting artifacts to $FINAL_DEST..."
python3 -m stages.jpg_to_det.promote \
  --run-dir "$DET_RUN_DIR" \
  --dest "$FINAL_DEST"

echo "Job finished at $(date)"
echo "Detection root:     $FINAL_DET_DIR"
echo "Detection run:      $DET_RUN_DIR"
echo "Artifacts:          $DET_ARTIFACT_DIR"
echo "Visualizations:     $FINAL_VIZ_DIR"
echo "Promoted to:        $FINAL_DEST"
