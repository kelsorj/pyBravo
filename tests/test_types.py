"""Tests for core types and constants."""

from pybravo.types import (
    LT_TIP_CURRENT_TABLE,
    X_TO_X_DISTANCE,
    Y_TO_Y_DISTANCE,
    Axis,
    DeviceStateFlag,
    HeadType,
    LightColor,
    interpolate_tip_current,
    location_to_row_col,
    row_col_to_location,
)


def test_axis_values():
    assert Axis.X == 0
    assert Axis.Y == 1
    assert Axis.Z == 2
    assert Axis.W == 3
    assert Axis.G == 4
    assert Axis.Zg == 5


def test_axis_labels():
    assert Axis.X.label == "X-axis"
    assert Axis.Zg.label == "Zg-axis"


def test_location_to_row_col():
    assert location_to_row_col(1) == (0, 0)
    assert location_to_row_col(2) == (0, 1)
    assert location_to_row_col(3) == (0, 2)
    assert location_to_row_col(5) == (1, 1)
    assert location_to_row_col(9) == (2, 2)


def test_row_col_to_location():
    assert row_col_to_location(0, 0) == 1
    assert row_col_to_location(1, 1) == 5
    assert row_col_to_location(2, 2) == 9


def test_location_roundtrip():
    for loc in range(1, 10):
        r, c = location_to_row_col(loc)
        assert row_col_to_location(r, c) == loc


def test_head_type_channels():
    assert HeadType.HT_96_D_70.channels == 96
    assert HeadType.HT_384_D_70.channels == 384
    assert HeadType.HT_8_D_LT.channels == 8
    assert HeadType.HT_1536_PINTOOL.channels == 1536


def test_head_type_disposable():
    assert HeadType.HT_96_D_70.is_disposable
    assert not HeadType.HT_96_F_50.is_disposable
    assert HeadType.HT_96_F_50.is_fixed


def test_light_color_flags():
    combined = LightColor.RED | LightColor.GREEN
    assert LightColor.RED in combined
    assert LightColor.GREEN in combined
    assert LightColor.BLUE not in combined


def test_device_state_flags():
    state = DeviceStateFlag.ROBOT_DISABLE | DeviceStateFlag.GO_BUTTON
    assert DeviceStateFlag.ROBOT_DISABLE in state
    assert DeviceStateFlag.GO_BUTTON in state
    assert DeviceStateFlag.MOTOR_POWER not in state


def test_deck_spacing():
    assert X_TO_X_DISTANCE == 186.690
    assert Y_TO_Y_DISTANCE == 109.093


def test_interpolate_tip_current():
    assert interpolate_tip_current(LT_TIP_CURRENT_TABLE, 1) == 0.04
    assert interpolate_tip_current(LT_TIP_CURRENT_TABLE, 96) == 0.60
    mid = interpolate_tip_current(LT_TIP_CURRENT_TABLE, 4)
    assert 0.04 < mid < 0.07
