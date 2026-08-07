"""Camera capture module for the Orbbec Femto Bolt.

Provides on-demand color + depth frame capture via a persistent pipeline,
with a static-image fallback for development/testing without hardware.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - depends on local environment
    cv2 = None

logger = logging.getLogger(__name__)

MIN_DEPTH_MM = 20
MAX_DEPTH_MM = 10000


@dataclass
class CameraFrame:
    """A captured frame pair from the depth camera."""

    color: np.ndarray  # BGR uint8, HxWx3
    depth: Optional[np.ndarray]  # uint16 mm, HxW (None if unavailable)
    timestamp: float  # time.time() when captured

    @property
    def color_rgb(self) -> np.ndarray:
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for color conversion")
        return cv2.cvtColor(self.color, cv2.COLOR_BGR2RGB)

    def depth_colorized(self) -> Optional[np.ndarray]:
        """Return a JET-colorized BGR visualization of the depth map."""
        if self.depth is None:
            return None
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for depth preview")
        depth = np.where(
            (self.depth > MIN_DEPTH_MM) & (self.depth < MAX_DEPTH_MM), self.depth, 0
        )
        norm = cv2.normalize(depth, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        return cv2.applyColorMap(norm, cv2.COLORMAP_JET)

    def color_jpeg(self, quality: int = 85) -> bytes:
        """Encode the color frame as JPEG bytes."""
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for JPEG preview encoding")
        ok, buf = cv2.imencode(".jpg", self.color, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buf.tobytes()


class StaticImageSource:
    """Fallback source that returns a static image (for dev/testing)."""

    def __init__(self, image_path: str | Path) -> None:
        self._path = Path(image_path)
        self._image: Optional[np.ndarray] = None

    def open(self) -> None:
        if cv2 is None:
            raise RuntimeError(
                "OpenCV (cv2) is not installed. Install opencv-python to use static image preview."
            )
        if not self._path.exists():
            raise FileNotFoundError(f"Reference image not found: {self._path}")
        self._image = cv2.imread(str(self._path))
        if self._image is None:
            raise RuntimeError(f"Failed to load image: {self._path}")
        logger.info("Static image source opened: %s (%dx%d)", self._path,
                     self._image.shape[1], self._image.shape[0])

    def close(self) -> None:
        self._image = None

    def capture(self) -> CameraFrame:
        if self._image is None:
            raise RuntimeError("Static image source not opened")
        return CameraFrame(
            color=self._image.copy(),
            depth=None,
            timestamp=time.time(),
        )

    @property
    def is_open(self) -> bool:
        return self._image is not None


class FemtoBoltCamera:
    """Orbbec Femto Bolt camera with on-demand capture.

    Maintains a persistent pipeline and caches the latest frames from
    the callback. capture() returns the most recent frame pair.
    """

    def __init__(self) -> None:
        self._pipeline = None
        self._align_filter = None
        self._lock = threading.Lock()
        self._latest_color: Optional[np.ndarray] = None
        self._latest_depth: Optional[np.ndarray] = None
        self._latest_ts: float = 0.0
        self._device_info: dict = {}

    def open(self) -> None:
        try:
            from pyorbbecsdk import (
                AlignFilter,
                Config,
                OBAlignMode,
                OBFrameAggregateOutputMode,
                OBSensorType,
                OBStreamType,
                Pipeline,
            )
        except ImportError as exc:
            raise RuntimeError(
                f"pyorbbecsdk import failed under Python {sys.version.split()[0]} "
                f"({sys.executable}): {exc}"
            ) from exc

        pipeline = Pipeline()
        device = pipeline.get_device()
        info = device.get_device_info()
        self._device_info = {
            "name": info.get_name(),
            "serial": info.get_serial_number(),
            "firmware": info.get_firmware_version(),
        }
        logger.info("Femto Bolt: %s (serial=%s, fw=%s)",
                     self._device_info["name"],
                     self._device_info["serial"],
                     self._device_info["firmware"])

        config = Config()
        try:
            pipeline.enable_frame_sync()
        except Exception as exc:
            logger.warning("Femto Bolt frame sync unavailable: %s", exc)
        try:
            config.set_frame_aggregate_output_mode(OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE)
        except Exception as exc:
            logger.warning("Femto Bolt full-frame aggregate mode unavailable: %s", exc)

        configured = False
        try:
            color_profiles = pipeline.get_stream_profile_list(OBSensorType.COLOR_SENSOR)
            if color_profiles is not None:
                for i in range(len(color_profiles)):
                    color_profile = color_profiles[i]
                    try:
                        d2c_profiles = pipeline.get_d2c_depth_profile_list(color_profile, OBAlignMode.HW_MODE)
                    except Exception:
                        d2c_profiles = []
                    if len(d2c_profiles) == 0:
                        continue
                    config.enable_stream(d2c_profiles[0])
                    config.enable_stream(color_profile)
                    config.set_align_mode(OBAlignMode.HW_MODE)
                    configured = True
                    logger.info("Femto Bolt using hardware depth-to-color alignment")
                    break
        except Exception as exc:
            logger.warning("Femto Bolt D2C profile setup failed: %s", exc)

        if not configured:
            sensor_list = device.get_sensor_list()
            for i in range(len(sensor_list)):
                sensor_type = sensor_list[i].get_type()
                if sensor_type in (OBSensorType.COLOR_SENSOR, OBSensorType.DEPTH_SENSOR):
                    try:
                        config.enable_stream(sensor_type)
                    except Exception:
                        pass

        pipeline.start(config, self._frame_callback)
        self._pipeline = pipeline
        try:
            self._align_filter = AlignFilter(align_to_stream=OBStreamType.COLOR_STREAM)
            logger.info("Femto Bolt software align filter enabled (depth to color)")
        except Exception as exc:
            self._align_filter = None
            logger.warning("Femto Bolt software align filter unavailable: %s", exc)
        logger.info("Femto Bolt pipeline started")

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
            self._align_filter = None
            logger.info("Femto Bolt pipeline stopped")

    @property
    def is_open(self) -> bool:
        return self._pipeline is not None

    @property
    def device_info(self) -> dict:
        return dict(self._device_info)

    def _frame_callback(self, frames) -> None:
        if frames is None:
            return
        if self._align_filter is not None:
            try:
                aligned = self._align_filter.process(frames)
                if aligned is not None:
                    frames = aligned.as_frame_set()
            except Exception as exc:
                logger.debug("Femto Bolt software alignment skipped: %s", exc)
        with self._lock:
            color_frame = frames.get_color_frame()
            if color_frame is not None:
                self._latest_color = self._decode_color(color_frame)

            depth_frame = frames.get_depth_frame()
            if depth_frame is not None:
                self._latest_depth = self._decode_depth(depth_frame)

            self._latest_ts = time.time()

    def _decode_color(self, frame) -> Optional[np.ndarray]:
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is required for live color decoding")
        from pyorbbecsdk import OBFormat
        width = frame.get_width()
        height = frame.get_height()
        fmt = frame.get_format()
        data = np.asanyarray(frame.get_data())

        if fmt == OBFormat.RGB:
            image = np.resize(data, (height, width, 3))
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif fmt == OBFormat.BGR:
            return np.resize(data, (height, width, 3))
        elif fmt == OBFormat.YUYV:
            image = np.resize(data, (height, width, 2))
            return cv2.cvtColor(image, cv2.COLOR_YUV2BGR_YUYV)
        elif fmt == OBFormat.MJPG:
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        elif fmt == OBFormat.UYVY:
            image = np.resize(data, (height, width, 2))
            return cv2.cvtColor(image, cv2.COLOR_YUV2BGR_UYVY)
        elif fmt == OBFormat.NV12:
            yuv = data.reshape((height * 3 // 2, width))
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
        elif fmt == OBFormat.NV21:
            yuv = data.reshape((height * 3 // 2, width))
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV21)
        else:
            logger.warning("Unsupported color format: %s", fmt)
            return None

    def _decode_depth(self, frame) -> Optional[np.ndarray]:
        try:
            width = frame.get_width()
            height = frame.get_height()
            scale = frame.get_depth_scale()
            raw = np.frombuffer(frame.get_data(), dtype=np.uint16).reshape((height, width))
            return (raw.astype(np.float32) * scale).astype(np.uint16)
        except Exception as e:
            logger.warning("Depth decode error: %s", e)
            return None

    def capture(self, timeout_s: float = 2.0) -> CameraFrame:
        """Return the most recent frame pair.

        Raises RuntimeError if no frame has been received within timeout.
        """
        if not self.is_open:
            raise RuntimeError("Camera is not open")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._lock:
                if self._latest_color is not None:
                    return CameraFrame(
                        color=self._latest_color.copy(),
                        depth=self._latest_depth.copy() if self._latest_depth is not None else None,
                        timestamp=self._latest_ts,
                    )
            time.sleep(0.05)

        raise RuntimeError("Timed out waiting for camera frame")


def create_camera_source(
    static_image: str | Path | None = None,
) -> FemtoBoltCamera | StaticImageSource:
    """Factory: returns a FemtoBoltCamera if possible, else StaticImageSource."""
    if static_image is not None:
        source = StaticImageSource(static_image)
        source.open()
        return source

    try:
        cam = FemtoBoltCamera()
        cam.open()
        return cam
    except Exception as e:
        logger.warning("Could not open Femto Bolt (%s), no fallback image provided", e)
        raise
