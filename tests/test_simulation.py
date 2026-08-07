"""Tests for the simulation controller."""

import pytest

from pybravo.controllers.base import AxisMoveInfo, JogParams
from pybravo.controllers.simulation import SimulationController
from pybravo.protocol.commands import LightCommandData
from pybravo.types import (
    Axis,
    DeviceStateFlag,
    GripperDetectionState,
    HeadType,
    LightColor,
    SpeedLevel,
)


@pytest.fixture
def sim():
    ctrl = SimulationController(head_type=HeadType.HT_96_D_70)
    ctrl.open_tcp("test")
    return ctrl


class TestConnection:
    def test_open_close(self, sim):
        assert sim.is_connected
        sim.close()
        assert not sim.is_connected

    def test_ping(self, sim):
        assert sim.ping()

    def test_firmware_version(self, sim):
        fw = sim.get_firmware_version()
        assert fw.master == "1.2.3"


class TestMotion:
    def test_absolute_move(self, sim):
        sim.move([AxisMoveInfo(axis=Axis.X, position=100.0)])
        assert sim.get_position(Axis.X) == 100.0

    def test_relative_move(self, sim):
        sim.move([AxisMoveInfo(axis=Axis.Y, position=50.0)])
        sim.move([AxisMoveInfo(axis=Axis.Y, position=25.0, absolute=False)])
        assert sim.get_position(Axis.Y) == 75.0

    def test_multi_axis_move(self, sim):
        sim.move([
            AxisMoveInfo(axis=Axis.X, position=100.0),
            AxisMoveInfo(axis=Axis.Y, position=200.0),
            AxisMoveInfo(axis=Axis.Z, position=50.0),
        ])
        assert sim.get_position(Axis.X) == 100.0
        assert sim.get_position(Axis.Y) == 200.0
        assert sim.get_position(Axis.Z) == 50.0

    def test_home_axes(self, sim):
        sim.move([AxisMoveInfo(axis=Axis.X, position=100.0)])
        sim.home_axes([Axis.X, Axis.Y, Axis.Z])
        assert sim.get_position(Axis.X) == 0.0
        assert sim.is_axis_homed(Axis.X)
        assert sim.is_axis_homed(Axis.Y)
        assert sim.is_axis_homed(Axis.Z)

    def test_jog(self, sim):
        params = JogParams(
            axis=Axis.Z, velocity=10.0, acceleration=100.0,
            max_position=5.0, tolerance=1.0, peak_current=0.5,
        )
        pos = sim.jog(params)
        assert pos == 5.0

    def test_get_all_positions(self, sim):
        positions = sim.get_all_positions()
        assert len(positions) == 6
        assert all(v == 0.0 for v in positions.values())


class TestDeviceState:
    def test_query_state_clean(self, sim):
        state = sim.query_state()
        assert state == DeviceStateFlag(0)

    def test_go_button(self, sim):
        assert not sim.is_go_button_pressed()
        sim.set_go_button(True)
        assert sim.is_go_button_pressed()
        sim.clear_go_button()
        assert not sim.is_go_button_pressed()


class TestHeadDetection:
    def test_read_adc(self, sim):
        adc = sim.read_head_adc()
        assert adc == 2745  # default for 96_D_70

    def test_detect_smart_head(self, sim):
        assert sim.detect_smart_head()

    def test_read_smart_head_type(self, sim):
        ht = sim.read_smart_head_type()
        assert ht == int(HeadType.HT_96_D_70)

    def test_change_head_type(self, sim):
        sim.set_head_type(HeadType.HT_384_D_70)
        assert sim.read_smart_head_type() == int(HeadType.HT_384_D_70)


class TestGripper:
    def test_detect_gripper(self, sim):
        assert sim.detect_gripper() == GripperDetectionState.DETECTED

    def test_grip_and_release(self, sim):
        assert not sim.is_plate_in_gripper()
        sim.grip(SpeedLevel.MED, 5.0)
        assert sim.is_plate_in_gripper()
        assert sim.get_position(Axis.G) == 5.0
        sim.open_gripper()
        assert not sim.is_plate_in_gripper()
        assert sim.get_position(Axis.G) == 0.0


class TestLights:
    def test_set_light(self, sim):
        cmd = LightCommandData(light=LightColor.GREEN, period_ms=0, duty_cycle=1.0)
        sim.set_light(cmd)
        assert sim._lights == cmd

    def test_clear_lights(self, sim):
        sim.set_light(LightCommandData(light=LightColor.RED))
        sim.clear_lights()
        assert sim._lights is None
