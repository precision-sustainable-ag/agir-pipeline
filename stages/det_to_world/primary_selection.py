"""Legacy primary-detection selection, adapted from SemiF-AnnotationPipeline.

The BBoxFilter traversal, thresholds, FOV rectangle IoU, polygon IoU,
centroid formula, and cleanup behavior are deliberately preserved for old
batches. This is a marker, not a row filter: cleanup may leave no primary
for an overlap group, and comparisons are not partitioned by species.

Call select_primary_rows with already remapped rows and references in the
same coordinate frame. Input order is preserved because legacy ties and
neighborhood traversal depend on it. Reference discovery, cluster paths,
and pipeline wiring belong to the caller; no Metashape project is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
from shapely.geometry import Polygon

log = logging.getLogger(__name__)

FOV_IOU_THRESH = 0.1
BBOX_OVERLAP_THRESH = 0.3


@dataclass
class BoxCoordinates:
    top_left: np.ndarray
    top_right: np.ndarray
    bottom_left: np.ndarray
    bottom_right: np.ndarray

    @property
    def centroid(self) -> np.ndarray:
        # Legacy formula; differs from remapper._construct_global_coords.
        return np.array(
            [
                (self.bottom_right[0] + self.bottom_left[0]) / 2.0,
                (self.bottom_left[1] + self.top_left[1]) / 2.0,
            ]
        )


@dataclass
class BBox:
    bbox_id: tuple[str, str]
    image_id: str
    local_coordinates: BoxCoordinates
    global_coordinates: BoxCoordinates
    is_primary: bool = False
    _overlapping_bboxes: list[BBox] = field(default_factory=list, repr=False)

    @property
    def local_centroid(self) -> np.ndarray:
        return self.local_coordinates.centroid

    @property
    def global_centroid(self) -> np.ndarray:
        return self.global_coordinates.centroid

    @property
    def local_area(self) -> float:
        coords = self.local_coordinates
        return float(
            (coords.bottom_left[1] - coords.top_left[1])
            * (coords.bottom_right[0] - coords.bottom_left[0])
        )

    def add_box(self, box: BBox) -> None:
        self._overlapping_bboxes.append(box)

    def bb_iou(self, comparison_box: BBox) -> float:
        # Adapted from semif_utils.datasets.BBox.bb_iou (global branch).
        def polygon(coords: BoxCoordinates) -> Polygon:
            return Polygon(
                [coords.top_left, coords.top_right, coords.bottom_right, coords.bottom_left]
            )

        polyA = polygon(self.global_coordinates)
        polyB = polygon(comparison_box.global_coordinates)
        if not polyA.is_valid:
            polyA = polyA.buffer(0)
        if not polyB.is_valid:
            polyB = polyB.buffer(0)
        if not polyA.intersects(polyB):
            return 0.0
        try:
            inter_area = polyA.intersection(polyB).area
            union_area = polyA.union(polyB).area
        except Exception as exc:
            log.error("Error calculating IoU: %s", exc)
            log.error("Box A: %s", polyA)
            log.error("Box B: %s", polyB)
            return 0.0
        if union_area == 0:
            return 0.0
        return inter_area / union_area


@dataclass
class CameraInfo:
    camera_location: np.ndarray
    fov: BoxCoordinates


@dataclass
class ImageData:
    image_id: str
    width: int
    height: int
    camera_info: CameraInfo
    bboxes: list[BBox] = field(default_factory=list)


def generate_hash(box: BBox, auxilliary_hash=None):
    # Tuple identities keep per-image row IDs unique, including punctuation.
    if auxilliary_hash is not None:
        return tuple(sorted((auxilliary_hash, box.bbox_id)))
    return box.bbox_id


def bb_iou(boxA: BoxCoordinates, boxB: BoxCoordinates):
    """Function to calculate the IoU of two bounding boxes

    Args:
        _boxA (BBox): First bounding box
        _boxB (BBox): Secong bounding box

    Returns:
        _type_: _description_
    """

    _boxA = [boxA.top_left[0], -boxA.top_left[1], boxA.bottom_right[0], -boxA.bottom_right[1]]
    _boxB = [boxB.top_left[0], -boxB.top_left[1], boxB.bottom_right[0], -boxB.bottom_right[1]]

    # determine the (x, y)-coordinates of the intersection rectangle
    xA = max(_boxA[0], _boxB[0])
    yA = max(_boxA[1], _boxB[1])
    xB = min(_boxA[2], _boxB[2])
    yB = min(_boxA[3], _boxB[3])

    # compute the area of intersection rectangle
    interArea = abs(max((xB - xA, 0)) * max((yB - yA), 0))
    if interArea == 0:
        return 0
    # compute the area of both the prediction and ground-truth
    # rectangles
    boxAArea = abs((_boxA[2] - _boxA[0]) * (_boxA[3] - _boxA[1]))
    boxBArea = abs((_boxB[2] - _boxB[0]) * (_boxB[3] - _boxB[1]))

    # compute the intersection over union by taking the intersection
    # area and dividing it by the sum of prediction + ground-truth
    # areas - the interesection area
    iou = interArea / float(boxAArea + boxBArea - interArea)

    # return the intersection over union value
    return iou


class BBoxFilter:
    def __init__(self, images: list[ImageData]):
        self.images = images
        self.image_map = {image.image_id: image for image in images}
        self.total_bboxes = sum([len(image.bboxes) for image in self.images])
        self.primary_boxes = []
        self.primary_box_ids = set()

    def deduplicate_bboxes(self):
        """Calculates the ideal bounding box and the associated image from all the
        bounding boxes
        """
        comparisons = self.filter_images()
        self.filter_bounding_boxes(comparisons)

    def filter_images(self) -> dict[str, list[str]]:
        """Filter the images to compare based on the overlap between their fields of
           view

        Returns:
            dict[str, list[str]]: A dictionary containing the image IDs as keys, and
                                  a list of image IDs each key overlaps with
        """
        image_ids = list(self.image_map.keys())
        comparisons = dict()
        # Find the overlap between FOVs of the images
        for i, image_id in enumerate(image_ids):
            image = self.image_map[image_id]
            comparisons[image_id] = []
            for j in range(i + 1, len(image_ids)):
                compare_image_id = image_ids[j]
                compare_image = self.image_map[compare_image_id]
                fov_iou = bb_iou(image.camera_info.fov, compare_image.camera_info.fov)
                if fov_iou > FOV_IOU_THRESH:
                    comparisons[image_id].append(compare_image_id)

        return comparisons

    def filter_bounding_boxes(self, comparisons: dict[str, list[str]]):
        """Find overlapping bounding boxes from the images to compare

        Args:
            comparisons (dict[str, list[str]]): Images to compare, found via
                                                filter_images
        """
        # For all the overlapping images
        visited_bboxes = set()
        areas = []

        for image_id, image_ids_for_comparison in comparisons.items():
            # For each bounding box in the key image
            for box in self.image_map[image_id].bboxes:
                if box.bbox_id not in visited_bboxes:
                    visited_bboxes.add(box.bbox_id)
                    areas.append(box.local_area)
                compared = set()
                # A unique ID for the bounding box in question
                box_hash = generate_hash(box)
                # For each overlapping image
                for compare_image_id in image_ids_for_comparison:
                    boxes = self.image_map[compare_image_id].bboxes
                    # And each of its bounding box
                    for _box in boxes:
                        if _box.bbox_id not in visited_bboxes:
                            visited_bboxes.add(_box.bbox_id)
                            areas.append(_box.local_area)

                        # A unique ID for a pair of bounding boxes
                        # Note that the order of the boxes does not matter
                        # i.e. Box_A,Box_B is the same as Box_B,Box_A
                        _box_hash = generate_hash(_box, box_hash)
                        if _box_hash in compared:
                            continue
                        compared.add(_box_hash)
                        iou = box.bb_iou(_box)
                        if iou > BBOX_OVERLAP_THRESH:
                            # Set the two boxes as overlapping
                            box.add_box(_box)
                            _box.add_box(box)

        self.select_best_bbox()
        self.cleanup_primary_boxes()

    def select_best_bbox(self):
        # visited will be a set of boxes that have been compared
        visited = set()
        for image in self.images:
            # If all the boxes have been checked, no need to
            # check the other images
            if len(visited) == self.total_bboxes:
                break
            bboxes = image.bboxes
            for box in bboxes:
                box_hash = generate_hash(box)
                if box_hash in visited:
                    continue
                all_boxes = [box] + box._overlapping_bboxes
                box_hashes = [generate_hash(_box) for _box in all_boxes]
                visited = visited.union(set(box_hashes))
                # Find the best bounding box
                centers = np.array(
                    [
                        self.image_map[_box.image_id].camera_info.camera_location
                        for _box in all_boxes
                    ]
                )
                centroids = np.array([_box.global_centroid for _box in all_boxes])
                distances = 0
                try:
                    distances = ((centroids - centers[:, :2]) ** 2).sum(axis=-1)
                except ValueError as e:
                    log.exception(f"Error calculating distances: {str(e)}")
                    log.error(f"Centroids: {centroids}")
                    log.error(f"Centers: {centers}")
                    log.error(f"Centers [:, :2]: {centers[:, :2]}")
                    continue
                min_idx = np.argmin(distances)
                all_boxes[min_idx].is_primary = True
                if all_boxes[min_idx].bbox_id not in self.primary_box_ids:
                    self.primary_boxes.append(all_boxes[min_idx])
                    self.primary_box_ids.add(all_boxes[min_idx].bbox_id)

    def cleanup_primary_boxes(self):
        _primary_boxes = []
        for i, box in enumerate(self.primary_boxes):
            image_width = self.image_map[box.image_id].width
            image_height = self.image_map[box.image_id].height

            if (
                box.local_centroid[0] < image_width // 4
                or box.local_centroid[0] > 3 * image_width // 4
                or box.local_centroid[1] < image_height // 4
                or box.local_centroid[1] > 3 * image_height // 4
            ):
                box.is_primary = False
                # del self.primary_boxes[i]
            else:
                _primary_boxes.append(box)

        # Revisit all bounding boxes identified as primary and
        # remove the overlapping ones
        for i in range(len(_primary_boxes)):
            box1 = _primary_boxes[i]
            camera_location1 = self.image_map[box1.image_id].camera_info.camera_location[
                :2
            ]  # get just x and y
            for j in range(i + 1, len(_primary_boxes)):
                box2 = _primary_boxes[j]
                camera_location2 = self.image_map[box2.image_id].camera_info.camera_location[
                    :2
                ]  # get just x and y
                iou = box1.bb_iou(box2)
                if iou > BBOX_OVERLAP_THRESH:
                    # De-duplicate
                    distance1 = ((box1.global_centroid - camera_location1) ** 2).sum(axis=-1)
                    distance2 = ((box2.global_centroid - camera_location2) ** 2).sum(axis=-1)

                    if distance1 < distance2:
                        box2.is_primary = False
                    else:
                        box1.is_primary = False


def _finite_values(row: Mapping[str, Any], columns: Sequence[str], context: str) -> np.ndarray:
    try:
        values = np.array([float(row[column]) for column in columns])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid coordinates for {context}: {columns}") from exc
    if not np.isfinite(values).all():
        raise ValueError(f"Non-finite coordinates for {context}")
    return values


def _reference_coordinates(row: Mapping[str, Any], context: str) -> BoxCoordinates:
    return BoxCoordinates(
        *[
            _finite_values(row, [f"{corner}_x", f"{corner}_y"], context)
            for corner in ["top_left", "top_right", "bottom_left", "bottom_right"]
        ]
    )


def build_primary_images(
    rows: Sequence[Mapping[str, Any]],
    camera_rows: Mapping[str, Mapping[str, Any]],
    fov_rows: Mapping[str, Mapping[str, Any]],
    image_dimensions: Mapping[str, tuple[int, int]],
) -> list[ImageData]:
    """Adapt remapped CSV rows and per-image references to the legacy selector.

    Dimensions are (width, height), normally from NPZ sensor dimensions.
    Normalized detection bounds are scaled to pixels for the legacy integer
    quarter-image boundary checks. World coordinates and camera/FOV references
    must already use one common coordinate frame; this adapter does not reproject.
    """
    images: dict[str, ImageData] = {}
    seen = set()
    for row in rows:
        image_id = str(row["image_id"])
        identity = (image_id, str(row["bounding_box_id"]))
        if identity in seen:
            raise ValueError(f"Duplicate detection identity: {identity}")
        seen.add(identity)
        if image_id not in images:
            try:
                width, height = image_dimensions[image_id]
                camera = camera_rows[image_id]
                fov = fov_rows[image_id]
            except KeyError as exc:
                raise ValueError(
                    f"Missing primary-selection input for image {image_id}: {exc}"
                ) from exc
            if width <= 0 or height <= 0 or int(width) != width or int(height) != height:
                raise ValueError(f"Invalid image dimensions for {image_id}: {(width, height)}")
            images[image_id] = ImageData(
                image_id=image_id,
                width=int(width),
                height=int(height),
                camera_info=CameraInfo(
                    # Selection only uses camera XY, not altitude.
                    camera_location=_finite_values(
                        camera, ["Estimated_X", "Estimated_Y"], image_id
                    ),
                    fov=_reference_coordinates(fov, image_id),
                ),
            )
        image = images[image_id]
        xmin, ymin, xmax, ymax = _finite_values(
            row, ["xmin", "ymin", "xmax", "ymax"], str(identity)
        )
        if not (0 <= xmin < xmax <= 1 and 0 <= ymin < ymax <= 1):
            raise ValueError(f"Invalid normalized bounding box for {identity}")
        local = BoxCoordinates(
            *[
                np.array([x * image.width, y * image.height])
                for x, y in [(xmin, ymin), (xmax, ymin), (xmin, ymax), (xmax, ymax)]
            ]
        )
        world = BoxCoordinates(
            *[
                _finite_values(row, [f"world_{corner}_x", f"world_{corner}_y"], str(identity))
                for corner in ["tl", "tr", "bl", "br"]
            ]
        )
        image.bboxes.append(BBox(identity, image_id, local, world))
    return list(images.values())


def select_primary_rows(
    rows: Sequence[Mapping[str, Any]],
    camera_rows: Mapping[str, Mapping[str, Any]],
    fov_rows: Mapping[str, Mapping[str, Any]],
    image_dimensions: Mapping[str, tuple[int, int]],
) -> list[dict[str, Any]]:
    """Return every input row, in order, with a fresh boolean is_primary flag.

    Inputs are not mutated. Existing flags are recomputed. Only successfully
    remapped rows should be passed; missing references or coordinates raise
    ValueError rather than silently marking detections primary.
    """
    images = build_primary_images(rows, camera_rows, fov_rows, image_dimensions)
    BBoxFilter(images).deduplicate_bboxes()
    flags = {box.bbox_id: box.is_primary for image in images for box in image.bboxes}
    return [
        {**row, "is_primary": flags[(str(row["image_id"]), str(row["bounding_box_id"]))]}
        for row in rows
    ]
