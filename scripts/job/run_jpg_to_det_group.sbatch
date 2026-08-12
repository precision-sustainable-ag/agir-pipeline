#!/usr/bin/env bash
#SBATCH --job-name=jpg_to_det_group
#SBATCH --account=dash_agir
#SBATCH --partition=atlas
#SBATCH --time=1-00:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --output=/project/dash_agir/matthew.kutugata/repos/agir-pipeline/logs/jpg_to_det_group_%j.out
#SBATCH --error=/project/dash_agir/matthew.kutugata/repos/agir-pipeline/logs/jpg_to_det_group_%j.err

set -Eeuo pipefail
umask 002

PROJECT_DIR="/project/dash_agir/matthew.kutugata/repos/agir-pipeline"
CONFIG_FILE="configs/config.jpg_to_det.atlas.example.yaml"
STAGE="jpg_to_det"
POLL_SECONDS=60

GROUP_FILE="${1:?Usage: sbatch run_jpg_to_det_group.sbatch <group-file>}"

GROUP_START_EPOCH=$(date +%s)
GROUP_START_TIME=$(date --iso-8601=seconds)

echo "Group started: ${GROUP_START_TIME}"

cd "${PROJECT_DIR}"

# Use your actual environment setup.
CUDA_VISIBLE_DEVICES="" bash ./setup.sh
source "/project/dash_agir/matthew.kutugata/software/uv/venvs/agir_pipeline/bin/activate"

if [[ ! -f "${GROUP_FILE}" ]]; then
    echo "Batch group file not found: ${GROUP_FILE}" >&2
    exit 1
fi

wait_for_batch_job() {
    local batch_id="$1"
    local job_name="${STAGE}_${batch_id}"

    echo "Waiting for Slurm job: ${job_name}"

    # First wait for the submitted job to appear in Slurm.
    local attempts=0

    while ! squeue \
        --noheader \
        --user="${USER}" \
        --name="${job_name}" \
        | grep -q .; do

        attempts=$((attempts + 1))

        if (( attempts >= 10 )); then
            echo "Could not find ${job_name} after submission." >&2
            return 1
        fi

        sleep 6
    done

    # Then wait until the job is no longer pending or running.
    while squeue \
        --noheader \
        --user="${USER}" \
        --name="${job_name}" \
        | grep -q .; do

        squeue \
            --user="${USER}" \
            --name="${job_name}" \
            --format="%.18i %.35j %.10T %.20S %.30R"

        sleep "${POLL_SECONDS}"
    done

    echo "Slurm job left the queue: ${job_name}"

    # Check the most recent matching job's final state.
    local final_state

    final_state=$(
        sacct \
            --name="${job_name}" \
            --user="${USER}" \
            --starttime="$(date -d '2 days ago' +%Y-%m-%d)" \
            --noheader \
            --parsable2 \
            --format=JobIDRaw,JobName,State \
        | awk -F'|' -v name="${job_name}" '
            $2 == name && $1 !~ /\./ {
                state = $3
            }
            END {
                print state
            }
        '
    )

    echo "Final state for ${job_name}: ${final_state:-UNKNOWN}"

    case "${final_state}" in
        COMPLETED)
            return 0
            ;;
        "")
            echo "Warning: final state could not be determined." >&2
            return 0
            ;;
        *)
            echo "Batch job did not complete successfully: ${job_name}" >&2
            return 1
            ;;
    esac
}

while IFS= read -r batch_id || [[ -n "${batch_id}" ]]; do
    # Ignore empty lines and comments.
    [[ -z "${batch_id//[[:space:]]/}" ]] && continue
    [[ "${batch_id}" == \#* ]] && continue

    batch_file=$(mktemp)
    printf '%s\n' "${batch_id}" > "${batch_file}"

    echo
    echo "=================================================="
    echo "Starting batch: ${batch_id}"
    echo "Time: $(date --iso-8601=seconds)"
    echo "=================================================="

    python scripts/job/stage_inputs.py \
        --stage "${STAGE}" \
        --config "${CONFIG_FILE}" \
        --batches "${batch_file}" \
        --site CERES

    python scripts/job/poll_stage_inputs.py \
        --stage "${STAGE}" \
        --config "${CONFIG_FILE}" \
        --wait \
        --interval 60

    python scripts/job/submit.py \
        --stage "${STAGE}" \
        --config "${CONFIG_FILE}" \
        --batches "${batch_file}" \
        --site JUNO

    rm -f "${batch_file}"

    # Do not submit the next batch until this batch's downstream job finishes.
    wait_for_batch_job "${batch_id}"

    echo "Finished batch: ${batch_id}"
done < "${GROUP_FILE}"

GROUP_END_EPOCH=$(date +%s)
GROUP_END_TIME=$(date --iso-8601=seconds)

GROUP_DURATION_SECONDS=$((GROUP_END_EPOCH - GROUP_START_EPOCH))

printf -v GROUP_DURATION '%02d:%02d:%02d' \
    $((GROUP_DURATION_SECONDS / 3600)) \
    $(((GROUP_DURATION_SECONDS % 3600) / 60)) \
    $((GROUP_DURATION_SECONDS % 60))

echo
echo "=================================================="
echo "Group completed: ${GROUP_END_TIME}"
echo "Total group duration: ${GROUP_DURATION}"
echo "=================================================="