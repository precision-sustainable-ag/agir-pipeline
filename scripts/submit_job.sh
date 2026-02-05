#!/usr/bin/env bash
set -euo pipefail

# This script lives in repo_root/scripts/
# Repo root is its parent directory.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Only allow exactly one argument
if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 {pg|index|transfer}"
  exit 2
fi

alias="$1"

case "${alias}" in
  pg)
    job_script="${REPO_ROOT}/server/db_server.sh"
    ;;
  index)
    job_script="${REPO_ROOT}/scripts/globus_index.sh"
    ;;
  transfer)
    job_script="${REPO_ROOT}/scripts/globus_transfer.sh"
    ;;
  *)
    echo "Invalid command: '${alias}'"
    echo "Allowed values: pg, index, transfer"
    exit 2
    ;;
esac

if [[ ! -f "${job_script}" ]]; then
  echo "Job script not found: ${job_script}"
  exit 1
fi

detect_partition() {
  local cluster host
  cluster="$(sacctmgr -n show cluster format=cluster 2>/dev/null | head -n1 | tr -d ' ' || true)"
  host="$(hostname -s || true)"

  if [[ "${cluster}" =~ [Aa]tlas ]] || [[ "${host}" =~ [Aa]tlas ]]; then
    echo "atlas"
  else
    echo "compute"
  fi
}

partition="$(detect_partition)"

echo "Repo root : ${REPO_ROOT}"
echo "Job       : ${alias}"
echo "Script    : ${job_script}"
echo "Partition : ${partition}"
echo "Submitting job..."

# CLI partition overrides whatever is in the sbatch file
sbatch -p "${partition}" "${job_script}"
