#!/usr/bin/env python3
"""
Live viewer for the Orbbec Femto Bolt camera using the Orbbec SDK.

Shows color + depth streams side-by-side with optional IR.
Requires Windows or Linux (macOS lacks the Microsoft depth engine).

Controls:
  q / ESC  - Quit
  s        - Save current frame as PNG
  r        - Toggle resolution info overlay
  f        - Toggle FPS overlay
  i        - Toggle IR stream display
"""

import sys
import time
import threading

import cv2
import numpy as np
from pyorbbecsdk import (
    Pipeline, Config, OBSensorType, OBFormat, FormatConvertFilter, OBConvertFormat,
)

MIN_DEPTH = 20    # mm
MAX_DEPTH = 10000  # mm
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720


def frame_to_bgr_image(frame):
    """Convert an SDK VideoFrame to a BGR numpy array."""
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
        # Try the SDK's built-in format converter
        try:
            convert_filter = FormatConvertFilter()
            if fmt == OBFormat.I420:
                convert_filter.set_format_convert_format(OBConvertFormat.I420_TO_RGB888)
            else:
                print(f"Unsupported color format: {fmt}")
                return None
            rgb_frame = convert_filter.process(frame)
            if rgb_frame is None:
                return None
            rgb_data = np.asanyarray(rgb_frame.get_data())
            image = np.resize(rgb_data, (height, width, 3))
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        except Exception:
            print(f"Unsupported color format: {fmt}")
            return None


def process_depth(frame):
    """Convert a depth frame to a colorized BGR image."""
    if frame is None:
        return None
    try:
        width = frame.get_width()
        height = frame.get_height()
        scale = frame.get_depth_scale()

        depth_data = np.frombuffer(frame.get_data(), dtype=np.uint16).reshape((height, width))
        depth_data = (depth_data.astype(np.float32) * scale).astype(np.uint16)
        depth_data = np.where(
            (depth_data > MIN_DEPTH) & (depth_data < MAX_DEPTH), depth_data, 0
        )
        depth_image = cv2.normalize(depth_data, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        return cv2.applyColorMap(depth_image, cv2.COLORMAP_JET)
    except (ValueError, Exception) as e:
        print(f"Depth processing error: {e}")
        return None


def process_ir(frame):
    """Convert an IR frame to a grayscale BGR image."""
    if frame is None:
        return None
    try:
        frame = frame.as_video_frame()
        width = frame.get_width()
        height = frame.get_height()
        fmt = frame.get_format()
        data = np.asanyarray(frame.get_data())

        if fmt == OBFormat.Y8:
            ir_data = data.reshape((height, width))
        elif fmt == OBFormat.MJPG:
            ir_data = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if ir_data is None:
                return None
        elif fmt == OBFormat.Y16:
            ir_data = np.frombuffer(data, dtype=np.uint16).reshape((height, width))
            ir_data = cv2.normalize(ir_data, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        else:
            ir_data = np.frombuffer(data, dtype=np.uint16).reshape((height, width))
            ir_data = cv2.normalize(ir_data, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

        return cv2.cvtColor(ir_data.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    except Exception as e:
        print(f"IR processing error: {e}")
        return None


class FemtoViewer:
    def __init__(self):
        self.frame_lock = threading.Lock()
        self.color_image = None
        self.depth_image = None
        self.ir_image = None
        self.show_ir = False
        self.show_info = True
        self.show_fps = True
        self.frame_count = 0
        self.fps_start = time.time()
        self.fps_display = 0.0

    def frame_callback(self, frames):
        if frames is None:
            return
        with self.frame_lock:
            color_frame = frames.get_color_frame()
            if color_frame is not None:
                self.color_image = frame_to_bgr_image(color_frame)

            depth_frame = frames.get_depth_frame()
            if depth_frame is not None:
                self.depth_image = process_depth(depth_frame)

            ir_frame = frames.get_ir_frame()
            if ir_frame is not None:
                self.ir_image = process_ir(ir_frame)

    def run(self):
        print("=" * 50)
        print("  Orbbec Femto Bolt - Live Viewer (SDK)")
        print("=" * 50)
        print()

        # Initialize pipeline
        print("Creating pipeline...", flush=True)
        try:
            pipeline = Pipeline()
        except Exception as e:
            print(f"ERROR creating pipeline: {e}")
            print("Is the Femto Bolt connected via USB 3.0?")
            input("Press Enter to exit...")
            sys.exit(1)

        print("Getting device...", flush=True)
        try:
            device = pipeline.get_device()
        except Exception as e:
            print(f"ERROR getting device: {e}")
            print("No Orbbec device found. Check USB connection.")
            input("Press Enter to exit...")
            sys.exit(1)

        device_info = device.get_device_info()
        print(f"Device: {device_info.get_name()}")
        print(f"Serial: {device_info.get_serial_number()}")
        print(f"Firmware: {device_info.get_firmware_version()}")
        print()

        # Configure streams
        config = Config()
        sensor_list = device.get_sensor_list()
        has_ir = False

        for i in range(len(sensor_list)):
            sensor_type = sensor_list[i].get_type()
            if sensor_type == OBSensorType.COLOR_SENSOR:
                try:
                    config.enable_stream(OBSensorType.COLOR_SENSOR)
                    print("Enabled: Color stream")
                except Exception as e:
                    print(f"Could not enable color: {e}")
            elif sensor_type == OBSensorType.DEPTH_SENSOR:
                try:
                    config.enable_stream(OBSensorType.DEPTH_SENSOR)
                    print("Enabled: Depth stream")
                except Exception as e:
                    print(f"Could not enable depth: {e}")
            elif sensor_type == OBSensorType.IR_SENSOR:
                try:
                    config.enable_stream(OBSensorType.IR_SENSOR)
                    has_ir = True
                    print("Enabled: IR stream")
                except Exception as e:
                    print(f"Could not enable IR: {e}")

        # Start pipeline with callback
        pipeline.start(config, self.frame_callback)
        print()
        print("Streaming... Press 'q' or ESC to quit.")
        print("  s = save frame | r = toggle resolution | f = toggle FPS | i = toggle IR")
        print()

        window_name = "Femto Bolt Viewer"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, WINDOW_WIDTH, WINDOW_HEIGHT)

        try:
            while True:
                with self.frame_lock:
                    color = self.color_image.copy() if self.color_image is not None else None
                    depth = self.depth_image.copy() if self.depth_image is not None else None
                    ir = self.ir_image.copy() if self.ir_image is not None else None

                if color is None and depth is None:
                    key = cv2.waitKey(30) & 0xFF
                    if key in (ord('q'), 27):
                        break
                    continue

                # FPS tracking
                self.frame_count += 1
                elapsed = time.time() - self.fps_start
                if elapsed >= 1.0:
                    self.fps_display = self.frame_count / elapsed
                    self.frame_count = 0
                    self.fps_start = time.time()

                # Build display panels
                panels = []
                labels = []

                if color is not None:
                    panels.append(color)
                    labels.append("Color")

                if depth is not None:
                    panels.append(depth)
                    labels.append("Depth")

                if self.show_ir and ir is not None:
                    panels.append(ir)
                    labels.append("IR")

                if not panels:
                    key = cv2.waitKey(30) & 0xFF
                    if key in (ord('q'), 27):
                        break
                    continue

                # Resize all panels to same height and combine
                target_h = WINDOW_HEIGHT
                target_w = WINDOW_WIDTH // len(panels)
                resized = []
                for panel, label in zip(panels, labels):
                    r = cv2.resize(panel, (target_w, target_h))
                    # Add stream label
                    cv2.putText(r, label, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                    resized.append(r)

                display = np.hstack(resized)

                # Overlays
                if self.show_info and color is not None:
                    with self.frame_lock:
                        c = self.color_image
                        if c is not None:
                            info = f"{c.shape[1]}x{c.shape[0]}"
                            cv2.putText(display, info, (10, display.shape[0] - 40),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                if self.show_fps:
                    fps_text = f"FPS: {self.fps_display:.1f}"
                    cv2.putText(display, fps_text, (10, display.shape[0] - 15),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                cv2.imshow(window_name, display)

                key = cv2.waitKey(1) & 0xFF
                if key in (ord('q'), 27):
                    break
                elif key == ord('s'):
                    filename = f"capture_{int(time.time())}.png"
                    cv2.imwrite(filename, display)
                    print(f"Saved: {filename}")
                elif key == ord('r'):
                    self.show_info = not self.show_info
                elif key == ord('f'):
                    self.show_fps = not self.show_fps
                elif key == ord('i') and has_ir:
                    self.show_ir = not self.show_ir
                    print(f"IR display: {'ON' if self.show_ir else 'OFF'}")

        except KeyboardInterrupt:
            pass

        pipeline.stop()
        cv2.destroyAllWindows()
        print("Viewer closed.")


if __name__ == "__main__":
    try:
        viewer = FemtoViewer()
        viewer.run()
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)
