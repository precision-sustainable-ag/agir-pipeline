# SAM 3 Plant Detection for jpg_to_det

This document covers the fine-tuned SAM 3 detector and the new `jpg_to_det` backend that runs it as an alternative to YOLO. It describes:

- how the model was trained in SemiF-Segmentation;
- how it compares with the production YOLO detector;
- how it plugs into agir-pipeline;
- what is still open.

**Short version:** a fine-tuned SAM 3 (`plant_boxes_002/boxes_v4_whole_2016`) finds 91% of labeled plants on 90 held-out images, against 79% for production YOLO. Its precision is slightly higher too (0.865 vs 0.841). It runs through the existing `jpg_to_det` stage and writes the same outputs, so no downstream stage changes. The labels it was scored against are human-reviewed CVAT labels that started from SAM 3 pre-labels, which may slightly favour SAM 3's box style (see [Caveats](#caveats)).

---

## 1. Background

[SAM 3](https://github.com/facebookresearch/sam3) (Meta, "Segment Anything with Concepts") detects objects from a **text prompt**. Given an image and the prompt `plant`, it returns a box and a score for every plant it finds.

Out of the box ("zero-shot") it misses about half of our plants. The fine-tuning below retrains its detection layers on our own labeled field images.

Only the **detector** is trained:
- the image and text encoders (`backbone.*`) stay frozen at Meta's weights;
- the mask head is not used;
- the loss covers boxes and classification only, following sam3's own box fine-tuning recipe.

The fine-tuned checkpoint holds only the retrained detector weights. It is a compact file of about 0.12 GB that is applied on top of the public `facebook/sam3` checkpoint (3.4 GB) when loaded.

Two prompts map onto the two classes `jpg_to_det` already writes:

| SAM 3 prompt | Class id | Class name | Used downstream by |
|---|---|---|---|
| `plant` | 0 | `plant` | det_to_world species assignment, det_to_seg |
| `color chart` | 1 | `color_checker` | det_to_world (assigned `COLORCHECKER`) |

---

## 2. Training procedure (SemiF-Segmentation)

Training is a **label → correct → fine-tune → pre-label again** loop. Each fine-tuned model pre-labels the next batch of images, so annotators correct boxes instead of drawing them from scratch.

```
            ┌──────────────────────────────────────────────────────────┐
            ▼                                                          │
  sample images ──► SAM 3 pre-labels ──► CVAT correction ──► pull labels ──► fine-tune ──► evaluate
  (AgIR v2)         (zero-shot, or the                (labels.db)      (sam3_finetune)
                     previous fine-tune)
```

All of this lives in SemiF-Segmentation (`python main.py mode=label ...` and `mode=sam3_finetune ...`). Run outputs are under `SemiF-Segmentation/outputs/runs/<project>/`.

### Round 1: `plant_boxes_001` (300 images)

- **Images:** the AgIR v2 sample of 300 images (`conf/label/r001_agir_v2_sample300_boxes.yaml`).
- **Pre-labels:** zero-shot SAM 3, with prompts `plant`, `grass` and `color chart`.
- **Correction:** annotators corrected the pre-labels as rectangles in CVAT project `semif_plant_boxes`, in 30 tasks of 10 images.
- **Fine-tunes trained on this round:**

| Run | Images (train / held out) | Input size | Epochs | Held-out AP | Held-out F1 @ 0.5 |
|---|---|---|---|---|---|
| zero-shot SAM 3 (on v2's held-out set) | – | 1008 | – | 0.411 | 0.604 |
| `boxes_v1_whole` | 162 / 28 | 1008 px | 100 | 0.709 | 0.834 |
| `boxes_v2_whole_2016` | 162 / 28 | 2016 px | 100 | 0.745 | 0.858 |
| `boxes_v3_whole_2016` | 255 / 45 (all 300) | 2016 px | 100 | 0.733 | 0.838 |

v1 and v2 were trained before all 300 images had been pulled. Going from 1008 to 2016 px input mostly helped with small plants. The three runs used different held-out sets, so their rows are not directly comparable with each other.

### Round 2: `plant_boxes_002` (600 new images)

- **Images:** the AgIR v2 sample of 600 images (`conf/label/r001_agir_v2_sample600_boxes.yaml`). None of them are in the 300 sample, and they are balanced across **MD / NC / TX**.
- **Pre-labels:** the round-1 fine-tune **v3** at 2016 px, using SAM 3's own predicted boxes (`box_from: model`).
- **Correction:** annotators corrected the pre-labels in CVAT project `semif_plant_boxes_sample600`, in 6 tasks of 100 images.

### Labeling conventions: what counts as a "plant"

A `plant` label means a **target plant growing in a pot**. Some plant material was deliberately left unlabeled, even though it is technically plant or plant-like:

- **Pulled weeds lying on the landscape fabric** between pots.
- **Non-target weeds** growing up through the fabric or out of the bottom of the pots.
- **Fallen leaves and other debris** on the landscape fabric.

Where SAM 3 pre-labeled any of these, the box was deleted during review. Annotators did not draw new boxes for them.

This applies in training and in evaluation:
- **Training:** the fine-tuned model learns that the `plant` prompt means "target potted plant", and to ignore this material. That is part of what fine-tuning teaches it over zero-shot SAM 3, which boxes anything plant-like.
- **Evaluation:** a detection on any of this material counts as a **false positive** for both SAM 3 and YOLO.

### The v4 fine-tune: `boxes_v4_whole_2016`

v4 is trained on both rounds: plant_boxes_002's labels plus plant_boxes_001's (`extra_labels_dbs`). The run's full settings are recorded in `outputs/runs/plant_boxes_002/sam3_finetune/boxes_v4_whole_2016/cfg.yaml`. The ones that matter:

| Setting | Value |
|---|---|
| Starting weights | `facebook/sam3` |
| Frozen | `backbone.*` (vision and text encoders) |
| Input | Each image whole, downscaled so its longest side is 2016 px. SAM 3 runs at 2016 px instead of its native 1008. |
| Data | 900 images: 810 train / 90 held out (10%, seed 42) |
| Boxes | 12,832 train / 1,160 held out. Boxes under 8 px (at the 2016 px scale) are dropped. |
| Schedule | 50 epochs, batch 1, lr 8e-5, weight decay 0.1, inverse-square-root schedule (warmup 200, cooldown 500, timescale 500) |
| Cost | 40,500 steps; 6 h 27 min on one GPU; 7 GB GPU memory |
| Selected checkpoint | Final epoch (best held-out AP among epochs 10/25/40/50) → `sam3_finetuned_best.pt` |

Reconstructed command (check it against `cfg.yaml` before rerunning):

```bash
cd SemiF-Segmentation
python main.py mode=sam3_finetune project.name=plant_boxes_002 \
  sam3_finetune.name=boxes_v4_whole_2016 proposals.resolution=2016 \
  'sam3_finetune.extra_labels_dbs=[outputs/runs/plant_boxes_001/labels.db]' \
  sam3_finetune.dataset.val_fraction=0.1 sam3_finetune.dataset.min_box_px=8 \
  sam3_finetune.train.epochs=50 'sam3_finetune.train.snapshots=[10,25,40]' \
  sam3_finetune.train.warmup=200 sam3_finetune.train.cooldown=500 sam3_finetune.train.timescale=500
```

### v4 results in SemiF-Segmentation's evaluation (90 held-out images, 1,160 boxes)

| Checkpoint | AP | AP50 | AP75 | Plant AP | Color chart AP | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|
| zero-shot SAM 3 | 0.451 | 0.686 | 0.496 | 0.299 | 0.602 | 0.746 | 0.522 | 0.614 |
| v3 (round-1 model) | 0.725 | 0.840 | – | – | – | 0.922 | 0.809 | 0.862 |
| v4, epoch 10 | 0.732 | 0.845 | 0.802 | 0.672 | 0.791 | 0.911 | 0.816 | 0.861 |
| **v4, final (selected)** | **0.750** | **0.846** | **0.815** | **0.700** | **0.800** | **0.923** | **0.822** | **0.870** |

Precision, recall and F1 are measured at score ≥ 0.5 and IoU ≥ 0.5.

Fine-tuning lifts AP from 0.45 to 0.75, and plant AP from 0.30 to 0.70. Most of that gain comes in the first 10 epochs. Round 2's 600 extra images add a smaller increase on top of v3 (AP 0.725 → 0.750).

---

## 3. Comparison with production YOLO in the pipeline

Both models ran through the real `jpg_to_det` CLI on the same 90 held-out v4 images. Inputs were the original full-resolution JPGs, and both runs finished 90/90.

- **SAM 3:** v4 `sam3_finetuned_best.pt` with `stages/jpg_to_det/configs/sam3.yaml`. One pass over the whole image at 2016 px, score ≥ 0.5, with no padding, multiscale, WBF or edge filtering.
- **YOLO:** `plant_detector/With_Synthetic_Train_Data/weights/last.pt` (the production model) with `stages/jpg_to_det/configs/default.yaml`. This is the full production setup:
  - 512 px mirror padding;
  - 5 scales (0.5–1.5 × 2048 px) merged with weighted box fusion;
  - an edge-aware confidence threshold (0.70, lowered to 0.45 near image edges).

A detection counts as correct (TP) if it overlaps an unmatched label of the same class with IoU ≥ 0.5. Matching is greedy, in order of descending confidence.

| Location | Images | Labels | SAM 3 P | SAM 3 R | SAM 3 F1 | YOLO P | YOLO R | YOLO F1 |
|---|---|---|---|---|---|---|---|---|
| MD | 38 | 510 | 0.888 | 0.918 | 0.903 | 0.860 | 0.843 | 0.851 |
| NC | 26 | 263 | 0.849 | 0.875 | 0.861 | 0.766 | 0.734 | 0.750 |
| TX | 26 | 387 | 0.845 | 0.917 | 0.880 | 0.868 | 0.749 | 0.804 |
| **All** | **90** | **1160** | **0.865** | **0.908** | **0.886** | **0.841** | **0.787** | **0.813** |

- **Overall:** SAM 3 finds 1,053 of 1,160 labeled objects against YOLO's 913, i.e. 140 more and 57% fewer misses (107 vs 247). It also produces slightly fewer false positives (165 vs 173). Some false positives for both models are boxes on deliberately unlabeled material: pulled or non-target weeds, and leaves on the landscape fabric (see [Labeling conventions](#labeling-conventions-what-counts-as-a-plant)). These are correct to exclude for this pipeline, so they are counted as errors.
- **Per image:** SAM 3 has the higher F1 on 57 images, YOLO on 17, and they tie on 16.
- **Location:** SAM 3 has higher recall at every site. NC improves most. In TX, YOLO is slightly more precise (0.868 vs 0.845) but misses far more plants.
- **What the previews show:**
  - SAM 3 is much better on small seedlings, where YOLO produces clusters of false boxes.
  - Both models struggle with dense, overlapping plants such as corn rows. There the "errors" are mostly disagreements about where one plant ends and the next begins.
- **Speed:** wall time for the 90 images, including model load, was 2 min 31 s for SAM 3 and 2 min 2 s for YOLO on a GH200. That's about 1.7 s per image for SAM 3. A100 timings on Atlas have not been measured yet.

The in-pipeline numbers (P 0.865 / R 0.908) differ from SemiF's evaluation (P 0.923 / R 0.822) for the same checkpoint. The pipeline feeds full-resolution JPGs while SemiF scored pre-downscaled copies, and the two use different matching code. The comparison between SAM 3 and YOLO is like-for-like within the pipeline run.

### Previews

The previews are in `reports/sam3_jpg_to_det_preview/`. `reports/` is gitignored, so they live in the local checkout only.

- `previews/<image>.jpg`: three panels per image, ground truth | SAM 3 | YOLO.
- `previews/legend.png`: what each box colour and style means.
  - Green: the detection matches a label.
  - Red: no matching label.
  - Dashed orange: a label the model missed.
  - The number on each box is its confidence, and `cc` marks a color checker.
- `summary.csv`: TP / FP / missed counts per image for both models.
- `make_previews.py`: regenerates the previews, the legend and the totals from the two run folders (`run_sam3/`, `run_yolo/`).

Good examples to start with:
- `NC_1661958325`: seedlings; SAM 3 has 3 false positives, YOLO 21.
- `MD_Row-30_1656093526`: dense corn; both models struggle.

---

## 4. Caveats

1. **The labels started from SAM 3 pre-labels.** Every held-out image was reviewed one by one in CVAT, and the scores use those reviewed labels, pulled before v4 was trained. Of the 1,160 held-out labels:
   - 1,139 are pre-labels the reviewer kept as they were;
   - 16 were edited;
   - 21 were drawn by hand.

   Across round 2, review also deleted 389 pre-labels. So the labels are human ground truth, but their box conventions were seeded by SAM 3: how a clump is split into plants, and how tight a box is. Where a SAM 3 box was acceptable, it was kept, even if a YOLO-style box would also have been acceptable. This can slightly favour SAM 3 when it is matched at IoU 0.5. YOLO's recall gap (247 missed against 107) is much larger than this effect plausibly explains. To remove the question entirely, a small test set could be labeled from scratch (see next steps).

2. **The held-out set is small:** 90 images (38 MD, 26 NC, 26 TX), with only 8 color-checker labels. Color-checker numbers should not be relied on.
3. **The comparison is uneven.** YOLO used its full production test-time stack, while SAM 3 used one pass at the default 0.5 threshold. YOLO's thresholds were tuned for production; SAM 3's have not been tuned at all.
4. **Tiny boxes:** training dropped boxes under 8 px at the 2016 px scale. Very small seedlings may be under-represented.

---

## 5. Pipeline integration

### Using it

Point the jpg_to_det stage config and model path at SAM 3 in the Atlas config (`configs/config.jpg_to_det.atlas.example.yaml` → your live config):

```yaml
paths:
  stage_config: stages/jpg_to_det/configs/sam3.yaml
  det_model_path: /90daydata/.../sam3_finetuned_best.pt   # copy of the v4 export
```

Run it locally:

```bash
python -m stages.jpg_to_det.cli --c stages/jpg_to_det/configs/sam3.yaml \
  --m /path/to/sam3_finetuned_best.pt --i <images> --o <out> --device cuda
```

Outputs are identical in format to the YOLO backend:
- `<stem>.txt` per image: `cls xc yc w h conf`, normalised;
- the batch CSV, with `classname` `plant` / `color_checker`;
- `run_report.json` and `manifest.json`.

No other stage changes.

### `stages/jpg_to_det/configs/sam3.yaml`

```yaml
backend: sam3
resolution: 2016          # must match the checkpoint's fine-tune resolution
base_checkpoint: null     # local sam3.pt; null = the base recorded in the checkpoint
conf: 0.5                 # score threshold
final_max_det: 1000
prompts:
  - {prompt: plant, class: 0, name: plant}
  - {prompt: color chart, class: 1, name: color_checker}
```

### What changed in agir-pipeline

| File | Change |
|---|---|
| `stages/jpg_to_det/sam3_detector.py` (new) | Loads a SemiF export or a full `sam3.pt`, ported from SemiF-Segmentation's `src/finetune/sam3_model.py` and `src/models/proposals/sam3.py`. Includes 2016 px support (`at_resolution`). `run_sam3()` returns the same `Nx6 [x1,y1,x2,y2,conf,cls]` tensor as `run_multiscale()`. |
| `stages/jpg_to_det/processor.py` | New `backend: yolo \| sam3` config key, defaulting to `yolo`, so existing configs are unaffected. SAM 3 branches for config validation, model loading and inference. SAM 3 always runs sequentially and ignores `--t`, so there is one model copy on the GPU. |
| `stages/jpg_to_det/configs/sam3.yaml` (new) | The config above. |
| `stages/jpg_to_det/cli.py` | `--m` help text. |
| `pyproject.toml` | New `sam3` extra (pinned sam3 commit `2345a4a`, `setuptools<81`, `einops`, `psutil`, `pycocotools`) and a `numpy>=2.2.6` override, because sam3 pins `numpy<2` but works with numpy 2. |
| `stages/jpg_to_det/tests/test_sam3_detector.py` (new) | 18 CPU tests with a fake SAM 3: box assembly, clipping, class mapping, config validation, YOLO-format output, sequential batching. |

Install the dependency with `uv pip install -e ".[all,sam3]"` (or add `sam3` to the extras `setup.sh` installs).

### Implementation notes

- **Per-image steps:**
  1. Read the JPG and convert BGR to RGB.
  2. SAM 3 resizes the image to 2016 × 2016 and normalises it.
  3. Encode the image once.
  4. Run one detection pass per prompt.
  5. Keep detections with score > `conf`, scale the boxes to full-image pixels, and clip them to the image.
  6. Sort by score and cap at `final_max_det`.
  7. Pass the result to the existing `export_predictions`.
- **Boxes only:** the model is built without the mask head, matching how it was fine-tuned, and returns boxes only. The stock `Sam3Processor` scales every detection's mask up to full image size, which on full-resolution field images wastes memory and time.
- **bf16:** inference runs under bf16 autocast on GPU, as SAM 3 requires. Scores are computed in float32.
- **sam3 quirk:** without the mask head, sam3 deletes the cached image features after the first prompt. The backend passes each prompt a copy, so all prompts reuse one image encoding.
- **Detection cap:** SAM 3 returns at most 200 detections per prompt. That hasn't been an issue on these images (the most in one image here was 99 plant detections), but very dense images could hit it.

### Deploying on Atlas

- **Base weights:** the v4 export records its base as `"sam3"`, which means a gated Hugging Face download at load time. Compute nodes are unlikely to have network or HF credentials. Copy `sam3.pt` (from `~/.cache/huggingface/hub/models--facebook--sam3/`) to `/90daydata/dash_agir/semifield-tools/models/` and set `base_checkpoint` to that path in `sam3.yaml`.
- **Model file:** copy `sam3_finetuned_best.pt` (122 MB) next to it and point `det_model_path` at it.
- **Environment:** install the `sam3` extra into the Atlas venv.
- **GPU and time limits:** benchmark on the A100 to set the sbatch time limit and check peak GPU memory. SAM 3 is roughly 850M parameters, and inference memory at 2016 px has not been measured yet.

---

## 6. Next steps

1. **Optional: a test set labeled from scratch.** A small set labeled without pre-labels would rule out any anchoring towards SAM 3's box conventions (caveat 1).
2. **Threshold tuning:** tune SAM 3's `conf` (0.5 is the default and was never tuned). A per-class threshold could help TX precision.
3. **Single-pass YOLO baseline:** compare against YOLO with multiscale, WBF, padding and edge filtering turned off, to see how much of YOLO's score comes from its test-time stack.
4. **Tiny-box filters:** rerun the 1008 and 2016 fine-tunes with the tiny-box filters (`min_box_px` / `min_area`) turned off, and check whether small-seedling recall improves.
5. **Atlas deployment:** stage the base and fine-tuned weights on `/90daydata`, install the extra, and run one real batch end to end (jpg_to_det → det_to_world → det_to_seg).
6. **Next labeling round:** pre-label it with v4 to continue the loop.
