#!/usr/bin/env bash
#SBATCH --job-name=seg_to_cut_group
#SBATCH --account=dash_agir
#SBATCH --partition=compute
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=1-00:00:00
#SBATCH --output=logs/seg_to_cut_group_%j.out
#SBATCH --error=logs/seg_to_cut_group_%j.err

set -Eeuo pipefail
umask 002

# Repo root = sbatch submit dir (Slurm runs a spooled copy of this script).
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$PWD}"
# Set SEG_TO_CUT_CONFIG when submitting (sbatch exports the caller's
# environment) to use another config.
CONFIG_FILE="${SEG_TO_CUT_CONFIG:-configs/config.seg_to_cut.ceres.example.yaml}"
STAGE="seg_to_cut"
# seg_to_cut stages inputs to, runs on, and publishes to CERES (see
# transfer.routes.seg_to_cut.destination_site in CONFIG_FILE).
SITE="CERES"
POLL_SECONDS=60

# No rerun option: seg_to_cut isn't in RERUN_STAGES
# (orchestrator/input_staging_planner.py), so stage_inputs.py rejects --rerun
# for it. To re-cut a batch that already has a successful seg_to_cut run,
# invalidate that run first with scripts/admin/invalidate_stage_runs.py.

GROUP_FILE="${1:?Usage: sbatch run_seg_to_cut_group.sbatch <group-file>}"

GROUP_START_EPOCH=$(date +%s)
GROUP_START_TIME=$(date --iso-8601=seconds)

echo "Group started: ${GROUP_START_TIME}"
echo "Config: ${CONFIG_FILE}"

cd "${PROJECT_DIR}"

# Use your actual environment setup.
CUDA_VISIBLE_DEVICES="" bash ./setup.sh
source "${PROJECT_DIR}/.venv/bin/activate"

if [[ ! -f "${GROUP_FILE}" ]]; then
    echo "Batch group file not found: ${GROUP_FILE}" >&2
    exit 1
fi

batch_job_submitted() {
    local batch_id="$1"
    local job_name="${STAGE}_${batch_id}"

    # Wait for the submitted job to appear in Slurm.
    local attempts=0

    while ! squeue \
        --noheader \
        --user="${USER}" \
        --name="${job_name}" \
        | grep -q .; do

        attempts=$((attempts + 1))

        if (( attempts >= 10 )); then
            return 1
        fi

        sleep 6
    done
}

wait_for_batch_job() {
    local batch_id="$1"
    local job_name="${STAGE}_${batch_id}"

    echo "Waiting for Slurm job: ${job_name}"

    # Wait until the job is no longer pending or running.
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

skipped_batches=()

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
        --site "${SITE}"

    python scripts/job/poll_stage_inputs.py \
        --stage "${STAGE}" \
        --config "${CONFIG_FILE}" \
        --wait \
        --interval 60

    python scripts/job/submit.py \
        --stage "${STAGE}" \
        --config "${CONFIG_FILE}" \
        --batches "${batch_file}" \
        --site "${SITE}"

    rm -f "${batch_file}"

    # submit.py exits 0 without submitting when a batch isn't in
    # v_batches_needing_seg_to_cut (missing images/segmentations/georeferenced
    # CSV, or already has a successful run), fails its staging gate, or holds
    # an active lease — skip it so one unready batch doesn't end the group.
    if ! batch_job_submitted "${batch_id}"; then
        echo "No Slurm job submitted for ${batch_id}; skipping." >&2
        skipped_batches+=("${batch_id}")
        continue
    fi

    # Do not submit the next batch until this batch's seg_to_cut job
    # finishes, so this group never holds more than one seg_to_cut job
    # (8 cpus / 64G, see slurm: in CONFIG_FILE) in flight at a time.
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
echo "Skipped (not submitted): ${#skipped_batches[@]}"
for batch_id in "${skipped_batches[@]}"; do
    echo "  ${batch_id}"
done
echo "=================================================="
