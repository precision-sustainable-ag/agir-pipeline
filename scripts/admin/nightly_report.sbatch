#!/usr/bin/env bash
#SBATCH --job-name=agir_nightly_report
#SBATCH --account=dash_agir
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:30:00
#SBATCH --output=logs/nightly_report/agir_nightly_report_%j.out
#SBATCH --error=logs/nightly_report/agir_nightly_report_%j.out
#
# Emails scripts/admin/nightly_report.py (stage runs, the nightly Globus
# index, failed Slurm jobs, stuck leases/staging/syncs over the last
# REPORT_HOURS) to REPORT_TO every day at REPORT_AT. The dashboard's
# "Daily report" panel shows the same report on demand.
#
# Start the daily cycle once, from the repo root, with:
#   mkdir -p logs/nightly_report
#   sbatch --begin=07:00 scripts/admin/nightly_report.sbatch
# Each run queues tomorrow's REPORT_AT run (like scripts/admin/dashboard.sbatch),
# so it keeps going until you scancel the pending agir_nightly_report job.

set -Eeuo pipefail
umask 002

# Repo root = sbatch submit dir (Slurm runs a spooled copy of this script).
PROJECT_DIR="${SLURM_SUBMIT_DIR:-$PWD}"
REPORT_AT="${REPORT_AT:-07:00}"
REPORT_HOURS="${REPORT_HOURS:-24}"
REPORT_TO="${REPORT_TO:-mkutuga@ncsu.edu}"  # space-separated for several
REPORT_FROM="${REPORT_FROM:-matthew.kutugata@usda.gov}"

# Queue tomorrow's run first, so a failure today doesn't break the chain.
# Submit the repo copy, not "$0" (Slurm's spool copy), so edits here take
# effect. Skip if a future run is already queued.
queued=$(squeue --noheader --user="${USER}" --name=agir_nightly_report --states=PENDING --format=%i \
    | grep -vx "${SLURM_JOB_ID:-none}" || true)
if [[ -z "${queued}" ]]; then
    next_run=$(date -d "tomorrow ${REPORT_AT}" +%Y-%m-%dT%H:%M:%S)
    echo "Queueing next report for ${next_run}"
    sbatch --begin="${next_run}" "${PROJECT_DIR}/scripts/admin/nightly_report.sbatch"
else
    echo "Next report already queued: ${queued}"
fi

cd "${PROJECT_DIR}"
source .venv/bin/activate

# shellcheck disable=SC2086  # REPORT_TO may hold several addresses
python scripts/admin/nightly_report.py \
    --hours "${REPORT_HOURS}" \
    --email ${REPORT_TO} \
    --from "${REPORT_FROM}"

find logs/nightly_report -name "agir_nightly_report_*.out" -type f -mtime +60 -delete
