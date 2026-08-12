#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="/project/dash_agir/matthew.kutugata/repos/agir-pipeline"
MASTER_BATCH_FILE="configs/batch_list.example.txt"
GROUP_DIR="configs/det_to_world_groups"
GROUP_RUNNER="scripts/job/run_det_to_world_group.sbatch"

# det_to_world is cheap and fast (4 cpus / 16G / <=1h per batch, see
# slurm: in configs/config.det_to_world.example.yaml) compared to
# jpg_to_det, so groups here are bigger and spaced closer together —
# this is just about not dumping 50 jobs on the queue at once, not about
# working around contention for scarce resources.
BATCHES_PER_GROUP=12
INTERVAL_HOURS=1

cd "${PROJECT_DIR}"

mkdir -p "${GROUP_DIR}"

# Remove only previously generated group files.
rm -f "${GROUP_DIR}"/batch_group_[0-9][0-9].txt

# Remove blank lines and comments before splitting.
CLEAN_BATCH_FILE=$(mktemp)
trap 'rm -f "${CLEAN_BATCH_FILE}"' EXIT

grep -Ev '^[[:space:]]*(#|$)' \
    "${MASTER_BATCH_FILE}" \
    > "${CLEAN_BATCH_FILE}"

total_batches=$(wc -l < "${CLEAN_BATCH_FILE}")

split \
    --lines="${BATCHES_PER_GROUP}" \
    --numeric-suffixes=1 \
    --suffix-length=2 \
    --additional-suffix=".txt" \
    "${CLEAN_BATCH_FILE}" \
    "${GROUP_DIR}/batch_group_"

mapfile -t group_files < <(
    find "${GROUP_DIR}" \
        -maxdepth 1 \
        -type f \
        -name 'batch_group_[0-9][0-9].txt' \
        | sort
)

echo "Total batches: ${total_batches}"
echo "Total groups: ${#group_files[@]}"
echo

for index in "${!group_files[@]}"; do
    group_file="${group_files[$index]}"
    offset_hours=$((index * INTERVAL_HOURS))
    group_number=$((index + 1))

    if (( offset_hours == 0 )); then
        begin_time="now"
    else
        begin_time="now+${offset_hours}hours"
    fi

    echo "Group ${group_number}"
    echo "  Start: ${begin_time}"
    echo "  File:  ${group_file}"
    echo "  Batches:"
    sed 's/^/    /' "${group_file}"

    submit_output=$(
        sbatch \
            --begin="${begin_time}" \
            --job-name="det_to_world_group_${group_number}" \
            "${GROUP_RUNNER}" \
            "${group_file}"
    )

    echo "  ${submit_output}"
    echo
done
