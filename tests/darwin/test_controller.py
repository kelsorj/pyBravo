"""Smoke tests for DarwinController against the fake server.

Verifies the controller wires up all the underlying Gemini primitives. These
are NOT substitutes for HITL testing — they just confirm the plumbing works.
"""

from __future__ import annotations

import pytest

from pybravo.controllers.base import AxisMoveInfo
from pybravo.darwin.controller import DarwinController
from pybravo.darwin.topology import axis_address
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    CommandTypes,
    CommonSubCommands,
    DarwinMasterNodeSubCommands,
    GeminiSubCommands,
    MotorState,
    ParamDBs,
)
from pybravo.protocol.gemini.instruction import pack_float32
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
def controller(fake):
    engine = GeminiEngine("127.0.0.1", port=fake.port)
    ctrl = DarwinController(engine=engine)
    ctrl.open_tcp("127.0.0.1")
    try:
        yield ctrl, fake
    finally:
        ctrl.close()


def _seed_motion_limits(fake: FakeGeminiServer, addr: InstructionAddress,
                         speed_frac: float = 0.2, accel_frac: float = 0.4) -> None:
    """Make SPEED and ACCELERATION reads return predictable values."""
    speed_bits = pack_float32(speed_frac)
    accel_bits = pack_float32(accel_frac)

    def get_value_handler_factory(v: int):
        def h(pkt: Packet) -> Packet:
            return Packet(
                src=pkt.dest, dest=pkt.src,
                cmd_type=CommandTypes.GETCMD_RESP,
                sub_command=pkt.sub_command, cmd_val=v,
            )
        return h
    state = {"ptr": -1}

    def rd_ptr_handler(pkt: Packet) -> Packet:
        state["ptr"] = pkt.cmd_val
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=0,
        )

    def value_handler(pkt: Packet) -> Packet:
        # Return different values based on the last RD_PTR
        if state["ptr"] == int(ParamDBs.SPEED):
            v = speed_bits
        elif state["ptr"] == int(ParamDBs.ACCELERATION):
            v = accel_bits
        else:
            v = 0
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.GETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=v,
        )

    fake.on_set(addr, CommonSubCommands.PARAM_DB_RD_PTR, rd_ptr_handler)
    fake.on_get(addr, CommonSubCommands.PARAM_DB_VALUE, value_handler)


# --- Basic lifecycle ------------------------------------------------------


def test_controller_connects_and_reports_connected(controller):
    ctrl, _ = controller
    assert ctrl.is_connected


def test_controller_requires_engine_or_address():
    with pytest.raises(ValueError):
        DarwinController()


# --- Ping / firmware ------------------------------------------------------


def test_ping_probes_master_node(controller):
    ctrl, fake = controller
    assert ctrl.ping() is True
    # Should have sent a GET to master (node 1) for SAFETY_STATUS
    assert any(
        p.dest.node_id == 1
        and p.sub_command == DarwinMasterNodeSubCommands.SAFETY_STATUS
        and p.cmd_type == CommandTypes.GETCMD
        for p in fake.received_packets
    )


def test_firmware_version_reads_four_nodes(controller):
    ctrl, fake = controller
    # Seed some non-zero firmware values
    fake.storage[(1, 0, CommonSubCommands.FW_VERSION)] = 0x01020005
    fake.storage[(4, 0, CommonSubCommands.FW_VERSION)] = 0x04000039
    fake.storage[(5, 0, CommonSubCommands.FW_VERSION)] = 0x04000040
    fake.storage[(6, 0, CommonSubCommands.FW_VERSION)] = 0x04000041
    fw = ctrl.get_firmware_version()
    assert fw.master == "1.2.5"
    assert "YX=4.0.57" in fw.sub1
    assert "ZW=4.0.64" in fw.sub1
    assert "GZg=4.0.65" in fw.sub2


# --- Lights ---------------------------------------------------------------


def test_clear_lights_sets_status_lights_to_zero(controller):
    ctrl, fake = controller
    ctrl.clear_lights()
    assert fake.storage[(1, 0, DarwinMasterNodeSubCommands.STATUS_LIGHTS)] == 0


def test_set_light_encodes_color_and_sends(controller):
    from pybravo.protocol.commands import LightCommandData
    ctrl, fake = controller
    # Color 1 = red, steady
    cmd = LightCommandData(light=1, period_ms=0, duty_cycle=1.0)
    ctrl.set_light(cmd)
    value = fake.storage[(1, 0, DarwinMasterNodeSubCommands.STATUS_LIGHTS)]
    # Red=100 in high byte → 0x64_00_00_00
    assert ((value >> 24) & 0xFF) == 100
    assert ((value >> 16) & 0xFF) == 0   # green
    assert ((value >> 8) & 0xFF) == 0    # blue


# --- Motor enable/disable -------------------------------------------------


def test_disable_motor_sends_motor_state_disable(controller):
    ctrl, fake = controller
    ctrl.disable_motor(Axis.X)
    x_addr = axis_address(Axis.X)
    set_state_pkts = [
        p for p in fake.received_packets
        if p.dest == x_addr and p.sub_command == GeminiSubCommands.MOTOR_STATE
        and p.cmd_type == CommandTypes.SETCMD
    ]
    assert set_state_pkts
    assert set_state_pkts[-1].cmd_val == int(MotorState.DISABLE)


def test_clear_go_button_sends_master_subcmd(controller):
    ctrl, fake = controller
    ctrl.clear_go_button()
    assert fake.storage[(1, 0, DarwinMasterNodeSubCommands.CLEAR_GO_BTN_LATCH)] == 1


# --- Position read (normalized → mm) --------------------------------------


def test_get_position_converts_normalized_to_mm(controller):
    ctrl, fake = controller
    # Hardware: [-118.375, 516.625], offset 0. Normalized 0.5 → center.
    fake.storage[(4, 1, GeminiSubCommands.POSITION)] = pack_float32(0.5)
    pos_mm = ctrl.get_position(Axis.X)
    # 0.5 * (516.625 - (-118.375)) + 0 + (-118.375) = 317.5 - 118.375 = 199.125
    assert abs(pos_mm - 199.125) < 1e-3


def test_get_park_position_returns_calibration_default(controller):
    ctrl, _ = controller
    assert ctrl.get_park_position(Axis.X) == 193.04  # from DEFAULT_CALIBRATION


# --- move (end-to-end through fake) ---------------------------------------


def test_move_single_axis_end_to_end(controller):
    ctrl, fake = controller
    # Seed motion limits so the mm→% conversion has nonzero denominators
    x_addr = axis_address(Axis.X)
    _seed_motion_limits(fake, x_addr, speed_frac=0.3, accel_frac=0.4)

    # Install the motion sim BEFORE the move
    sim = _AxisMotionSim(x_addr, complete_ms=80)
    sim.install(fake)

    # Also seed POSITION for the direction-determination GET
    fake.storage[(4, 1, GeminiSubCommands.POSITION)] = pack_float32(0.1)

    ctrl.move([AxisMoveInfo(axis=Axis.X, position=200.0, velocity=100.0,
                             acceleration=500.0, absolute=True)])
    assert sim.state == MotorState.READY
    # One multipacket got sent (the instruction load for X)
    assert len(fake.received_multipackets) >= 1
