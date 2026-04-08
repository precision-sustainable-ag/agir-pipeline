#!/usr/bin/env bash
set -euo pipefail

sbatch tests/gpu/jpg_to_det_a100.sh
sbatch tests/gpu/jpg_to_det_l40s.sh
sbatch tests/gpu/jpg_to_det_a100_mig7.sh
