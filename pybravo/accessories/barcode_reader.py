"""Microscan MS3 barcode reader driver.

Communicates with a Microscan MS3 fixed-mount barcode scanner over a serial
(COM) port.  The reader is NOT part of the Bravo controller — it is an
independent device that sits at a deck location.  Control software historically talked
to it as a separate COM device; pybravo replaces that with this driver.

Typical setup:
    reader = BarcodeReader(port="COM5")
    reader.open()
    barcode = reader.trigger_and_read()   # "PLATE-00123-A"
    reader.close()

Serial protocol (MS3 defaults):
    Trigger command:  ESC (0x1B) or configurable string
    Response:         barcode data + terminator (CR/LF)
    No-read:          configurable no-read string (default "NR")
    Baud:             9600 (factory default, often reconfigured to 115200)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Try to import pyserial; allow graceful fallback for environments without it
try:
    import serial
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False


@dataclass
class BarcodeReaderConfig:
    """Configuration for a serial barcode reader.

    Device-specific parameters (baud, parity, trigger protocol, etc.) are
    determined by ``device_type``.  Only the COM port is user-configurable.
    """
    port: str = "COM5"
    device_type: str = "ms3"   # selects protocol preset; see DEVICE_PRESETS

    # Resolved from device_type — not set by the user
    baud_rate: int = 9600
    timeout_s: float = 5.0
    trigger_command: bytes = b"\x1d"
    terminator: bytes = b"\r\n"
    no_read_response: str = "NR"
    retries: int = 2
    retry_delay_s: float = 0.5
    data_bits: int = 7
    stop_bits: float = 1.0
    parity: str = "E"
    configure_on_open: bool = True


# -- Device presets ---------------------------------------------------------
# Each preset captures the serial parameters and trigger protocol for a
# supported scanner model so the user only has to pick the type and COM port.

DEVICE_PRESETS: dict[str, dict] = {
    "ms3": {
        # Microscan MS-3 Laser Scanner (84-000003-02 user manual)
        # Factory serial: 9600-7-E-1.
        # We set the trigger char to '1' (printable) because the default GS
        # control character doesn't work reliably as a delimited trigger.
        # Trigger is sent as b"<1>" in Serial Data mode (K200,4).
        # No-read response is "EROR" (scanner-configured, not factory "NR").
        "label": "Microscan MS-3",
        "baud_rate": 9600,
        "data_bits": 7,
        "parity": "E",
        "stop_bits": 1.0,
        "trigger_char": b"1",             # sent as <1> in Serial Data mode
        "trigger_command": b"<1>",        # delimited trigger for serial data mode
        "terminator": b"\r\n",
        "no_read_response": "EROR",
        "timeout_s": 5.0,
        "retries": 2,
        "retry_delay_s": 0.5,
        "configure_on_open": True,
    },
}


def config_for_device(device_type: str, port: str = "COM5") -> BarcodeReaderConfig:
    """Build a ``BarcodeReaderConfig`` from a device preset and COM port."""
    preset = DEVICE_PRESETS.get(device_type)
    if preset is None:
        raise ValueError(f"Unknown barcode reader device type: {device_type!r}")
    return BarcodeReaderConfig(
        port=port,
        device_type=device_type,
        baud_rate=preset["baud_rate"],
        data_bits=preset["data_bits"],
        parity=preset["parity"],
        stop_bits=preset["stop_bits"],
        trigger_command=preset["trigger_command"],
        terminator=preset["terminator"],
        no_read_response=preset["no_read_response"],
        timeout_s=preset["timeout_s"],
        retries=preset["retries"],
        retry_delay_s=preset["retry_delay_s"],
        configure_on_open=preset.get("configure_on_open", False),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "baud_rate": self.baud_rate,
            "timeout_s": self.timeout_s,
            "no_read_response": self.no_read_response,
            "retries": self.retries,
        }


class BarcodeReadError(Exception):
    """Raised when a barcode cannot be read."""
    pass


class BarcodeReader:
    """Driver for a serial barcode reader (Microscan MS3 or compatible).

    The reader is opened/closed explicitly.  While open, call
    ``trigger_and_read()`` to trigger a scan and return the barcode string.
    """

    def __init__(self, config: BarcodeReaderConfig | None = None, **kwargs: Any) -> None:
        if config is not None:
            self._config = config
        else:
            self._config = BarcodeReaderConfig(**kwargs)
        self._serial: Any = None
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def port(self) -> str:
        return self._config.port

    def open(self) -> None:
        """Open the serial connection to the barcode reader."""
        if self._is_open:
            logger.debug("Barcode reader already open on %s", self._config.port)
            return

        if not _HAS_SERIAL:
            raise ImportError(
                "pyserial is required for barcode reader support. "
                "Install it with: pip install pyserial"
            )

        stop_bits_map = {1.0: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2.0: serial.STOPBITS_TWO}
        parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}

        try:
            self._serial = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baud_rate,
                bytesize=self._config.data_bits,
                stopbits=stop_bits_map.get(self._config.stop_bits, serial.STOPBITS_ONE),
                parity=parity_map.get(self._config.parity, serial.PARITY_NONE),
                timeout=self._config.timeout_s,
            )
            self._is_open = True
            # Flush any stale data in the buffer
            self._serial.reset_input_buffer()
            logger.info("Barcode reader opened on %s at %d baud", self._config.port, self._config.baud_rate)
            if self._config.configure_on_open:
                self._probe_and_configure()
        except serial.SerialException as exc:
            raise BarcodeReadError(f"Could not open barcode reader on {self._config.port}: {exc}") from exc

    def _probe_and_configure(self) -> None:
        """Probe the MS-3's serial settings and configure Serial Data trigger mode.

        The MS-3 factory default is 9600-7-E-1, but it's commonly reconfigured
        to 9600-8-N-1.  We try the current config first, then the alternate.
        Once we can talk to the scanner, we query its settings with <K100?>
        and switch to Serial Data trigger mode.
        """
        if self._serial is None:
            return

        # Try current serial settings first, then the alternate
        configs_to_try = [
            None,  # current settings — try first
            {"bytesize": 8, "parity": "N"} if self._config.data_bits == 7 else {"bytesize": 7, "parity": "E"},
        ]

        parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}

        for alt in configs_to_try:
            if alt is not None:
                bs = alt["bytesize"]
                p = alt["parity"]
                logger.info("Retrying MS-3 probe with %d-%s-1...", bs, p)
                self._serial.bytesize = bs
                self._serial.parity = parity_map.get(p, serial.PARITY_NONE)

            try:
                self._serial.reset_input_buffer()
                # Query host port settings — works in any trigger mode
                self._serial.write(b"<K100?>")
                self._serial.flush()
                time.sleep(0.3)
                raw = self._serial.read(self._serial.in_waiting or 128)
                if raw:
                    response = raw.decode("ascii", errors="replace").strip()
                    logger.info("MS-3 probe response (%d-%s-1): %s",
                                self._serial.bytesize,
                                {serial.PARITY_NONE: "N", serial.PARITY_EVEN: "E", serial.PARITY_ODD: "O"}.get(self._serial.parity, "?"),
                                response)
                    # Got a response — these serial settings work
                    self._configure_serial_trigger_mode()
                    return
            except Exception as exc:
                logger.debug("MS-3 probe attempt failed: %s", exc)

        logger.warning("MS-3 did not respond to probe on %s. Check COM port, cabling, and power.",
                        self._config.port)

    def _configure_serial_trigger_mode(self) -> None:
        """Configure the MS-3 for serial-triggered reads.

        Sets the trigger character to a printable char (from the preset),
        enables Serial Data trigger mode (K200,4), and activates without
        saving to power-on so a power cycle restores factory settings.
        """
        if self._serial is None:
            return
        trigger_char = self._config.trigger_command.strip(b"<>")  # e.g. b"1"
        try:
            # Set trigger char (e.g. <K201,1>)
            self._serial.write(b"<K201," + trigger_char + b">")
            self._serial.flush()
            time.sleep(0.1)
            # Set Serial Data trigger mode
            self._serial.write(b"<K200,4>")
            self._serial.flush()
            time.sleep(0.1)
            # Activate in current memory (no power-on save)
            self._serial.write(b"<A>")
            self._serial.flush()
            time.sleep(0.1)
            self._serial.reset_input_buffer()
            logger.info("MS-3 configured: trigger char=%r, Serial Data mode", trigger_char)
        except Exception as exc:
            logger.warning("Could not configure MS-3 trigger mode: %s", exc)

    def close(self) -> None:
        """Close the serial connection."""
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self._is_open = False
        logger.info("Barcode reader closed")

    def trigger_and_read(self) -> str:
        """Trigger the scanner and return the barcode string.

        Retries up to ``config.retries`` times on no-read responses.

        Raises:
            BarcodeReadError: If the reader is not open, times out, or
                exhausts retries without a successful read.
        """
        if not self._is_open or self._serial is None:
            raise BarcodeReadError("Barcode reader is not open")

        last_response = ""
        for attempt in range(1 + self._config.retries):
            if attempt > 0:
                logger.debug("Barcode read retry %d/%d", attempt, self._config.retries)
                time.sleep(self._config.retry_delay_s)

            # Clear input buffer before triggering
            self._serial.reset_input_buffer()

            # Send trigger command (e.g. b"<1>" for MS-3 Serial Data mode)
            try:
                self._serial.write(self._config.trigger_command)
                self._serial.flush()
            except Exception as exc:
                raise BarcodeReadError(f"Failed to send trigger command: {exc}") from exc

            # Read response
            try:
                raw = self._serial.read_until(self._config.terminator)
            except Exception as exc:
                raise BarcodeReadError(f"Failed to read barcode response: {exc}") from exc

            if not raw:
                logger.warning("Barcode reader timeout (no response within %.1fs)", self._config.timeout_s)
                last_response = ""
                continue

            # Decode and strip terminator
            response = raw.decode("ascii", errors="replace").strip()
            last_response = response

            if not response:
                continue

            # Check for no-read response
            if response == self._config.no_read_response:
                logger.debug("Barcode reader returned no-read response: %s", response)
                continue

            logger.info("Barcode read: %s", response)
            return response

        raise BarcodeReadError(
            f"Barcode reader failed after {1 + self._config.retries} attempts. "
            f"Last response: {last_response!r}"
        )

    def __enter__(self) -> BarcodeReader:
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        status = "open" if self._is_open else "closed"
        return f"BarcodeReader(port={self._config.port!r}, status={status})"
