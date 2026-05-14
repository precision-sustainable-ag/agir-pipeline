# JPG To Detection Performance

## Compute Job Settings

```sh
#SBATCH --partition=gpu-a100
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
```

## Standard Config

| Hardware | Script | Start | End | Runtime (s) | Status |
|----------|--------|-------|-----|-------------|--------|
| A100 | `tests/gpu/jpg_to_det/jpg_to_det_a100.sh` | `2026-04-07 14:14:14` | `2026-04-07 14:28:00` | `825` | Success |
| L40S | `tests/gpu/jpg_to_det/jpg_to_det_l40s.sh` | `2026-04-07 14:14:38` | `2026-04-07 14:32:53` | `1094` | Success |

## Low-Latency / Reduced Config

| Hardware | Script | Start | End | Runtime (s) | Status |
|----------|--------|-------|-----|-------------|--------|
| A100 | low-latency script | `2026-04-07 16:04:36` | `2026-04-07 16:10:27` | `351` | Success |
| A100 MIG7 | `tests/gpu/jpg_to_det/jpg_to_det_a100_mig7.sh` | `2026-04-07 14:14:26` | `2026-04-07 14:22:43` | `497` | Success |
| L40S | low-latency script | `2026-04-07 16:04:30` | `2026-04-07 16:11:40` | `430` | Not run yet |

## Bash Script Notes
The table below describes the different elements required for the example GPU SLURM script.

The visualization step in the GPU test scripts uses [visualize_detections.py](/Users/brennenfarrell/Random/psa/agir-pipeline/scripts/visualize_detections.py). That helper reads YOLO-format `.txt` outputs from the resolved `artifacts/` directory, draws the predicted boxes and confidence values onto a random sample of JPGs, and downscales the rendered images for easier review.

| Item | Value |
|------|-------|
| JPG_INPUT_DIR | Directory of input JPG images passed into the detector. |
| FINAL_DET_DIR | Top-level detection output directory. The stage writes run outputs underneath this path. |
| FINAL_VIZ_DIR | Directory where rendered overlay visualization JPGs are written. |
| MODEL_PATH | Path to the YOLO checkpoint used for inference. |
| DET_CFG_PATH | Path to the detection config YAML used for the run. |
| DET_DEVICE | Torch device used for inference, such as cuda or cpu. |
| DET_THREADS | Number of worker processes used by the detector. |
| BATCH_ID | Batch identifier recorded in the run metadata and output naming. |
| VIZ_SCRIPT | Path to the visualization script that draws boxes on sampled images. |
| VIZ_SAMPLE_SIZE | Number of images randomly selected for visualization output. |
| VIZ_MAX_WIDTH | Maximum width of each rendered visualization image after downscaling. |
| PYTORCH_CUDA_ALLOC_CONF | PyTorch CUDA allocator setting used to reduce fragmentation on smaller GPUs. |
| artifacts/ layout | Detection .txt files are written under {FINAL_DET_DIR}/jpg_to_det/{run_id}/artifacts/, not directly into FINAL_DET_DIR. |
| DET_ARTIFACT_DIR | Artifacts directory used as the input to the visualization step. |
| SLURM log path | Stdout/stderr output path written by the batch job. |
