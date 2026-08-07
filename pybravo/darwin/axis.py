"""Per-axis state machines — commutate, home, initialize.

Commutation, homing, and initialization are driven directly through
``SUBCMD_MOTOR_STATE`` writes and polled reads rather than through a single
firmware initialize call. Driving them step by step is what makes the
retry-on-regression semantics and the timing-sensitive behavior possible.

Public entry points:
    :func:`read_motor_state`, :func:`set_motor_state`
    :func:`commutate`, :func:`home`, :func:`initialize`
    :func:`enable`, :func:`disable`, :func:`is_enabled`, :func:`reset_faults`
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    GeminiSubCommands,
    MotorState,
)
from pybravo.protocol.gemini.packet import InstructionAddress
from pybravo.types import Axis

# Default polling, timeout, and retry values.
_STATE_POLL_MS = 200
_DEFAULT_COMMUTATE_TIMEOUT_MS = 15_000
_DEFAULT_HOME_TIMEOUT_MS = 20_000
_COMMUTATE_RETRIES = 2
_HOMING_RETRIES = 3


@dataclass(frozen=True)
class AxisTimeouts:
    """Per-axis timing overrides. ``None`` means use defaults."""

    commutate_ms: int | None = None
    home_ms: int | None = None


# G axis has an extended commutate timeout (30s).
# W axis has an extended home timeout (40s).
_AXIS_TIMEOUTS: dict[Axis, AxisTimeouts] = {
    Axis.G: AxisTimeouts(commutate_ms=30_000),
    Axis.W: AxisTimeouts(home_ms=40_000),
}


def timeouts_for(axis: Axis) -> AxisTimeouts:
    return _AXIS_TIMEOUTS.get(axis, AxisTimeouts())


# --- Primitive state read/write --------------------------------------------


def read_motor_state(
    engine: GeminiEngine, address: InstructionAddress, timeout_ms: int = 5000
) -> MotorState:
    raw = engine.get_value(address, GeminiSubCommands.MOTOR_STATE, timeout_ms)
    try:
        return MotorState(raw)
    except ValueError:
        # Unknown state codes should be surfaced as-is; represent with Initial so
        # callers can still compare numerically.
        return MotorState(raw) if raw in MotorState._value2member_map_ else MotorState.INITIAL


def set_motor_state(
    engine: GeminiEngine,
    address: InstructionAddress,
    state: MotorState,
    timeout_ms: int = 5000,
) -> None:
    engine.set_uint(address, GeminiSubCommands.MOTOR_STATE, int(state), timeout_ms)


# --- Commutate --------------------------------------------------------------


def commutate(
    engine: GeminiEngine,
    address: InstructionAddress,
    axis_name: str,
    *,
    timeout_ms: int | None = None,
    poll_ms: int = _STATE_POLL_MS,
    get_estop_engaged: callable = lambda: False,
) -> None:
    """Commutate the axis: set state→Commutate, wait for Commutated.

    Retries up to ``_COMMUTATE_RETRIES`` times if the state regresses to
    ``INITIAL``.

    ``get_estop_engaged`` is a callable that returns True if E-stop is engaged
    (bit 0 of the controller state flags); checked once at entry.
    """
    if get_estop_engaged():
        raise BravoError(ErrorType.ROBOT_DISABLE)

    deadline_ms = timeout_ms or _DEFAULT_COMMUTATE_TIMEOUT_MS
    retries = 0

    set_motor_state(engine, address, MotorState.COMMUTATE)
    start = time.monotonic()
    state = read_motor_state(engine, address)
    while state != MotorState.COMMUTATED:
        time.sleep(poll_ms / 1000.0)
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > deadline_ms:
            raise BravoError(
                ErrorType.COULD_NOT_ALIGN,
                custom_text=f"Axis commutation timeout [{axis_name}]",
            )
        state = read_motor_state(engine, address)
        if state == MotorState.INITIAL:
            retries += 1
            if retries > _COMMUTATE_RETRIES:
                raise BravoError(
                    ErrorType.COULD_NOT_ALIGN,
                    custom_text=(
                        f"Axis commutation failed after {_COMMUTATE_RETRIES} "
                        f"retries [{axis_name}]"
                    ),
                )
            set_motor_state(engine, address, MotorState.COMMUTATE)
            state = read_motor_state(engine, address)


# --- Home -------------------------------------------------------------------


def home(
    engine: GeminiEngine,
    address: InstructionAddress,
    axis_name: str,
    *,
    timeout_ms: int | None = None,
    poll_ms: int = _STATE_POLL_MS,
    commutate_timeout_ms: int | None = None,
    get_estop_engaged: callable = lambda: False,
) -> None:
    """Home the axis: reset homing index, set state→Home, wait for Ready.

    If the post-Home state doesn't climb above Home (indicating the homing
    sequence didn't start cleanly), the sequence retries up to
    ``_HOMING_RETRIES`` times, re-commutating between attempts.

    Requires the axis to be at least ``COMMUTATED`` before starting.
    """
    deadline_ms = timeout_ms or _DEFAULT_HOME_TIMEOUT_MS

    for attempt in range(_HOMING_RETRIES):
        state = read_motor_state(engine, address)
        if int(state) < int(MotorState.COMMUTATED):
            raise BravoError(
                ErrorType.NOT_HOMED,
                custom_text=f"Axis not commutated [{axis_name}]",
            )
        if state == MotorState.DISABLED:
            raise BravoError(
                ErrorType.COULD_NOT_HOME,
                custom_text=f"Motor cannot be disabled when homed [{axis_name}]",
            )

        # SUBCMD_HIDX_REC_DIST=54 — reset homing index record distance
        engine.set_uint(address, GeminiSubCommands.HIDX_REC_DIST, 0)
        set_motor_state(engine, address, MotorState.HOME)

        start = time.monotonic()
        state = read_motor_state(engine, address)
        while (
            int(state) >= int(MotorState.HOME)
            and int(state) < int(MotorState.READY)
        ):
            time.sleep(poll_ms / 1000.0)
            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms > deadline_ms:
                raise BravoError(
                    ErrorType.COULD_NOT_HOME,
                    custom_text=f"Axis homing timeout [{axis_name}]",
                )
            state = read_motor_state(engine, address)

        if int(state) < int(MotorState.HOME):
            # State regressed below HOME (e.g., back to COMMUTATED) — re-commutate and retry.
            commutate(
                engine,
                address,
                axis_name,
                timeout_ms=commutate_timeout_ms,
                poll_ms=poll_ms,
                get_estop_engaged=get_estop_engaged,
            )
            continue
        return

    raise BravoError(
        ErrorType.COULD_NOT_HOME,
        custom_text=f"Axis homing retries exceeded [{axis_name}]",
    )


# --- Initialize (commutate + home) -----------------------------------------


def is_initialized(
    engine: GeminiEngine, address: InstructionAddress, timeout_ms: int = 5000
) -> bool:
    """Whether the axis has been commutated + homed (motor_state >= READY).

    Re-homing an already- initialized axis requires disabling it first, or the
    controller NAKs with ``MOVE_IN_PROGRESS``.
    """
    state = read_motor_state(engine, address, timeout_ms)
    return int(state) >= int(MotorState.READY)


def initialize(
    engine: GeminiEngine,
    address: InstructionAddress,
    axis_name: str,
    *,
    commutate_timeout_ms: int | None = None,
    home_timeout_ms: int | None = None,
    force: bool = False,
    get_estop_engaged: callable = lambda: False,
) -> None:
    """Commutate + home the axis. Skips both if already initialized.

    An axis that reports itself initialized is left alone. Pass ``force=True``
    to home even an already-homed axis (requires disabling first).
    """
    if not force and is_initialized(engine, address):
        return
    if force:
        disable(engine, address, axis_name)
        # Give the controller a moment to honor the disable
        time.sleep(0.05)
    commutate(
        engine,
        address,
        axis_name,
        timeout_ms=commutate_timeout_ms,
        get_estop_engaged=get_estop_engaged,
    )
    home(
        engine,
        address,
        axis_name,
        timeout_ms=home_timeout_ms,
        commutate_timeout_ms=commutate_timeout_ms,
        get_estop_engaged=get_estop_engaged,
    )


# --- Enable / disable ------------------------------------------------------


def is_enabled(
    engine: GeminiEngine, address: InstructionAddress, timeout_ms: int = 5000
) -> bool:
    state = read_motor_state(engine, address, timeout_ms)
    return state != MotorState.DISABLED


def enable(
    engine: GeminiEngine,
    address: InstructionAddress,
    axis_name: str,
    timeout_ms: int = 5000,
) -> None:
    """Transition from DISABLED to READY. If already non-disabled, no-op."""
    state = read_motor_state(engine, address, timeout_ms)
    if state != MotorState.DISABLED:
        return
    set_motor_state(engine, address, MotorState.ENABLE, timeout_ms)
    # Poll for state != DISABLE* family
    start = time.monotonic()
    while True:
        time.sleep(_STATE_POLL_MS / 1000.0)
        state = read_motor_state(engine, address, timeout_ms)
        if state not in (
            MotorState.DISABLED,
            MotorState.DISABLE,
            MotorState.ENABLE,
        ):
            return
        if (time.monotonic() - start) * 1000 > timeout_ms:
            raise BravoError(
                ErrorType.COULD_NOT_ENABLE_MOTOR,
                custom_text=f"Motor enable timeout [{axis_name}]",
            )


def disable(
    engine: GeminiEngine,
    address: InstructionAddress,
    axis_name: str,
    timeout_ms: int = 5000,
) -> None:
    """Transition to DISABLED."""
    set_motor_state(engine, address, MotorState.DISABLE, timeout_ms)
    # Don't wait — the disable path is fire-and-forget.


def reset_faults(
    engine: GeminiEngine, address: InstructionAddress, timeout_ms: int = 5000
) -> None:
    """Clear axis fault state.

    This is a no-op on DARWIN. Kept as a stub so callers don't have to
    special-case it.
    """
    # No-op on DARWIN.
    return


__all__ = [
    "AxisTimeouts",
    "commutate",
    "disable",
    "enable",
    "home",
    "initialize",
    "is_enabled",
    "is_initialized",
    "read_motor_state",
    "reset_faults",
    "set_motor_state",
    "timeouts_for",
]
