#!/usr/bin/env bash
#SBATCH --job-name=agir_dashboard
#SBATCH --account=dash_agir
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=9:00:00
#SBATCH --output=/project/dash_agir/matthew.kutugata/repos/agir-pipeline/logs/dashboard/agir_dashboard_%j.out
#SBATCH --error=/project/dash_agir/matthew.kutugata/repos/agir-pipeline/logs/dashboard/agir_dashboard_%j.out
#
# Runs scripts/admin/dashboard.py on a compute node from START_AT to STOP_AT
# (default 08:00-17:00) and records its node/port in ~/.agir_dashboard.json.
# To open it, on the login node run:
#   /project/dash_agir/matthew.kutugata/repos/agir-pipeline/.venv/bin/python \
#       /project/dash_agir/matthew.kutugata/repos/agir-pipeline/scripts/admin/dashboard.py --connect
# which relays localhost:8765 to the job and prints the URL.
#
# Start the daily cycle once with:
#   sbatch scripts/admin/dashboard.sbatch
# Each run queues tomorrow's START_AT run (like scripts/admin/globus_index.sh),
# so it keeps going until you scancel the pending agir_dashboard job.

set -Eeuo pipefail
umask 002

PROJECT_DIR="/project/dash_agir/matthew.kutugata/repos/agir-pipeline"
START_AT="${START_AT:-08:00}"
STOP_AT="${STOP_AT:-17:00}"
TOKEN_FILE="${HOME}/.agir_dashboard_token"

# Queue tomorrow's run first, so a crash or scancel of today's run doesn't
# break the chain. Submit the repo copy, not "$0" (Slurm's spool copy), so
# edits here take effect. Skip if a future run is already queued.
queued=$(squeue --noheader --user="${USER}" --name=agir_dashboard --states=PENDING --format=%i \
    | grep -vx "${SLURM_JOB_ID:-none}" || true)
if [[ -z "${queued}" ]]; then
    next_start=$(date -d "tomorrow ${START_AT}" +%Y-%m-%dT%H:%M:%S)
    echo "Queueing next dashboard for ${next_start}"
    sbatch --begin="${next_start}" "${PROJECT_DIR}/scripts/admin/dashboard.sbatch"
else
    echo "Next dashboard already queued: ${queued}"
fi

# Only one dashboard at a time (e.g. a manual sbatch while the daily run is up).
others=$(squeue --noheader --user="${USER}" --name=agir_dashboard --states=RUNNING --format=%i \
    | grep -vx "${SLURM_JOB_ID:-none}" || true)
if [[ -n "${others}" ]]; then
    echo "agir_dashboard already running as job(s): ${others}; exiting."
    exit 0
fi

stop_epoch=$(date -d "${STOP_AT}" +%s)
run_seconds=$(( stop_epoch - $(date +%s) ))
if (( run_seconds <= 60 )); then
    echo "Past ${STOP_AT}; not starting."
    exit 0
fi

# Same token every day, so the browser cookie keeps working as the job moves
# between nodes.
if [[ ! -s "${TOKEN_FILE}" ]]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "${TOKEN_FILE}")
fi
export AGIR_DASHBOARD_TOKEN="$(< "${TOKEN_FILE}")"

# The node's -eth name is the one reachable from the login nodes (same
# lookup Open OnDemand's interactive apps use).
node_host=$(hostname -A | tr ' ' '\n' | grep eth | head -1)
node_host="${node_host:-$(hostname -f)}"

cd "${PROJECT_DIR}"
source .venv/bin/activate

echo "Dashboard on ${node_host} until ${STOP_AT} (${run_seconds}s)"
# timeout sends SIGTERM at STOP_AT; dashboard.py then marks itself stopped.
timeout "${run_seconds}" python scripts/admin/dashboard.py \
    --host 0.0.0.0 \
    --port 0 \
    --advertise-host "${node_host}" \
    || [[ $? -eq 124 ]]
