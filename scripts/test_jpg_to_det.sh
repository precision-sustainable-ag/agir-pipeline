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
#SBATCH --chdir=/project/dash_agir/matthew.kutugata
#SBATCH -o /project/dash_agir/logs/jpg_to_det/logs/%x-%j.out
#SBATCH -e /project/dash_agir/logs/jpg_to_det/logs/%x-%j.err

set -euo pipefail

export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

# repo and environment setup
REPO_DIR="${REPO_DIR:-/project/dash_agir/matthew.kutugata/repos/agir-pipeline}"
UV_ENV="${UV_ENV:-/project/dash_agir/matthew.kutugata/software/uv/venvs/agir_pipeline/bin/activate}"

# processing parameters
BATCH_ID="${BATCH_ID:-MD_2025-04-25}"

# input/output paths
JPG_INPUT_DIR="${JPG_INPUT_DIR:-/90daydata/dash_agir/semifield-developed-images/MD_2025-04-25/images}"
FINAL_DET_DIR="${FINAL_DET_DIR:-$REPO_DIR/output/MD_2025-04-25/detections}"
FINAL_VIZ_DIR="${FINAL_VIZ_DIR:-$REPO_DIR/output/MD_2025-04-25/visualizations}"

# model, config, and script paths
DET_CFG_PATH="${DET_CFG_PATH:-$REPO_DIR/configs/myconfig.yaml}"
MODEL_PATH="${MODEL_PATH:-/90daydata/dash_agir/semifield-tools/models/plant_detector/With_Synthetic_Train_Data/weights/best.pt}"
VIZ_SCRIPT="${VIZ_SCRIPT:-$REPO_DIR/scripts/visualize_detections.py}"

# viz sampling and rendering parameters
VIZ_SAMPLE_SIZE="${VIZ_SAMPLE_SIZE:-24}"
VIZ_MAX_WIDTH="${VIZ_MAX_WIDTH:-1800}"
DET_THREADS="${DET_THREADS:-1}"
DET_DEVICE="${DET_DEVICE:-cuda}"

mkdir -p /project/dash_agir/matthew.kutugata/logs
mkdir -p "$FINAL_DET_DIR" "$FINAL_VIZ_DIR"

# Track child PIDs we explicitly launch in background
CHILD_PIDS=()

cleanup() {
  local exit_code=$?

  echo "[$(date)] cleanup start (exit code: $exit_code)" >&2

  # Prevent recursive trap behavior
  trap - EXIT ERR SIGINT SIGTERM

  # First try to terminate tracked children gracefully
  if ((${#CHILD_PIDS[@]} > 0)); then
    for pid in "${CHILD_PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        echo "Sending TERM to child PID $pid" >&2
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done

    sleep 5

    for pid in "${CHILD_PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        echo "Child PID $pid still alive; sending KILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  fi

  # Optional: kill any remaining descendants of this shell
  # This is a stronger cleanup layer.
  pkill -TERM -P $$ 2>/dev/null || true
  sleep 2
  pkill -KILL -P $$ 2>/dev/null || true

  echo "[$(date)] cleanup done" >&2
  exit "$exit_code"
}

trap cleanup EXIT ERR SIGINT SIGTERM

echo "Sourcing environment..."
source "$UV_ENV"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

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