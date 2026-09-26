"""Generate a standalone PDF quality-control report for a cutout batch.

The report is deliberately a visualization product, not a stage contract.  It
reads the four-file cutout sets after ``seg_to_cut`` finishes and can therefore
be rerun without regenerating cutouts.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import textwrap
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, median
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from PIL import Image, ImageDraw

from .processor import load_detection_rows, normalized_bbox_to_pixels

logger = logging.getLogger(__name__)

INK = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
WARNING = "#b45309"
SCHEMA_VERSION = 1
AREA_BINS = (
    "0-1",
    "1-10",
    "10-100",
    "100-500",
    "500-1000",
    "1000-5000",
    "5000-10000",
    "10000+",
)


@dataclass(frozen=True)
class CutoutRecord:
    cutout_id: str
    image_id: str
    batch_id: str
    metadata: Mapping[str, Any]
    cropout_path: Path
    cutout_path: Path
    mask_path: Path

    @property
    def props(self) -> Mapping[str, Any]:
        value = self.metadata.get("cutout_props")
        return value if isinstance(value, Mapping) else {}

    @property
    def category(self) -> Mapping[str, Any]:
        value = self.metadata.get("category")
        return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class CleanupPreviewSources:
    """Original masks and detections used to reconstruct removed pixels."""

    masks: Mapping[str, Path]
    detections: Mapping[tuple[str, int], Any]


def _read_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    return value if isinstance(value, dict) else {}


def load_cutout_records(cutouts_dir: str | Path) -> tuple[CutoutRecord, ...]:
    """Load complete cutout sets, skipping malformed or incomplete entries."""

    root = Path(cutouts_dir)
    if not root.is_dir():
        raise ValueError(f"cutout directory does not exist: {root}")

    records: list[CutoutRecord] = []
    for metadata_path in sorted(root.glob("*.json")):
        metadata = _read_object(metadata_path)
        cutout_id = metadata.get("cutout_id")
        image_id = metadata.get("image_id")
        if not isinstance(cutout_id, str) or cutout_id != metadata_path.stem:
            logger.warning("Skipping metadata with invalid cutout_id: %s", metadata_path)
            continue
        if not isinstance(image_id, str) or not image_id:
            logger.warning("Skipping metadata with invalid image_id: %s", metadata_path)
            continue

        cropout_path = root / f"{cutout_id}.jpg"
        cutout_path = root / f"{cutout_id}.png"
        mask_path = root / f"{cutout_id}_mask.png"
        missing = [path.name for path in (cropout_path, cutout_path, mask_path) if not path.is_file()]
        if missing:
            logger.warning("Skipping incomplete cutout %s; missing %s", cutout_id, missing)
            continue
        records.append(
            CutoutRecord(
                cutout_id=cutout_id,
                image_id=image_id,
                batch_id=str(metadata.get("batch_id") or "unknown"),
                metadata=metadata,
                cropout_path=cropout_path,
                cutout_path=cutout_path,
                mask_path=mask_path,
            )
        )
    return tuple(records)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _metric(record: CutoutRecord, field: str) -> float | None:
    return _number(record.props.get(field))


def _display_name(record: CutoutRecord) -> str:
    category = record.category
    for field in ("cultivar_name", "common_name", "species_id", "USDA_symbol"):
        value = category.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "Unknown"


def _flag_reasons(record: CutoutRecord) -> tuple[str, ...]:
    props = record.props
    reasons: list[str] = []
    if props.get("abnormal_bbox_size") is True:
        reasons.append("abnormal size")
    edge_cut = props.get("edge_cut")
    if isinstance(edge_cut, Mapping) and edge_cut.get("flagged") is True:
        sides = edge_cut.get("flagged_sides")
        suffix = f" ({','.join(map(str, sides))})" if isinstance(sides, list) and sides else ""
        reasons.append(f"edge cut{suffix}")
        if edge_cut.get("synthetic_placement") == "unsuitable":
            reasons.append("unsuitable placement")
    components = _number(props.get("num_components"))
    if components is not None and components >= 10:
        reasons.append(f"{int(components)} components")
    cleanup = props.get("intruder_cleanup")
    if isinstance(cleanup, Mapping):
        removed = _number(cleanup.get("removed_components"))
        if removed is not None and removed > 0:
            reasons.append(f"cleanup removed {int(removed)}")
    if props.get("bbox_area_cm2") is None and props.get("estimated_bbox_area_cm2") is None:
        reasons.append("missing area")
    return tuple(reasons)


def select_random_records(
    records: Sequence[CutoutRecord], sample_size: int, seed: int
) -> tuple[CutoutRecord, ...]:
    if sample_size < 0:
        raise ValueError("sample_size must be non-negative")
    rng = random.Random(seed)
    groups: dict[str, list[CutoutRecord]] = {}
    for record in sorted(records, key=lambda item: item.cutout_id):
        species_id = str(record.category.get("species_id") or "Unknown")
        groups.setdefault(species_id, []).append(record)
    for group in groups.values():
        rng.shuffle(group)

    selected: list[CutoutRecord] = []
    target = min(sample_size, len(records))
    while len(selected) < target:
        added = False
        for species_id in sorted(groups):
            if groups[species_id] and len(selected) < target:
                selected.append(groups[species_id].pop())
                added = True
        if not added:
            break
    return tuple(
        sorted(
            selected,
            key=lambda record: (
                str(record.category.get("species_id") or "Unknown"),
                record.cutout_id,
            ),
        )
    )


def select_flagged_records(
    records: Sequence[CutoutRecord], max_flagged: int, seed: int
) -> tuple[CutoutRecord, ...]:
    if max_flagged < 0:
        raise ValueError("max_flagged must be non-negative")
    flagged = [record for record in records if _flag_reasons(record)]
    rng = random.Random(seed ^ 0x5EEDC0DE)
    if len(flagged) > max_flagged:
        flagged = rng.sample(flagged, max_flagged)
    return tuple(sorted(flagged, key=lambda record: record.cutout_id))


def _counts_from_manifest(manifest: Mapping[str, Any]) -> Counter[str]:
    items = manifest.get("items")
    if not isinstance(items, list):
        return Counter()
    return Counter(
        str(item.get("status"))
        for item in items
        if isinstance(item, Mapping) and item.get("status") is not None
    )


def _checkerboard(size: tuple[int, int], tile: int = 18) -> Image.Image:
    width, height = size
    canvas = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(canvas)
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            color = "#d1d5db" if (x // tile + y // tile) % 2 else "#f3f4f6"
            draw.rectangle((x, y, min(x + tile, width), min(y + tile, height)), fill=color)
    return canvas


def _format_metric(value: float | None) -> str:
    return "NA" if value is None else f"{value:.3g}"



def _clean_axis(axis, *, grid_axis: str = "y") -> None:
    axis.spines[["top", "right"]].set_visible(False)
    axis.grid(axis=grid_axis, color="#e5e7eb", linewidth=0.7)
    axis.set_axisbelow(True)


def _mpl_overview_figure(
    records: Sequence[CutoutRecord],
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    seed: int,
) -> Figure:
    """Build the report overview and batch composition page."""

    batch_id = str(report.get("batch_id") or records[0].batch_id)
    counts = _counts_from_manifest(manifest)
    outputs = report.get("outputs") if isinstance(report.get("outputs"), Mapping) else {}
    report_counts = outputs.get("counts") if isinstance(outputs.get("counts"), Mapping) else {}
    succeeded = counts.get("ok", report_counts.get("n_units_succeeded", len(records)))
    failed = counts.get("failed", report_counts.get("n_units_failed", 0))
    skipped = counts.get("skipped", report_counts.get("n_units_skipped", 0))

    fig = plt.figure(figsize=(8.27, 11.69))
    fig.suptitle(batch_id, y=0.972, fontsize=21, fontweight="bold")
    fig.text(
        0.5,
        0.933,
        "Segmentation-to-cutout report",
        ha="center",
        color=MUTED,
        fontsize=16,
    )

    identity_ax = fig.add_axes((0.08, 0.64, 0.84, 0.23))
    identity_ax.axis("off")
    identity_rows = [
        ("Run ID", str(report.get("run_id") or "Unavailable")),
        ("Stage version", str(report.get("stage_version") or "Unavailable")),
        ("Cutout version", str(records[0].metadata.get("cutout_version") or "Unavailable")),
        ("Season", str(records[0].metadata.get("season") or "Unavailable")),
        ("BBot version", str(records[0].metadata.get("bbot_version") or "Unavailable")),
        ("Lens", str(records[0].metadata.get("lens_model") or "Unavailable")),
        ("Sample seed", str(seed)),
        ("Generated UTC", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    ]
    table = identity_ax.table(
        cellText=identity_rows,
        colLabels=("Run identity", "Value"),
        cellLoc="left",
        colLoc="left",
        colWidths=(0.25, 0.75),
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    table.scale(1, 1.22)
    for (row, _), cell in table.get_celld().items():
        cell.set_edgecolor("#e5e7eb")
        cell.PAD = 0.08
        if row == 0:
            cell.set_facecolor("#eff6ff")
            cell.set_text_props(fontweight="bold")

    totals_ax = fig.add_axes((0.18, 0.48, 0.64, 0.09))
    labels = ("Succeeded", "Skipped", "Failed")
    values = (succeeded, skipped, failed)
    bars = totals_ax.bar(labels, values, color=("#15803d", "#b45309", "#b91c1c"), width=0.48)
    totals_ax.bar_label(bars, padding=3, fontsize=9, fontweight="bold")
    totals_ax.set_title("Instances", loc="left", pad=8, fontweight="bold")
    totals_ax.margins(y=0.25)
    totals_ax.spines[["top", "right", "left"]].set_visible(False)
    totals_ax.tick_params(axis="y", left=False, labelleft=False)

    fig.text(0.10, 0.425, "Note", fontsize=9, fontweight="bold", color=INK)
    fig.text(
        0.10,
        0.402,
        (
            "Skipped means a detection did not produce a cutout artifact set because "
            "the expected class was absent from its mask crop or cleanup left no foreground."
        ),
        fontsize=8,
        color=MUTED,
        ha="left",
        va="top",
        wrap=True,
    )

    category_ax = fig.add_axes((0.34, 0.075, 0.57, 0.255))
    species = Counter(_display_name(record) for record in records)
    top_categories = species.most_common(12)
    names = [
        textwrap.shorten(name, width=48, placeholder="…")
        for name, _ in reversed(top_categories)
    ]
    category_counts = [count for _, count in reversed(top_categories)]
    bars = category_ax.barh(names, category_counts, color=ACCENT, height=0.62)
    category_ax.bar_label(bars, padding=4, fontsize=8)
    category_ax.set_title("Category composition (top 12)", loc="left", pad=10, fontweight="bold")
    category_ax.set_xlabel("Cutouts", labelpad=6)
    category_ax.margins(x=0.12, y=0.05)
    category_ax.tick_params(axis="y", labelsize=8.5, pad=5)
    _clean_axis(category_ax, grid_axis="x")
    return fig


def _metric_stats(values: Sequence[float]) -> str:
    if not values:
        return "No available values"
    array = np.asarray(values, dtype=np.float64)
    return (
        f"n={len(values):,}   median={median(values):.3g}   mean={fmean(values):.3g}   "
        f"p95={np.percentile(array, 95):.3g}   range={array.min():.3g}–{array.max():.3g}"
    )


def _format_decimal(value: float | None, *, suffix: str = "") -> str:
    if value is None:
        return "NA"
    formatted = f"{value:.5f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def _area_label_and_value(record: CutoutRecord) -> tuple[str, float | None]:
    bbox_area = _metric(record, "bbox_area_cm2")
    if bbox_area is not None:
        return "area", bbox_area
    estimated_area = _metric(record, "estimated_bbox_area_cm2")
    if estimated_area is not None:
        return "estimated area", estimated_area
    return "area", None


def _area_value(record: CutoutRecord) -> float | None:
    return _area_label_and_value(record)[1]


def _area_bin(record: CutoutRecord) -> str | None:
    value = record.props.get("estimated_area_bin")
    if isinstance(value, str) and value in AREA_BINS:
        return value
    area = _area_value(record)
    if area is None or area <= 0:
        return None
    edges = (1.0, 10.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0)
    return AREA_BINS[sum(area >= edge for edge in edges)]


def _blur_examples(
    records: Sequence[CutoutRecord],
) -> tuple[tuple[str, CutoutRecord, float], ...]:
    available = [
        (value, record)
        for record in records
        if (value := _metric(record, "blur_effect")) is not None
    ]
    if not available:
        return ()
    available.sort(key=lambda item: (item[0], item[1].cutout_id))
    median_value = median(value for value, _ in available)
    median_item = min(
        available,
        key=lambda item: (abs(item[0] - median_value), item[1].cutout_id),
    )
    chosen = (
        ("Lowest", available[0]),
        ("Median", median_item),
        ("Highest", available[-1]),
    )
    return tuple((label, record, value) for label, (value, record) in chosen)


def _mpl_blur_figure(records: Sequence[CutoutRecord]) -> Figure:
    """Show the blur distribution with low, median, and high examples."""

    values = [
        value
        for record in records
        if (value := _metric(record, "blur_effect")) is not None
    ]
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.subplots_adjust(top=0.90, bottom=0.08, left=0.09, right=0.95, hspace=0.42, wspace=0.16)
    grid = fig.add_gridspec(2, 3, height_ratios=(0.9, 1.1))
    fig.suptitle("Blur effect", y=0.965, fontsize=20, fontweight="bold")

    histogram_ax = fig.add_subplot(grid[0, :])
    if values:
        bins = min(24, max(5, int(math.sqrt(len(values)))))
        histogram_ax.hist(values, bins=bins, color=ACCENT, edgecolor="white", linewidth=0.65)
    else:
        histogram_ax.text(0.5, 0.5, "No available values", ha="center", va="center", color=MUTED)
    histogram_ax.set_title("Distribution", loc="left", pad=12, fontsize=13, fontweight="bold")
    histogram_ax.text(
        1.0,
        1.03,
        _metric_stats(values),
        transform=histogram_ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        color=MUTED,
    )
    histogram_ax.set_xlabel("Blur effect", labelpad=7)
    histogram_ax.set_ylabel("Cutouts", labelpad=7)
    histogram_ax.margins(y=0.12)
    _clean_axis(histogram_ax)

    examples = _blur_examples(records)
    for column in range(3):
        axis = fig.add_subplot(grid[1, column])
        axis.set_xticks([])
        axis.set_yticks([])
        if column < len(examples):
            label, record, value = examples[column]
            axis.imshow(_native_preview(record, "cutout"), interpolation="none")
            axis.set_title(
                f"{label}: {_format_decimal(value)}\n{record.cutout_id}",
                fontsize=9,
                fontweight="bold",
                pad=9,
            )
        else:
            axis.text(0.5, 0.5, "Unavailable", ha="center", va="center", color=MUTED)
        for spine in axis.spines.values():
            spine.set_color("#d1d5db")
            spine.set_linewidth(0.7)
    return fig


def _mpl_area_bins_figure(records: Sequence[CutoutRecord]) -> Figure:
    """Show fixed area-bin composition overall and by species."""

    overall = Counter(_area_bin(record) for record in records)
    species_ids = sorted(
        {str(record.category.get("species_id") or "Unknown") for record in records}
    )
    matrix = np.zeros((len(species_ids), len(AREA_BINS)), dtype=int)
    for row, species_id in enumerate(species_ids):
        for record in records:
            record_species = str(record.category.get("species_id") or "Unknown")
            area_bin = _area_bin(record)
            if record_species == species_id and area_bin in AREA_BINS:
                matrix[row, AREA_BINS.index(area_bin)] += 1

    fig, axes = plt.subplots(2, 1, figsize=(11.69, 8.27), gridspec_kw={"height_ratios": (0.9, 1.25)})
    fig.subplots_adjust(top=0.87, bottom=0.14, left=0.12, right=0.96, hspace=0.52)
    fig.suptitle("Bounding-box area bin", y=0.96, fontsize=20, fontweight="bold")

    counts = [overall[label] for label in AREA_BINS]
    bars = axes[0].bar(AREA_BINS, counts, color=ACCENT, width=0.68)
    axes[0].bar_label(bars, padding=3, fontsize=8)
    axes[0].set_title("All species", loc="left", pad=10, fontweight="bold")
    axes[0].set_ylabel("Cutouts")
    axes[0].tick_params(axis="x", rotation=25, labelsize=8)
    axes[0].margins(y=0.16)
    _clean_axis(axes[0])

    image = axes[1].imshow(matrix, aspect="auto", cmap="Blues")
    axes[1].set_title("Bounding-box area bin by species", loc="left", pad=10, fontweight="bold")
    axes[1].set_xticks(range(len(AREA_BINS)), AREA_BINS, rotation=30, ha="right")
    axes[1].set_yticks(range(len(species_ids)), species_ids)
    axes[1].set_xlabel("Area bin (cm²)")
    axes[1].set_ylabel("Species")
    maximum = int(matrix.max()) if matrix.size else 0
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = int(matrix[row, column])
            axes[1].text(
                column,
                row,
                str(value),
                ha="center",
                va="center",
                fontsize=8,
                color="white" if maximum and value > maximum * 0.55 else INK,
            )
    fig.colorbar(image, ax=axes[1], label="Cutouts", fraction=0.025, pad=0.025)
    return fig


def _normalized_qc_counts(records: Sequence[CutoutRecord]) -> Counter[str]:
    reason_counts: Counter[str] = Counter()
    for record in records:
        reason_counts.update(_flag_reasons(record))
    normalized: Counter[str] = Counter()
    for reason, count in reason_counts.items():
        key = reason
        if reason.startswith("edge cut"):
            key = "Edge cut"
        elif reason.endswith("components"):
            key = "≥10 Components"
        elif reason.startswith("cleanup removed"):
            key = "Intruder cleanup applied"
        normalized[key] += count
    return normalized


def _placement_category(raw_value: Any) -> str | None:
    value = str(raw_value or "").strip().lower()
    if value in {"unsuitable", "unrestricted"}:
        return value
    if value.endswith("_corner_only"):
        return "double edge restricted"
    if value.endswith("_edge_only"):
        return "single edge restricted"
    return None


def _mpl_qc_figure(records: Sequence[CutoutRecord]) -> Figure:
    """Build QC indicators and normalized synthetic-placement counts."""

    indicators = (
        ("Edge cut", "Edge cut"),
        ("Unsuitable placement", "unsuitable placement"),
        ("Abnormal size", "abnormal size"),
        ("≥10 Components", "≥10 Components"),
        ("Intruder cleanup applied", "Intruder cleanup applied"),
        ("Missing area", "missing area"),
    )
    normalized = _normalized_qc_counts(records)
    labels = [display for display, _ in indicators]
    values = [normalized[key] for _, key in indicators]

    placement_labels = (
        "unsuitable",
        "unrestricted",
        "single edge restricted",
        "double edge restricted",
    )
    placements: Counter[str] = Counter()
    for record in records:
        edge = record.props.get("edge_cut")
        if not isinstance(edge, Mapping):
            continue
        category = _placement_category(edge.get("synthetic_placement"))
        if category is not None:
            placements[category] += 1

    fig, axes = plt.subplots(2, 1, figsize=(8.27, 11.69))
    fig.subplots_adjust(top=0.90, bottom=0.10, left=0.31, right=0.94, hspace=0.48)
    fig.suptitle("Quality-control indicators", y=0.97, fontsize=20, fontweight="bold")

    bars = axes[0].barh(
        list(reversed(labels)),
        list(reversed(values)),
        color=WARNING,
        height=0.58,
    )
    axes[0].bar_label(bars, padding=4, fontsize=9)
    axes[0].set_title("Indicators (not mutually exclusive)", loc="left", pad=12, fontweight="bold")
    axes[0].set_xlabel("Cutouts", labelpad=8)
    axes[0].margins(x=0.14, y=0.10)
    _clean_axis(axes[0], grid_axis="x")

    placement_values = [placements[label] for label in placement_labels]
    bars = axes[1].barh(
        list(reversed(placement_labels)),
        list(reversed(placement_values)),
        color="#7c3aed",
        height=0.58,
    )
    axes[1].bar_label(bars, padding=4, fontsize=9)
    axes[1].set_title("Synthetic placement", loc="left", pad=12, fontweight="bold")
    axes[1].set_xlabel("Cutouts", labelpad=8)
    axes[1].margins(x=0.14, y=0.12)
    _clean_axis(axes[1], grid_axis="x")
    return fig


def _preview_array(image: Image.Image, *, resample: Image.Resampling) -> np.ndarray:
    """Cap a panel at 720 px, enough for its 300-DPI printed dimensions."""

    image.thumbnail((720, 720), resample=resample)
    return np.asarray(image.convert("RGB")).copy()


def _cleanup_count(record: CutoutRecord, field: str) -> int:
    cleanup = record.props.get("intruder_cleanup")
    if not isinstance(cleanup, Mapping):
        return 0
    value = cleanup.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _resolved_class_id(record: CutoutRecord) -> int | None:
    value = record.category.get("cultivar_class_id")
    if value is None:
        value = record.category.get("class_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 255 else None


def _load_cleanup_preview_sources(
    segmentations_dir: str | Path | None,
    georeferenced_csv: str | Path | None,
) -> CleanupPreviewSources | None:
    """Load optional source indexes; ordinary mask previews remain the fallback."""

    if segmentations_dir is None and georeferenced_csv is None:
        return None
    if segmentations_dir is None or georeferenced_csv is None:
        logger.warning(
            "Both --segmentations and --georeferenced-csv are required to show removed mask components"
        )
        return None
    root = Path(segmentations_dir)
    csv_path = Path(georeferenced_csv)
    if not root.is_dir() or not csv_path.is_file():
        logger.warning(
            "Cleanup comparison inputs are unavailable (segmentations=%s, georeferenced_csv=%s)",
            root,
            csv_path,
        )
        return None
    try:
        rows = load_detection_rows(csv_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not load cleanup comparison detections: %s", exc)
        return None

    masks: dict[str, Path] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        key = path.stem.casefold()
        if key in masks:
            logger.warning(
                "Duplicate source masks for image_id %s; disabling cleanup comparison", key
            )
            return None
        masks[key] = path
    detections = {
        (row.normalized_image_id, row.bounding_box_id): row
        for row in rows
    }
    return CleanupPreviewSources(masks=masks, detections=detections)


def _reconstructed_removed_pixels(
    record: CutoutRecord,
    cleaned_mask: np.ndarray,
    sources: CleanupPreviewSources | None,
) -> np.ndarray | None:
    """Return verified pixels removed by cleanup for one selected cutout."""

    removed_components = _cleanup_count(record, "removed_components")
    expected_removed = _cleanup_count(record, "removed_pixels")
    if removed_components == 0 or expected_removed == 0 or sources is None:
        return None
    cutout_num = record.metadata.get("cutout_num")
    class_id = _resolved_class_id(record)
    if not isinstance(cutout_num, int) or isinstance(cutout_num, bool) or class_id is None:
        logger.warning(
            "Cannot reconstruct removed pixels for %s: invalid identity or class",
            record.cutout_id,
        )
        return None
    key = (record.image_id.casefold(), cutout_num)
    detection = sources.detections.get(key)
    mask_path = sources.masks.get(key[0])
    if detection is None or mask_path is None:
        logger.warning(
            "Cannot reconstruct removed pixels for %s: source detection or mask missing",
            record.cutout_id,
        )
        return None
    try:
        with Image.open(mask_path) as source:
            original_mask = np.asarray(source).copy()
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read source mask for %s: %s", record.cutout_id, exc)
        return None
    if original_mask.ndim != 2 or original_mask.dtype != np.uint8:
        logger.warning(
            "Cannot reconstruct removed pixels for %s: source mask is not uint8 grayscale",
            record.cutout_id,
        )
        return None
    height, width = original_mask.shape
    try:
        box = normalized_bbox_to_pixels(
            detection.normalized_bbox,
            width=width,
            height=height,
            image_id=record.image_id,
            bounding_box_id=cutout_num,
        )
    except ValueError as exc:
        logger.warning("Cannot reconstruct removed pixels for %s: %s", record.cutout_id, exc)
        return None
    original_crop = original_mask[box.ymin : box.ymax, box.xmin : box.xmax]
    if original_crop.shape != cleaned_mask.shape:
        logger.warning(
            "Cannot reconstruct removed pixels for %s: source crop %s != saved mask %s",
            record.cutout_id,
            original_crop.shape,
            cleaned_mask.shape,
        )
        return None
    removed = (original_crop == class_id) & (cleaned_mask != class_id)
    actual_removed = int(np.count_nonzero(removed))
    if actual_removed != expected_removed:
        logger.warning(
            "Cannot verify removed pixels for %s: reconstructed %d, metadata says %d",
            record.cutout_id,
            actual_removed,
            expected_removed,
        )
        return None
    return removed


def _native_preview(
    record: CutoutRecord,
    kind: str,
    cleanup_sources: CleanupPreviewSources | None = None,
) -> np.ndarray:
    """Return a print-resolution RGB preview without embedding huge source arrays."""

    if kind == "cropout":
        with Image.open(record.cropout_path) as source:
            return _preview_array(source.convert("RGB"), resample=Image.Resampling.LANCZOS)
    if kind == "cutout":
        with Image.open(record.cutout_path) as source:
            rgba = source.convert("RGBA")
            background = _checkerboard(rgba.size, tile=max(4, min(rgba.size) // 12))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return _preview_array(background, resample=Image.Resampling.LANCZOS)
    with Image.open(record.mask_path) as source:
        mask = np.asarray(source).copy()
    foreground = mask != 0
    rgb = np.zeros((*foreground.shape, 3), dtype=np.uint8)
    rgb[foreground] = (234, 88, 12)
    removed = _reconstructed_removed_pixels(record, mask, cleanup_sources)
    if removed is not None:
        rgb[removed] = (236, 72, 153)
    return _preview_array(Image.fromarray(rgb, "RGB"), resample=Image.Resampling.NEAREST)


def _sample_caption(record: CutoutRecord, *, annotate_flags: bool) -> str:
    area_label, area_value = _area_label_and_value(record)
    details = (
        f"{record.metadata.get('cutout_width', '?')} × "
        f"{record.metadata.get('cutout_height', '?')} px   |   "
        f"blur {_format_metric(_metric(record, 'blur_effect'))}   |   "
        f"components {_format_metric(_metric(record, 'num_components'))}   |   "
        f"{area_label} {_format_decimal(area_value, suffix=' cm²')}"
    )
    if annotate_flags:
        details += (
            "   |   area ratio "
            f"{_format_decimal(_metric(record, 'species_bbox_area_ratio'))}"
        )
    lines = [
        f"{record.cutout_id}  •  {_display_name(record)}",
        details,
    ]
    removed_components = _cleanup_count(record, "removed_components")
    if removed_components:
        lines.append(f"Intruder cleanup: {removed_components} component(s) removed (pink)")
    if annotate_flags:
        flags = "; ".join(_flag_reasons(record)) or "No QC indicators"
        lines.append(f"QC: {flags}")
    return "\n".join(lines)


def _mpl_sample_figures(
    records: Sequence[CutoutRecord],
    *,
    title: str,
    subtitle: str,
    annotate_flags: bool,
    cleanup_sources: CleanupPreviewSources | None,
) -> Iterable[Figure]:
    """Yield spacious landscape contact sheets with two cutouts per page."""

    per_page = 2
    for offset in range(0, len(records), per_page):
        subset = records[offset : offset + per_page]
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.subplots_adjust(top=0.87, bottom=0.06, left=0.04, right=0.98, hspace=0.20, wspace=0.08)
        grid = fig.add_gridspec(4, 3, height_ratios=(0.30, 1.0, 0.30, 1.0))
        fig.suptitle(title, y=0.965, fontsize=18, fontweight="bold")
        fig.text(
            0.5,
            0.92,
            f"{subtitle} — {offset + 1}–{offset + len(subset)} of {len(records)}",
            ha="center",
            color=MUTED,
            fontsize=9,
        )
        for index, record in enumerate(subset):
            caption_row = index * 2
            caption_axis = fig.add_subplot(grid[caption_row, :])
            caption_axis.axis("off")
            caption_axis.text(
                0.01,
                0.55,
                _sample_caption(record, annotate_flags=annotate_flags),
                ha="left",
                va="center",
                fontsize=8.5,
                color=WARNING if annotate_flags else INK,
                linespacing=1.35,
            )
            for column, kind in enumerate(("cropout", "cutout", "mask")):
                axis = fig.add_subplot(grid[caption_row + 1, column])
                axis.imshow(
                    _native_preview(record, kind, cleanup_sources),
                    interpolation="none",
                )
                axis.set_xticks([])
                axis.set_yticks([])
                axis.set_title(kind.title(), fontsize=10, fontweight="bold", pad=7)
                for spine in axis.spines.values():
                    spine.set_color("#d1d5db")
                    spine.set_linewidth(0.7)
        yield fig


def generate_pdf(
    cutouts_dir: str | Path,
    output: str | Path,
    *,
    run_report_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    segmentations_dir: str | Path | None = None,
    georeferenced_csv: str | Path | None = None,
    sample_size: int = 24,
    max_flagged: int = 24,
    seed: int = 42,
) -> Path:
    """Generate the QC PDF and return its path."""

    records = load_cutout_records(cutouts_dir)
    if not records:
        raise ValueError(f"no complete cutout sets found in {cutouts_dir}")
    report = _read_object(Path(run_report_path) if run_report_path else None)
    manifest = _read_object(Path(manifest_path) if manifest_path else None)
    destination = Path(output)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if destination.suffix.lower() != ".pdf":
        destination = destination / (
            f"seg_to_cut_qc_{records[0].batch_id}_{timestamp}.pdf"
        )
    elif destination.exists():
        destination = destination.with_name(
            f"{destination.stem}_{timestamp}{destination.suffix}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    random_records = select_random_records(records, sample_size, seed)
    flagged_records = select_flagged_records(records, max_flagged, seed)
    cleanup_sources = _load_cleanup_preview_sources(
        segmentations_dir,
        georeferenced_csv,
    )
    metadata = {
        "Title": f"seg_to_cut QC: {records[0].batch_id}",
        "Author": "AgIR pipeline",
        "Subject": f"Schema version {SCHEMA_VERSION} batch visualization",
        "Keywords": "seg_to_cut, cutouts, quality control",
    }
    page_count = 0
    with PdfPages(destination, metadata=metadata) as pdf:
        figure = _mpl_overview_figure(records, report, manifest, seed=seed)
        pdf.savefig(figure, dpi=300)
        plt.close(figure)
        page_count += 1
        for figure in (
            _mpl_blur_figure(records),
            _mpl_area_bins_figure(records),
        ):
            pdf.savefig(figure, dpi=300)
            plt.close(figure)
            page_count += 1
        figure = _mpl_qc_figure(records)
        pdf.savefig(figure, dpi=300)
        plt.close(figure)
        page_count += 1
        for figure in _mpl_sample_figures(
            random_records,
            title="Random cutout sample",
            subtitle=f"Species-stratified deterministic sample, seed {seed}",
            annotate_flags=False,
            cleanup_sources=cleanup_sources,
        ):
            pdf.savefig(figure, dpi=300)
            plt.close(figure)
            page_count += 1
        for figure in _mpl_sample_figures(
            flagged_records,
            title="Flagged cutout sample",
            subtitle="Cutouts with one or more QC indicators",
            annotate_flags=True,
            cleanup_sources=cleanup_sources,
        ):
            pdf.savefig(figure, dpi=300)
            plt.close(figure)
            page_count += 1
    logger.info("Wrote %d-page cutout QC report to %s", page_count, destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutouts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-report", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--segmentations", type=Path)
    parser.add_argument("--georeferenced-csv", type=Path)
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--max-flagged", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))
    try:
        generate_pdf(
            args.cutouts,
            args.output,
            run_report_path=args.run_report,
            manifest_path=args.manifest,
            segmentations_dir=args.segmentations,
            georeferenced_csv=args.georeferenced_csv,
            sample_size=args.sample_size,
            max_flagged=args.max_flagged,
            seed=args.seed,
        )
    except (OSError, ValueError) as exc:
        logger.error("Could not generate seg_to_cut QC report: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
