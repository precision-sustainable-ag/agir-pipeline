#!/usr/bin/env bash
#
# Weekly Globus Transfer - Wrapper Script
# Run this via cron for automated weekly transfers
#
# Usage: ./globus_transfer.sh

#!/usr/bin/env bash
#SBATCH --job-name=globus_transfer
#SBATCH --account=dash_agir
#SBATCH --partition=compute          # adjust if needed
#SBATCH --time=1:00:00
#SBATCH --cpus-per-task=12
#SBATCH --mem=8G
#SBATCH --output=/project/dash_agir/logs/weekly_globus/globus_transfer_%x_%j.out.log
#SBATCH --error=/project/dash_agir/logs/weekly_globus/globus_transfer_%x_%j.err.log

set -Euo pipefail
source /project/dash_agir/postgres/pg_coords.env
source # source to your uv env

STATES=  # enter state(s) like "NC TX MD"
START_DATE=  # enter date like "2025-10-01"
END_DATE=  # enter date like "2026-06-30"

python /project/dash_agir/matthew.kutugata/repos/agir-pipeline/scripts/globus_transfer.py \
    --states $STATES \
    --start-date $START_DATE \
    --end-date $END_DATE