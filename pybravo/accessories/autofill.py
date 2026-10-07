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
        self._speeds: dict[str, float] = {}  # pump role -> last commanded speed while running

    @property
    def config(self) -> AutofillConfig:
        return self._config

    @property
    def run_token(self) -> int:
        """Changes on every start and stop; lets a supervisor tell its own run from a later one."""
        return self._run_token

    @property
    def pump_speeds(self) -> dict[str, float]:
        """Speed (%) last commanded to each running pump: {"fill": .., "empty": ..}."""
        return dict(self._speeds) if self._running else {}

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
        speeds: dict[str, float] = {}
        if fill:
            p = self._config.fill
            speeds["fill"] = p.speed_pct if fill_speed_pct is None else fill_speed_pct
            commands.append(("fill", build_run_pump(p.module, p.pump, p.direction, speeds["fill"])))
        if empty:
            p = self._config.empty
            speeds["empty"] = p.speed_pct if empty_speed_pct is None else empty_speed_pct
            commands.append(("empty", build_run_pump(p.module, p.pump, p.direction, speeds["empty"])))

        with self._lock:
            if self._running:
                self.stop_pumps()
            self._run_token += 1
            token = self._run_token
            self._running = True
            self._speeds = speeds
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

    def set_pump_speed(self, role: str, speed_pct: float, *, run_token: int | None = None) -> None:
        """Change the speed of the running fill or empty pump.

        Re-sends the run command for that pump with the new speed; the run's
        deadline and watchdog are unchanged. Speed 0 keeps the pump commanded
        but still. Does nothing once the run has ended, or, with ``run_token``,
        when that run has been stopped or replaced by another one.
        """
        settings = {"fill": self._config.fill, "empty": self._config.empty}.get(role)
        if settings is None:
            raise AutofillError(f"Unknown pump role {role!r}; use 'fill' or 'empty'")
        payload = build_run_pump(settings.module, settings.pump, settings.direction, speed_pct)
        with self._lock:
            if not self._running or (run_token is not None and run_token != self._run_token):
                return
            self._send(payload, f"set {role} pump speed")
            self._speeds[role] = float(speed_pct)

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


PRIME_MIN_LEVEL_PCT = 5.0  # never prime the drain from an (almost) empty tray
FILL_PRIMED_RISE_PCT = 1.5  # level rise (above weigh-pad ripple) that shows the supply delivers
DRAIN_PRIME_TIMEOUT_S = 8.0  # a dry drain line took several seconds even at 100 %
DRAIN_PRIMED_SLOPE_PCT_S = 1.5  # level falling at least this fast: the drain pulls
INTEGRAL_UNWIND_GAIN = 3.0
# Supply stall: filling at >= this inflow with the drain closed, the level
# rising slower than FILL_STALL_SLOPE_PCT_S for FILL_STALL_S -> prime again
# (measured 2026-10-07: air in the supply hose, 50 % then lifts nothing).
FILL_STALL_MIN_INFLOW_PCT = 20.0
FILL_STALL_SLOPE_PCT_S = 0.3
FILL_STALL_S = 4.0


class LevelHoldController:
    """Hold the reservoir at a target level while liquid flows through it.

    Deliberately simple and smooth:

    * The working target (``target``) moves continuously toward the requested
      target (``requested_target``) at ``target_ramp_pct_s``; it starts at the
      current level, so the tray is brought up (or down) along a ramp rather
      than in one jump.
    * The drain follows a PI controller around "drain = inflow".
    * The inflow runs at the requested value while the drain has headroom and
      is scaled down continuously as the drain nears its limit (full inflow at
      ``throttle_from_pct`` drain, none at 100 %), so a drain that cannot keep
      up never lets the tray overflow and nothing switches on and off.
    * Pump speeds never jump: both move at most ``max_speed_change_pct_s``.
      The one exception is priming: once the level gets within
      ``prime_before_target_pct`` of the target the drain runs at full speed
      until the level clearly falls (at least ``prime_s``), as a drain line
      that ran dry does not pull at low speed.
      Likewise the inflow starts at full speed until the level rises
      (``prime_fill``), as the supply line drains back while idle.

    Target and inflow can be changed while it runs (:meth:`set_target`,
    :meth:`set_inflow`).
    """

    def __init__(
        self,
        target_pct: float,
        inflow_pct: float,
        *,
        # Defaults tuned against the 384ST autofill station (2026-10-06): fill
        # ~0.094 %/s per % speed with ~0.6 s dead time, drain ~0.10-0.13 %/s
        # per % with ~1 s dead time, weigh-pad ripple about +-0.7 %. A drain
        # line that ran dry does not prime below ~50 % speed but does within
        # ~1 s at 100 %, hence the priming pulse.
        kp: float = 1.5,
        ki: float = 0.2,
        target_ramp_pct_s: float = 15.0,
        throttle_from_pct: float = 85.0,
        max_speed_change_pct_s: float = 20.0,
        inflow_change_pct_s: float = 10.0,
        lookahead_s: float = 1.0,
        level_smoothing: float = 1.0,
        slope_smoothing: float = 0.15,
        prime_s: float = 1.5,
        prime_before_target_pct: float = 15.0,
        integral_limit_pct: float = 60.0,
        prime_fill: bool = True,
        fill_prime_timeout_s: float = 20.0,
    ) -> None:
        # The supply line drains back to the source while idle and, like the
        # drain, does not lift water at low speed: start the inflow at full
        # speed until the level actually rises, then drop to the request.
        self._fill_priming = prime_fill
        self._fill_prime_left = fill_prime_timeout_s
        self._fill_prime_timeout = fill_prime_timeout_s
        self._stall_s = 0.0
        self.fill_reprimes = 0  # times the supply had to be primed again
        self._start_level: float | None = None
        self._prime_s = prime_s
        self._prime_lead = prime_before_target_pct
        self._priming_left = 0.0
        self._priming_min = 0.0
        self._primed = prime_s <= 0
        self._integral_limit = integral_limit_pct
        self._inflow_rate = inflow_change_pct_s
        self._level_alpha = level_smoothing  # 1.0 = raw reading; lower = calmer, slower
        self._slope_alpha = slope_smoothing
        self._smoothed: float | None = None
        self.requested_target = target_pct
        self.target = target_pct  # working target; starts at the level on first update
        self.requested_inflow = inflow_pct
        self.inflow = inflow_pct
        # The drain starts closed and only opens (ramping) when the level calls
        # for it, so a hold that begins below its target never drains first.
        self.drain = 0.0
        self.inflow_limit: float | None = None  # set while the drain is throttling the inflow
        self._kp, self._ki = kp, ki
        self._target_ramp = target_ramp_pct_s
        self._throttle_from = throttle_from_pct
        self._max_rate = max_speed_change_pct_s
        self._integral = 0.0
        self._started = False
        self._lookahead = lookahead_s
        self._last_level: float | None = None
        self._slope = 0.0  # smoothed level change, %/s

    def set_target(self, target_pct: float) -> None:
        self.requested_target = float(target_pct)

    def set_inflow(self, inflow_pct: float) -> None:
        self.requested_inflow = min(100.0, max(0.0, float(inflow_pct)))

    def _ramp(self, current: float, wanted: float, rate: float, dt_s: float) -> float:
        limit = rate * dt_s
        return current + max(-limit, min(limit, wanted - current))

    def update(self, level_pct: float, dt_s: float) -> tuple[float, float]:
        """Return (inflow %, drain %) for the next interval."""
        if not self._started:
            self._started = True
            self.target = level_pct
        self.target = self._ramp(self.target, self.requested_target, self._target_ramp, dt_s)

        # The weigh pad reads with ripple from the pumps; smooth it before use.
        if self._smoothed is None:
            self._smoothed = level_pct
        self._smoothed += self._level_alpha * (level_pct - self._smoothed)
        level_pct = self._smoothed

        # The level a moment ahead (smoothed rate, so weigh-pad noise stays out).
        if self._last_level is not None and dt_s > 0:
            measured = (level_pct - self._last_level) / dt_s
            self._slope += self._slope_alpha * (measured - self._slope)
        self._last_level = level_pct
        error = level_pct + self._slope * self._lookahead - self.target  # positive: too full

        # Inflow: the request, scaled down smoothly as the drain nears its limit
        # and while the level is heading over the target.
        drain_load = 0.0 if self.priming else self.drain  # a priming pulse is not a drain at its limit
        headroom = (100.0 - drain_load) / (100.0 - self._throttle_from)
        heading_over = 1.0 - max(0.0, error) / 10.0
        goal = self.requested_inflow * min(1.0, max(0.0, headroom), max(0.0, heading_over))
        self.inflow_limit = goal if goal < self.requested_inflow - 0.5 else None
        if self._start_level is None:
            self._start_level = level_pct
        if self._fill_priming:
            self._fill_prime_left -= dt_s
            if (level_pct >= self._start_level + FILL_PRIMED_RISE_PCT or self._fill_prime_left <= 0
                    or self.requested_inflow <= 0 or level_pct >= self.target):
                self._fill_priming = False
                self.inflow = min(self.inflow, goal)  # primed: straight to the request
            else:
                self.inflow = 100.0
        if not self._fill_priming:
            # The inflow is the slow loop and the drain the fast one, so the
            # two never chase each other.
            self.inflow = self._ramp(self.inflow, goal, self._inflow_rate, dt_s)
            # Supply lost its prime (air in the hose): filling with the drain
            # closed, yet the level has stopped rising. Prime it again.
            stalled = (self.inflow >= FILL_STALL_MIN_INFLOW_PCT and self.drain < 5.0
                       and self._slope < FILL_STALL_SLOPE_PCT_S and level_pct < self.target)
            self._stall_s = self._stall_s + dt_s if stalled else 0.0
            if self._stall_s >= FILL_STALL_S:
                self._stall_s = 0.0
                self._fill_priming = True
                self._start_level = level_pct
                self._fill_prime_left = self._fill_prime_timeout
                self.fill_reprimes += 1
                self.inflow = 100.0

        # Drain: PI around "drain = inflow". Well below the target the
        # "drain = inflow" share fades out, so the drain stays closed while filling.
        share = min(1.0, max(0.0, 1.0 + error / 5.0))
        unclamped = share * self.inflow + self._kp * error + self._ki * self._integral
        wanted = min(100.0, max(0.0, unclamped))

        # Prime the drain once, a little before the target, with one short
        # full-speed pulse: an empty drain line does not pull at low speed.
        # Prime the drain once, a little before the target: full speed until
        # the level clearly falls (at least prime_s, at most
        # DRAIN_PRIME_TIMEOUT_S), as an empty drain line does not pull at low speed.
        prime_from = max(PRIME_MIN_LEVEL_PCT, self.requested_target - self._prime_lead)
        if not self._primed and level_pct >= prime_from:
            self._primed = True
            self._priming_left = DRAIN_PRIME_TIMEOUT_S
            self._priming_min = self._prime_s
        if self._priming_left > 1e-9:
            self._priming_left -= dt_s
            self._priming_min -= dt_s
            pulling = self._priming_min <= 1e-9 and self._slope <= -DRAIN_PRIMED_SLOPE_PCT_S
            if pulling or self._priming_left <= 1e-9:
                self._priming_left = 0.0
                self.drain = 0.0  # primed: the drain ramps up from closed again
                return self.inflow, 100.0
            self.drain = 100.0
            return self.inflow, self.drain

        if wanted == unclamped and self._ki > 0:
            # No wind-up while saturated, never more than integral_limit_pct of
            # drain speed, and unwinding faster than it winds up, so a slow
            # start cannot leave a lasting offset.
            step = error * dt_s
            if step * self._integral < 0:
                step *= INTEGRAL_UNWIND_GAIN
            bound = self._integral_limit / self._ki
            self._integral = min(bound, max(-bound, self._integral + step))
        self.drain = self._ramp(self.drain, wanted, self._max_rate, dt_s)
        return self.inflow, self.drain

    @property
    def priming(self) -> bool:
        """True while the drain-priming pulse is running."""
        return self._priming_left > 1e-9

    @property
    def ready(self) -> bool:
        """Both lines primed and the working target at the requested one."""
        drain_primed = self._primed or self.requested_target < PRIME_MIN_LEVEL_PCT
        return (not self._fill_priming and drain_primed and not self.priming
                and abs(self.target - self.requested_target) < 1e-6)


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
