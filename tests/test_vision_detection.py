import pytest

# These imports must follow importorskip: without numpy installed they would
# raise ImportError at collection instead of skipping the module.
np = pytest.importorskip("numpy")

from pybravo.vision.calibration import ROI, DeckCalibration  # noqa: E402
from pybravo.vision.camera import CameraFrame  # noqa: E402
from pybravo.vision.detector import BaselineSlotResult, SlotReport, detect_occupancy  # noqa: E402


def test_detect_occupancy_classifies_tall_slot_as_tip_box():
    color = np.zeros((200, 300, 3), dtype=np.uint8)
    depth = np.full((200, 300), 1000, dtype=np.uint16)

    roi = ROI(points=[[50, 40], [250, 40], [250, 160], [50, 160]])
    # Create a large, tall object inside the slot ROI.
    depth[60:150, 80:220] = 950
    color[60:150, 80:220] = 180

    calibration = DeckCalibration(
        rois={1: roi},
        depth_baselines={1: 1000.0},
        image_width=300,
        image_height=200,
    )
    frame = CameraFrame(color=color, depth=depth, timestamp=0.0)

    report = detect_occupancy(frame, calibration)
    slot = report.get_slot(1)

    assert slot is not None
    assert slot.occupied is True
    assert slot.observed_class == "tip_box"
    assert slot.class_confidence is not None and slot.class_confidence >= 0.55
    assert slot.object_height_mm is not None and slot.object_height_mm >= 15.0


def test_detect_occupancy_classifies_shorter_slot_as_low_profile_labware():
    color = np.zeros((200, 300, 3), dtype=np.uint8)
    depth = np.full((200, 300), 1000, dtype=np.uint16)

    roi = ROI(points=[[40, 40], [260, 40], [260, 170], [40, 170]])
    depth[55:165, 60:240] = 980
    color[55:165, 60:240] = 160

    calibration = DeckCalibration(
        rois={1: roi},
        depth_baselines={1: 1000.0},
        image_width=300,
        image_height=200,
    )
    frame = CameraFrame(color=color, depth=depth, timestamp=0.0)

    report = detect_occupancy(frame, calibration)
    slot = report.get_slot(1)

    assert slot is not None
    assert slot.occupied is True
    assert slot.observed_class == "low_profile_labware"
    assert slot.object_height_mm is not None and 15.0 <= slot.object_height_mm <= 25.0


def test_detector_reports_convert_numpy_scalars_to_builtin_json_types():
    slot = SlotReport(
        location=np.int64(5),
        occupied=np.bool_(True),
        confidence=np.float64(0.75),
        depth_delta_mm=np.float32(22.5),
        class_confidence=np.float32(0.9),
        fill_ratio=np.float32(0.4),
        surface_flatness_mm=np.float32(1.2),
    )
    baseline = BaselineSlotResult(
        location=np.int64(2),
        accepted=np.bool_(True),
        valid_pixels=np.int64(1234),
        median_depth_mm=np.float32(999.5),
    )

    slot_payload = slot.to_dict()
    baseline_payload = baseline.to_dict()

    assert slot_payload["occupied"] is True
    assert isinstance(slot_payload["occupied"], bool)
    assert isinstance(slot_payload["location"], int)
    assert isinstance(slot_payload["confidence"], float)
    assert isinstance(slot_payload["depth_delta_mm"], float)
    assert isinstance(baseline_payload["accepted"], bool)
    assert isinstance(baseline_payload["valid_pixels"], int)
