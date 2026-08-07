import pytest

pytest.importorskip("numpy")

import pybravo.vision_service as vision_service
from pybravo.vision.calibration import ROI, DeckCalibration
from pybravo.vision.camera import CameraFrame


async def test_calibration_run_persists_scaffold(tmp_path, monkeypatch):
    calibration_path = tmp_path / "vision_calibration.yaml"
    monkeypatch.setattr(vision_service, "_CALIBRATION_PATH", calibration_path)
    monkeypatch.setattr(
        vision_service,
        "_camera_status",
        lambda: {"camera_available": False, "mode": "none", "message": "no camera"},
    )

    response = await vision_service.run_calibration(
        vision_service.CalibrationRunRequest(machine_id="BRAVO-1", notes="fixed side mount")
    )
    assert response["ok"] is True
    assert response["calibration"]["machine_id"] == "BRAVO-1"
    assert calibration_path.exists()


async def test_calibration_run_captures_reference_snapshot(tmp_path, monkeypatch):
    calibration_path = tmp_path / "vision_calibration.yaml"
    monkeypatch.setattr(vision_service, "_CALIBRATION_PATH", calibration_path)

    class _Frame:
        def __init__(self):
            self.color = __import__("numpy").zeros((480, 640, 3), dtype="uint8")

    class _Source:
        def capture(self):
            return _Frame()

    saved = tmp_path / "deck_reference.png"
    monkeypatch.setattr(
        vision_service,
        "_camera_status",
        lambda: {"camera_available": True, "mode": "femto_bolt", "message": "camera ok"},
    )
    monkeypatch.setattr(vision_service, "_get_camera", lambda: _Source())
    monkeypatch.setattr(vision_service, "save_reference_image", lambda image: saved)

    response = await vision_service.run_calibration(
        vision_service.CalibrationRunRequest(machine_id="BRAVO-1", notes="fixed side mount")
    )

    assert response["ok"] is True
    assert response["calibration"]["reference_image_path"] == str(saved)
    assert response["calibration"]["image_width"] == 640
    assert response["calibration"]["image_height"] == 480


async def test_verify_returns_slot_report_shape(tmp_path, monkeypatch):
    calibration_path = tmp_path / "vision_calibration.yaml"
    monkeypatch.setattr(vision_service, "_CALIBRATION_PATH", calibration_path)

    response = await vision_service.verify(
        vision_service.VerifyRequest(
            expected_scene={
                "machine_id": "BRAVO-1",
                "slots": [
                    {"location": 1, "expected_labware": {"name": "Plate A", "model_3d": "/labware-assets/a.gltf"}},
                    {"location": 2, "expected_labware": None},
                ],
            }
        )
    )
    assert response["ok"] is True
    assert response["report"]["summary"]["slot_count"] == 2
    assert response["report"]["slots"][0]["expected_labware"]["name"] == "Plate A"
    assert response["report"]["slots"][1]["status"] == "empty_ok"


async def test_status_prefers_live_camera_state_over_saved_snapshot(tmp_path, monkeypatch):
    calibration_path = tmp_path / "vision_calibration.yaml"
    calibration_path.write_text(
        "\n".join(
            [
                "machine_id: BRAVO-1",
                "camera_status:",
                "  camera_available: false",
                "  mode: none",
                "  message: old snapshot",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vision_service, "_CALIBRATION_PATH", calibration_path)
    monkeypatch.setattr(
        vision_service,
        "_camera_status",
        lambda: {
            "camera_available": True,
            "mode": "femto_bolt",
            "message": "live camera ok",
        },
    )

    response = await vision_service.status()

    assert response["camera"]["camera_available"] is True
    assert response["calibration"]["file"]["camera_status"]["message"] == "live camera ok"
    assert response["calibration"]["file"]["saved_camera_status"]["message"] == "old snapshot"


async def test_verify_reports_needs_baselines_when_rois_exist_without_depth_baselines(monkeypatch):
    class _Cal:
        rois = {i: object() for i in range(1, 10)}
        depth_baselines = {}

        def is_complete(self):
            return True

    monkeypatch.setattr(vision_service, "load_calibration", lambda: _Cal())

    response = await vision_service.verify(
        vision_service.VerifyRequest(
            expected_scene={"slots": [{"location": 5, "expected_labware": {"name": "384 V11 ST10 Tip Box 10734.102"}}]}
        )
    )

    assert response["ok"] is True
    assert response["report"]["summary"]["status"] == "needs_baselines"
    assert response["report"]["summary"]["baselines_ready"] is False


async def test_capture_baselines_returns_per_slot_diagnostics(monkeypatch):
    calibration = DeckCalibration(
        rois={
            1: ROI(points=[[0, 0], [9, 0], [9, 9], [0, 9]]),
            2: ROI(points=[[10, 0], [19, 0], [19, 9], [10, 9]]),
        },
        image_width=20,
        image_height=10,
    )

    depth = __import__("numpy").zeros((40, 60), dtype="uint16")
    depth[:, :30] = 1000
    frame = CameraFrame(
        color=__import__("numpy").zeros((40, 60, 3), dtype="uint8"),
        depth=depth,
        timestamp=0.0,
    )

    class _Source:
        def capture(self):
            return frame

    monkeypatch.setattr(vision_service, "load_calibration", lambda: calibration)
    monkeypatch.setattr(vision_service, "_get_camera", lambda: _Source())
    monkeypatch.setattr(vision_service, "save_calibration", lambda cal: None)

    calibration.image_width = 60
    calibration.image_height = 40
    calibration.rois = {
        1: ROI(points=[[0, 0], [29, 0], [29, 39], [0, 39]]),
        2: ROI(points=[[30, 0], [59, 0], [59, 39], [30, 39]]),
    }

    response = await vision_service.api_capture_baselines(vision_service.CaptureBaselineRequest())

    assert response["ok"] is True
    assert "results" in response
    assert any(result["location"] == 1 and result["accepted"] is True for result in response["results"])
    assert any(result["location"] == 2 and result["accepted"] is False for result in response["results"])


def test_occluding_slots_marks_farther_empty_slot_as_occluded():
    calibration = DeckCalibration(
        rois={
            5: ROI(points=[[100, 100], [220, 100], [220, 220], [100, 220]]),
            6: ROI(points=[[180, 90], [300, 90], [300, 210], [180, 210]]),
        },
        depth_baselines={5: 420.0, 6: 680.0},
    )

    class _Slot:
        def __init__(self, location, occupied, observed_class):
            self.location = location
            self.occupied = occupied
            self.observed_class = observed_class

    class _Report:
        def __init__(self):
            self.slots = [_Slot(5, True, "tip_box"), _Slot(6, True, "tip_box")]

    occluded = vision_service._occluding_slots(
        calibration,
        _Report(),
        {
            5: {"location": 5, "expected_labware": {"name": "384 V11 ST10 Tip Box 10734.102"}},
            6: {"location": 6, "expected_labware": None},
        },
    )

    assert 6 in occluded
    assert occluded[6]["by_location"] == 5


async def test_expected_non_tipbox_labware_is_present_even_if_coarse_classifier_calls_it_tip_box(monkeypatch):
    class _Cal:
        depth_baselines = {i: 300.0 + i for i in range(1, 10)}

        def is_complete(self):
            return True

        def get_roi(self, location):
            return None

    class _Slot:
        location = 8
        occupied = True
        confidence = 0.9
        depth_median_mm = 250.0
        depth_delta_mm = 148.0
        object_height_mm = 148.0
        observed_class = "tip_box"
        class_confidence = 0.8
        fill_ratio = 0.3
        surface_flatness_mm = 4.0

    class _Report:
        slots = [_Slot()]

        def get_slot(self, location):
            return _Slot() if location == 8 else None

    monkeypatch.setattr(vision_service, "load_calibration", lambda: _Cal())
    monkeypatch.setattr(vision_service, "_get_camera", lambda: object())
    monkeypatch.setattr(vision_service, "detect_occupancy", lambda frame, calibration: _Report())

    class _Source:
        def capture(self):
            return object()

    monkeypatch.setattr(vision_service, "_get_camera", lambda: _Source())

    response = await vision_service.verify(
        vision_service.VerifyRequest(
            expected_scene={
                "slots": [
                    {
                        "location": 8,
                        "expected_labware": {
                            "name": "384 Greiner 781091 PS uclear",
                            "kind": "sbs_plate",
                            "base_class": "microplate",
                            "height_mm": 14.4,
                            "length_mm": 127.76,
                            "width_mm": 85.48,
                            "rows": 16,
                            "cols": 24,
                            "wells": 384,
                            "model_3d": "/labware/greiner_384.gltf",
                        },
                    },
                ]
            }
        )
    )

    assert response["ok"] is True
    assert response["report"]["slots"][0]["status"] == "present"
    observed = response["report"]["slots"][0]["observed_labware"]
    assert observed["matched_name"] == "384 Greiner 781091 PS uclear"
    assert observed["matched_family"] == "plate"
    assert observed["coarse_class"] == "tip_box"
    assert "model_3d" in observed["used_properties"]

