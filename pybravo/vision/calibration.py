"""Deck ROI calibration — define and persist regions of interest for the 9 deck positions.

Calibration data is stored in config/vision_calibration.yaml alongside the existing
calibration scaffold fields.

Each deck position (1-9) gets a 4-point polygon ROI in pixel coordinates
to handle perspective distortion from the camera angle:

    location_N:
      points: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]

Points are ordered clockwise starting from top-left.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import yaml

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - depends on local environment
    cv2 = None

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_CALIBRATION_PATH = _CONFIG_DIR / "vision_calibration.yaml"
_REFERENCE_DIR = _CONFIG_DIR / "vision_reference"


@dataclass
class ROI:
    """A 4-point polygon region of interest in pixel coordinates.

    Points are ordered clockwise: top-left, top-right, bottom-right, bottom-left.
    """
    points: list[list[int]]  # [[x,y], [x,y], [x,y], [x,y]]

    @property
    def np_points(self) -> np.ndarray:
        """Return points as an int32 numpy array suitable for cv2 drawing/masking."""
        return np.array(self.points, dtype=np.int32)

    @property
    def center(self) -> tuple[int, int]:
        pts = self.np_points
        cx = int(pts[:, 0].mean())
        cy = int(pts[:, 1].mean())
        return cx, cy

    @property
    def bounding_rect(self) -> tuple[int, int, int, int]:
        """Return axis-aligned bounding box (x, y, w, h)."""
        pts = self.np_points
        x = int(pts[:, 0].min())
        y = int(pts[:, 1].min())
        w = int(pts[:, 0].max()) - x
        h = int(pts[:, 1].max()) - y
        return x, y, w, h

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        """Return a boolean mask of this polygon over an image of given (h, w)."""
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for ROI masking")
        m = np.zeros(shape[:2], dtype=np.uint8)
        cv2.fillPoly(m, [self.np_points], 255)
        return m > 0

    def crop_masked(self, image: np.ndarray) -> np.ndarray:
        """Crop the bounding rect and zero out pixels outside the polygon."""
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for ROI cropping")
        x, y, w, h = self.bounding_rect
        crop = image[y:y + h, x:x + w].copy()
        # Build mask relative to the crop
        shifted = self.np_points.copy()
        shifted[:, 0] -= x
        shifted[:, 1] -= y
        m = np.zeros(crop.shape[:2], dtype=np.uint8)
        cv2.fillPoly(m, [shifted], 255)
        if crop.ndim == 3:
            crop[m == 0] = 0
        else:
            crop[m == 0] = 0
        return crop

    def warp(self, image: np.ndarray, width: int = 240, height: int = 160) -> np.ndarray:
        """Perspective-warp this ROI into a normalized slot view."""
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for ROI warping")
        src = np.array(self.points, dtype=np.float32)
        dst = np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(image, matrix, (width, height))


@dataclass
class DeckCalibration:
    """Full calibration state for the 9-position deck."""
    rois: dict[int, ROI] = field(default_factory=dict)
    depth_baselines: dict[int, float] = field(default_factory=dict)
    image_width: int = 0
    image_height: int = 0
    reference_image_path: Optional[str] = None

    def get_roi(self, location: int) -> Optional[ROI]:
        return self.rois.get(location)

    def is_complete(self) -> bool:
        return len(self.rois) == 9 and all(i in self.rois for i in range(1, 10))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "image_width": self.image_width,
            "image_height": self.image_height,
        }
        if self.reference_image_path:
            data["reference_image_path"] = self.reference_image_path
        rois_dict: dict[str, Any] = {}
        for loc, roi in sorted(self.rois.items()):
            rois_dict[f"location_{loc}"] = {"points": roi.points}
        data["rois"] = rois_dict
        if self.depth_baselines:
            baselines: dict[str, float] = {}
            for loc, val in sorted(self.depth_baselines.items()):
                baselines[f"location_{loc}"] = val
            data["depth_baselines"] = baselines
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeckCalibration:
        cal = cls(
            image_width=data.get("image_width", 0),
            image_height=data.get("image_height", 0),
            reference_image_path=data.get("reference_image_path"),
        )
        rois_data = data.get("rois") or {}
        for key, roi_data in rois_data.items():
            loc = int(key.replace("location_", ""))
            cal.rois[loc] = ROI(points=roi_data["points"])
        baselines_data = data.get("depth_baselines") or {}
        for key, val in baselines_data.items():
            loc = int(key.replace("location_", ""))
            cal.depth_baselines[loc] = float(val)
        return cal


def load_calibration() -> Optional[DeckCalibration]:
    """Load deck vision calibration from config/vision_calibration.yaml."""
    if not _CALIBRATION_PATH.exists():
        return None
    with _CALIBRATION_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    vision_cal = raw.get("vision_rois")
    if vision_cal is None:
        return None
    return DeckCalibration.from_dict(vision_cal)


def save_calibration(cal: DeckCalibration) -> None:
    """Save deck vision calibration, merging with existing calibration fields."""
    existing: dict[str, Any] = {}
    if _CALIBRATION_PATH.exists():
        with _CALIBRATION_PATH.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    existing["vision_rois"] = cal.to_dict()
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _CALIBRATION_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, sort_keys=False)
    logger.info("Vision calibration saved to %s", _CALIBRATION_PATH)


def save_reference_image(image, filename: str = "deck_reference.png") -> Path:
    """Save a reference image to config/vision_reference/."""
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) is required to save a reference image")
    _REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = _REFERENCE_DIR / filename
    cv2.imwrite(str(path), image)
    logger.info("Reference image saved: %s", path)
    return path


def draw_roi_overlays(
    image: np.ndarray,
    calibration: Optional[DeckCalibration],
    *,
    show_labels: bool = True,
) -> np.ndarray:
    """Draw calibrated slot ROI outlines and location labels onto an image."""
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) is required to draw ROI overlays")
    annotated = image.copy()
    if calibration is None:
        return annotated

    for location in range(1, 10):
        roi = calibration.get_roi(location)
        if roi is None:
            continue
        pts = roi.np_points.reshape((-1, 1, 2))
        has_baseline = location in calibration.depth_baselines
        color = (82, 210, 115) if has_baseline else (240, 195, 91)
        cv2.polylines(annotated, [pts], isClosed=True, color=color, thickness=2, lineType=cv2.LINE_AA)
        if show_labels:
            cx, cy = roi.center
            cv2.circle(annotated, (cx, cy), 4, color, -1, lineType=cv2.LINE_AA)
            cv2.putText(
                annotated,
                str(location),
                (cx + 6, cy - 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2,
                cv2.LINE_AA,
            )

    return annotated
