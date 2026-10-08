#!/usr/bin/env bash

set -uo pipefail

usage() {
    cat <<'EOF'
Usage:
  benchmark_det_to_seg.sh BATCH_ID DETECTIONS_DIR IMAGES_DIR GEOREFERENCED_CSV BATCH_SIZE [BATCH_SIZE ...]

Examples:
  benchmark_det_to_seg.sh <batch-id> <detections-dir> <images-dir> <georeferenced.csv> 16
  benchmark_det_to_seg.sh <batch-id> <detections-dir> <images-dir> <georeferenced.csv> 8 16 32

Each batch-size setting is run sequentially for the supplied batch.

Machine-specific paths can be supplied through these environment variables:
  PYTHON, SEG_CONFIG, SEG_MODEL, SPECIES_CATALOG, OUTPUT,
  BENCHMARK_ROOT, and SEG_DEVICE.
EOF
}

if [[ $# -lt 5 ]]; then
    usage >&2
    exit 2
fi

batch_id="$1"
detections_dir="$2"
images_dir="$3"
georeferenced_csv="$4"
shift 4

for batch_size in "$@"; do
    if [[ ! "$batch_size" =~ ^[1-9][0-9]*$ ]]; then
        echo "Invalid batch size: $batch_size (expected a positive integer)" >&2
        exit 2
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
BASE_CONFIG="${SEG_CONFIG:-$REPO_DIR/configs/config.det_to_seg.benchmark.yaml}"
SPECIES_CATALOG="${SPECIES_CATALOG:-/path/to/species_catalog.json}"
OUTPUT="${OUTPUT:-/path/to/stage-output}"
BENCHMARK_ROOT="${BENCHMARK_ROOT:-/path/to/benchmarks/det_to_seg}"
DEVICE="${SEG_DEVICE:-cuda:0}"
MODEL="${SEG_MODEL:-/path/to/plant_segmentor.pth}"

for required_file in "$PYTHON" "$BASE_CONFIG" "$SPECIES_CATALOG" "$MODEL"; do
    if [[ ! -f "$required_file" ]]; then
        echo "Required file not found: $required_file" >&2
        exit 2
    fi
done

if [[ ! -d "$detections_dir" ]]; then
    echo "Detection directory not found: $detections_dir" >&2
    exit 2
fi
if [[ ! -d "$images_dir" ]]; then
    echo "JPG directory not found: $images_dir" >&2
    exit 2
fi
if [[ ! -f "$georeferenced_csv" ]]; then
    echo "Georeferenced CSV not found: $georeferenced_csv" >&2
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

for batch_size in "$@"; do
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    metrics_dir="$BENCHMARK_ROOT/${batch_id}_bs${batch_size}_${stamp}"
    mkdir -p "$metrics_dir"

    run_config="$metrics_dir/config.yaml"
    awk -v batch_size="$batch_size" -v model="$MODEL" '
            BEGIN { batch_replaced = 0; weights_replaced = 0 }
            /^batch_size:[[:space:]]*/ {
                print "batch_size: " batch_size
                batch_replaced = 1
                next
            }
            /^weights:[[:space:]]*/ {
                print "weights: " model
                weights_replaced = 1
                next
            }
            { print }
            END {
                if (!batch_replaced) print "batch_size: " batch_size
                if (!weights_replaced) print "weights: " model
            }
    ' "$BASE_CONFIG" > "$run_config"

    echo
    echo "Starting det_to_seg benchmark"
    echo "  batch:      $batch_id"
    echo "  batch size: $batch_size"
    echo "  device:     $DEVICE"
    echo "  metrics:    $metrics_dir"

    nvidia-smi > "$metrics_dir/nvidia-smi-before.txt"

    nvidia-smi \
            --query-gpu=timestamp,index,name,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,temperature.gpu \
            --format=csv \
            --loop=1 > "$metrics_dir/nvidia-smi.csv" &
    GPU_LOG_PID=$!

    start_epoch=$(date +%s)

    /usr/bin/time -v -o "$metrics_dir/time.txt" \
            "$PYTHON" -m stages.det_to_seg.cli \
            --i "$detections_dir" \
            --j "$images_dir" \
            --c "$run_config" \
            --o "$OUTPUT" \
            --georeferenced-csv "$georeferenced_csv" \
            --species-catalog "$SPECIES_CATALOG" \
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
            echo "batch_size=$batch_size"
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

with open(sys.argv[1], encoding="utf-8") as report_file:
    report = json.load(report_file)

duration_seconds = report["duration_ms"] / 1000
image_count = report["inputs"]["n_units_discovered"]
counts = report["outputs"]["counts"]
timing = report.get("timing", {})

print(f"status={report['status']}")
print(f"images={image_count}")
print(f"images_succeeded={counts['n_units_succeeded']}")
print(f"images_failed={counts['n_units_failed']}")
print(f"stage_duration_seconds={duration_seconds:.3f}")
if duration_seconds:
    print(f"throughput_images_per_second={image_count / duration_seconds:.3f}")
print(f"fallback_detection_count={report.get('fallback_detection_count', 0)}")
print(f"forward_calls_total={timing.get('forward_calls_total', 0)}")
for name in ("decode_seconds", "inference_seconds", "write_seconds"):
    values = timing.get(name, {})
    print(f"{name}_mean={values.get('mean', 0):.3f}")
    print(f"{name}_p90={values.get('p90', 0):.3f}")
if "peak_gpu_memory_mb" in timing:
    print(f"torch_peak_gpu_memory_mb={timing['peak_gpu_memory_mb']:.1f}")
PY
            fi
    } | tee "$metrics_dir/summary.txt"

    if [[ $stage_exit -ne 0 ]]; then
        echo "Benchmark failed for batch=$batch_id batch_size=$batch_size; stopping." >&2
        exit "$stage_exit"
    fi
done

echo
echo "All requested det_to_seg benchmarks completed."
