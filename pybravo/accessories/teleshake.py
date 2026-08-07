import logging
import time
from dataclasses import dataclass
from typing import Any

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

logger = logging.getLogger(__name__)


DIRECTION_VALUES = {
    "NWSE": 0x01,
    "NESW": 0x02,
    "EW": 0x10,
    "NS": 0x20,
    "NE,SW": 0x08,
    "NW,SE": 0x04,
}

COMMANDS = {
    "START": 0x30,
    "STOP": 0x31,
    "SET_SPEED": 0x33,
    "SET_DIRECTION": 0x34,
    "APPLY_SETTINGS": 0x3C,
}


@dataclass
class TeleshakeConfig:
    port: str = "COM4"
    baud_rate: int = 9600
    timeout_s: float = 0.6
    default_rpm: int = 100
    default_direction: str = "NWSE"


class TeleshakeError(Exception):
    """Raised when the Teleshake cannot complete a command."""


class Teleshake:
    """Serial driver for the Teleshake orbital shaking station.

    This driver uses the packet shape validated in the standalone
    ``TeleshakeSimpleControl`` helper. It intentionally does not apply any
    teachpoint offsets; deck geometry remains explicit profile data.
    """

    def __init__(self, config: TeleshakeConfig | None = None, **kwargs: Any) -> None:
        self._config = config or TeleshakeConfig(**kwargs)
        self._serial: Any = None
        self._is_running = False

    @property
    def is_open(self) -> bool:
        return bool(self._serial is not None and self._serial.is_open)

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def port(self) -> str:
        return self._config.port

    def open(self) -> None:
        if self.is_open:
            logger.debug("Teleshake already open on %s", self._config.port)
            return
        if serial is None:
            raise ImportError("pyserial is required for Teleshake support. Install it with: pip install pyserial")

        try:
            self._serial = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=False,
                dsrdtr=False,
                timeout=0.1,
            )
            self._serial.setRTS(True)
            self._serial.setDTR(True)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception as exc:
            raise TeleshakeError(f"Could not open Teleshake on {self._config.port}: {exc}") from exc
        logger.info("Teleshake opened on %s at %d baud", self._config.port, self._config.baud_rate)

    def close(self) -> None:
        if self._serial is not None:
            try:
                if self._is_running:
                    self.stop()
            except Exception:
                logger.debug("Ignoring Teleshake stop failure during close", exc_info=True)
            try:
                self._serial.close()
            except Exception:
                logger.debug("Ignoring Teleshake close failure", exc_info=True)
        self._serial = None
        self._is_running = False
        logger.info("Teleshake closed")

    def start(self, rpm: int | None = None, direction: str | None = None) -> None:
        self.open()
        rpm = int(rpm if rpm is not None else self._config.default_rpm)
        direction = direction or self._config.default_direction
        if not 100 <= rpm <= 2000:
            raise TeleshakeError("RPM must be from 100 to 2000")
        if direction not in DIRECTION_VALUES:
            raise TeleshakeError(f"Unknown Teleshake direction: {direction}")

        speed_bytes = self._speed_to_cycle_time_bytes(rpm)
        sequence = [
            ("speed", self._build_packet(COMMANDS["SET_SPEED"], list(speed_bytes))),
            ("direction", self._build_packet(COMMANDS["SET_DIRECTION"], [0x00, 0x00, DIRECTION_VALUES[direction]])),
            ("apply", self._build_packet(COMMANDS["APPLY_SETTINGS"], [0x00, 0xFF, 0xFF])),
            ("start", self._build_packet(COMMANDS["START"], [0x00, 0x00, 0x00])),
        ]
        for label, packet in sequence:
            self._write_packet(label, packet)
            time.sleep(0.08)
        self._is_running = True
        logger.info("Teleshake running on %s at %d RPM, %s", self._config.port, rpm, direction)

    def stop(self) -> None:
        self.open()
        self._write_packet("stop", self._build_packet(COMMANDS["STOP"], [0x00, 0x00, 0x00]))
        self._is_running = False
        logger.info("Teleshake stopped on %s", self._config.port)

    def _write_packet(self, label: str, packet: bytes) -> bytes:
        if not self._serial or not self._serial.is_open:
            raise TeleshakeError("Teleshake is not open")
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass
        try:
            self._serial.write(packet)
            self._serial.flush()
            reply = self._read_exact(6, timeout_s=self._config.timeout_s)
        except Exception as exc:
            raise TeleshakeError(f"Teleshake {label} command failed: {exc}") from exc
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Teleshake %s TX %s RX %s",
                label,
                self._format_hex(packet),
                self._format_hex(reply) if reply else "timeout",
            )
        return reply

    def _read_exact(self, size: int, timeout_s: float) -> bytes:
        if not self._serial or not self._serial.is_open:
            return b""
        deadline = time.time() + timeout_s
        response = bytearray()
        while len(response) < size and time.time() < deadline:
            chunk = self._serial.read(size - len(response))
            if chunk:
                response.extend(chunk)
            else:
                time.sleep(0.01)
        return bytes(response)

    @staticmethod
    def _build_packet(command: int, data: list[int]) -> bytes:
        payload = [0x61, command & 0xFF] + [byte & 0xFF for byte in data[:3]]
        while len(payload) < 5:
            payload.append(0)
        payload.append(sum(payload[:5]) % 256)
        return bytes(payload)

    @staticmethod
    def _speed_to_cycle_time_bytes(rpm: int) -> tuple[int, int, int]:
        cycle_time_us = int(60_000_000 / rpm)
        return (
            (cycle_time_us >> 16) & 0xFF,
            (cycle_time_us >> 8) & 0xFF,
            cycle_time_us & 0xFF,
        )

    @staticmethod
    def _format_hex(data: bytes) -> str:
        return " ".join(f"{byte:02X}" for byte in data)


def main() -> None:
    """Launch the standalone control panel.

    The GUI lives in a separate module so importing this driver never requires
    tkinter, which is absent from some Python builds. Kept here so the familiar
    ``python -m pybravo.accessories.teleshake`` still opens the panel.
    """
    try:
        from pybravo.accessories.teleshake_gui import main as gui_main
    except ImportError as exc:
        raise SystemExit(
            "The Teleshake control panel needs tkinter, which this Python was "
            f"built without ({exc}).\n"
            "Install a Python with Tk support (on macOS: brew install python-tk), "
            "or drive the shaker through the API instead — the serial driver in "
            "this module does not need tkinter."
        ) from exc

    gui_main()


if __name__ == "__main__":
    main()
