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
FINAL_VIZ_DIR="${FINAL_VIZ_DIR:-$FINAL_SEG_DIR/visualizations}"

SEG_CFG_PATH="${SEG_CFG_PATH:-$REPO_DIR/stages/det_to_seg/configs/default.yaml}"
VIZ_SCRIPT="${VIZ_SCRIPT:-$REPO_DIR/scripts/visualize_segmentation.py}"

# viz sampling and rendering parameters
VIZ_SAMPLE_SIZE="${VIZ_SAMPLE_SIZE:-24}"
VIZ_MAX_WIDTH="${VIZ_MAX_WIDTH:-1800}"
SEG_THREADS="${SEG_THREADS:-1}"
SEG_DEVICE="${SEG_DEVICE:-cuda:0}"
BATCH_ID="${BATCH_ID:-new-test-sample}"

mkdir -p /project/dash_agir/brennen.farrell/logs
mkdir -p "$FINAL_SEG_DIR" "$FINAL_VIZ_DIR"

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

RUN_DIR="$(ls -td "$FINAL_SEG_DIR"/det_to_seg/* 2>/dev/null | head -n 1)"
MASK_DIR="$RUN_DIR/artifacts/masks"

if [[ -z "$RUN_DIR" || ! -d "$RUN_DIR" ]]; then
  echo "det_to_seg run directory was not created under $FINAL_SEG_DIR/det_to_seg" >&2
  exit 1
fi

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

echo "Rendering random overlay sample to $FINAL_VIZ_DIR..."
python3 "$VIZ_SCRIPT" \
  --images "$JPG_INPUT_DIR" \
  --detections "$DET_ARTIFACT_DIR" \
  --masks "$MASK_DIR" \
  --output "$FINAL_VIZ_DIR" \
  --sample-size "$VIZ_SAMPLE_SIZE" \
  --max-width "$VIZ_MAX_WIDTH"

echo "Job finished at $(date)"
echo "Detections:           $DET_ARTIFACT_DIR"
echo "Segmentation outputs: $SEG_ARTIFACT_DIR"
echo "Visualizations:   $FINAL_VIZ_DIR"
