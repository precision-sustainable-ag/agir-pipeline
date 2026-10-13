"""
SAM 3 plant detection (text-prompted), as an alternative to the YOLO backend.

Loads a SAM 3 checkpoint fine-tuned in SemiF-Segmentation (mode=sam3_finetune):
either a full sam3.pt, or the compact export it writes (sam3_finetuned*.pt:
the trained detector weights, the base checkpoint they apply to, and the input
resolution they were trained at). The loading code is ported from
SemiF-Segmentation src/finetune/sam3_model.py and src/models/proposals/sam3.py.

Each image runs once through SAM 3 per text prompt, whole (SAM 3 resizes it to
resolution x resolution), with no mirror padding, multiscale, WBF or
edge-aware filtering. Only boxes and scores are computed: the model is built
without its segmentation head, as it was fine-tuned. Several images can share
one forward pass (run_sam3_batch): the image encoder takes them as a batch and
the grounding heads every (image, prompt) pair at once; the text prompts are
encoded once per processor.

The sam3 package is an optional dependency (`pip install -e ".[sam3]"`) and is
only imported when a model is loaded.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import cv2
import torch

logger = logging.getLogger(__name__)

SAM3_RESOLUTION = 1008  # what facebook/sam3 was trained at
PATCH = 14
DELTA_FORMAT = "sam3_detector_delta"  # SemiF-Segmentation's export_checkpoint format


# ---------------------------------------------------------------------------
# Checkpoint loading
# ---------------------------------------------------------------------------

def read_delta(path: str | Path) -> dict | None:
    """The fine-tuned export at `path`, or None if it's a full sam3.pt
    (memory-mapped, so a full checkpoint isn't read just to find out)."""
    obj = torch.load(str(path), map_location="cpu", weights_only=True, mmap=True)
    return obj if isinstance(obj, dict) and obj.get("format") == DELTA_FORMAT else None


def apply_delta(model, delta: dict) -> None:
    """Load the export's trained weights into a built SAM 3 image model, all or nothing."""
    state = model.state_dict()
    unknown = [k for k in delta["detector"] if k not in state]
    if unknown:
        raise ValueError(f"{len(unknown)} fine-tuned weights aren't in the model, e.g. {unknown[:5]}")
    for name, value in delta["detector"].items():
        if state[name].shape != value.shape:
            raise ValueError(f"{name}: fine-tuned shape {tuple(value.shape)} != model {tuple(state[name].shape)}")
    model.load_state_dict(delta["detector"], strict=False)


@contextlib.contextmanager
def at_resolution(resolution: int):
    """sam3's builders, within this block, make the image model for `resolution`
    px inputs instead of 1008: the ViT gets a (resolution / 14)^2 patch grid,
    and loading a checkpoint skips the position tables sized for another grid
    (RoPE freqs; computed, not learned, so the build's own are right)."""
    resolution = int(resolution)
    if resolution % PATCH:
        raise ValueError(f"SAM 3 resolution must be a multiple of {PATCH}, not {resolution}")
    if resolution == SAM3_RESOLUTION:
        yield
        return
    import sam3.model_builder as mb

    vit, load = mb.ViT, mb._load_checkpoint

    def sized_vit(*args, **kwargs):
        return vit(*args, **{**kwargs, "img_size": resolution})

    def load_checkpoint(model, checkpoint_path):
        state = model.state_dict()
        ckpt = torch.load(str(checkpoint_path), map_location="cpu", weights_only=True, mmap=True)
        ckpt = ckpt.get("model", ckpt) if isinstance(ckpt.get("model"), dict) else ckpt
        weights = {k[len("detector."):]: v for k, v in ckpt.items() if k.startswith("detector.")}
        weights = {k: v for k, v in weights.items()
                   if not (k.endswith("freqs_cis") and k in state and state[k].shape != v.shape)}
        missing, _ = model.load_state_dict(weights, strict=False)
        missing = [k for k in missing if not k.endswith("freqs_cis")]
        if missing:
            logger.warning("SAM 3 checkpoint %s is missing %d weights, e.g. %s",
                           checkpoint_path, len(missing), missing[:5])

    mb.ViT, mb._load_checkpoint = sized_vit, load_checkpoint
    try:
        yield
    finally:
        mb.ViT, mb._load_checkpoint = vit, load


def _base_checkpoint_path(base: str | None) -> str:
    """A sam3.pt path; "sam3" (or None) downloads facebook/sam3 from Hugging Face
    (gated: needs access and `hf auth login`, and network on the node)."""
    if base in (None, "sam3"):
        from sam3.model_builder import download_ckpt_from_hf

        return download_ckpt_from_hf(version="sam3")
    if not Path(base).exists():
        raise FileNotFoundError(f"SAM 3 base checkpoint not found: {base}")
    return str(base)


def _box_only_processor(model, resolution: int, device: str, confidence_threshold: float):
    """A Sam3Processor that returns boxes and scores only: the stock one also
    upsamples every detection's mask to the full image size."""
    from sam3.model import box_ops
    from sam3.model.sam3_image_processor import Sam3Processor

    from sam3.model.data_misc import FindStage

    class BoxOnlySam3Processor(Sam3Processor):
        _text_cache = None  # (prompts, forward_text output)

        def _text_features(self, prompts: tuple[str, ...]) -> dict:
            if self._text_cache is None or self._text_cache[0] != prompts:
                self._text_cache = (prompts, self.model.backbone.forward_text(list(prompts), device=self.device))
            return self._text_cache[1]

        @torch.inference_mode()
        def detect_batch(self, images: list[torch.Tensor], prompts: list[str]):
            """
            Boxes and scores for every prompt on every image, in one forward pass.

            Args:
                images: RGB uint8 CxHxW tensors, any size (resized here, on the
                    device, as set_image does).
                prompts: text prompts.

            Returns:
                [image][prompt] -> (boxes Nx4 xyxy in image px, scores N), on the device.
            """
            sizes = [tuple(im.shape[-2:]) for im in images]
            batch = torch.stack([self.transform(im.to(self.device, non_blocking=True)) for im in images])
            backbone_out = self.model.backbone.forward_image(batch)
            del batch
            backbone_out.update(self._text_features(tuple(prompts)))

            # one query per (image, prompt), image-major
            n_img, n_txt = len(images), len(prompts)
            find = FindStage(
                img_ids=torch.arange(n_img, device=self.device).repeat_interleave(n_txt),
                text_ids=torch.arange(n_txt, device=self.device).repeat(n_img),
                input_boxes=None, input_boxes_mask=None, input_boxes_label=None,
                input_points=None, input_points_mask=None,
            )
            outputs = self.model.forward_grounding(
                backbone_out=backbone_out, find_input=find,
                geometric_prompt=self.model._get_dummy_prompt(num_prompts=n_img * n_txt),
                find_target=None,
            )
            presence = outputs["presence_logit_dec"].float().sigmoid().unsqueeze(1)
            scores = (outputs["pred_logits"].float().sigmoid() * presence).squeeze(-1)
            boxes = box_ops.box_cxcywh_to_xyxy(outputs["pred_boxes"].float())

            results = []
            for i, (h, w) in enumerate(sizes):
                scale = torch.tensor([w, h, w, h], device=boxes.device, dtype=boxes.dtype)
                per_prompt = []
                for j in range(n_txt):
                    q = i * n_txt + j
                    keep = scores[q] > self.confidence_threshold
                    per_prompt.append((boxes[q][keep] * scale, scores[q][keep]))
                results.append(per_prompt)
            return results

        def _forward_grounding(self, state):
            outputs = self.model.forward_grounding(
                # a copy: without the segmentation head, sam3 drops the image
                # features from backbone_out after use, and later prompts need them
                backbone_out=dict(state["backbone_out"]),
                find_input=self.find_stage,
                geometric_prompt=state["geometric_prompt"],
                find_target=None,
            )
            presence = outputs["presence_logit_dec"].float().sigmoid().unsqueeze(1)
            scores = (outputs["pred_logits"].float().sigmoid() * presence).squeeze(-1)
            keep = scores > self.confidence_threshold
            boxes = box_ops.box_cxcywh_to_xyxy(outputs["pred_boxes"][keep].float())
            w, h = state["original_width"], state["original_height"]
            state["boxes"] = boxes * torch.tensor([w, h, w, h], device=boxes.device)
            state["scores"] = scores[keep]
            return state

    return BoxOnlySam3Processor(model, resolution=resolution, device=device,
                                confidence_threshold=confidence_threshold)


def load_sam3_detector(model_path: str | Path, config: dict, device: str = "cuda"):
    """
    Build SAM 3 (detector only) from `model_path` and return its processor.

    Args:
        model_path: a SemiF-Segmentation sam3_finetuned*.pt export, or a full sam3.pt.
        config: jpg_to_det config; reads resolution, conf and base_checkpoint
            (overrides the export's recorded base, e.g. a local sam3.pt where
            the node can't reach Hugging Face).
        device: torch device.
    """
    from sam3.model_builder import build_sam3_image_model

    resolution = int(config.get("resolution", SAM3_RESOLUTION))
    delta = read_delta(model_path)
    if delta is not None:
        base = config.get("base_checkpoint") or delta["base"]
        trained_at = int(delta.get("resolution", SAM3_RESOLUTION))
        logger.info("SAM 3 fine-tuned checkpoint %s (base %s, trained at %d px)", model_path, base, trained_at)
        if trained_at != resolution:
            logger.warning("%s was fine-tuned at %d px but runs at resolution=%d", model_path, trained_at, resolution)
    else:
        base, trained_at = model_path, None

    with at_resolution(resolution):
        model = build_sam3_image_model(device="cpu", checkpoint_path=_base_checkpoint_path(base),
                                       load_from_HF=False, enable_segmentation=False)
    if delta is not None:
        apply_delta(model, delta)
    model = model.to(device).eval()
    return _box_only_processor(model, resolution, device, float(config["conf"]))


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def class_names(config: dict) -> dict[int, str]:
    """{class id: name} from the config's prompts, as YOLO's model.names."""
    return {int(p["class"]): str(p["name"]) for p in config["prompts"]}


def to_rgb_tensor(im_bgr) -> torch.Tensor:
    """RGB uint8 CxHxW tensor of a BGR HxWx3 array, as the image detect_batch
    takes (what set_image makes of a PIL image). Converts im_bgr in place and
    shares its memory."""
    cv2.cvtColor(im_bgr, cv2.COLOR_BGR2RGB, dst=im_bgr)
    return torch.from_numpy(im_bgr).permute(2, 0, 1)


def run_sam3(processor, im0_bgr, config: dict) -> torch.Tensor:
    """
    Detect every prompt in config["prompts"] on one image.

    Args:
        processor: from load_sam3_detector (or any object with its detect_batch).
        im0_bgr: original BGR image in numpy HxWx3 format; left as is.
        config: jpg_to_det config (prompts, final_max_det).

    Returns:
        Nx6 tensor [x1, y1, x2, y2, conf, cls] in absolute pixel coordinates
        on the CPU, sorted by descending conf, or an empty (0, 6) tensor, as
        detector.run_multiscale returns.
    """
    return run_sam3_batch(processor, [to_rgb_tensor(im0_bgr.copy())], config)[0]


@torch.inference_mode()
def run_sam3_batch(processor, images: list[torch.Tensor], config: dict) -> list[torch.Tensor]:
    """
    Detect every prompt in config["prompts"] on several images in one pass.

    Args:
        processor: from load_sam3_detector (or any object with its detect_batch).
        images: RGB uint8 CxHxW tensors (to_rgb_tensor), any sizes.
        config: jpg_to_det config (prompts, final_max_det).

    Returns:
        One Nx6 tensor per image, as run_sam3 returns.
    """
    # SAM 3 runs in bfloat16 on GPU (its kernels expect it, as in sam3's own examples)
    device = str(getattr(processor, "device", "cpu"))
    autocast = (torch.autocast("cuda", dtype=torch.bfloat16) if device.startswith("cuda")
                else contextlib.nullcontext())
    prompts = config["prompts"]
    with autocast:
        outputs = processor.detect_batch(images, [str(p["prompt"]) for p in prompts])

    max_det = int(config.get("final_max_det", 1000))
    dets = []
    for image, per_prompt in zip(images, outputs):
        h, w = image.shape[-2:]
        det = torch.cat([
            torch.cat([torch.as_tensor(boxes).float().reshape(-1, 4).cpu(),
                       torch.as_tensor(scores).float().reshape(-1, 1).cpu(),
                       torch.full((len(scores), 1), float(prompt["class"]))], dim=1)
            for prompt, (boxes, scores) in zip(prompts, per_prompt)
        ]) if per_prompt else torch.zeros((0, 6))
        det[:, 0:4:2] = det[:, 0:4:2].clamp(0, w)
        det[:, 1:4:2] = det[:, 1:4:2].clamp(0, h)
        det = det[(det[:, 2] > det[:, 0]) & (det[:, 3] > det[:, 1])]
        det = det[det[:, 4].argsort(descending=True)]
        dets.append(det[:max_det])
    return dets
