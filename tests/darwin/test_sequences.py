"""Smoke tests for composite sequences.

These tests exercise the sequences against the same motion simulator as
test_motion — they validate the right packets are sent in the right order, but
not the wire-level force-control semantics (which only a real axis can verify).
"""

from __future__ import annotations

import pytest

from pybravo.darwin.params import ParameterAccess
from pybravo.darwin.sequences import (
    GripParams,
    JogParams,
    OpenGripperParams,
    _g_axis_force_percent,
    _z_axis_force_percent,
    force_move,
    grip,
    jog,
    open_gripper,
    set_peak_current_amps,
)
from pybravo.darwin.topology import axis_address
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    AxisDirection,
    CommandTypes,
    CommonSubCommands,
    GeminiSubCommands,
    MotorState,
)
from pybravo.protocol.gemini.instruction import pack_float32, unpack_float32
from pybravo.protocol.gemini.packet import InstructionAddress, Packet
from pybravo.types import Axis
from tests.darwin.test_motion import _AxisMotionSim
from tests.fakes.gemini_fake import FakeGeminiServer


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


# --- Force-percent lookup tables --------------------------------------------


@pytest.mark.parametrize(
    "amps, expected",
    [
        # Reference (0.5A) returns 0 by bridge convention
        (0.5, 0.0),
        # Below reference: linear in amps * (100/0.5) * (80/30) = amps * 533.33
        (0.15, pytest.approx(80.0, abs=0.1)),
        (0.10, pytest.approx(53.33, abs=0.1)),
        (0.05, pytest.approx(26.67, abs=0.1)),
        # Above reference but below clamp: caps at 100
        (0.3, 100.0),
        (1.0, 100.0),
        (0.0, 0.0),
    ],
)
def test_g_axis_force_percent(amps, expected):
    assert _g_axis_force_percent(amps) == expected


@pytest.mark.parametrize(
    "amps, expected",
    [
        # Exact anchors from Get-ZAxisForcePercent
        (0.04, 2.0),
        (0.07, 9.0),
        (0.10, 11.0),
        (0.16, 20.0),
        (0.30, 38.0),
        (0.60, 67.0),
        (0.80, 90.0),
        # Below minimum anchor → anchored at 2.0
        (0.0, 2.0),
        (0.02, 2.0),
        # Midway between anchors — piecewise linear
        (0.055, pytest.approx(5.5, abs=0.05)),  # halfway 0.04→0.07
        (0.13, pytest.approx(15.5, abs=0.1)),   # halfway 0.10→0.16
        # Beyond top anchor: extrapolates then clamps at 100
        (1.0, 100.0),
    ],
)
def test_z_axis_force_percent(amps, expected):
    assert _z_axis_force_percent(amps) == expected


# --- set_peak_current_amps --------------------------------------------------


def test_set_peak_current_writes_amps_directly(engine, fake):
    addr = axis_address(Axis.G)
    params = ParameterAccess(engine, addr)

    set_peak_current_amps(params, peak_current_amps=0.6)
    value_writes = [
        p for p in fake.received_packets
        if p.dest == addr
        and p.sub_command == CommonSubCommands.PARAM_DB_VALUE
        and p.cmd_type == CommandTypes.SETCMD
    ]
    assert len(value_writes) == 1
    assert abs(unpack_float32(value_writes[0].cmd_val) - 0.6) < 1e-5
    # Apply was sent too
    applies = [
        p for p in fake.received_packets
        if p.dest == addr and p.sub_command == CommonSubCommands.PARAM_DB_APPLY
    ]
    assert len(applies) == 1


def test_set_peak_current_clamps_negative_to_zero(engine, fake):
    addr = axis_address(Axis.G)
    params = ParameterAccess(engine, addr)
    set_peak_current_amps(params, peak_current_amps=-0.1)
    value_writes = [
        p for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_VALUE
    ]
    assert abs(unpack_float32(value_writes[0].cmd_val) - 0.0) < 1e-5


# --- force_move primitive ---------------------------------------------------


def test_force_move_loads_instruction_and_waits(engine, fake):
    addr = axis_address(Axis.Z)
    # BUSY window must be long enough for the polling loop to observe it at
    # least once (motion.py requires BUSY→READY to count as "move complete").
    sim = _AxisMotionSim(addr, complete_ms=80)
    sim.install(fake)

    force_move(
        engine, addr, "Z", target_normalized=0.3,
        direction=AxisDirection.POSITIVE,
        velocity_percent=50.0, acceleration_percent=50.0,
        force_percent=80.0, timeout_ms=2000,
    )
    assert sim.state == MotorState.READY
    # Instruction was loaded (NEW + 4*TBL_VAL + START + SEND = 7 packets)
    assert len(fake.received_multipackets) == 1
    assert len(fake.received_multipackets[0]) == 7


def test_force_move_sets_reset_pos_after_stop_when_force_nonzero(engine, fake):
    """Regression: ResetPosAfterStop must be set whenever force > 0.

     Without this bit the firmware's commanded-position
    counter stays at the full target even when the motor stops early on a
    force threshold hit — the next move then trips POS_ERR_LIMIT
    (RESERVED_EVENT_ERROR cat 5 spec 3) before any travel. Tips On retract
    was failing on this exact bug.
    """
    addr = axis_address(Axis.Z)
    sim = _AxisMotionSim(addr, complete_ms=80)
    sim.install(fake)

    force_move(
        engine, addr, "Z", target_normalized=0.3,
        direction=AxisDirection.POSITIVE,
        velocity_percent=50.0, acceleration_percent=50.0,
        force_percent=80.0, timeout_ms=2000,
    )
    # Find the INSTR_TBL_VAL writes carrying word1 (jerk low byte = 0xFF
    # since we always clamp jerk to 100%).
    from pybravo.protocol.gemini.enums import GeminiSubCommands as GSC
    word1_writes = [
        p.cmd_val for p in fake.received_multipackets[0]
        if p.dest == addr
        and p.sub_command == GSC.INSTR_TBL_VAL
        and (p.cmd_val & 0xFF) == 0xFF  # word1 signature
    ]
    assert word1_writes, "expected a word1 INSTR_TBL_VAL write for force move"
    # word1 bit 18 = reset_pos_after_stop. Must be SET when force > 0.
    _BIT_RESET_POS_AFTER_STOP = 1 << 18
    for w1 in word1_writes:
        assert (w1 & _BIT_RESET_POS_AFTER_STOP) != 0, (
            f"force_move produced word1=0x{w1:08x} without "
            "reset_pos_after_stop bit (0x40000); firmware will carry "
            "commanded-vs-actual residual into the next move and trip "
            "POS_ERR_LIMIT."
        )


def test_force_move_skips_reset_pos_after_stop_when_force_zero(engine, fake):
    """Inverse: zero force = ordinary MoveAbsolute path in the
     only sets ResetPosAfterStop when force != 0).
    """
    addr = axis_address(Axis.Z)
    sim = _AxisMotionSim(addr, complete_ms=80)
    sim.install(fake)

    force_move(
        engine, addr, "Z", target_normalized=0.3,
        direction=AxisDirection.POSITIVE,
        velocity_percent=50.0, acceleration_percent=50.0,
        force_percent=0.0, timeout_ms=2000,  # force=0 = ordinary move
    )
    from pybravo.protocol.gemini.enums import GeminiSubCommands as GSC
    word1_writes = [
        p.cmd_val for p in fake.received_multipackets[0]
        if p.dest == addr
        and p.sub_command == GSC.INSTR_TBL_VAL
        and (p.cmd_val & 0xFF) == 0xFF
    ]
    assert word1_writes
    _BIT_RESET_POS_AFTER_STOP = 1 << 18
    for w1 in word1_writes:
        assert (w1 & _BIT_RESET_POS_AFTER_STOP) == 0, (
            f"force=0 move produced word1=0x{w1:08x} with "
            "reset_pos_after_stop bit set; sets it only when "
            "force != 0."
        )


# --- grip -------------------------------------------------------------------


def test_grip_does_not_write_to_param_db(engine, fake):
    """grip() must not issue any PARAM_DB writes to the G axis.

    Regression: an earlier port wrote and restored I2T_PEAK_CURRENT around
    the force move. On bench hardware the restore leg NAK'd OUT_OF_RANGE
    (the axis didn't accept the WR_PTR after an aborted force-move) and
    masked the real error. Observed traffic shows that
    emits zero PARAM_DB writes to node 6 during a grip — same pattern we
    already enforce for jog (test_jog_does_not_write_to_param_db).
    """
    g_addr = axis_address(Axis.G)
    sim = _AxisMotionSim(g_addr, complete_ms=80)
    sim.install(fake)
    g_params = ParameterAccess(engine, g_addr)

    grip(
        engine, g_addr, g_params,
        GripParams(
            target_position=0.5,  # normalized
            velocity_limit=30.0,
            acceleration_limit=500.0,
            grip_current_amps=0.25,
            overshoot_normalized=0.1,  # kept strictly within [0, 1]
            velocity_mm=300.0,
            acceleration_mm=300.0,
        ),
        timeout_ms=2000,
    )

    param_db_subs = {
        CommonSubCommands.PARAM_DB_WR_PTR,
        CommonSubCommands.PARAM_DB_RD_PTR,
        CommonSubCommands.PARAM_DB_VALUE,
        CommonSubCommands.PARAM_DB_APPLY,
    }
    g_param_writes = [
        p for p in fake.received_packets
        if p.dest == g_addr and p.sub_command in param_db_subs
    ]
    assert g_param_writes == [], (
        f"grip unexpectedly wrote to G param DB: {g_param_writes!r}. "
        f"A grip must not write to the param DB."
    )
    # Motor was disabled after grip
    assert sim.state in (MotorState.DISABLED, MotorState.DISABLE)


def test_grip_clamps_farthest_to_normalized_hardware_max():
    """Regression: earlier the overshoot field was 4.0 in NORMALIZED units
    (meant to be 4 mm) which drove farthest to ~4.78 normalized ≈ 93 mm on
    G, well past hardware_max=13.583 mm. The firmware responded with NAK
    OUT_OF_RANGE on the force-move multipacket.

    With the fix, GripParams.overshoot_normalized is pre-divided by
    hardware_range by the caller; grip() additionally clamps farthest to
    1.0 as a defense-in-depth guard against miscalibrated callers.
    """
    # Test the clamp directly with an intentionally-huge overshoot (mimics
    # the old bug's effective value).
    from pybravo.darwin.sequences import grip as grip_fn  # noqa: F401
    params = GripParams(
        target_position=0.8,
        velocity_limit=30.0,
        acceleration_limit=500.0,
        grip_current_amps=0.25,
        overshoot_normalized=4.0,  # wildly out of bounds — what the bug produced
        velocity_mm=300.0,
        acceleration_mm=300.0,
    )
    farthest = min(1.0, params.target_position + params.overshoot_normalized)
    assert farthest == 1.0, (
        f"Expected farthest clamped to 1.0, got {farthest}. "
        "Mirrors the clamp in sequences.grip."
    )


# --- open_gripper -----------------------------------------------------------


def test_open_gripper_sets_max_current_and_moves(engine, fake):
    g_addr = axis_address(Axis.G)
    sim = _AxisMotionSim(g_addr, complete_ms=80)
    sim.install(fake)
    g_params = ParameterAccess(engine, g_addr)

    open_gripper(
        engine, g_addr, g_params,
        OpenGripperParams(
            target_position=0.0,
            current_position=10.0,
            velocity_limit=30.0,
            acceleration_limit=500.0,
            peak_current_amps=1.2,
        ),
        timeout_ms=2000,
    )

    # Peak-current write should be the full cached max
    peak_writes = [
        p for p in fake.received_packets
        if p.dest == g_addr and p.sub_command == CommonSubCommands.PARAM_DB_VALUE
    ]
    assert peak_writes, "expected at least one peak-current write"
    assert abs(unpack_float32(peak_writes[0].cmd_val) - 1.2) < 1e-5

    # Move was loaded (one multipacket of 8 packets)
    assert len(fake.received_multipackets) == 1

    # Direction in the instruction word1: target (0) < current (10) → NEGATIVE
    w1_pkt = fake.received_multipackets[0][3]
    assert (w1_pkt.cmd_val & (1 << 16)) == 0, "expected NEGATIVE direction bit"

    assert sim.state in (MotorState.DISABLED, MotorState.DISABLE)


# --- jog --------------------------------------------------------------------


def _install_z_sim_that_lands_at(fake, addr, landed_position_normalized,
                                  complete_ms=80):
    """Install a sim that reports a specific final position after the move."""
    sim = _AxisMotionSim(addr, complete_ms=complete_ms)
    sim.install(fake)

    # Override POSITION reads to return our canned landed position
    def pos_handler(pkt):
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.GETCMD_RESP,
            sub_command=pkt.sub_command,
            cmd_val=pack_float32(landed_position_normalized),
        )
    fake.on_get(addr, GeminiSubCommands.POSITION, pos_handler)
    return sim


def test_jog_succeeds_within_tolerance(engine, fake):
    z_addr = axis_address(Axis.Z)
    _install_z_sim_that_lands_at(fake, z_addr, landed_position_normalized=0.28,
                                         complete_ms=80)
    z_params = ParameterAccess(engine, z_addr)

    def read_pos(e, a):
        return e.get_float(a, GeminiSubCommands.POSITION)

    final = jog(
        engine, z_addr, z_params,
        JogParams(
            axis_name="Z", target_position=0.30, tolerance=0.05,
            peak_current_amps=0.4,
            velocity_mm=100.0, acceleration_mm=100.0,
            velocity_limit=300.0, acceleration_limit=3000.0,
        ),
        read_position=read_pos,
        timeout_ms=2000,
        settle_ms=0,
    )
    assert abs(final - 0.28) < 1e-5


def test_jog_does_not_write_to_param_db(engine, fake):
    """A Tips-On jog must emit ZERO param-DB writes to the Z axis. Writing
    POS_ERR_LIMIT=0 or I2T_PEAK_CURRENT into a force move trips the firmware's
    pos-error guard (RESERVED_EVENT_ERROR) as soon as the motor starts to
    stall against resistance.
    """
    z_addr = axis_address(Axis.Z)
    _install_z_sim_that_lands_at(fake, z_addr, landed_position_normalized=0.28,
                                   complete_ms=80)
    z_params = ParameterAccess(engine, z_addr)

    def read_pos(e, a):
        return e.get_float(a, GeminiSubCommands.POSITION)

    jog(
        engine, z_addr, z_params,
        JogParams(
            axis_name="Z", target_position=0.30, tolerance=0.05,
            peak_current_amps=0.4,
            velocity_mm=100.0, acceleration_mm=100.0,
            velocity_limit=300.0, acceleration_limit=3000.0,
        ),
        read_position=read_pos,
        timeout_ms=2000,
        settle_ms=0,
    )
    param_db_subs = {
        CommonSubCommands.PARAM_DB_WR_PTR,
        CommonSubCommands.PARAM_DB_RD_PTR,
        CommonSubCommands.PARAM_DB_VALUE,
        CommonSubCommands.PARAM_DB_APPLY,
    }
    z_param_writes = [
        p for p in fake.received_packets
        if p.dest == z_addr and p.sub_command in param_db_subs
    ]
    assert z_param_writes == [], (
        f"jog unexpectedly wrote to Z param DB: {z_param_writes!r}. "
        f"A TipsOn jog must not write to the param DB."
    )


def test_jog_raises_if_target_exceeded(engine, fake):
    z_addr = axis_address(Axis.Z)
    # Land AT farthest → "exceeded destination"
    _install_z_sim_that_lands_at(fake, z_addr, landed_position_normalized=0.36,
                                   complete_ms=80)
    z_params = ParameterAccess(engine, z_addr)

    def read_pos(e, a):
        return e.get_float(a, GeminiSubCommands.POSITION)

    with pytest.raises(BravoError) as exc_info:
        jog(
            engine, z_addr, z_params,
            JogParams(
                axis_name="Z", target_position=0.30, tolerance=0.05,
                peak_current_amps=0.4,
                velocity_mm=100.0, acceleration_mm=100.0,
                velocity_limit=300.0, acceleration_limit=3000.0,
            ),
            read_position=read_pos,
            timeout_ms=2000,
            settle_ms=0,
        )
    assert exc_info.value.error_type == ErrorType.EXCEEDED_DEST


def test_jog_near_target_landing_is_not_exceeded(engine, fake):
    """Regression: earlier the ``0.05`` literal in the exceed check was
    interpreted as normalized units (≈12.5mm of a 250mm Z), so a perfectly
    good landing ~0.125mm short of farthest falsely raised EXCEEDED_DEST.

    With a hardware-range-scaled epsilon (0.05 mm / 250 mm = 0.0002), a
    landing within the tolerance window but not right at farthest must
    return cleanly — this is the common Tips-On "tips seated correctly"
    case.
    """
    z_addr = axis_address(Axis.Z)
    # Target=0.6552, tolerance=0.02, farthest=0.6752, epsilon_norm≈0.0002.
    # Land at 0.6547 (0.125mm short of farthest on a 250mm Z) — must NOT trip.
    _install_z_sim_that_lands_at(fake, z_addr, landed_position_normalized=0.6547,
                                   complete_ms=80)
    z_params = ParameterAccess(engine, z_addr)

    def read_pos(e, a):
        return e.get_float(a, GeminiSubCommands.POSITION)

    final = jog(
        engine, z_addr, z_params,
        JogParams(
            axis_name="Z", target_position=0.6552, tolerance=0.02,
            peak_current_amps=0.8,
            velocity_mm=25.0, acceleration_mm=250.0,
            velocity_limit=125.0, acceleration_limit=2000.0,
            exceed_epsilon=0.0002,  # 0.05 mm / 250 mm Z range
        ),
        read_position=read_pos,
        timeout_ms=2000,
        settle_ms=0,
    )
    assert abs(final - 0.6547) < 1e-5


def test_jog_raises_if_short_of_target(engine, fake):
    z_addr = axis_address(Axis.Z)
    # Land well below target - tolerance
    _install_z_sim_that_lands_at(fake, z_addr, landed_position_normalized=0.10,
                                   complete_ms=80)
    z_params = ParameterAccess(engine, z_addr)

    def read_pos(e, a):
        return e.get_float(a, GeminiSubCommands.POSITION)

    with pytest.raises(BravoError) as exc_info:
        jog(
            engine, z_addr, z_params,
            JogParams(
                axis_name="Z", target_position=0.30, tolerance=0.05,
                peak_current_amps=0.4,
                velocity_mm=100.0, acceleration_mm=100.0,
                velocity_limit=300.0, acceleration_limit=3000.0,
            ),
            read_position=read_pos,
            timeout_ms=2000,
            settle_ms=0,
        )
    assert exc_info.value.error_type == ErrorType.UNABLE_TO_REACH_DEST


def test_jog_rejects_unsupported_axis():
    params = ParameterAccess(None, InstructionAddress(4))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        jog(
            None,  # type: ignore[arg-type]
            InstructionAddress(4),
            params,
            JogParams(
                axis_name="X",  # ← invalid
                target_position=0.0, tolerance=0.0,
                peak_current_amps=0.1,
                velocity_mm=100.0, acceleration_mm=100.0,
                velocity_limit=300.0, acceleration_limit=3000.0,
            ),
            read_position=lambda e, a: 0.0,
        )
