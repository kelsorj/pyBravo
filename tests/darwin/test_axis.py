"""Tests for axis state machines against a stateful fake."""

from __future__ import annotations

import threading
import time

import pytest

from pybravo.darwin.axis import (
    commutate,
    disable,
    enable,
    home,
    initialize,
    is_enabled,
    read_motor_state,
    set_motor_state,
)
from pybravo.darwin.topology import axis_address
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    CommandTypes,
    GeminiSubCommands,
    MotorState,
)
from pybravo.protocol.gemini.packet import InstructionAddress, Packet
from pybravo.types import Axis
from tests.fakes.gemini_fake import FakeGeminiServer


class _AxisSimulator:
    """Thread-safe simulated motor-state progression for a single axis.

    Transitions:
      SET COMMUTATE  → schedules next GET after ``transition_ms`` to return COMMUTATED
      SET HOME       → schedules GET after ``transition_ms`` to return HOMED, then READY
      SET DISABLE    → next GET returns DISABLED
      SET ENABLE     → next GET returns READY
    """

    def __init__(self, transition_ms: int = 20):
        self._lock = threading.Lock()
        self._state = MotorState.INITIAL
        self._transition_ms = transition_ms
        self._transition_at: float = 0.0
        self._pending_state: MotorState | None = None

    def current_state(self) -> MotorState:
        with self._lock:
            if self._pending_state is not None and time.monotonic() >= self._transition_at:
                self._state = self._pending_state
                self._pending_state = None
            return self._state

    def set_state(self, requested: MotorState) -> None:
        with self._lock:
            if requested == MotorState.COMMUTATE:
                self._state = MotorState.COMMUTATE
                self._pending_state = MotorState.COMMUTATED
                self._transition_at = time.monotonic() + self._transition_ms / 1000.0
            elif requested == MotorState.HOME:
                self._state = MotorState.HOME
                self._pending_state = MotorState.READY
                self._transition_at = time.monotonic() + self._transition_ms / 1000.0
            elif requested == MotorState.DISABLE:
                self._state = MotorState.DISABLED
                self._pending_state = None
            elif requested == MotorState.ENABLE:
                self._state = MotorState.READY
                self._pending_state = None
            else:
                self._state = requested

    def force(self, state: MotorState) -> None:
        with self._lock:
            self._state = state
            self._pending_state = None


def _install_axis_simulator(
    fake: FakeGeminiServer,
    addr: InstructionAddress,
    simulator: _AxisSimulator,
) -> None:
    def get_handler(pkt: Packet) -> Packet | None:
        value = int(simulator.current_state())
        return Packet(
            src=pkt.dest,
            dest=pkt.src,
            cmd_type=CommandTypes.GETCMD_RESP,
            sub_command=pkt.sub_command,
            cmd_val=value,
        )

    def set_handler(pkt: Packet) -> Packet | None:
        try:
            requested = MotorState(pkt.cmd_val)
        except ValueError:
            requested = MotorState.INITIAL
        simulator.set_state(requested)
        return Packet(
            src=pkt.dest,
            dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command,
            cmd_val=0,
        )

    fake.on_get(addr, GeminiSubCommands.MOTOR_STATE, get_handler)
    fake.on_set(addr, GeminiSubCommands.MOTOR_STATE, set_handler)


@pytest.fixture
def fake():
    s = FakeGeminiServer()
    s.start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def engine(fake):
    e = GeminiEngine("127.0.0.1", port=fake.port)
    e.connect()
    try:
        yield e
    finally:
        e.close()


# --- Basic state read/write -------------------------------------------------


def test_read_and_set_motor_state_roundtrip(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator()
    _install_axis_simulator(fake, addr, sim)

    set_motor_state(engine, addr, MotorState.DISABLE)
    assert read_motor_state(engine, addr) == MotorState.DISABLED


def test_is_enabled_reflects_motor_state(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator()
    _install_axis_simulator(fake, addr, sim)
    sim.force(MotorState.READY)
    assert is_enabled(engine, addr) is True
    sim.force(MotorState.DISABLED)
    assert is_enabled(engine, addr) is False


# --- Commutate --------------------------------------------------------------


def test_commutate_drives_state_to_commutated(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator(transition_ms=20)
    _install_axis_simulator(fake, addr, sim)
    commutate(engine, addr, "X", poll_ms=5)
    assert sim.current_state() == MotorState.COMMUTATED


def test_commutate_raises_on_timeout(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator(transition_ms=10_000)  # never transitions in test budget
    _install_axis_simulator(fake, addr, sim)
    with pytest.raises(BravoError) as exc_info:
        commutate(engine, addr, "X", poll_ms=5, timeout_ms=100)
    assert exc_info.value.error_type == ErrorType.COULD_NOT_ALIGN


def test_commutate_retries_on_regression(engine, fake):
    """If state regresses to INITIAL after a set-to-COMMUTATE, we resend."""
    addr = axis_address(Axis.X)
    sim = _AxisSimulator(transition_ms=50)

    set_calls = {"count": 0}

    def set_handler(pkt: Packet) -> Packet | None:
        try:
            requested = MotorState(pkt.cmd_val)
        except ValueError:
            requested = MotorState.INITIAL
        if requested == MotorState.COMMUTATE:
            set_calls["count"] += 1
            if set_calls["count"] == 1:
                # First attempt: pretend to commutate but then regress
                sim.set_state(MotorState.COMMUTATE)
                sim.force(MotorState.INITIAL)
            else:
                # Subsequent attempts: normal
                sim.set_state(MotorState.COMMUTATE)
        else:
            sim.set_state(requested)
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=0,
        )

    def get_handler(pkt: Packet) -> Packet | None:
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.GETCMD_RESP,
            sub_command=pkt.sub_command,
            cmd_val=int(sim.current_state()),
        )

    fake.on_set(addr, GeminiSubCommands.MOTOR_STATE, set_handler)
    fake.on_get(addr, GeminiSubCommands.MOTOR_STATE, get_handler)

    commutate(engine, addr, "X", poll_ms=5, timeout_ms=2000)
    assert sim.current_state() == MotorState.COMMUTATED
    assert set_calls["count"] >= 2, "retry should have resent the COMMUTATE set"


# --- Home -------------------------------------------------------------------


def test_home_from_commutated_reaches_ready(engine, fake):
    addr = axis_address(Axis.Y)
    sim = _AxisSimulator(transition_ms=20)
    sim.force(MotorState.COMMUTATED)
    _install_axis_simulator(fake, addr, sim)

    home(engine, addr, "Y", poll_ms=5, timeout_ms=2000)
    assert sim.current_state() == MotorState.READY


def test_home_refuses_uncommutated_axis(engine, fake):
    addr = axis_address(Axis.Z)
    sim = _AxisSimulator()
    sim.force(MotorState.INITIAL)
    _install_axis_simulator(fake, addr, sim)

    with pytest.raises(BravoError) as exc_info:
        home(engine, addr, "Z", poll_ms=5, timeout_ms=500)
    assert exc_info.value.error_type == ErrorType.NOT_HOMED


def test_home_refuses_disabled_axis(engine, fake):
    addr = axis_address(Axis.Z)
    sim = _AxisSimulator()
    sim.force(MotorState.DISABLED)
    _install_axis_simulator(fake, addr, sim)

    with pytest.raises(BravoError) as exc_info:
        home(engine, addr, "Z", poll_ms=5, timeout_ms=500)
    # DISABLED (21) is numerically above COMMUTATED (4), so the first check
    # passes; the second ("can't be disabled") fires and raises COULD_NOT_HOME.
    assert exc_info.value.error_type == ErrorType.COULD_NOT_HOME
    assert "disabled" in str(exc_info.value).lower()


def test_home_times_out_if_ready_never_reached(engine, fake):
    addr = axis_address(Axis.Y)
    sim = _AxisSimulator(transition_ms=10_000)
    sim.force(MotorState.COMMUTATED)
    _install_axis_simulator(fake, addr, sim)

    with pytest.raises(BravoError) as exc_info:
        home(engine, addr, "Y", poll_ms=5, timeout_ms=80)
    assert exc_info.value.error_type == ErrorType.COULD_NOT_HOME


# --- Initialize -------------------------------------------------------------


def test_initialize_commutates_then_homes(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator(transition_ms=20)
    _install_axis_simulator(fake, addr, sim)

    initialize(engine, addr, "X")
    # Can't easily assert on poll_ms in initialize() since we don't expose it;
    # trust the behavior test below.
    assert sim.current_state() == MotorState.READY


# --- Enable / disable -------------------------------------------------------


def test_disable_sets_motor_to_disabled(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator()
    sim.force(MotorState.READY)
    _install_axis_simulator(fake, addr, sim)

    disable(engine, addr, "X")
    assert sim.current_state() == MotorState.DISABLED


def test_enable_from_disabled_reaches_ready(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator()
    sim.force(MotorState.DISABLED)
    _install_axis_simulator(fake, addr, sim)

    enable(engine, addr, "X")
    assert sim.current_state() == MotorState.READY


def test_enable_is_noop_if_already_enabled(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisSimulator()
    sim.force(MotorState.READY)
    _install_axis_simulator(fake, addr, sim)

    set_count = 0

    def set_handler(pkt: Packet) -> Packet | None:
        nonlocal set_count
        set_count += 1
        sim.set_state(MotorState(pkt.cmd_val))
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=0,
        )
    fake.on_set(addr, GeminiSubCommands.MOTOR_STATE, set_handler)

    enable(engine, addr, "X")
    assert set_count == 0
    assert sim.current_state() == MotorState.READY
