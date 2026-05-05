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
#SBATCH --chdir=/project/dash_agir
#SBATCH -o /project/dash_agir/logs/det_to_seg/%u-%x-%j.out
#SBATCH -e /project/dash_agir/logs/det_to_seg/%u-%x-%j.err

set -euo pipefail

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

umask 002

# Shared/project setup

AGIR_PROJECT_ROOT="${AGIR_PROJECT_ROOT:-/project/dash_agir}"
AGIR_LOG_DIR="${AGIR_LOG_DIR:-$AGIR_PROJECT_ROOT/logs/det_to_seg}"

# Override with:
#   sbatch --export=ALL,AGIR_USER=matthew.kutugata det_to_seg_a100.sh
AGIR_USER="${AGIR_USER:-${USER:-$(whoami)}}"
AGIR_USER_ROOT="${AGIR_USER_ROOT:-$AGIR_PROJECT_ROOT/$AGIR_USER}"

# Repo and environment setup

REPO_DIR="${REPO_DIR:-$AGIR_USER_ROOT/repos/agir-pipeline}"
UV_ENV="${UV_ENV:-$AGIR_USER_ROOT/software/uv/venvs/agir_pipeline/bin/activate}"

# Input/output paths

BATCH_ID="${BATCH_ID:-MD_2025-04-25}"
BATCH_ROOT="${BATCH_ROOT:-/90daydata/dash_agir/tmp/semifield-developed-images/$BATCH_ID}"

JPG_INPUT_DIR="${JPG_INPUT_DIR:-$BATCH_ROOT/images}"
FINAL_DET_DIR="${FINAL_DET_DIR:-$BATCH_ROOT/a100/detections}"
FINAL_SEG_DIR="${FINAL_SEG_DIR:-$BATCH_ROOT/a100/segmentations}"

# Config and script paths

SEG_CFG_PATH="${SEG_CFG_PATH:-$REPO_DIR/stages/det_to_seg/configs/default.yaml}"
VIZ_SCRIPT="${VIZ_SCRIPT:-$REPO_DIR/tests/gpu/det_to_seg/visualize_segmentation.py}"

# Visualization/rendering parameters

VIZ_SAMPLE_SIZE="${VIZ_SAMPLE_SIZE:-24}"
VIZ_MAX_WIDTH="${VIZ_MAX_WIDTH:-1800}"
SEG_THREADS="${SEG_THREADS:-1}"
SEG_DEVICE="${SEG_DEVICE:-cuda:0}"

# Directory setup

mkdir -p "$AGIR_LOG_DIR"
mkdir -p "$FINAL_SEG_DIR"

chmod u+rwx "$AGIR_LOG_DIR" 2>/dev/null || true
chmod g+rwx "$AGIR_LOG_DIR" 2>/dev/null || true
chmod g+s "$AGIR_LOG_DIR" 2>/dev/null || true
chmod -t "$AGIR_LOG_DIR" 2>/dev/null || true

echo "Job started at $(date)"
echo "Running user:             ${USER:-unknown}"
echo "AGIR_USER:                $AGIR_USER"
echo "AGIR_USER_ROOT:           $AGIR_USER_ROOT"
echo "Repo:                     $REPO_DIR"
echo "UV env:                   $UV_ENV"
echo "Batch ID:                 $BATCH_ID"
echo "JPG input dir:            $JPG_INPUT_DIR"
echo "Detection output dir:     $FINAL_DET_DIR"
echo "Segmentation output dir:  $FINAL_SEG_DIR"
echo "Shared log dir:           $AGIR_LOG_DIR"

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

if [[ ! -d "$FINAL_DET_DIR" ]]; then
  echo "Detection directory does not exist: $FINAL_DET_DIR" >&2
  exit 1
fi

if [[ ! -f "$SEG_CFG_PATH" ]]; then
  echo "Segmentation config does not exist: $SEG_CFG_PATH" >&2
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

# Resolve latest detection artifacts

DET_ARTIFACT_DIR="$(ls -td "$FINAL_DET_DIR"/jpg_to_det/*/artifacts 2>/dev/null | head -n 1)"

if [[ -z "$DET_ARTIFACT_DIR" || ! -d "$DET_ARTIFACT_DIR" ]]; then
  echo "Could not resolve detection artifacts directory under $FINAL_DET_DIR/jpg_to_det" >&2
  exit 1
fi

# Run DET -> SEG

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

# Resolve and validate latest segmentation outputs

RUN_DIR="$(ls -td "$FINAL_SEG_DIR"/det_to_seg/* 2>/dev/null | head -n 1)"

if [[ -z "$RUN_DIR" || ! -d "$RUN_DIR" ]]; then
  echo "det_to_seg run directory was not created under $FINAL_SEG_DIR/det_to_seg" >&2
  exit 1
fi

MASK_DIR="$RUN_DIR/artifacts/masks"

FINAL_VIZ_DIR="$RUN_DIR/visualizations"
mkdir -p $FINAL_VIZ_DIR

if [[ ! -f "$RUN_DIR/manifest.json" ]]; then
  echo "manifest.json missing from $RUN_DIR" >&2
  exit 1
fi

if [[ ! -f "$RUN_DIR/run_report.json" ]]; then
  echo "run_report.json missing from $RUN_DIR" >&2
  exit 1
fi

if [[ ! -d "$MASK_DIR" ]]; then
  echo "mask artifacts directory missing: $MASK_DIR" >&2
  exit 1
fi

MASK_COUNT="$(find "$MASK_DIR" -name '*.png' | wc -l | tr -d ' ')"

if [[ "$MASK_COUNT" -eq 0 ]]; then
  echo "no mask PNGs were produced in $MASK_DIR" >&2
  exit 1
fi

echo "Validated output: $MASK_COUNT mask PNGs in $MASK_DIR"

# Render visualization sample

echo "Rendering random overlay sample to $FINAL_VIZ_DIR..."

python3 "$VIZ_SCRIPT" \
  --images "$JPG_INPUT_DIR" \
  --detections "$DET_ARTIFACT_DIR" \
  --masks "$MASK_DIR" \
  --output "$FINAL_VIZ_DIR" \
  --sample-size "$VIZ_SAMPLE_SIZE" \
  --max-width "$VIZ_MAX_WIDTH"

echo "Job finished at $(date)"
echo "Detections:              $DET_ARTIFACT_DIR"
echo "Segmentation run:        $RUN_DIR"
echo "Segmentation artifacts:  $RUN_DIR/artifacts/masks"
echo "Masks:                   $MASK_DIR"
echo "Visualizations:          $FINAL_VIZ_DIR"
