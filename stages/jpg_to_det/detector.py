"""
Multiscale YOLO plant detection with weighted box fusion.

Ported from SemiF-Preprocessing detect_plants.py — OmegaConf dot-access
replaced with plain dict access.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
import torch
from torchvision.ops import batched_nms

logger = logging.getLogger(__name__)



def iou_xyxy(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute intersection-over-union between two axis-aligned bounding boxes.

    Args:
        a: Array of shape (4,) with [x1, y1, x2, y2] coordinates.
        b: Array of shape (4,) with [x1, y1, x2, y2] coordinates.

    Returns:
        IoU score as a float in [0, 1].
    """
    x1 = max(float(a[0]), float(b[0]))
    y1 = max(float(a[1]), float(b[1]))
    x2 = min(float(a[2]), float(b[2]))
    y2 = min(float(a[3]), float(b[3]))
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, float(a[2]) - float(a[0])) * max(0.0, float(a[3]) - float(a[1]))
    area_b = max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))
    union = area_a + area_b - inter + 1e-9
    return inter / union


def edge_aware_filter(
    boxes_xyxy: np.ndarray,
    scores: np.ndarray,
    img_wh: tuple[int, int],
    *,
    base_conf: float = 0.70,
    edge_band_rel: float = 0.08,
    min_factor: float = 0.60,
    taper_rel: float = 0.20,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Dynamic confidence threshold based on distance to nearest image edge.

    Args:
        boxes_xyxy: Nx4 array of absolute-pixel xyxy bounding boxes.
        scores: N array of confidence scores.
        img_wh: (width, height) of the image in pixels.
        base_conf: Normal confidence threshold for non-edge boxes.
        edge_band_rel: Relative distance (fraction of shorter side) defining the
            hard edge zone where min_factor applies.
        min_factor: Multiplier on base_conf at the very edge (e.g. 0.60).
        taper_rel: Relative distance at which the threshold returns to base_conf.

    Returns:
        (keep_mask, dyn_thr) — boolean mask of kept boxes and per-box thresholds.
    """
    if len(boxes_xyxy) == 0:
        return np.zeros((0,), dtype=bool), np.zeros((0,), dtype=np.float32)

    W, H = map(float, img_wh)
    cx = (boxes_xyxy[:, 0] + boxes_xyxy[:, 2]) * 0.5
    cy = (boxes_xyxy[:, 1] + boxes_xyxy[:, 3]) * 0.5

    d_left = cx
    d_right = W - cx
    d_top = cy
    d_bottom = H - cy
    d_edge_px = np.minimum.reduce([d_left, d_right, d_top, d_bottom])

    min_side = min(W, H)
    d_edge_rel = d_edge_px / (min_side + 1e-9)

    f = np.ones_like(d_edge_rel, dtype=np.float32)
    near = d_edge_rel <= edge_band_rel
    far = d_edge_rel >= taper_rel
    mid = ~(near | far)

    f[near] = float(min_factor)
    if np.any(mid):
        t = (d_edge_rel[mid] - edge_band_rel) / max(taper_rel - edge_band_rel, 1e-6)
        f[mid] = min_factor + t * (1.0 - min_factor)

    dyn_thr = (base_conf * f).astype(np.float32)
    keep_mask = scores >= dyn_thr
    return keep_mask, dyn_thr




def weighted_box_fusion_single_class(
    boxes: np.ndarray,
    scores: np.ndarray,
    iou_thr: float,
    score_thr: float,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Weighted box fusion for a single object class.

    Clusters overlapping boxes by IoU, then fuses each cluster into a single
    box via confidence-weighted averaging.

    Args:
        boxes: Nx4 array of normalized xyxy coordinates.
        scores: N array of confidence scores.
        iou_thr: IoU threshold for clustering boxes together.
        score_thr: Minimum score to keep a box before fusion.

    Returns:
        (fused_boxes, fused_scores) — Mx4 fused coordinates and M scores.
    """
    if boxes is None or len(boxes) == 0:
        return np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32)

    boxes = np.asarray(boxes, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)

    keep = scores >= float(score_thr)
    boxes, scores = boxes[keep], scores[keep]
    if len(boxes) == 0:
        return np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32)

    order = np.argsort(-scores)
    boxes, scores = boxes[order], scores[order]

    fused_boxes = []
    fused_scores = []
    used = np.zeros(len(boxes), dtype=bool)

    for i in range(len(boxes)):
        if used[i]:
            continue

        cluster = [i]
        used[i] = True

        for j in range(i + 1, len(boxes)):
            if used[j]:
                continue
            if iou_xyxy(boxes[i], boxes[j]) >= float(iou_thr):
                cluster.append(j)
                used[j] = True

        cb = boxes[cluster]
        cs = scores[cluster]

        w = cs / (cs.sum() + 1e-9)
        fused = (cb * w[:, None]).sum(axis=0)
        fused_score = float((cs * w).sum())

        fused_boxes.append(fused)
        fused_scores.append(fused_score)

    return np.stack(fused_boxes, axis=0).astype(np.float32), np.asarray(fused_scores, dtype=np.float32)


def weighted_box_fusion_all_classes(
    boxes_xyxy_norm: np.ndarray,
    scores: np.ndarray,
    classes: np.ndarray,
    iou_thr: float,
    score_thr: float,
) -> torch.Tensor:
    """
    Weighted box fusion applied independently per class.

    Splits detections by class ID, runs single-class WBF on each, then
    concatenates and sorts by confidence descending.

    Args:
        boxes_xyxy_norm: Nx4 array of normalized xyxy coordinates.
        scores: N array of confidence scores.
        classes: N array of integer class IDs.
        iou_thr: IoU threshold for clustering boxes together.
        score_thr: Minimum score to keep a box before fusion.

    Returns:
        Mx6 tensor [x1, y1, x2, y2, conf, cls] in normalized coordinates.
    """
    boxes_xyxy_norm = np.asarray(boxes_xyxy_norm, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    classes = np.asarray(classes, dtype=np.int64)

    out = []
    for cls_id in np.unique(classes):
        m = classes == cls_id
        fb, fs = weighted_box_fusion_single_class(
            boxes_xyxy_norm[m],
            scores[m],
            iou_thr=iou_thr,
            score_thr=score_thr,
        )
        if len(fb):
            cls_col = np.full((len(fb), 1), cls_id, dtype=np.float32)
            conf_col = fs.reshape(-1, 1).astype(np.float32)
            out.append(np.concatenate([fb, conf_col, cls_col], axis=1))

    if not out:
        return torch.zeros((0, 6), dtype=torch.float32)

    out = np.concatenate(out, axis=0)
    out = out[np.argsort(-out[:, 4])]
    return torch.from_numpy(out).float()





def nms_xyxy_abs(dets_xyxy_conf_cls: torch.Tensor, iou_thr: float, max_det: int) -> torch.Tensor:
    """
    Class-aware non-maximum suppression on absolute-coordinate detections.

    Uses torchvision batched_nms so boxes of different classes do not
    suppress each other.

    Args:
        dets_xyxy_conf_cls: Nx6 tensor [x1, y1, x2, y2, conf, cls] in
            absolute pixel coordinates.
        iou_thr: IoU threshold above which the lower-confidence box is removed.
        max_det: Maximum number of detections to keep after NMS.

    Returns:
        Mx6 tensor of surviving detections, sorted by confidence.
    """
    if dets_xyxy_conf_cls is None or dets_xyxy_conf_cls.numel() == 0:
        return torch.zeros((0, 6), dtype=torch.float32)

    dets = dets_xyxy_conf_cls.float()
    boxes = dets[:, :4]
    scores = dets[:, 4]
    cls = dets[:, 5].to(torch.int64)

    keep = batched_nms(boxes, scores, cls, float(iou_thr))
    keep = keep[: int(max_det)]
    return dets[keep]



def export_predictions(
    results_raw_xyxy_abs: torch.Tensor,
    save_dir,
    filename: str,
    names: dict,
    im0,
) -> tuple[Path, list[dict]]:
    """
    Write per-image YOLO .txt file and return detection rows for batch CSV.

    The .txt file contains one line per detection in YOLO format:
        cls x_center y_center width height conf  (all normalized to [0,1]).

    Args:
        results_raw_xyxy_abs: Nx6 tensor [x1,y1,x2,y2,conf,cls] in absolute
            pixel coordinates, or None / empty tensor for zero detections.
        save_dir: Directory where the .txt file is written.
        filename: Original image filename (stem is used for the output name).
        names: Dict mapping integer class IDs to human-readable names.
        im0: Original image array (HxWx3) used to get dimensions for
            coordinate normalization.

    Returns:
        (txt_path, detection_rows)
            txt_path: a yolo format txt written to disk
            detection_rows: object detection metadata
    """
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(filename).stem
    txt_path = save_dir / f"{stem}.txt"

    if results_raw_xyxy_abs is None or results_raw_xyxy_abs.numel() == 0:
        txt_path.touch()
        return txt_path, []

    boxes = results_raw_xyxy_abs.detach().cpu().float()
    xyxy = boxes[:, :4]
    conf = boxes[:, 4]
    cls = boxes[:, 5].to(torch.int64)

    h, w = im0.shape[:2]

    x1, y1, x2, y2 = xyxy[:, 0], xyxy[:, 1], xyxy[:, 2], xyxy[:, 3]
    xc = ((x1 + x2) / 2.0) / w
    yc = ((y1 + y2) / 2.0) / h
    bw = (x2 - x1) / w
    bh = (y2 - y1) / h

    # Write YOLO txt
    with open(txt_path, "w") as f:
        for i in range(xc.shape[0]):
            f.write(
                f"{int(cls[i])} "
                f"{xc[i]:.6f} {yc[i]:.6f} "
                f"{bw[i]:.6f} {bh[i]:.6f} "
                f"{conf[i]:.6f}\n"
            )

    # Build detection rows (normalized xyxy) for batch CSV
    xyxy_norm = xyxy.clone()
    xyxy_norm[:, 0] /= w
    xyxy_norm[:, 2] /= w
    xyxy_norm[:, 1] /= h
    xyxy_norm[:, 3] /= h

    detection_rows = []
    for i in range(xyxy_norm.shape[0]):
        cls_id = int(cls[i])
        detection_rows.append({
            "image_id": stem,
            "bounding_box_id": i,
            "xmin": float(xyxy_norm[i, 0]),
            "ymin": float(xyxy_norm[i, 1]),
            "xmax": float(xyxy_norm[i, 2]),
            "ymax": float(xyxy_norm[i, 3]),
            "conf": float(conf[i]),
            "class": cls_id,
            "classname": names.get(cls_id, "unknown"),
        })

    return txt_path, detection_rows


# ── main multiscale pipeline ─────────────────────────────────────────────────

def run_multiscale(model, im0_bgr, config: dict, device=None) -> torch.Tensor:
    """
    Run multiscale YOLO inference on images.

    Args:
        model: Loaded YOLO model instance
        im0_bgr: original BGR image in numpy HxWx3 format
        config: Detection config dict 
        device: Torch device string (e.g. "cpu", "cuda:0") or None

    Returns:
        Nx6 tensor [x1, y1, x2, y2, conf, cls] in absolute pixel coordinates,
        or an empty (0, 6) tensor when no detections survive.
    """
    h, w = im0_bgr.shape[:2]

    base_imgsz = int(config["base_imgsz"])
    scale_factors = list(config["scales"])

    # array of scaled images
    imgszs = [max(32, int(round(base_imgsz * float(s)))) for s in scale_factors]

    # ====== per scale metadata ======
    # confidence interval per scale
    per_conf = float(config["per_scale_conf"])

    # internal overlap allowed per scale
    per_iou = float(config["per_scale_iou"])

    # max detections per scale
    per_max_det = int(config["per_scale_max_det"])


    # ====== final metadata ======
    # final confidence interval
    final_conf = float(config["conf"])

    # final internal overlap allowed
    final_iou = float(config["iou"])

    # final detections
    final_max_det = int(config["final_max_det"])


    wbf_iou = float(config.get("wbf_iou", 0.55))
    wbf_score_thr = float(config.get("wbf_score_thr", 0.001))

    all_boxes_norm, all_scores, all_classes = [], [], []

    im_rgb = cv2.cvtColor(im0_bgr, cv2.COLOR_BGR2RGB)

    # go through image scale predicitons
    for imgsz in imgszs:
        preds = model.predict(
            source=im_rgb,
            imgsz=int(imgsz),
            conf=per_conf,
            iou=per_iou,
            max_det=per_max_det,
            device=device,
            verbose=False,
            save=False,
        )

        r = preds[0]
        if r.boxes is None or len(r.boxes) == 0:
            continue

        xyxy_abs = r.boxes.xyxy.detach().cpu().float().numpy()
        scores = r.boxes.conf.detach().cpu().float().numpy()
        classes = r.boxes.cls.detach().cpu().float().numpy().astype(np.int64)

        # normalize boxes
        xyxy_norm = xyxy_abs.copy()
        xyxy_norm[:, 0] /= w
        xyxy_norm[:, 2] /= w
        xyxy_norm[:, 1] /= h
        xyxy_norm[:, 3] /= h

        all_boxes_norm.append(xyxy_norm)
        all_scores.append(scores)
        all_classes.append(classes)

    if not all_boxes_norm:
        return torch.zeros((0, 6), dtype=torch.float32)

    boxes_norm = np.concatenate(all_boxes_norm, axis=0)
    scores = np.concatenate(all_scores, axis=0)
    classes = np.concatenate(all_classes, axis=0)

    # Edge-aware filtering
    ea_cfg = config.get("edge_aware")
    if ea_cfg is not None and ea_cfg.get("enabled", False):
        boxes_abs = boxes_norm.copy()
        boxes_abs[:, 0] *= w
        boxes_abs[:, 2] *= w
        boxes_abs[:, 1] *= h
        boxes_abs[:, 3] *= h

        keep_mask, dyn_thr = edge_aware_filter(
            boxes_xyxy=boxes_abs,
            scores=scores,
            img_wh=(int(w), int(h)),
            base_conf=float(config["conf"]),
            edge_band_rel=float(ea_cfg.get("edge_band_rel", 0.08)),
            min_factor=float(ea_cfg.get("min_factor", 0.60)),
            taper_rel=float(ea_cfg.get("taper_rel", 0.20)),
        )

        before_ea = int(scores.shape[0])
        boxes_norm = boxes_norm[keep_mask]
        scores = scores[keep_mask]
        classes = classes[keep_mask]
        after_ea = int(scores.shape[0])

        logger.info(
            "[edge_aware] %d -> %d (base_conf=%.2f, edge_band_rel=%.2f, min_factor=%.2f, taper_rel=%.2f)",
            before_ea, after_ea,
            float(config["conf"]),
            float(ea_cfg.get("edge_band_rel", 0.08)),
            float(ea_cfg.get("min_factor", 0.60)),
            float(ea_cfg.get("taper_rel", 0.20)),
        )
    else:
        logger.info("[edge_aware] disabled")

    raw_n = int(boxes_norm.shape[0])
    logger.info(
        "[multiscale] raw_concat=%d (scales=%d, per_conf=%.2f, per_iou=%.2f, per_max_det=%d)",
        raw_n, len(imgszs), per_conf, per_iou, per_max_det,
    )

    fused_norm = weighted_box_fusion_all_classes(
        boxes_xyxy_norm=boxes_norm,
        scores=scores,
        classes=classes,
        iou_thr=wbf_iou,
        score_thr=wbf_score_thr,
    )

    wbf_n = int(fused_norm.shape[0])
    logger.info("[wbf] fused=%d (wbf_iou=%.2f, wbf_score_thr=%.3f)", wbf_n, wbf_iou, wbf_score_thr)

    if fused_norm.numel() == 0:
        return fused_norm

    fused_abs = fused_norm.clone()
    fused_abs[:, 0] *= w
    fused_abs[:, 2] *= w
    fused_abs[:, 1] *= h
    fused_abs[:, 3] *= h

    # Optional post-fusion NMS
    pfn = config.get("post_fusion_nms")
    if pfn is not None and pfn.get("enabled", False):
        fused_abs = nms_xyxy_abs(
            fused_abs,
            iou_thr=float(pfn["iou"]),
            max_det=final_max_det,
        )

    if fused_abs.numel() == 0:
        return fused_abs

    fused_abs = fused_abs[fused_abs[:, 4] >= final_conf]
    fused_abs = nms_xyxy_abs(fused_abs, iou_thr=final_iou, max_det=final_max_det)

    return fused_abs
