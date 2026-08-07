"""Interactive 4-point ROI calibration tool.

Usage:
    python -m pybravo.vision.calibrate_rois <image_path> [location]

Opens a window showing the deck image. For each location (1-9), or for one
specific location if provided:
  - Click 4 corners clockwise: top-left, top-right, bottom-right, bottom-left.
  - Press ENTER to confirm, or 'r' to redo the current position.
  - Press 'q' to quit early (partial calibration is saved).

The 9 positions follow the Bravo deck layout:
    1  2  3   (back row)
    4  5  6   (middle row)
    7  8  9   (front row)

Saved to config/vision_calibration.yaml.
"""

from __future__ import annotations

import sys

import cv2
import numpy as np

from pybravo.vision.calibration import (
    DeckCalibration, ROI, save_calibration, save_reference_image, load_calibration,
)

LOCATION_ORDER = [1, 2, 3, 4, 5, 6, 7, 8, 9]
LOCATION_LABELS = {
    1: "Pos 1 (back-left)",
    2: "Pos 2 (back-center)",
    3: "Pos 3 (back-right)",
    4: "Pos 4 (mid-left)",
    5: "Pos 5 (mid-center)",
    6: "Pos 6 (mid-right)",
    7: "Pos 7 (front-left)",
    8: "Pos 8 (front-center)",
    9: "Pos 9 (front-right)",
}

CORNER_LABELS = ["top-left", "top-right", "bottom-right", "bottom-left"]

# Colors for each location (BGR)
COLORS = {
    1: (0, 255, 0),
    2: (0, 200, 255),
    3: (255, 0, 0),
    4: (0, 255, 255),
    5: (255, 0, 255),
    6: (255, 255, 0),
    7: (128, 255, 0),
    8: (0, 128, 255),
    9: (255, 128, 0),
}


class QuadSelector:
    def __init__(self, image: np.ndarray):
        self.image = image
        self.current_points: list[list[int]] = []
        self.mouse_pos = (0, 0)

    def mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            if len(self.current_points) < 4:
                self.current_points.append([x, y])
        elif event == cv2.EVENT_MOUSEMOVE:
            self.mouse_pos = (x, y)

    def reset(self):
        self.current_points = []

    @property
    def is_complete(self) -> bool:
        return len(self.current_points) == 4

    def get_display(self, existing_rois: dict[int, ROI], current_location: int) -> np.ndarray:
        display = self.image.copy()

        # Draw existing confirmed polygons
        for loc, roi in existing_rois.items():
            color = COLORS.get(loc, (255, 255, 255))
            pts = roi.np_points
            cv2.polylines(display, [pts], isClosed=True, color=color, thickness=2)
            cx, cy = roi.center
            cv2.putText(display, str(loc), (cx - 8, cy + 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        # Draw current points being placed
        color = COLORS.get(current_location, (255, 255, 255))
        for i, pt in enumerate(self.current_points):
            cv2.circle(display, tuple(pt), 5, color, -1)
            cv2.putText(display, CORNER_LABELS[i], (pt[0] + 8, pt[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

        # Draw lines between placed points
        if len(self.current_points) >= 2:
            for i in range(len(self.current_points) - 1):
                cv2.line(display, tuple(self.current_points[i]),
                         tuple(self.current_points[i + 1]), color, 2)
        # Draw closing line and rubber-band
        if len(self.current_points) == 4:
            cv2.line(display, tuple(self.current_points[3]),
                     tuple(self.current_points[0]), color, 2)
            # Fill with transparent overlay
            overlay = display.copy()
            pts = np.array(self.current_points, dtype=np.int32)
            cv2.fillPoly(overlay, [pts], color)
            cv2.addWeighted(overlay, 0.15, display, 0.85, 0, display)
        elif len(self.current_points) >= 1:
            # Rubber-band line from last point to mouse
            cv2.line(display, tuple(self.current_points[-1]),
                     self.mouse_pos, color, 1, cv2.LINE_AA)

        # Instructions
        label = LOCATION_LABELS.get(current_location, f"Pos {current_location}")
        n = len(self.current_points)
        if n < 4:
            corner = CORNER_LABELS[n]
            instruction = f"Click {corner} corner ({n+1}/4)"
        else:
            instruction = "ENTER=confirm  R=redo"

        cv2.putText(display, f"{label}: {instruction}", (10, display.shape[0] - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(display, "R=redo  Q=quit  U=undo last point", (10, display.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        return display


def run_calibration(image_path: str, locations: list[int] | None = None) -> DeckCalibration:
    image = cv2.imread(image_path)
    if image is None:
        print(f"ERROR: Could not load image: {image_path}")
        sys.exit(1)

    h, w = image.shape[:2]
    print(f"Loaded image: {w}x{h}")
    order = locations or LOCATION_ORDER
    print(f"Define 4-point polygon ROIs for {len(order)} deck position(s).")
    print("Click 4 corners clockwise: top-left, top-right, bottom-right, bottom-left.")
    print("ENTER=confirm, R=redo, U=undo last point, Q=quit.\n")

    # Save reference image
    ref_path = save_reference_image(image)

    # Load existing calibration if any
    existing = load_calibration()
    cal = DeckCalibration(
        image_width=w,
        image_height=h,
        reference_image_path=str(ref_path),
    )
    if existing and existing.rois:
        cal.rois = dict(existing.rois)
        print(f"Loaded {len(cal.rois)} existing ROIs. Will overwrite as you draw new ones.\n")

    window = "Deck ROI Calibration"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, min(w, 1400), min(h, 900))

    selector = QuadSelector(image)
    cv2.setMouseCallback(window, selector.mouse_callback)

    loc_idx = 0
    while loc_idx < len(order):
        location = order[loc_idx]
        selector.reset()
        label = LOCATION_LABELS[location]
        print(f"  [{loc_idx + 1}/{len(order)}] Define polygon for {label}...")

        while True:
            display = selector.get_display(cal.rois, location)
            cv2.imshow(window, display)
            key = cv2.waitKey(30) & 0xFF

            if key == 13:  # ENTER
                if selector.is_complete:
                    cal.rois[location] = ROI(points=selector.current_points[:])
                    pts = selector.current_points
                    print(f"    Confirmed: {pts}")
                    loc_idx += 1
                    break
                else:
                    n = len(selector.current_points)
                    print(f"    Need 4 points, have {n} — keep clicking.")
            elif key == ord('r'):
                selector.reset()
                print(f"    Reset — click 4 corners for {label}")
            elif key == ord('u'):
                if selector.current_points:
                    removed = selector.current_points.pop()
                    print(f"    Undo: removed point {removed}")
            elif key == ord('q') or key == 27:
                print("\nQuitting early.")
                loc_idx = len(LOCATION_ORDER)
                break

    cv2.destroyAllWindows()

    # Save
    save_calibration(cal)
    n = len(cal.rois)
    print(f"\nCalibration saved with {n}/9 ROIs.")
    if cal.is_complete():
        print("All 9 positions defined.")
    else:
        missing = [i for i in range(1, 10) if i not in cal.rois]
        print(f"Missing positions: {missing}")

    return cal


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m pybravo.vision.calibrate_rois <image_path> [location]")
        print("\nProvide a deck reference image to define ROIs for the 9 deck positions or one specific location.")
        sys.exit(1)
    locations = None
    if len(sys.argv) >= 3:
        try:
            location = int(sys.argv[2])
        except ValueError:
            print(f"Invalid location: {sys.argv[2]!r}")
            sys.exit(1)
        if location not in LOCATION_LABELS:
            print(f"Location must be 1-9, got {location}")
            sys.exit(1)
        locations = [location]
    run_calibration(sys.argv[1], locations)


if __name__ == "__main__":
    main()
