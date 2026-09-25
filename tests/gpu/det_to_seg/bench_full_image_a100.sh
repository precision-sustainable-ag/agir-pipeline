#!/usr/bin/env bash
#SBATCH --job-name=det_to_seg_bench_full_image
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

# Compare per-box (production) vs whole-image segmentation on a sample of
# one batch. See tests/gpu/det_to_seg/bench_full_image.py for the modes.
#
# Override with e.g.:
#   sbatch --export=ALL,BATCH_ID=NC_2026-09-02,N_IMAGES=50 bench_full_image_a100.sh
#   sbatch --export=ALL,IMAGES_DIR=/path/images,DET_DIR=/path/detections bench_full_image_a100.sh

set -euo pipefail

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

umask 002

AGIR_PROJECT_ROOT="${AGIR_PROJECT_ROOT:-/project/dash_agir}"
AGIR_USER="${AGIR_USER:-${USER:-$(whoami)}}"
AGIR_USER_ROOT="${AGIR_USER_ROOT:-$AGIR_PROJECT_ROOT/$AGIR_USER}"
REPO_DIR="${REPO_DIR:-$AGIR_USER_ROOT/repos/agir-pipeline}"
UV_ENV="${UV_ENV:-$REPO_DIR/.venv/bin/activate}"

BATCH_ID="${BATCH_ID:-NC_2026-09-02}"
BATCH_ROOT="${BATCH_ROOT:-/90daydata/dash_agir/semifield-developed-images/$BATCH_ID}"
IMAGES_DIR="${IMAGES_DIR:-$BATCH_ROOT/images}"
DET_DIR="${DET_DIR:-$BATCH_ROOT/detections}"
OUT_DIR="${OUT_DIR:-/90daydata/dash_agir/tmp/bench_full_image/${BATCH_ID}_${SLURM_JOB_ID:-local}}"

SEG_CFG_PATH="${SEG_CFG_PATH:-$REPO_DIR/stages/det_to_seg/configs/default.yaml}"
SEG_DEVICE="${SEG_DEVICE:-cuda:0}"
N_IMAGES="${N_IMAGES:-30}"
MODES="${MODES:-boxes,full,grid}"
SAVE_DIFFS="${SAVE_DIFFS:-1}"

for path in "$REPO_DIR" "$IMAGES_DIR" "$DET_DIR"; do
  if [[ ! -d "$path" ]]; then
    echo "Directory does not exist: $path" >&2
    exit 1
  fi
done
for path in "$UV_ENV" "$SEG_CFG_PATH"; do
  if [[ ! -f "$path" ]]; then
    echo "File does not exist: $path" >&2
    exit 1
  fi
done

echo "Job started at $(date) on $(hostname)"
echo "Images:      $IMAGES_DIR"
echo "Detections:  $DET_DIR"
echo "Config:      $SEG_CFG_PATH"
echo "Output:      $OUT_DIR"
nvidia-smi -L || true

source "$UV_ENV"
cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

DIFF_FLAG=()
if [[ "$SAVE_DIFFS" == "1" ]]; then
  DIFF_FLAG=(--save-diffs)
fi

python3 tests/gpu/det_to_seg/bench_full_image.py \
  --images "$IMAGES_DIR" \
  --detections "$DET_DIR" \
  --c "$SEG_CFG_PATH" \
  --out "$OUT_DIR" \
  --n "$N_IMAGES" \
  --modes "$MODES" \
  --device "$SEG_DEVICE" \
  "${DIFF_FLAG[@]}"

echo "Job finished at $(date)"
