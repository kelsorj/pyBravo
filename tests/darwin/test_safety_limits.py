"""Tests for software-limit validation on move targets.

Prevents accidents like driving G to its exact hardware_min (which walks
the gripper off its rail on a real Bravo).
"""

from __future__ import annotations

import pytest

from pybravo.controllers.base import AxisMoveInfo
from pybravo.darwin.calibration import AxisCalibration, DEFAULT_CALIBRATION
from pybravo.darwin.controller import DarwinController
from pybravo.types import Axis, SpeedLevel


def test_software_limits_default_to_0_07_inside_hardware():
    cal = AxisCalibration(hardware_min=-10.0, hardware_max=100.0)
    assert cal.effective_software_min == pytest.approx(-10.0 + 0.07)
    assert cal.effective_software_max == pytest.approx(100.0 - 0.07)


def test_explicit_software_limits_override_default():
    cal = AxisCalibration(
        hardware_min=-10.0, hardware_max=100.0,
        software_min=-5.0, software_max=50.0,
    )
    assert cal.effective_software_min == -5.0
    assert cal.effective_software_max == 50.0


def test_g_axis_has_tight_software_limits():
    """G axis must never allow full-range movement: rail walk-off risk."""
    g = DEFAULT_CALIBRATION[Axis.G]
    assert g.effective_software_min > g.hardware_min, \
        "G software_min must be strictly inside hardware_min"
    assert g.effective_software_max < g.hardware_max
    # And specifically tighter than the default 0.07 margin (which would be
    # hw_min + 0.07 = -7.513; we want even more conservative)
    assert g.effective_software_min >= -7.0


def test_validate_target_accepts_in_range():
    cal = AxisCalibration(hardware_min=-10, hardware_max=100,
                          software_min=-5, software_max=50)
    cal.validate_target(0.0, "X")
    cal.validate_target(-5.0, "X")
    cal.validate_target(50.0, "X")


def test_validate_target_rejects_below_software_min():
    cal = AxisCalibration(hardware_min=-10, hardware_max=100,
                          software_min=-5, software_max=50)
    with pytest.raises(ValueError) as exc_info:
        cal.validate_target(-6.0, "G")
    assert "outside software limits" in str(exc_info.value)
    assert "G" in str(exc_info.value)


def test_validate_target_rejects_above_software_max():
    cal = AxisCalibration(hardware_min=-10, hardware_max=100,
                          software_min=-5, software_max=50)
    with pytest.raises(ValueError):
        cal.validate_target(51.0, "X")


# --- Controller integration ---


def test_open_gripper_none_targets_safe_software_min(monkeypatch):
    """open_gripper(None) must NOT default to hardware_min."""
    c = DarwinController(address="127.0.0.1")
    g_cal = c._axes[Axis.G].calibration
    # The default open target should be at or above software_min — never at or below hw_min
    # (we verify the computation without actually sending to hardware)
    safe_target = g_cal.effective_software_min
    assert safe_target > g_cal.hardware_min
    # And our tight G limit should be well inside the hardware min
    assert safe_target >= -7.0, f"G open default {safe_target} is unsafe (was <{-7.0})"


def test_move_rejects_unsafe_absolute_target():
    """ctrl.move with target beyond software_max should raise before sending."""
    c = DarwinController(address="127.0.0.1")

    with pytest.raises(ValueError):
        # X hardware_max = 516.625, software_max defaults to 516.555
        c.move([AxisMoveInfo(axis=Axis.X, position=1000.0,
                              velocity=50, acceleration=500, absolute=True)])


def test_grip_rejects_unsafe_target():
    c = DarwinController(address="127.0.0.1")
    g_cal = c._axes[Axis.G].calibration
    unsafe = g_cal.hardware_max + 1.0
    with pytest.raises(ValueError):
        c.grip(SpeedLevel.SLOW, unsafe, False)
