#!/usr/bin/env bash

set -uo pipefail

usage() {
    cat <<'EOF'
Usage:
  benchmark_jpg_to_det.sh BATCH_ID INPUT_DIR THREADS [THREADS ...]

Examples:
  benchmark_jpg_to_det.sh <batch-id> <input-dir> 8
  benchmark_jpg_to_det.sh <batch-id> <input-dir> 8 12

Each worker count is run sequentially for the supplied batch.

Machine-specific paths can be supplied through these environment variables:
  PYTHON, JPG_TO_DET_CONFIG, JPG_TO_DET_MODEL, OUTPUT,
  BENCHMARK_ROOT, and JPG_TO_DET_DEVICE.
EOF
}

if [[ $# -lt 3 ]]; then
    usage >&2
    exit 2
fi

batch_id="$1"
input_dir="$2"
shift 2

for threads in "$@"; do
    if [[ ! "$threads" =~ ^[1-9][0-9]*$ ]]; then
        echo "Invalid thread count: $threads (expected a positive integer)" >&2
        exit 2
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
CONFIG="${JPG_TO_DET_CONFIG:-$REPO_DIR/stages/jpg_to_det/configs/default.yaml}"
MODEL="${JPG_TO_DET_MODEL:-/path/to/plant_detector.pt}"
OUTPUT="${OUTPUT:-/path/to/stage-output}"
BENCHMARK_ROOT="${BENCHMARK_ROOT:-/path/to/benchmarks/jpg_to_det}"
DEVICE="${JPG_TO_DET_DEVICE:-cuda:0}"

for required_file in "$PYTHON" "$CONFIG" "$MODEL"; do
    if [[ ! -f "$required_file" ]]; then
        echo "Required file not found: $required_file" >&2
        exit 2
    fi
done

if [[ ! -d "$input_dir" ]]; then
    echo "Input directory not found: $input_dir" >&2
    exit 2
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "nvidia-smi is not available" >&2
    exit 2
fi

if [[ ! -x /usr/bin/time ]]; then
    echo "/usr/bin/time is not available" >&2
    exit 2
fi

mkdir -p "$BENCHMARK_ROOT" "$OUTPUT"
cd "$REPO_DIR" || exit 2
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

GPU_LOG_PID=""

stop_gpu_logger() {
    if [[ -n "$GPU_LOG_PID" ]] && kill -0 "$GPU_LOG_PID" 2>/dev/null; then
        kill "$GPU_LOG_PID" 2>/dev/null || true
        wait "$GPU_LOG_PID" 2>/dev/null || true
    fi
    GPU_LOG_PID=""
}

trap stop_gpu_logger EXIT
trap 'stop_gpu_logger; exit 130' INT TERM

for threads in "$@"; do
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    metrics_dir="$BENCHMARK_ROOT/${batch_id}_t${threads}_${stamp}"
    mkdir -p "$metrics_dir"

    echo
    echo "Starting jpg_to_det benchmark"
    echo "  batch:   $batch_id"
    echo "  threads: $threads"
    echo "  device:  $DEVICE"
    echo "  metrics: $metrics_dir"

    nvidia-smi > "$metrics_dir/nvidia-smi-before.txt"

    nvidia-smi \
        --query-gpu=timestamp,index,name,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,temperature.gpu \
        --format=csv \
        --loop=1 > "$metrics_dir/nvidia-smi.csv" &
    GPU_LOG_PID=$!

    start_epoch=$(date +%s)

    /usr/bin/time -v -o "$metrics_dir/time.txt" \
        "$PYTHON" -m stages.jpg_to_det.cli \
        --c "$CONFIG" \
        --m "$MODEL" \
        --i "$input_dir" \
        --o "$OUTPUT" \
        --t "$threads" \
        --fs \
        --batch-id "$batch_id" \
        --device "$DEVICE" \
        2>&1 | tee "$metrics_dir/stage-console.log"

    stage_exit=${PIPESTATUS[0]}
    end_epoch=$(date +%s)
    stop_gpu_logger

    nvidia-smi > "$metrics_dir/nvidia-smi-after.txt"

    report_path=$(sed -n 's/^.*Report:[[:space:]]*//p' "$metrics_dir/stage-console.log" | tail -n 1)

    {
        echo "batch_id=$batch_id"
        echo "threads=$threads"
        echo "device=$DEVICE"
        echo "stage_exit=$stage_exit"
        echo "wall_clock_seconds=$((end_epoch - start_epoch))"
        echo "metrics_dir=$metrics_dir"
        echo "run_report=${report_path:-not-found}"

        awk -F',' '
            NR > 1 {
                util = $4
                mem = $6
                power = $8
                temp = $9

                gsub(/[^0-9.]/, "", util)
                gsub(/[^0-9.]/, "", mem)
                gsub(/[^0-9.]/, "", power)
                gsub(/[^0-9.]/, "", temp)

                util += 0
                mem += 0
                power += 0
                temp += 0

                util_sum += util
                power_sum += power
                if (util > util_max) util_max = util
                if (mem > mem_max) mem_max = mem
                if (power > power_max) power_max = power
                if (temp > temp_max) temp_max = temp
                samples++
            }
            END {
                if (samples == 0) {
                    print "gpu_samples=0"
                    exit
                }
                printf "gpu_samples=%d\n", samples
                printf "gpu_utilization_average_percent=%.2f\n", util_sum / samples
                printf "gpu_utilization_peak_percent=%.0f\n", util_max
                printf "gpu_memory_peak_mib=%.0f\n", mem_max
                printf "gpu_power_average_w=%.2f\n", power_sum / samples
                printf "gpu_power_peak_w=%.2f\n", power_max
                printf "gpu_temperature_peak_c=%.0f\n", temp_max
            }
        ' "$metrics_dir/nvidia-smi.csv"

        if [[ -n "$report_path" && -f "$report_path" ]]; then
            "$PYTHON" - "$report_path" <<'PY'
import json
import sys

report_path = sys.argv[1]
with open(report_path, encoding="utf-8") as report_file:
    report = json.load(report_file)

duration_seconds = report["duration_ms"] / 1000
image_count = report["inputs"]["n_units_discovered"]

print(f"status={report['status']}")
print(f"images={image_count}")
print(f"stage_duration_seconds={duration_seconds:.3f}")
print(f"throughput_images_per_second={image_count / duration_seconds:.3f}")
PY
        fi
    } | tee "$metrics_dir/summary.txt"

    if [[ $stage_exit -ne 0 ]]; then
        echo "Benchmark failed for t=$threads; stopping." >&2
        exit "$stage_exit"
    fi
done

echo
echo "All requested benchmarks completed."
