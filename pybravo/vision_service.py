from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from pybravo.vision.camera import FemtoBoltCamera, StaticImageSource, create_camera_source
from pybravo.vision.calibration import (
    DeckCalibration, draw_roi_overlays, load_calibration, save_calibration, save_reference_image,
)
from pybravo.vision.detector import (
    capture_depth_baselines_with_diagnostics,
    detect_occupancy,
)

logger = logging.getLogger(__name__)

_CALIBRATION_PATH = Path(__file__).resolve().parents[1] / "config" / "vision_calibration.yaml"
_REFERENCE_DIR = Path(__file__).resolve().parents[1] / "config" / "vision_reference"


class CalibrationRunRequest(BaseModel):
    machine_id: str = ""
    notes: str = ""


class VerifyRequest(BaseModel):
    expected_scene: dict[str, Any]


class CaptureBaselineRequest(BaseModel):
    """Request to capture depth baselines for the empty deck."""
    pass


# ---------------------------------------------------------------------------
# Camera singleton
# ---------------------------------------------------------------------------

_camera_source: Optional[FemtoBoltCamera | StaticImageSource] = None
_preview_cache: dict[str, Any] = {
    "captured_at": 0.0,
    "rgb_jpeg": None,
    "depth_jpeg": None,
}
_PREVIEW_CACHE_TTL_S = 0.06
_STREAM_BOUNDARY = "frame"


def _encode_jpeg(image, quality: int = 85) -> bytes:
    cv2 = __import__("cv2")
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


def _preview_payload(force: bool = False) -> dict[str, bytes | float | None]:
    now = datetime.now(timezone.utc).timestamp()
    cached_at = float(_preview_cache.get("captured_at") or 0.0)
    if (
        not force
        and _preview_cache.get("rgb_jpeg") is not None
        and now - cached_at <= _PREVIEW_CACHE_TTL_S
    ):
        return _preview_cache

    source = _get_camera()
    frame = source.capture()
    calibration = load_calibration()
    annotated_rgb = draw_roi_overlays(frame.color, calibration)
    rgb_jpeg = _encode_jpeg(annotated_rgb, quality=72)

    depth_vis = frame.depth_colorized()
    depth_jpeg = None
    if depth_vis is not None:
        annotated_depth = draw_roi_overlays(depth_vis, calibration)
        depth_jpeg = _encode_jpeg(annotated_depth, quality=72)

    _preview_cache.update({
        "captured_at": now,
        "rgb_jpeg": rgb_jpeg,
        "depth_jpeg": depth_jpeg,
    })
    return _preview_cache


def _mjpeg_stream(which: str):
    while True:
        payload = _preview_payload(force=True)
        jpeg_bytes = payload.get("depth_jpeg" if which == "depth" else "rgb_jpeg")
        if jpeg_bytes is not None:
            yield (
                f"--{_STREAM_BOUNDARY}\r\n"
                "Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg_bytes)}\r\n\r\n"
            ).encode("ascii") + jpeg_bytes + b"\r\n"
        time.sleep(0.08)


def _get_camera() -> FemtoBoltCamera | StaticImageSource:
    """Return the camera singleton, initializing on first call."""
    global _camera_source
    explicit_static_path = os.getenv("PYBRAVO_VISION_STATIC_IMAGE")
    if _camera_source is not None and _camera_source.is_open:
        if explicit_static_path or isinstance(_camera_source, FemtoBoltCamera):
            return _camera_source
        try:
            live_source = create_camera_source(static_image=None)
            if _camera_source is not None:
                _camera_source.close()
            _camera_source = live_source
            logger.info("Switched vision service from static reference image to live Femto camera")
            return _camera_source
        except Exception:
            return _camera_source

    static_fallback_path: str | None = None
    if explicit_static_path:
        static_fallback_path = explicit_static_path
    else:
        ref = _REFERENCE_DIR / "deck_reference.png"
        if ref.exists():
            static_fallback_path = str(ref)

    try:
        _camera_source = create_camera_source(static_image=None)
        logger.info("Vision service using live Femto camera")
    except Exception as live_error:
        if static_fallback_path:
            logger.warning(
                "Live Femto camera unavailable (%s); falling back to static reference image %s",
                live_error,
                static_fallback_path,
            )
            _camera_source = create_camera_source(static_image=static_fallback_path)
        else:
            logger.error("Failed to initialize camera: %s", live_error)
            raise

    return _camera_source


def _camera_status() -> dict[str, Any]:
    """Report camera/SDK availability."""
    try:
        source = _get_camera()
        if isinstance(source, StaticImageSource):
            return {
                "camera_available": True,
                "mode": "static_image",
                "message": "Using static reference image (no live camera)",
            }
        elif isinstance(source, FemtoBoltCamera):
            return {
                "camera_available": True,
                "mode": "femto_bolt",
                "device_info": source.device_info,
                "message": "Femto Bolt connected",
            }
        return {"camera_available": True, "mode": "unknown"}
    except Exception as e:
        return {
            "camera_available": False,
            "mode": "none",
            "message": str(e),
        }


def _load_calibration_file() -> dict[str, Any] | None:
    if not _CALIBRATION_PATH.exists():
        return None
    with _CALIBRATION_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else None


def _roi_x_overlap_ratio(calibration: DeckCalibration, a: int, b: int) -> float:
    roi_a = calibration.get_roi(a)
    roi_b = calibration.get_roi(b)
    if roi_a is None or roi_b is None:
        return 0.0
    ax, _, aw, _ = roi_a.bounding_rect
    bx, _, bw, _ = roi_b.bounding_rect
    a1, a2 = ax, ax + aw
    b1, b2 = bx, bx + bw
    overlap = max(0, min(a2, b2) - max(a1, b1))
    min_width = max(1, min(aw, bw))
    return overlap / float(min_width)


def _occluding_slots(
    calibration: DeckCalibration,
    detection_report,
    expected_slots: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    occluded: dict[int, dict[str, Any]] = {}
    if detection_report is None:
        return occluded

    for slot in detection_report.slots:
        if not slot.occupied or slot.observed_class != "tip_box":
            continue
        expected_slot = expected_slots.get(slot.location) or {}
        expected_labware = expected_slot.get("expected_labware") or {}
        expected_name_lc = str(expected_labware.get("name") or "").lower()
        expected_is_tip_box = "tip box" in expected_name_lc or "tipbox" in expected_name_lc
        if expected_labware and not expected_is_tip_box:
            continue
        slot_depth = calibration.depth_baselines.get(slot.location)
        if slot_depth is None:
            continue

        for other_location, expected in expected_slots.items():
            if other_location == slot.location or expected.get("expected_labware") is not None:
                continue
            other_depth = calibration.depth_baselines.get(other_location)
            if other_depth is None or other_depth <= slot_depth + 40.0:
                continue
            overlap_ratio = _roi_x_overlap_ratio(calibration, slot.location, other_location)
            if overlap_ratio < 0.25:
                continue
            occluded[other_location] = {
                "by_location": slot.location,
                "overlap_ratio": overlap_ratio,
                "occluder_depth_mm": slot_depth,
                "blocked_depth_mm": other_depth,
            }
    return occluded


def _numeric_field(expected: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = expected.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _expected_family(expected: dict[str, Any] | None) -> str | None:
    if not expected:
        return None
    name_lc = str(expected.get("name") or "").lower()
    kind_lc = str(expected.get("kind") or expected.get("labware_type") or "").lower()
    base_lc = str(expected.get("base_class") or "").lower()
    if "tip box" in name_lc or "tipbox" in name_lc or kind_lc == "tip_box":
        return "tip_box"
    if kind_lc == "sbs_plate" or "plate" in kind_lc or "microplate" in base_lc:
        return "plate"
    return "labware"


def _match_expected_labware(expected: dict[str, Any], det_slot) -> dict[str, Any]:
    expected_name = str(expected.get("name") or "expected labware")
    family = _expected_family(expected)
    observed_height = float(det_slot.object_height_mm or 0.0)
    fill_ratio = float(det_slot.fill_ratio or 0.0)
    expected_height = _numeric_field(expected, "height_mm", "height")
    rows = _numeric_field(expected, "rows")
    cols = _numeric_field(expected, "cols")
    wells = _numeric_field(expected, "wells")

    score = float(det_slot.class_confidence or det_slot.confidence or 0.3)
    reason = "Matched expected labware using occupied ROI and labware metadata."
    display_class = det_slot.observed_class or family or "object"
    display_name = expected_name
    used_properties = ["name", "kind", "base_class"]

    if family == "tip_box":
        score = 0.9 if det_slot.observed_class == "tip_box" else max(0.2, score - 0.3)
        display_class = "tip_box"
        reason = "Matched expected tip box using occupied ROI and tip-box-specific catalog metadata."
        used_properties.extend(["height_mm", "tip_definition_id", "supported_tip_ids"])
    elif family == "plate":
        score = max(0.7, score)
        display_class = "plate"
        reason = "Matched expected plate using occupied ROI and plate metadata."
        used_properties.extend(["height_mm", "length_mm", "width_mm", "rows", "cols", "wells"])
        if wells and wells >= 384:
            score = min(0.95, score + 0.05)
        if rows and cols:
            score = min(0.97, score + 0.03)
        if fill_ratio > 0:
            score = min(0.97, score + min(fill_ratio, 0.25))
    else:
        score = max(0.65, score)
        display_class = family or display_class
        used_properties.extend(["height_mm", "length_mm", "width_mm"])

    if expected_height and observed_height:
        used_properties.append("height_mm")
        height_ratio = abs(observed_height - expected_height) / max(expected_height, 1.0)
        if family == "tip_box":
            if height_ratio < 0.8:
                score = min(0.98, score + 0.04)
            else:
                score = max(0.5, score - 0.1)
        else:
            # Side-view depth delta is not a direct physical height, so use this only as a weak prior.
            if height_ratio < 4.0:
                score = min(0.98, score + 0.02)

    model_3d = expected.get("model_3d")
    if model_3d:
        used_properties.append("model_3d")

    return {
        "name": display_name,
        "matched_name": display_name,
        "class": display_class,
        "matched_family": family or display_class,
        "confidence": round(min(score, 0.99), 3),
        "coarse_class": det_slot.observed_class,
        "expected_height_mm": expected_height,
        "expected_rows": rows,
        "expected_cols": cols,
        "expected_wells": wells,
        "expected_length_mm": _numeric_field(expected, "length_mm", "length"),
        "expected_width_mm": _numeric_field(expected, "width_mm", "width"),
        "model_3d": model_3d,
        "used_properties": sorted(set(prop for prop in used_properties if expected.get(prop) is not None or prop in {"name", "kind", "base_class", "model_3d"})),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Verify logic
# ---------------------------------------------------------------------------

def _verify_scene(expected_scene: dict[str, Any]) -> dict[str, Any]:
    """Verify the deck against expected scene using camera + calibration."""
    calibration = load_calibration()
    baselines_ready = calibration is not None and len(calibration.depth_baselines) == 9

    slots = expected_scene.get("slots") or []
    expected_slot_map = {slot.get("location"): slot for slot in slots if slot.get("location") is not None}
    slot_reports: list[dict[str, Any]] = []

    # Try to get a live frame for detection
    detection_report = None
    if calibration and calibration.is_complete() and baselines_ready:
        try:
            source = _get_camera()
            frame = source.capture()
            detection_report = detect_occupancy(frame, calibration)
        except Exception as e:
            logger.warning("Could not capture for verification: %s", e)

    occluded_slots = _occluding_slots(calibration, detection_report, expected_slot_map) if calibration and detection_report else {}

    for slot in slots:
        expected = slot.get("expected_labware")
        location = slot.get("location")
        occupied_expected = expected is not None
        expected_class = _expected_family(expected) if occupied_expected else None

        # Check detection results if available
        if detection_report and location:
            det_slot = detection_report.get_slot(location)
            if det_slot is not None:
                if occupied_expected:
                    if not det_slot.occupied:
                        status = "missing"
                        reason = "Expected labware, but depth did not detect an object in the calibrated ROI."
                    elif expected_class == "tip_box" and det_slot.observed_class and det_slot.observed_class != expected_class:
                        status = "mismatch"
                        reason = (
                            f"Observed class {det_slot.observed_class} does not match expected "
                            f"{expected_class}."
                        )
                    else:
                        matched = _match_expected_labware(expected, det_slot)
                        status = "present"
                        reason = matched["reason"]
                else:
                    occlusion = occluded_slots.get(location)
                    if occlusion is not None:
                        status = "occluded"
                        reason = (
                            f"View is occluded by detected tip_box in location {occlusion['by_location']} "
                            f"(horizontal overlap {occlusion['overlap_ratio']:.2f})."
                        )
                    elif det_slot.occupied:
                        status = "unexpected"
                        reason = (
                            f"Unexpected {det_slot.observed_class or 'object'} detected with height "
                            f"{(det_slot.object_height_mm or 0.0):.1f} mm."
                        )
                    else:
                        status = "empty_ok"
                        reason = "Detection result"
                slot_reports.append({
                    "location": location,
                    "status": status,
                    "confidence": det_slot.confidence,
                    "expected_labware": expected,
                    "expected_class": expected_class,
                    "observed_labware": (
                        {
                            **_match_expected_labware(expected, det_slot),
                            "fill_ratio": det_slot.fill_ratio,
                            "surface_flatness_mm": det_slot.surface_flatness_mm,
                        }
                        if occupied_expected and det_slot.occupied and expected is not None else
                        ({
                            "class": det_slot.observed_class,
                            "confidence": det_slot.class_confidence,
                            "fill_ratio": det_slot.fill_ratio,
                            "surface_flatness_mm": det_slot.surface_flatness_mm,
                        } if det_slot.observed_class else None)
                    ),
                    "depth_mm": det_slot.depth_median_mm,
                    "object_height_mm": det_slot.object_height_mm,
                    "reason": reason if det_slot.depth_delta_mm is not None else "Detection result",
                })
                continue

        # Fallback: no detection available
        slot_reports.append({
            "location": location,
            "status": "unknown" if occupied_expected else "empty_ok",
            "confidence": 0.0 if occupied_expected else 1.0,
            "expected_labware": expected,
            "expected_class": expected_class,
            "observed_labware": None,
            "reason": "Camera verification not yet available" if occupied_expected else "No expected labware",
        })

    summary = {
        "pass": all(s["status"] in ("present", "empty_ok") for s in slot_reports),
        "status": "verified" if detection_report else ("needs_baselines" if calibration and calibration.is_complete() and not baselines_ready else "needs_review"),
        "calibrated": calibration is not None and calibration.is_complete(),
        "baselines_ready": baselines_ready,
        "slot_count": len(slot_reports),
    }
    return {
        "summary": summary,
        "slots": slot_reports,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="PyBravo Vision Service", version="0.2.0")


@app.get("/status")
async def status() -> dict[str, Any]:
    live_camera = _camera_status()
    calibration_file = _load_calibration_file()
    if isinstance(calibration_file, dict):
        calibration_file = dict(calibration_file)
        saved_camera = calibration_file.get("camera_status")
        if saved_camera is not None:
            calibration_file["saved_camera_status"] = saved_camera
        calibration_file["camera_status"] = live_camera
    deck_cal = load_calibration()
    return {
        "camera": live_camera,
        "calibration": {
            "file": calibration_file,
            "rois_defined": len(deck_cal.rois) if deck_cal else 0,
            "rois_complete": deck_cal.is_complete() if deck_cal else False,
            "baselines_defined": len(deck_cal.depth_baselines) if deck_cal else 0,
        },
    }


@app.get("/calibration")
async def calibration() -> dict[str, Any]:
    deck_cal = load_calibration()
    return {
        "calibrated": deck_cal is not None and deck_cal.is_complete(),
        "calibration": deck_cal.to_dict() if deck_cal else None,
    }


@app.post("/calibration/run")
async def run_calibration(req: CalibrationRunRequest) -> dict[str, Any]:
    camera = _camera_status()
    raw: dict[str, Any] = {}
    if _CALIBRATION_PATH.exists():
        with _CALIBRATION_PATH.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    reference_image_path: str | None = None
    image_width = 0
    image_height = 0
    capture_message = "Calibration scaffold saved."
    if camera.get("camera_available"):
        try:
            source = _get_camera()
            frame = source.capture()
            saved_path = save_reference_image(frame.color)
            reference_image_path = str(saved_path)
            image_height, image_width = frame.color.shape[:2]
            capture_message = "Calibration scaffold saved and reference snapshot captured."
        except Exception as exc:
            logger.warning("Calibration snapshot capture failed: %s", exc)
            capture_message = f"Calibration scaffold saved, but reference snapshot failed: {exc}"

    raw.update({
        "machine_id": req.machine_id,
        "notes": req.notes,
        "calibration_mode": "fiducials_plus_geometry",
        "camera_mount": "fixed_side_view",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "camera_status": camera,
        "transform_status": "pending_hardware_capture",
        "reference_image_path": reference_image_path,
        "image_width": image_width,
        "image_height": image_height,
    })

    _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CALIBRATION_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False)

    return {
        "ok": True,
        "calibration": raw,
        "message": capture_message,
    }


@app.post("/calibration/capture_baselines")
async def api_capture_baselines(req: CaptureBaselineRequest) -> dict[str, Any]:
    """Capture depth baselines for the empty deck."""
    deck_cal = load_calibration()
    if deck_cal is None or not deck_cal.rois:
        return JSONResponse(
            {"ok": False, "message": "No ROI calibration found. Run the ROI calibration tool first."},
            status_code=400,
        )

    try:
        source = _get_camera()
        frame = source.capture()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "message": f"Camera error: {e}"},
            status_code=503,
        )

    if frame.depth is None:
        return JSONResponse(
            {"ok": False, "message": "No depth data available (static image mode?)"},
            status_code=400,
        )

    deck_cal, baseline_results = capture_depth_baselines_with_diagnostics(frame, deck_cal)
    save_calibration(deck_cal)

    accepted_results = [result for result in baseline_results if result.accepted]
    skipped_results = [result for result in baseline_results if not result.accepted]

    if not accepted_results:
        return JSONResponse(
            {
                "ok": False,
                "message": "No valid depth samples were found inside the calibrated ROIs. Depth and RGB may not be aligned yet.",
                "results": [result.to_dict() for result in baseline_results],
            },
            status_code=400,
        )

    return {
        "ok": True,
        "baselines": {
            f"location_{loc}": round(val, 1)
            for loc, val in sorted(deck_cal.depth_baselines.items())
        },
        "results": [result.to_dict() for result in baseline_results],
        "message": f"Captured baselines for {len(accepted_results)} positions; skipped {len(skipped_results)}.",
    }


@app.post("/verify")
async def verify(req: VerifyRequest) -> dict[str, Any]:
    report = _verify_scene(req.expected_scene)
    return {
        "ok": True,
        "report": report,
    }


@app.get("/preview")
async def preview() -> Response:
    """Return a JPEG snapshot from the camera."""
    try:
        jpeg_bytes = _preview_payload()["rgb_jpeg"]
        return Response(content=jpeg_bytes, media_type="image/jpeg")
    except Exception as e:
        return JSONResponse(
            {"ok": False, "message": f"Preview unavailable: {e}"},
            status_code=503,
        )


@app.get("/preview/depth")
async def preview_depth() -> Response:
    """Return a colorized depth JPEG snapshot."""
    try:
        depth_jpeg = _preview_payload()["depth_jpeg"]
        if depth_jpeg is None:
            return JSONResponse(
                {"ok": False, "message": "No depth data available"},
                status_code=503,
            )
        return Response(content=depth_jpeg, media_type="image/jpeg")
    except Exception as e:
        return JSONResponse(
            {"ok": False, "message": f"Depth preview unavailable: {e}"},
            status_code=503,
        )


@app.get("/stream")
async def stream_preview() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_stream("rgb"),
        media_type=f"multipart/x-mixed-replace; boundary={_STREAM_BOUNDARY}",
    )


@app.get("/stream/depth")
async def stream_preview_depth() -> StreamingResponse:
    return StreamingResponse(
        _mjpeg_stream("depth"),
        media_type=f"multipart/x-mixed-replace; boundary={_STREAM_BOUNDARY}",
    )


@app.get("/detect")
async def detect() -> dict[str, Any]:
    """Run occupancy detection on a live frame and return results."""
    deck_cal = load_calibration()
    if deck_cal is None or not deck_cal.rois:
        return JSONResponse(
            {"ok": False, "message": "No ROI calibration. Run calibrate_rois first."},
            status_code=400,
        )

    try:
        source = _get_camera()
        frame = source.capture()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "message": f"Camera error: {e}"},
            status_code=503,
        )

    report = detect_occupancy(frame, deck_cal)
    return {
        "ok": True,
        "report": report.to_dict(),
    }


def main(host: str = "127.0.0.1", port: int = 8101) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
