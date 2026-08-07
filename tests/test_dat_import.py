"""Unit tests for the legacy Bravo2 .dat directory-tree importer.

Fixtures here are synthetic: small in-memory .dat trees and .reg exports built
from the format's documented shape, rather than a dump of a real instrument's
profile. Real exports carry a specific machine's calibration and teachpoint
data, which is lab-specific and not ours to publish. Synthetic fixtures
exercise the same parser paths and make the expected values readable inline.
"""

from __future__ import annotations

import pytest

from pybravo.profile.reg_import import (
    _dat_path_to_section,
    dat_tree_to_profile,
    dat_tree_to_reg,
    reg_to_profile,
)
from pybravo.types import HeadType

# A minimal but structurally complete .dat tree: profile root, one axis, the
# gripper subkey, and one teachpoint. Values mirror the format's conventions —
# floats are quoted strings, integers are dword-encoded hex.
DAT_TREE: dict[str, str] = {
    "96LT/96LT.dat": (
        '"Approach height"="10.000000"\n'
        '"W-axis tips-off position"="-35.000000"\n'
        '"Z-axis tips-off offset"="15.000000"\n'
        '"Safe location"=dword:00000005\n'
        '"Head type"=dword:00000003\n'
        '"Default tip"=dword:000000fa\n'
    ),
    "96LT/Axes/X/X.dat": (
        '"Ticks per engineering unit"="314.960000"\n'
        '"Min range"="0.000000"\n'
        '"Max range"="390.000000"\n'
    ),
    "96LT/Gripper settings/Gripper settings.dat": (
        '"Grip current"="0.150000"\n'
        '"Lid Grip current"="0.055000"\n'
    ),
    "96LT/Teachpoints/Location 1/Location 1.dat": (
        '"X"="100.000000"\n'
        '"Y"="50.000000"\n'
        '"Z"="20.000000"\n'
    ),
}

# A .reg export exercising the other known head-type value: 1 → HT_384_D_70
# with a 30 uL default tip.
REG_384_EXPORT = (
    "Windows Registry Editor Version 5.00\r\n"
    "\r\n"
    r"[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Velocity11\Bravo2\Profiles\384]"
    "\r\n"
    '"Head type"=dword:00000001\r\n'
    '"Default tip"=dword:0000001e\r\n'
)


def test_dat_path_to_section_root():
    assert _dat_path_to_section("96LT/96LT.dat", "96LT") == ""


def test_dat_path_to_section_nested():
    assert _dat_path_to_section("96LT/Axes/X/X.dat", "96LT") == "Axes\\X"
    assert (
        _dat_path_to_section("96LT/Teachpoints/Location 1/Location 1.dat", "96LT")
        == "Teachpoints\\Location 1"
    )


def test_dat_path_to_section_ignores_non_dat():
    assert _dat_path_to_section("96LT/README.txt", "96LT") is None


def test_dat_tree_to_reg_includes_all_sections():
    reg_text = dat_tree_to_reg("96LT", DAT_TREE)
    # Header is present and the root section comes first.
    assert reg_text.startswith("Windows Registry Editor Version 5.00")
    assert (
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Velocity11\\Bravo2\\Profiles\\96LT]"
        in reg_text
    )
    # Nested sub-keys are emitted.
    assert "Profiles\\96LT\\Axes\\X]" in reg_text
    assert "Profiles\\96LT\\Teachpoints\\Location 1]" in reg_text
    assert "Profiles\\96LT\\Gripper settings]" in reg_text


def test_dat_tree_to_profile_96lt_fixture():
    profile, warnings = dat_tree_to_profile("96LT", DAT_TREE)

    # Profile identity.
    assert profile.name == "96LT"

    # Root-level safety values pulled from 96LT/96LT.dat.
    assert profile.safety.approach_height == pytest.approx(10.0)
    assert profile.safety.tips_off_w_position == pytest.approx(-35.0)
    assert profile.safety.tips_off_z_offset == pytest.approx(15.0)
    assert profile.safety.safe_location == 5

    # Axis config pulled from 96LT/Axes/X/X.dat.
    assert "X" in profile.axes
    x_axis = profile.axes["X"]
    assert x_axis.ticks_per_eng_unit == pytest.approx(314.96)
    assert x_axis.range.max_pos == pytest.approx(390.0)

    # Gripper config pulled from 96LT/Gripper settings/Gripper settings.dat.
    assert profile.gripper.grip_current == pytest.approx(0.15)
    assert profile.gripper.lid_grip_current == pytest.approx(0.055)

    # Teachpoints pulled from 96LT/Teachpoints/Location */Location *.dat.
    assert profile.teachpoints is not None
    assert 1 in profile.teachpoints.locations

    # Vendor registry head-type 3 → HT_96_D_200 (96LT, 200 uL LT head).
    assert profile.head.head_type is HeadType.HT_96_D_200
    # Default tip 250 uL → lt_250ul in the long-tip option set.
    assert profile.head.default_tip_id == "lt_250ul"
    assert profile.head.teach_tip_id == "lt_250ul"
    assert profile.head.default_tip_capacity == pytest.approx(250.0)
    # Raw registry values are still stashed in extra for auditing.
    assert profile.extra["registry_head_type"] == 3
    assert profile.extra["registry_default_tip"] == 250
    # No head-type warning should fire for values we do map.
    assert not any("Head type" in w and "no pybravo HeadType mapping" in w for w in warnings)
    assert not any("Default tip" in w for w in warnings)


def test_reg_import_384_head_and_tip_mapping():
    """Head type 1 maps to HT_384_D_70 with a 30 uL default tip."""
    profile, warnings = reg_to_profile(REG_384_EXPORT)
    assert profile.head.head_type is HeadType.HT_384_D_70
    assert profile.head.default_tip_id == "st_30ul"
    assert profile.head.default_tip_capacity == pytest.approx(30.0)
    assert profile.extra["registry_head_type"] == 1
    assert profile.extra["registry_default_tip"] == 30
    assert not any("no pybravo HeadType mapping" in w for w in warnings)


def test_unknown_head_type_still_warns():
    """Values outside the mapping table should land in extra + warn."""
    synthetic = {
        "96LT/96LT.dat": '"Head type"=dword:000000ff\n"Default tip"=dword:0000001e\n',
    }
    profile, warnings = dat_tree_to_profile("96LT", synthetic)
    assert profile.extra["registry_head_type"] == 255
    assert any("no pybravo HeadType mapping" in w for w in warnings)


def test_dat_tree_to_profile_rejects_empty():
    with pytest.raises(ValueError):
        dat_tree_to_profile("96LT", {})


def test_dat_tree_to_profile_rejects_missing_name():
    with pytest.raises(ValueError):
        dat_tree_to_profile("", {"foo/foo.dat": '"x"="1"'})
