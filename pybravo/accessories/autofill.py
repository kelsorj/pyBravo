"""Autofill station: the accessory-bus pump module and weigh pad.

These modules hang off the instrument's accessory bus, not a PC COM port. On
Darwin-generation instruments each command is a 9-byte serial payload that the
controller forwards (see ``DarwinController.send_serial``); the first byte of
the reply echoes the command and the second is a status byte (0 = OK).

Commands::

    AB 00                          detect accessory modules (sent once at init)
    AC mm pp dd ss ss              run pump pp on module mm, direction dd
                                   (1 forward, 0 reverse), speed ss ss as a
                                   little-endian uint16 in 0.01 % (0-10000)
    AE 00                          stop all pumps
    AF mm                          pump module status
    B3 mm                          read weigh pad mm; reply bytes 2-3 are the
                                   raw A/D value, little-endian uint16

Bytes not listed are zero. Reply bytes past the ones listed are left over from
earlier traffic and carry no meaning.

**A pump never stops on its own.** ``AC`` has no duration: the pump runs until
``AE`` arrives. Every run therefore goes through :meth:`AutofillStation.run_pumps`,
which arms a watchdog that sends the stop, and :meth:`close` stops anything
still running. Tare and range are host-side calibration only; the module just
reports raw A/D counts.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

CMD_DETECT = 0xAB
CMD_RUN_PUMP = 0xAC
CMD_STOP_PUMPS = 0xAE
CMD_PUMP_STATUS = 0xAF
CMD_READ_WEIGH_PAD = 0xB3

DIRECTIONS = {"forward": 1, "reverse": 0}
MAX_RUN_S = 600.0
KEEPALIVE_INTERVAL_S = 0.3  # status polls while pumps run, as the instrument software does

SerialSender = Callable[[bytes], bytes]


class AutofillError(Exception):
    """Raised when the autofill station cannot complete a command."""


@dataclass
class PumpSettings:
    module: int = 1
    pump: int = 1
    direction: str = "forward"
    speed_pct: float = 50.0


@dataclass
class AutofillConfig:
    fill: PumpSettings
    empty: PumpSettings
    weigh_module: int = 1
    tare: int = 0
    range: int = 0
    max_run_s: float = MAX_RUN_S


def build_run_pump(module: int, pump: int, direction: str, speed_pct: float) -> bytes:
    if direction not in DIRECTIONS:
        raise AutofillError(f"Unknown pump direction {direction!r}; use 'forward' or 'reverse'")
    if not 0.0 <= speed_pct <= 100.0:
        raise AutofillError("Pump speed must be from 0 to 100 %")
    speed = round(speed_pct * 100)
    return bytes([CMD_RUN_PUMP, module & 0xFF, pump & 0xFF, DIRECTIONS[direction],
                  speed & 0xFF, (speed >> 8) & 0xFF, 0, 0, 0])


def build_stop_pumps() -> bytes:
    return bytes([CMD_STOP_PUMPS, 0, 0, 0, 0, 0, 0, 0, 0])


def build_pump_status(module: int) -> bytes:
    return bytes([CMD_PUMP_STATUS, module & 0xFF, 0, 0, 0, 0, 0, 0, 0])


def build_read_weigh_pad(module: int) -> bytes:
    return bytes([CMD_READ_WEIGH_PAD, module & 0xFF, 0, 0, 0, 0, 0, 0, 0])


def level_percent(reading: int, tare: int, range_: int) -> float | None:
    """Fill level the way the calibration is defined: tare = 0 %, range = 100 %."""
    if range_ == tare:
        return None
    return (reading - tare) * 100.0 / (range_ - tare)


class AutofillStation:
    """Driver for one autofill station (fill pump, empty pump, weigh pad).

    ``sender_provider`` returns a callable that sends one 9-byte payload and
    returns the reply. It is resolved on every command so a reconnect is picked
    up without rebuilding the driver.
    """

    def __init__(self, config: AutofillConfig, sender_provider: Callable[[], SerialSender]) -> None:
        self._config = config
        self._sender_provider = sender_provider
        self._lock = threading.RLock()
        self._running = False
        self._run_token = 0
        self._stop_at: float | None = None

    @property
    def config(self) -> AutofillConfig:
        return self._config

    @property
    def is_open(self) -> bool:
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def seconds_remaining(self) -> float | None:
        if not self._running or self._stop_at is None:
            return None
        return max(0.0, self._stop_at - time.monotonic())

    # -- Weigh pad --

    def read_weight(self) -> int:
        reply = self._send(build_read_weigh_pad(self._config.weigh_module), "read weigh pad")
        return reply[2] | (reply[3] << 8)

    def read_level(self) -> dict[str, float | int | None]:
        reading = self.read_weight()
        return {
            "reading": reading,
            "tare": self._config.tare,
            "range": self._config.range,
            "level_pct": level_percent(reading, self._config.tare, self._config.range),
        }

    # -- Pumps --

    def run_pumps(
        self,
        duration_s: float,
        *,
        fill: bool = True,
        empty: bool = True,
        fill_speed_pct: float | None = None,
        empty_speed_pct: float | None = None,
    ) -> None:
        """Run the fill and/or empty pump for ``duration_s``, then stop them.

        Returns once the pumps are started; a watchdog thread sends the stop.
        """
        if not fill and not empty:
            raise AutofillError("Select at least one pump to run")
        if not 0.0 < duration_s <= self._config.max_run_s:
            raise AutofillError(f"Pump run time must be from 0 to {self._config.max_run_s:g} s")

        commands = []
        if fill:
            p = self._config.fill
            commands.append(("fill", build_run_pump(
                p.module, p.pump, p.direction, p.speed_pct if fill_speed_pct is None else fill_speed_pct)))
        if empty:
            p = self._config.empty
            commands.append(("empty", build_run_pump(
                p.module, p.pump, p.direction, p.speed_pct if empty_speed_pct is None else empty_speed_pct)))

        with self._lock:
            if self._running:
                self.stop_pumps()
            self._run_token += 1
            token = self._run_token
            self._running = True
            self._stop_at = time.monotonic() + duration_s
            try:
                for label, payload in commands:
                    self._send(payload, f"start {label} pump")
            except Exception:
                logger.error("Autofill pump start failed; stopping all pumps")
                self._stop_quietly()
                raise
            watchdog = threading.Thread(
                target=self._watchdog, args=(token, duration_s), name="autofill-watchdog", daemon=True
            )
            watchdog.start()
        logger.info(
            "Autofill pumps running for %.1f s (fill=%s, empty=%s)", duration_s, fill, empty
        )

    def stop_pumps(self) -> None:
        """Stop every pump on the module. Sent twice, as the stop is not acknowledged by state."""
        with self._lock:
            self._run_token += 1
            try:
                self._send(build_stop_pumps(), "stop pumps")
                self._send(build_stop_pumps(), "stop pumps (repeat)")
            finally:
                self._running = False
                self._stop_at = None
        logger.info("Autofill pumps stopped")

    def close(self) -> None:
        if self._running:
            self._stop_quietly()

    # -- Internals --

    def _watchdog(self, token: int, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL_S
        while time.monotonic() < deadline:
            if self._run_token != token:
                return  # stopped or superseded by another run
            if time.monotonic() >= next_keepalive:
                self._keepalive()
                next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL_S
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        with self._lock:
            if self._run_token != token:
                return
            for attempt in range(3):
                try:
                    self.stop_pumps()
                    return
                except Exception as exc:
                    logger.error("Autofill watchdog stop attempt %d failed: %s", attempt + 1, exc)
                    time.sleep(0.2)
            logger.error("Autofill watchdog could not stop the pumps; they may still be running")

    def _keepalive(self) -> None:
        """Poll the pump module's status while pumps run.

        The module stops its pumps by itself after a short while unless it
        keeps hearing from the host; the instrument's own software polls
        ``AF`` about three times a second during a run, and so do we.
        """
        for module in sorted({self._config.fill.module, self._config.empty.module}):
            try:
                self._send(build_pump_status(module), "pump status")
            except Exception as exc:
                logger.warning("Autofill keepalive to module %d failed: %s", module, exc)

    def _stop_quietly(self) -> None:
        try:
            self.stop_pumps()
        except Exception:
            logger.exception("Could not stop autofill pumps")

    def _send(self, payload: bytes, label: str) -> bytes:
        try:
            sender = self._sender_provider()
            reply = sender(payload)
        except AutofillError:
            raise
        except Exception as exc:
            raise AutofillError(f"Autofill {label} failed: {exc}") from exc
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Autofill %s TX %s RX %s", label, payload.hex(" "), reply.hex(" "))
        if len(reply) < 4 or reply[0] != payload[0]:
            raise AutofillError(f"Autofill {label}: unexpected reply {reply.hex(' ')}")
        if reply[1] != 0:
            raise AutofillError(f"Autofill {label}: module reported status 0x{reply[1]:02X}")
        return reply


class SimulatedAutofillModule:
    """Stand-in for the accessory bus in simulation.

    Weight rises while a forward pump runs and falls while a reverse pump runs,
    so a fill/empty cycle can be exercised without hardware.
    """

    def __init__(self, start_reading: int = 21300, counts_per_s_at_full_speed: float = 400.0) -> None:
        self._reading = float(start_reading)
        self._empty_reading = float(start_reading)  # an empty tray cannot weigh less
        self._rate = counts_per_s_at_full_speed
        self._pumps: dict[int, float] = {}  # pump -> signed fraction of full speed
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def __call__(self, payload: bytes) -> bytes:
        with self._lock:
            self._advance()
            cmd = payload[0]
            if cmd == CMD_RUN_PUMP:
                speed = (payload[4] | (payload[5] << 8)) / 10000.0
                self._pumps[payload[2]] = speed if payload[3] == 1 else -speed
            elif cmd == CMD_STOP_PUMPS:
                self._pumps.clear()
            reading = max(0, min(0xFFFF, int(self._reading)))
            return bytes([cmd, 0, reading & 0xFF, reading >> 8, 0, 0, 0, 0])

    def _advance(self) -> None:
        now = time.monotonic()
        self._reading += sum(self._pumps.values()) * self._rate * (now - self._last)
        self._reading = max(self._empty_reading, self._reading)
        self._last = now
