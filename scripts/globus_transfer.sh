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

python /project/dash_agir/matthew.kutugata/repos/agir-db/scripts/globus_transfer.py

sbatch --begin=now+2days "$0"