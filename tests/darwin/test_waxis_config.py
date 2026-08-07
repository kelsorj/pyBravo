"""Tests for per-head-type W-axis config (hardware range + µL/mm factor)."""

from __future__ import annotations

import pytest

from pybravo.darwin.waxis_config import (
    HEAD_CONFIGS,
    config_for_head,
    mm_to_ul,
    ul_to_mm,
)
from pybravo.types import HeadType

# --- Factor lookups ------------------------------------------------------


@pytest.mark.parametrize(
    "head_type, expected_factor",
    [
        (HeadType.HT_96_ASSAYMAP, 385.0 / 1600.0),
        (HeadType.HT_8_D_LT, 448.0 / 2000.0),
        (HeadType.HT_96_D_70, 448.0 / 2000.0),
        (HeadType.HT_96_D_70_S2, 448.0 / 2000.0),
        (HeadType.HT_96_D_200, 448.0 / 2000.0),
        (HeadType.HT_96_D_200_S2, 448.0 / 2000.0),
        (HeadType.HT_16_D_ST, 1692.0 / 2000.0),
        (HeadType.HT_384_D_70, 1692.0 / 2000.0),
        (HeadType.HT_384_D_70_S2, 1692.0 / 2000.0),
        (HeadType.HT_384_F_50, 1692.0 / 2000.0),
        (HeadType.HT_8_F_50, 1692.0 / 2000.0),
        (HeadType.HT_96_F_50, 1236.0 / 2000.0),
        (HeadType.HT_96_F_200, 487.0 / 2000.0),
    ],
)
def test_ul_to_mm_factor_matches_bridge_table(head_type, expected_factor):
    cfg = config_for_head(head_type)
    assert cfg is not None
    assert abs(cfg.ul_to_mm_factor - expected_factor) < 1e-9


def test_ul_to_mm_and_back_roundtrip():
    for head in [HeadType.HT_96_ASSAYMAP, HeadType.HT_96_D_70, HeadType.HT_384_F_50]:
        mm = ul_to_mm(42.0, head)
        ul = mm_to_ul(mm, head)
        assert abs(ul - 42.0) < 1e-5


def test_ul_to_mm_unknown_head_raises():
    with pytest.raises(ValueError):
        ul_to_mm(10.0, HeadType.HT_UNKNOWN)


# --- Hardware ranges (cross-check against Configure-WAxis:238-317) -------


@pytest.mark.parametrize(
    "head_type, hw_min, hw_max",
    [
        (HeadType.HT_96_ASSAYMAP, -19.921875, 80.078125),
        (HeadType.HT_8_D_LT, -16.48, 63.52),
        (HeadType.HT_96_D_70, -16.48, 63.52),
        (HeadType.HT_16_D_ST, -14.197, 65.803),
        (HeadType.HT_384_D_70, -14.197, 65.803),
        (HeadType.HT_96_F_50, -24.55, 55.45),
        (HeadType.HT_96_F_200, -13.98, 61.02),
    ],
)
def test_hardware_ranges_match_bridge(head_type, hw_min, hw_max):
    cfg = config_for_head(head_type)
    assert cfg.hardware_min == hw_min
    assert cfg.hardware_max == hw_max


def test_every_head_config_uses_40s_homing_timeout():
    # Firmware-defined value.
    for cfg in HEAD_CONFIGS.values():
        assert cfg.homing_timeout_ms == 40_000


def test_calibration_object_respects_offset():
    cfg = config_for_head(HeadType.HT_96_D_70)
    calib = cfg.calibration(calibration_offset=0.5)
    assert calib.calibration_offset == 0.5
    assert calib.hardware_min == -16.48


# --- Controller integration ------------------------------------------------


def test_set_head_type_updates_waxis_calibration():
    """DarwinController.set_head_type should replace the W-axis calibration
    and invalidate cached motion limits."""
    from pybravo.darwin.controller import DarwinController
    from pybravo.types import Axis

    c = DarwinController(address="127.0.0.1")
    pre = c._axes[Axis.W].calibration
    c.set_head_type(HeadType.HT_96_ASSAYMAP)
    post = c._axes[Axis.W].calibration
    assert post.hardware_min == -19.921875
    assert post.hardware_max == 80.078125
    assert post != pre
    assert c._axes[Axis.W].limits is None


def test_set_head_type_on_unknown_preserves_placeholder():
    from pybravo.darwin.controller import DarwinController
    from pybravo.types import Axis

    c = DarwinController(address="127.0.0.1")
    pre = c._axes[Axis.W].calibration
    c.set_head_type(HeadType.HT_UNKNOWN)
    # HT_UNKNOWN has no config → calibration unchanged
    assert c._axes[Axis.W].calibration == pre


# ---------------------------------------------------------------------------
# Regression: per-head software limits are applied by calibration()
# ---------------------------------------------------------------------------
#
# Previously `WAxisHeadConfig.calibration()` dropped the configured
# software_min/software_max — so `AxisCalibration.validate_target` fell
# back to the hardware-margin default (hw_min + 0.07), ~5 mm looser than
# the real per-head safety envelope. That let a profile with
# `tips_off_w_position = -11.0` reach the wire even though the ST384
# head's software_min is -9.31446 mm. The firmware's own software-limit
# enforcement then clamped to -9, which was still past the operator-safe
# eject depth.


def test_calibration_applies_software_limits_from_head_config():
    """validate_target must reject a target outside the head's
    software_min / software_max window."""
    cfg = config_for_head(HeadType.HT_384_D_70)
    cal = cfg.calibration()
    assert cal.effective_software_min == cfg.software_min
    assert cal.effective_software_max == cfg.software_max
    # In-range target is fine
    cal.validate_target(-7.5, "W")
    # Past software_min should raise (the bug that let tips_off_w_position=-11
    # get sent to the firmware).
    with pytest.raises(ValueError):
        cal.validate_target(-11.0, "W")
    # Past software_max also raises
    with pytest.raises(ValueError):
        cal.validate_target(100.0, "W")


@pytest.mark.parametrize("head_type, expected_min, expected_max", [
    (HeadType.HT_8_D_LT, -9.1862, 56.226),
    (HeadType.HT_96_D_70, -9.1862, 56.226),
    (HeadType.HT_96_D_70_S2, -9.1862, 56.226),
    (HeadType.HT_96_D_200, -9.1862, 56.226),
    (HeadType.HT_96_D_200_S2, -9.1862, 56.226),
    (HeadType.HT_16_D_ST, -9.31446, 60.92),
    (HeadType.HT_384_D_70, -9.31446, 60.92),
    (HeadType.HT_384_D_70_S2, -9.31446, 60.92),
    (HeadType.HT_384_F_50, -9.31446, 60.92),
    (HeadType.HT_8_F_50, -9.31446, 60.92),
    (HeadType.HT_96_ASSAYMAP, -0.0024, 60.15865),
    (HeadType.HT_96_F_50, -0.00618, 30.90618),
    (HeadType.HT_96_F_200, -9.1862, 56.226),
])
def test_software_limits_match_bridge_configure_waxis(head_type, expected_min, expected_max):
    """Per-head software envelope must match Configure-WAxis
    These are the firmware-enforced limits
    the bridge writes at connect time."""
    cfg = config_for_head(head_type)
    assert cfg is not None
    assert abs(cfg.software_min - expected_min) < 1e-4, (
        f"{head_type.name}: software_min={cfg.software_min}, "
        f"expected {expected_min}"
    )
    assert abs(cfg.software_max - expected_max) < 1e-4, (
        f"{head_type.name}: software_max={cfg.software_max}, "
        f"expected {expected_max}"
    )
