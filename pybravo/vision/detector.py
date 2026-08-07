"""Deck occupancy detection using depth and color data.

Given a CameraFrame and a DeckCalibration, determines which deck positions
are occupied and provides basic measurements.

ROIs are 4-point polygons to handle perspective distortion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - depends on local environment
    cv2 = None

from pybravo.vision.calibration import ROI, DeckCalibration
from pybravo.vision.camera import CameraFrame

logger = logging.getLogger(__name__)

# If the median depth in an ROI is more than this many mm closer than baseline,
# something is occupying that position.
OCCUPANCY_THRESHOLD_MM = 15.0
BASELINE_GOOD_VALID_PIXELS = 1000
BASELINE_WARN_VALID_PIXELS = 100


def _py_bool(value: Any) -> bool:
    return bool(value)


def _py_int(value: Any) -> int:
    return int(value)


def _py_float(value: Any) -> float:
    return float(value)


@dataclass
class SlotReport:
    """Detection result for a single deck position."""
    location: int
    occupied: bool
    confidence: float  # 0.0 to 1.0
    depth_median_mm: Optional[float] = None
    depth_baseline_mm: Optional[float] = None
    depth_delta_mm: Optional[float] = None
    object_height_mm: Optional[float] = None
    observed_class: Optional[str] = None
    class_confidence: Optional[float] = None
    fill_ratio: Optional[float] = None
    surface_flatness_mm: Optional[float] = None
    roi_thumbnail: Optional[np.ndarray] = None  # cropped color image

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "location": _py_int(self.location),
            "occupied": _py_bool(self.occupied),
            "confidence": round(_py_float(self.confidence), 3),
        }
        if self.depth_median_mm is not None:
            d["depth_median_mm"] = round(_py_float(self.depth_median_mm), 1)
        if self.depth_baseline_mm is not None:
            d["depth_baseline_mm"] = round(_py_float(self.depth_baseline_mm), 1)
        if self.depth_delta_mm is not None:
            d["depth_delta_mm"] = round(_py_float(self.depth_delta_mm), 1)
        if self.object_height_mm is not None:
            d["object_height_mm"] = round(_py_float(self.object_height_mm), 1)
        if self.observed_class is not None:
            d["observed_class"] = self.observed_class
        if self.class_confidence is not None:
            d["class_confidence"] = round(_py_float(self.class_confidence), 3)
        if self.fill_ratio is not None:
            d["fill_ratio"] = round(_py_float(self.fill_ratio), 3)
        if self.surface_flatness_mm is not None:
            d["surface_flatness_mm"] = round(_py_float(self.surface_flatness_mm), 1)
        return d


@dataclass
class DeckReport:
    """Full deck detection result."""
    slots: list[SlotReport] = field(default_factory=list)
    calibrated: bool = False

    @property
    def occupied_locations(self) -> list[int]:
        return [s.location for s in self.slots if s.occupied]

    @property
    def empty_locations(self) -> list[int]:
        return [s.location for s in self.slots if not s.occupied]

    def get_slot(self, location: int) -> Optional[SlotReport]:
        for s in self.slots:
            if s.location == location:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibrated": _py_bool(self.calibrated),
            "occupied": [_py_int(location) for location in self.occupied_locations],
            "empty": [_py_int(location) for location in self.empty_locations],
            "slots": [s.to_dict() for s in self.slots],
        }


@dataclass
class BaselineSlotResult:
    location: int
    accepted: bool
    valid_pixels: int
    quality: str = "fail"
    median_depth_mm: Optional[float] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "location": _py_int(self.location),
            "accepted": _py_bool(self.accepted),
            "valid_pixels": _py_int(self.valid_pixels),
            "quality": self.quality,
        }
        if self.median_depth_mm is not None:
            data["median_depth_mm"] = round(_py_float(self.median_depth_mm), 1)
        if self.reason:
            data["reason"] = self.reason
        return data


def _roi_depth_stats(depth: np.ndarray, roi: ROI) -> tuple[Optional[float], int]:
    """Get median depth and valid pixel count inside a polygon ROI."""
    mask = roi.mask(depth.shape)
    valid = depth[mask & (depth > 0)]
    if len(valid) == 0:
        return None, 0
    return float(np.median(valid)), len(valid)


def _crop_color(color: np.ndarray, roi: ROI) -> np.ndarray:
    """Crop the color image to the polygon's bounding rect, masking outside pixels."""
    return roi.crop_masked(color)


def _warp_slot_views(frame: CameraFrame, roi: ROI) -> tuple[np.ndarray, Optional[np.ndarray]]:
    color_view = roi.warp(frame.color)
    depth_view = None if frame.depth is None else roi.warp(frame.depth)
    return color_view, depth_view


def _slot_shape_features(
    color_view: np.ndarray,
    depth_view: Optional[np.ndarray],
    baseline_mm: Optional[float],
) -> dict[str, Optional[float | str]]:
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) is required for slot feature extraction")

    gray = cv2.cvtColor(color_view, cv2.COLOR_BGR2GRAY)
    nonzero_mask = color_view.any(axis=2)
    edges = cv2.Canny(gray, 50, 150)
    edge_density = (
        float(np.count_nonzero(edges[nonzero_mask])) / float(np.count_nonzero(nonzero_mask))
        if np.count_nonzero(nonzero_mask)
        else 0.0
    )

    if depth_view is None:
        return {
            "fill_ratio": None,
            "surface_flatness_mm": None,
            "edge_density": edge_density,
            "observed_class": "unknown_object",
            "class_confidence": 0.2,
        }

    valid_depth = depth_view[depth_view > 0]
    if valid_depth.size == 0 or baseline_mm is None:
        return {
            "fill_ratio": None,
            "surface_flatness_mm": None,
            "edge_density": edge_density,
            "observed_class": "unknown_object",
            "class_confidence": 0.2,
        }

    object_mask = (depth_view > 0) & ((baseline_mm - depth_view) > 8.0)
    fill_ratio = float(np.count_nonzero(object_mask)) / float(object_mask.size)
    object_depth = depth_view[object_mask]
    surface_flatness = float(np.std(object_depth)) if object_depth.size else None
    object_height = float(baseline_mm - np.median(object_depth)) if object_depth.size else 0.0

    observed_class = "unknown_object"
    class_confidence = 0.35

    # First-pass rule-based classifier: large, tall, relatively flat object = tip box.
    if object_height >= 30.0 and fill_ratio >= 0.18:
        observed_class = "tip_box"
        class_confidence = min(0.95, 0.55 + min(object_height, 70.0) / 120.0 + min(fill_ratio, 0.5) / 2.0)
    elif object_height >= 8.0 and fill_ratio >= 0.08:
        observed_class = "low_profile_labware"
        class_confidence = min(0.85, 0.45 + min(object_height, 30.0) / 90.0 + min(fill_ratio, 0.35) / 3.0)
    elif fill_ratio >= 0.03 or edge_density >= 0.05:
        observed_class = "small_object"
        class_confidence = 0.4

    return {
        "fill_ratio": fill_ratio,
        "surface_flatness_mm": surface_flatness,
        "edge_density": edge_density,
        "observed_class": observed_class,
        "class_confidence": class_confidence,
    }


def detect_occupancy(
    frame: CameraFrame,
    calibration: DeckCalibration,
    threshold_mm: float = OCCUPANCY_THRESHOLD_MM,
) -> DeckReport:
    """Detect which deck positions are occupied.

    If depth data and baselines are available, uses depth delta.
    Otherwise falls back to color-based heuristics (edge density inside polygon).
    """
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) is required for deck occupancy detection")
    report = DeckReport(calibrated=calibration.is_complete())

    for location in range(1, 10):
        roi = calibration.get_roi(location)
        if roi is None:
            report.slots.append(SlotReport(
                location=location, occupied=False, confidence=0.0,
            ))
            continue

        # Crop color for thumbnail and warp to a normalized slot view.
        thumbnail = _crop_color(frame.color, roi)
        color_view, depth_view = _warp_slot_views(frame, roi)

        # Depth-based detection
        if frame.depth is not None:
            median_depth, valid_count = _roi_depth_stats(frame.depth, roi)
            baseline = calibration.depth_baselines.get(location)

            if median_depth is not None and baseline is not None:
                delta = baseline - median_depth  # positive = object closer to camera
                occupied = delta > threshold_mm
                features = _slot_shape_features(color_view, depth_view, baseline)
                if occupied:
                    conf = min(1.0, 0.5 + (delta - threshold_mm) / 50.0)
                else:
                    conf = min(1.0, 0.5 + (threshold_mm - delta) / 50.0)

                report.slots.append(SlotReport(
                    location=location,
                    occupied=occupied,
                    confidence=conf,
                    depth_median_mm=median_depth,
                    depth_baseline_mm=baseline,
                    depth_delta_mm=delta,
                    object_height_mm=delta if occupied else None,
                    observed_class=features["observed_class"] if occupied else None,
                    class_confidence=features["class_confidence"] if occupied else None,
                    fill_ratio=features["fill_ratio"] if occupied else None,
                    surface_flatness_mm=features["surface_flatness_mm"] if occupied else None,
                    roi_thumbnail=thumbnail,
                ))
                continue

            # Depth available but no baseline
            if median_depth is not None:
                features = _slot_shape_features(color_view, depth_view, baseline)
                report.slots.append(SlotReport(
                    location=location,
                    occupied=False,
                    confidence=0.0,
                    depth_median_mm=median_depth,
                    observed_class=features["observed_class"],
                    class_confidence=features["class_confidence"],
                    fill_ratio=features["fill_ratio"],
                    surface_flatness_mm=features["surface_flatness_mm"],
                    roi_thumbnail=thumbnail,
                ))
                continue

        # Color-only fallback: edge density inside the polygon mask
        mask = roi.mask(frame.color.shape[:2])
        gray = cv2.cvtColor(frame.color, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        masked_edges = edges[mask]
        total_pixels = mask.sum()
        if total_pixels == 0:
            report.slots.append(SlotReport(
                location=location, occupied=False, confidence=0.0,
                roi_thumbnail=thumbnail,
            ))
            continue

        edge_density = np.count_nonzero(masked_edges) / total_pixels
        occupied = edge_density > 0.08
        conf = min(1.0, edge_density / 0.15) if occupied else 0.3

        report.slots.append(SlotReport(
            location=location,
            occupied=occupied,
            confidence=conf,
            observed_class="unknown_object" if occupied else None,
            class_confidence=0.3 if occupied else None,
            roi_thumbnail=thumbnail,
        ))

    return report


def capture_depth_baselines(
    frame: CameraFrame,
    calibration: DeckCalibration,
) -> DeckCalibration:
    """Capture depth baselines for the empty deck.

    Call this with an empty deck to establish the reference depth for each position.
    """
    if frame.depth is None:
        raise RuntimeError("Depth data is required for baseline capture")

    for location in range(1, 10):
        roi = calibration.get_roi(location)
        if roi is None:
            continue
        median, valid = _roi_depth_stats(frame.depth, roi)
        if median is not None:
            calibration.depth_baselines[location] = median
            logger.info("Baseline for location %d: %.1f mm (%d valid pixels)",
                        location, median, valid)
        else:
            logger.warning("No valid depth data for location %d", location)

    return calibration


def capture_depth_baselines_with_diagnostics(
    frame: CameraFrame,
    calibration: DeckCalibration,
) -> tuple[DeckCalibration, list[BaselineSlotResult]]:
    """Capture depth baselines and report per-slot diagnostics."""
    if frame.depth is None:
        raise RuntimeError("Depth data is required for baseline capture")

    results: list[BaselineSlotResult] = []
    for location in range(1, 10):
        roi = calibration.get_roi(location)
        if roi is None:
            result = BaselineSlotResult(
                location=location,
                accepted=False,
                valid_pixels=0,
                quality="fail",
                reason="No ROI defined",
            )
            logger.warning("Baseline skipped for location %d: no ROI defined", location)
            results.append(result)
            continue

        median, valid = _roi_depth_stats(frame.depth, roi)
        if median is not None:
            if valid >= BASELINE_GOOD_VALID_PIXELS:
                calibration.depth_baselines[location] = median
                result = BaselineSlotResult(
                    location=location,
                    accepted=True,
                    valid_pixels=valid,
                    quality="good",
                    median_depth_mm=median,
                )
                logger.info(
                    "Baseline accepted for location %d: median=%.1f mm, valid_pixels=%d",
                    location,
                    median,
                    valid,
                )
            elif valid >= BASELINE_WARN_VALID_PIXELS:
                result = BaselineSlotResult(
                    location=location,
                    accepted=False,
                    valid_pixels=valid,
                    quality="warn",
                    median_depth_mm=median,
                    reason=f"Weak baseline: only {valid} valid depth pixels (need >= {BASELINE_GOOD_VALID_PIXELS})",
                )
                logger.warning(
                    "Baseline weak for location %d: median=%.1f mm, valid_pixels=%d (need >= %d)",
                    location,
                    median,
                    valid,
                    BASELINE_GOOD_VALID_PIXELS,
                )
            else:
                result = BaselineSlotResult(
                    location=location,
                    accepted=False,
                    valid_pixels=valid,
                    quality="fail",
                    median_depth_mm=median,
                    reason=f"Too few valid depth pixels: {valid} (need >= {BASELINE_WARN_VALID_PIXELS})",
                )
                logger.warning(
                    "Baseline skipped for location %d: only %d valid depth pixels (need >= %d)",
                    location,
                    valid,
                    BASELINE_WARN_VALID_PIXELS,
                )
            results.append(result)
            continue

        result = BaselineSlotResult(
            location=location,
            accepted=False,
            valid_pixels=valid,
            quality="fail",
            reason="No valid depth pixels inside ROI",
        )
        logger.warning(
            "Baseline skipped for location %d: no valid depth pixels inside ROI",
            location,
        )
        results.append(result)

    return calibration, results
