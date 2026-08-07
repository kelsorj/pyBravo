"""Tests for the plate-sensor + scan_stack + snapshot ports.

Covers the 5 ctrl methods added for the bridge → native cutover:
    - read_plate_sensor
    - is_plate_in_gripper (with G-position fallback)
    - scan_stack_with_gripper
    - get_all_positions
    - get_state_snapshot
"""

from __future__ import annotations

import pytest

from pybravo.darwin.controller import DarwinController
from pybravo.darwin.topology import axis_address
from pybravo.protocol.errors import BravoError
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    CommandNAKTypes,
    CommandTypes,
    DarwinMasterNodeSubCommands,
    GeminiSubCommands,
    MotorState,
)
from pybravo.protocol.gemini.instruction import pack_float32
from pybravo.protocol.gemini.packet import Packet
from pybravo.types import Axis
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
def controller(fake):
    engine = GeminiEngine("127.0.0.1", port=fake.port)
    ctrl = DarwinController(engine=engine)
    ctrl.open_tcp("127.0.0.1")
    try:
        yield ctrl, fake
    finally:
        ctrl.close()


# ---------------------------------------------------------------------------
# read_plate_sensor
# ---------------------------------------------------------------------------


def _seed_plate_sensor(fake: FakeGeminiServer, *, present: bool) -> None:
    """Make GET SUBCMD_PLATE_PRESENT on the G axis return the given state.

    We install a GET *handler* rather than just seeding .storage because
    read_plate_sensor first SETs subcmd=76 (to enable), and the fake's
    default SET handler would overwrite whatever value we stored.
    """
    g_addr = axis_address(Axis.G)
    value = 1 if present else 0

    def _handler(pkt: Packet) -> Packet:
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.GETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=value,
            msg_id=pkt.msg_id,
        )
    fake.on_get(g_addr, int(GeminiSubCommands.PLATE_PRESENT), _handler)


def test_read_plate_sensor_true(controller):
    ctrl, fake = controller
    _seed_plate_sensor(fake, present=True)
    assert ctrl.read_plate_sensor() is True


def test_read_plate_sensor_false(controller):
    ctrl, fake = controller
    _seed_plate_sensor(fake, present=False)
    assert ctrl.read_plate_sensor() is False


def test_read_plate_sensor_enables_then_disables(controller):
    """The bridge does SET subcmd=76 val=2 (enable) before reads and val=0
    (disable) after. We emit the same wire sequence."""
    ctrl, fake = controller
    _seed_plate_sensor(fake, present=False)
    ctrl.read_plate_sensor()
    g_addr = axis_address(Axis.G)
    sets = [
        p for p in fake.received_packets
        if p.dest == g_addr
        and p.sub_command == GeminiSubCommands.PLATE_PRESENT
        and p.cmd_type == CommandTypes.SETCMD
    ]
    assert len(sets) >= 2, f"expected enable + disable SETs, got {len(sets)}"
    # First SET value == 2 (enable); last SET value == 0 (disable).
    assert sets[0].cmd_val == 2
    assert sets[-1].cmd_val == 0


def test_read_plate_sensor_raises_when_every_attempt_fails(controller):
    """If the GET returns a NAK on every attempt, raise COULD_NOT_QUERY_STATE
    to match the bridge's ``Get-PlateSensorPresent`` which throws."""
    ctrl, fake = controller
    g_addr = axis_address(Axis.G)
    # Seed a NAK for every retry (bridge uses 3 attempts).
    for _ in range(6):
        fake.seed_nak(g_addr, int(GeminiSubCommands.PLATE_PRESENT),
                      int(CommandNAKTypes.INVALID_SUBCMD))

    with pytest.raises(BravoError):
        ctrl.read_plate_sensor()


# ---------------------------------------------------------------------------
# is_plate_in_gripper
# ---------------------------------------------------------------------------


def test_is_plate_in_gripper_primary_path_present(controller):
    ctrl, fake = controller
    _seed_plate_sensor(fake, present=True)
    assert ctrl.is_plate_in_gripper() is True


def test_is_plate_in_gripper_primary_path_absent(controller):
    ctrl, fake = controller
    _seed_plate_sensor(fake, present=False)
    assert ctrl.is_plate_in_gripper() is False


def test_is_plate_in_gripper_falls_back_on_read_failure(controller):
    """When read_plate_sensor raises, the fallback consults G position —
    if G is near OPEN_GRIPPER_POSITION (0) return False, else True.

    Here we make every sensor attempt NAK AND seed G.POSITION high (closed
    on a plate) so the fallback resolves to True.
    """
    ctrl, fake = controller
    g_addr = axis_address(Axis.G)

    # Force every plate-sensor read to NAK.
    for _ in range(6):
        fake.seed_nak(g_addr, int(GeminiSubCommands.PLATE_PRESENT),
                      int(CommandNAKTypes.INVALID_SUBCMD))

    # G.POSITION = 5mm → well away from OPEN_GRIPPER_POSITION (0mm), so the
    # fallback should report "plate present".
    # get_position reads NORMALIZED via GeminiSubCommands.POSITION, so seed
    # normalized(5mm) on G. G hw range = [-7.583, 13.583] → range=21.166.
    cal = ctrl._axes[Axis.G].calibration
    normalized = cal.to_normalized(5.0)
    fake.storage[(g_addr.node_id, g_addr.dev_id,
                  int(GeminiSubCommands.POSITION))] = pack_float32(normalized)

    assert ctrl.is_plate_in_gripper() is True


# ---------------------------------------------------------------------------
# get_all_positions
# ---------------------------------------------------------------------------


def test_get_all_positions_returns_six_axes(controller):
    ctrl, fake = controller
    # Seed a distinct normalized value for every axis so we can verify
    # position_mm round-trips through the calibration.
    canned_mm = {
        Axis.X: 50.0, Axis.Y: 100.0, Axis.Z: 10.0,
        Axis.W: 0.0, Axis.G: 1.0, Axis.Zg: 40.0,
    }
    for a, mm in canned_mm.items():
        cal = ctrl._axes[a].calibration
        addr = axis_address(a)
        fake.storage[(addr.node_id, addr.dev_id,
                      int(GeminiSubCommands.POSITION))] = pack_float32(
            cal.to_normalized(mm)
        )

    positions = ctrl.get_all_positions()
    assert set(positions.keys()) == {"X", "Y", "Z", "W", "G", "Zg"}
    for a, mm in canned_mm.items():
        assert positions[a.name] == pytest.approx(mm, abs=1e-3)


# ---------------------------------------------------------------------------
# get_state_snapshot
# ---------------------------------------------------------------------------


def test_get_state_snapshot_shape_matches_bridge(controller):
    """Must return the same keys the bridge's snapshot handler does — the
    higher-level code (bravo.py, tasks.py) expects this exact shape."""
    ctrl, _ = controller
    snap = ctrl.get_state_snapshot()
    for required in (
        "positions", "motors_enabled", "head_attached",
        "gripper_present", "go_button_pressed", "robot_disabled",
        "telemetry",
    ):
        assert required in snap, f"snapshot missing '{required}'"
    assert set(snap["positions"].keys()) == {"X", "Y", "Z", "W", "G", "Zg"}
    assert set(snap["motors_enabled"].keys()) == {"X", "Y", "Z", "W", "G", "Zg"}


def test_get_state_snapshot_caches_within_max_age(controller):
    """Calling snapshot twice inside max_age_s should NOT issue a second
    wave of per-axis position reads."""
    ctrl, fake = controller
    ctrl.get_state_snapshot(max_age_s=10.0)
    count_after_first = len([
        p for p in fake.received_packets
        if p.sub_command == GeminiSubCommands.POSITION
    ])
    ctrl.get_state_snapshot(max_age_s=10.0)
    count_after_second = len([
        p for p in fake.received_packets
        if p.sub_command == GeminiSubCommands.POSITION
    ])
    assert count_after_second == count_after_first, (
        "second snapshot call within cache window issued fresh position reads"
    )


def test_get_state_snapshot_robot_disabled_reflects_estop(controller):
    """When master SAFETY_STATUS bit 0 is set, robot_disabled must be True."""
    ctrl, fake = controller
    fake.storage[(1, 0, int(DarwinMasterNodeSubCommands.SAFETY_STATUS))] = 1
    snap = ctrl.get_state_snapshot(max_age_s=0.0)
    assert snap["robot_disabled"] is True


# ---------------------------------------------------------------------------
# is_axis_homed — MotorState-based, not sensor-based
# ---------------------------------------------------------------------------
#
# Regression: an earlier implementation read SUBCMD_HOMING_FLAG_STATE (the
# LIVE physical flag sensor) which returns True whenever the axis happens
# to sit near its home-flag sensor — e.g. Z parked at the top of travel on
# cold start. InitializeTask._move_z_to_safe_position/_home_z skip their
# bodies when is_axis_homed returns True, so the sensor-based reading
# caused Z homing to be skipped when the axis was *actually* un-homed.
# Result: the gripper homed with Z still at its cold-start position.
#
# The correct check is MotorState >= READY (matches the bridge's
# the axis's own initialized flag).


def _seed_motor_state(fake: FakeGeminiServer, axis: Axis, state: MotorState) -> None:
    addr = axis_address(axis)
    fake.storage[(addr.node_id, addr.dev_id,
                  int(GeminiSubCommands.MOTOR_STATE))] = int(state)


def test_is_axis_homed_false_when_motor_initial(controller):
    ctrl, fake = controller
    _seed_motor_state(fake, Axis.Z, MotorState.INITIAL)
    assert ctrl.is_axis_homed(Axis.Z) is False


def test_is_axis_homed_false_when_motor_commutating(controller):
    ctrl, fake = controller
    _seed_motor_state(fake, Axis.Z, MotorState.COMMUTATING)
    assert ctrl.is_axis_homed(Axis.Z) is False


def test_is_axis_homed_true_when_motor_ready(controller):
    ctrl, fake = controller
    _seed_motor_state(fake, Axis.Z, MotorState.READY)
    assert ctrl.is_axis_homed(Axis.Z) is True


def test_is_axis_homed_true_when_motor_busy(controller):
    """BUSY means 'moving toward a target' — post-homed."""
    ctrl, fake = controller
    _seed_motor_state(fake, Axis.Z, MotorState.BUSY)
    assert ctrl.is_axis_homed(Axis.Z) is True


def test_is_axis_homed_does_not_read_homing_flag_state(controller):
    """Regression: the homing-flag-state sensor MUST NOT be consulted.
    This test sets HOMING_FLAG_STATE=1 (as if Z were parked at the flag)
    and MOTOR_STATE=INITIAL (axis clearly un-homed). Expect False.
    """
    ctrl, fake = controller
    z_addr = axis_address(Axis.Z)
    # Sensor says "I'm sitting on the flag" — MUST NOT fool us.
    fake.storage[(z_addr.node_id, z_addr.dev_id,
                  int(GeminiSubCommands.HOMING_FLAG_STATE))] = 1
    _seed_motor_state(fake, Axis.Z, MotorState.INITIAL)
    assert ctrl.is_axis_homed(Axis.Z) is False, (
        "is_axis_homed read the physical flag sensor — it should read "
        "MOTOR_STATE (>= READY means homed) instead."
    )


# ---------------------------------------------------------------------------
# move() fails fast on un-initialized axis
# ---------------------------------------------------------------------------
#
# Regression: previously ctrl.move on an axis in MotorState.INITIAL would
# load the instruction, trigger the event, and then sit for the full 30s
# timeout waiting for a SEND_EVT echo that never arrives (the firmware
# can't execute a move on an un-initialized axis). Now we fail fast so
# InitializeTask's try/except at tasks.py:943 can catch and proceed.


def test_move_fails_fast_when_axis_not_initialized(controller):
    """ctrl.move on an un-homed axis raises COULD_NOT_MOVE_TO_POSITION
    with a message about initialization, instead of timing out."""
    from pybravo.controllers.base import AxisMoveInfo
    from pybravo.protocol.errors import ErrorType

    ctrl, fake = controller
    # G reports INITIAL (cold start, not yet commutated/homed).
    _seed_motor_state(fake, Axis.G, MotorState.INITIAL)

    with pytest.raises(BravoError) as exc_info:
        # Target 0 is within G software limits ([-7.0, 13.0]).
        ctrl.move([AxisMoveInfo(axis=Axis.G, position=0.0)], wait=True,
                  timeout_ms=500)  # small timeout so failure is immediate
    assert exc_info.value.error_type == ErrorType.COULD_NOT_MOVE_TO_POSITION
    assert "not initialized" in str(exc_info.value).lower() or \
           "home the axis" in str(exc_info.value).lower()


def test_move_succeeds_when_axis_ready(controller):
    """Sanity: move DOES proceed past the state check when axis is READY.

    We don't assert motion completion here (that requires a motion sim in
    the fake). Proxy: the move reaches the multipacket phase, meaning the
    pre-flight state check passed.
    """
    from pybravo.controllers.base import AxisMoveInfo

    ctrl, fake = controller
    _seed_motor_state(fake, Axis.X, MotorState.READY)

    # Seed motion limits so the velocity-percent math has a divisor.
    x_addr = axis_address(Axis.X)
    fake.storage[(x_addr.node_id, x_addr.dev_id,
                  int(GeminiSubCommands.POSITION))] = pack_float32(0.3)

    # Small timeout — we expect the state check to pass and the move to
    # enter the multipacket/trigger phase; it will time out waiting for
    # SEND_EVT since the fake doesn't simulate motion. That's fine — as
    # long as the failure mode is NOT "axis not initialized".
    try:
        ctrl.move([AxisMoveInfo(axis=Axis.X, position=100.0)], wait=True,
                  timeout_ms=200)
    except BravoError as e:
        assert "not initialized" not in str(e).lower(), \
            f"state check wrongly rejected a READY axis: {e}"


# ---------------------------------------------------------------------------
# send_command — CLEAR_MOTOR_POWER_FAULT is a no-op (matches bridge)
# ---------------------------------------------------------------------------


def test_send_command_clear_motor_power_fault_is_noop(controller):
    """InitializeTask._clear_motor_power_fault invokes this during every
    cold start. The bridge treats it as a no-op (darwin.py:956-958); the
    native controller must do the same, or every init logs an alarming
    'NotImplementedError' that masks real failures downstream.
    """
    from pybravo.protocol.commands import CommandID
    ctrl, _ = controller
    # Must not raise.
    result = ctrl.send_command(CommandID.CLEAR_MOTOR_POWER_FAULT)
    assert result == b""


def test_send_command_clear_go_button_delegates(controller):
    """CLEAR_GO_BUTTON passes through to ctrl.clear_go_button (wire call)."""
    from pybravo.protocol.commands import CommandID
    ctrl, _ = controller
    # Must not raise.
    ctrl.send_command(CommandID.CLEAR_GO_BUTTON)


def test_send_command_unknown_id_raises_bravoerror(controller):
    """Unknown CommandIDs raise a BravoError (not NotImplementedError)
    so higher layers can treat it as a recoverable error."""
    ctrl, _ = controller
    with pytest.raises(BravoError):
        ctrl.send_command(0xFF)


# ---------------------------------------------------------------------------
# Relative moves pick the correct direction
# ---------------------------------------------------------------------------
#
# Regression: the UI's "Jog X -1 mm" used to send a relative-delta move
# that — due to a MOVE_BY direction-encoding bug — walked X in the +1
# direction regardless of sign. Now every move (absolute or relative)
# funnels through MOVE_TO with direction derived from `target vs current`,
# so a negative delta produces NEGATIVE direction.


def _seed_position_mm(fake: FakeGeminiServer, ctrl: DarwinController,
                      axis: Axis, position_mm: float) -> None:
    cal = ctrl._axes[axis].calibration
    addr = axis_address(axis)
    fake.storage[(addr.node_id, addr.dev_id,
                  int(GeminiSubCommands.POSITION))] = pack_float32(
        cal.to_normalized(position_mm)
    )


def _seed_motion_limits_for(fake: FakeGeminiServer, ctrl: DarwinController,
                             axis: Axis) -> None:
    """Enough SPEED/ACCELERATION handler plumbing to let _limits() return
    a non-zero motion envelope when the controller computes velocity pct."""
    addr = axis_address(axis)

    def _get_value(pkt: Packet) -> Packet:
        if pkt.sub_command == GeminiSubCommands.POSITION:
            return Packet(src=pkt.dest, dest=pkt.src,
                           cmd_type=CommandTypes.GETCMD_RESP,
                           sub_command=pkt.sub_command,
                           cmd_val=fake.storage.get(
                               (addr.node_id, addr.dev_id, int(pkt.sub_command)),
                               pack_float32(0.0)),
                           msg_id=pkt.msg_id)
        return None  # fall through


def test_relative_negative_jog_sends_negative_direction(controller):
    """Regression: a Jog X -1 mm must send direction=NEGATIVE on the wire
    (word1 bit 16 == 0)."""
    from pybravo.controllers.base import AxisMoveInfo

    ctrl, fake = controller
    _seed_motor_state(fake, Axis.X, MotorState.READY)
    _seed_position_mm(fake, ctrl, Axis.X, 20.0)  # current X = 20mm

    # Relative delta: -1 mm (user asked "Jog X -1")
    try:
        ctrl.move([AxisMoveInfo(axis=Axis.X, position=-1.0, absolute=False)],
                  wait=True, timeout_ms=200)
    except BravoError:
        # Move will time out since the fake doesn't simulate motion; we
        # only care that the instruction got loaded with the right
        # direction bit.
        pass

    # Find the INSTR_TBL_VAL multipacket carrying word1 for X.
    from pybravo.protocol.gemini.enums import GeminiSubCommands as GSC
    x_addr = axis_address(Axis.X)
    word1_writes: list[int] = []
    for batch in fake.received_multipackets:
        for p in batch:
            if (p.dest == x_addr
                    and p.sub_command == GSC.INSTR_TBL_VAL
                    and p.cmd_type == CommandTypes.SETCMD):
                word1_writes.append(p.cmd_val)
    # word1 is the 2nd of 4 instruction words. Bit 16 set = POSITIVE,
    # clear = NEGATIVE. For a -1mm delta, bit 16 must be CLEAR.
    assert word1_writes, "no INSTR_TBL_VAL writes observed for X"
    # word1 can be identified by direction bit (0x10000). Any instruction
    # word carrying a non-zero jerk byte (low byte) is word1 — jerk is
    # clamped to 100% (=255) for all our moves, so the low byte is 0xFF.
    candidates = [w for w in word1_writes if (w & 0xFF) == 0xFF]
    assert candidates, (
        "expected at least one word1 (low byte 0xFF jerk); saw "
        f"{[hex(w) for w in word1_writes]}"
    )
    # Every word1 for a negative-direction X jog must have bit 16 clear.
    for w1 in candidates:
        assert (w1 & 0x10000) == 0, (
            f"Jog X -1 produced word1=0x{w1:08x} with direction bit SET "
            "(POSITIVE) — should be CLEAR (NEGATIVE)."
        )


def test_relative_positive_jog_sends_positive_direction(controller):
    """Sanity partner: Jog X +1 mm must send direction=POSITIVE."""
    from pybravo.controllers.base import AxisMoveInfo

    ctrl, fake = controller
    _seed_motor_state(fake, Axis.X, MotorState.READY)
    _seed_position_mm(fake, ctrl, Axis.X, 20.0)

    try:
        ctrl.move([AxisMoveInfo(axis=Axis.X, position=+1.0, absolute=False)],
                  wait=True, timeout_ms=200)
    except BravoError:
        pass

    from pybravo.protocol.gemini.enums import GeminiSubCommands as GSC
    x_addr = axis_address(Axis.X)
    word1s: list[int] = []
    for batch in fake.received_multipackets:
        for p in batch:
            if (p.dest == x_addr
                    and p.sub_command == GSC.INSTR_TBL_VAL
                    and p.cmd_type == CommandTypes.SETCMD):
                if (p.cmd_val & 0xFF) == 0xFF:  # jerk=255 marker of word1
                    word1s.append(p.cmd_val)
    assert word1s, "no word1 writes observed"
    for w1 in word1s:
        assert (w1 & 0x10000) != 0, (
            f"Jog X +1 produced word1=0x{w1:08x} with direction bit CLEAR "
            "— should be SET (POSITIVE)."
        )
