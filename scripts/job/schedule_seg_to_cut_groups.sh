#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Optional first argument: master batch list. Relative paths resolve against
# the repo root (PROJECT_DIR), not the caller's cwd.
MASTER_BATCH_FILE="${1:-configs/batch_list.example.txt}"
GROUP_DIR="configs/seg_to_cut_groups"
GROUP_RUNNER="scripts/job/run_seg_to_cut_group.sbatch"

# seg_to_cut is CPU-only but heavy (8 cpus / 64G / 12h per batch, see slurm:
# in configs/config.seg_to_cut.ceres.example.yaml). NC_2026-07-22 took ~3h
# end to end (~40 min input copy, ~70 min cutting, ~50 min promotion), so a
# 4-batch group runs ~12h and a 2h stagger keeps roughly 6 groups — i.e. 6
# seg_to_cut jobs — in flight at once. Each group's runner processes its
# batches one at a time (see wait_for_batch_job in GROUP_RUNNER).
BATCHES_PER_GROUP=4
INTERVAL_HOURS=2

cd "${PROJECT_DIR}"

if [[ ! -f "${MASTER_BATCH_FILE}" ]]; then
    echo "Master batch file not found: ${MASTER_BATCH_FILE}" >&2
    exit 1
fi

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

echo "Master batch file: ${MASTER_BATCH_FILE}"
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
            --job-name="seg_to_cut_group_${group_number}" \
            "${GROUP_RUNNER}" \
            "${group_file}"
    )

    echo "  ${submit_output}"
    echo
done
