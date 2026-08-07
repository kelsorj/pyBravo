"""Teaching the gripper Y offset.

The gripper does not sit at the same Y as the pipette head, so every pick adds
a correction to the location's teachpoint Y:

    y = teachpoint_y + profile.gripper.y_offset + head_y_offset

``head_y_offset`` is a per-head constant (-2.25 mm for 384-class heads, 0
otherwise). Teaching measures the *total* offset from the teachpoint, so the
head constant must be removed before storing — otherwise pick/place adds it a
second time and the value drifts by 2.25 mm on every teach.

The give-away that this was hit before: the old UI hardcoded -2.67, which is
exactly -0.42 (a real stored offset) + -2.25 (the head constant).
"""

from __future__ import annotations

import pytest

from pybravo.bravo import Bravo
from pybravo.profile.profile import BravoProfile
from pybravo.state_machine.tasks import _gripper_head_offsets
from pybravo.types import Axis, HeadType

TEACHPOINT_Y = 9.68284878730774


def _bravo(head_type: HeadType, y_offset: float) -> Bravo:
    profile = BravoProfile.default()
    profile.head.head_type = head_type
    profile.gripper.y_offset = y_offset
    bravo = Bravo(profile=profile, mode="simulation")
    bravo.connect()
    bravo.teachpoints.set_teachpoint(3, Axis.Y, TEACHPOINT_Y)
    return bravo


def test_teaching_removes_the_head_constant(monkeypatch):
    """Storing the raw delta would double-count the head offset."""
    bravo = _bravo(HeadType.HT_384_D_70, -0.42)
    try:
        head_y = _gripper_head_offsets(HeadType.HT_384_D_70)[1]
        assert head_y == pytest.approx(-2.25)

        # Gripper sitting exactly where a pick would place it.
        current_y = TEACHPOINT_Y + (-0.42) + head_y
        monkeypatch.setattr(
            type(bravo), "get_position",
            lambda self, axis: current_y if axis is Axis.Y else 0.0,
        )

        res = bravo.teach_gripper_y_offset(3)

        assert res["measured_offset"] == pytest.approx(-2.67)
        assert res["head_y_offset"] == pytest.approx(-2.25)
        # The stored value excludes the head term.
        assert res["y_offset"] == pytest.approx(-0.42)
    finally:
        bravo.disconnect()


def test_teaching_at_the_current_grip_position_is_a_fixed_point(monkeypatch):
    """Teaching where the robot already thinks the grip point is changes nothing.

    This is the property that catches a double-count: if the head constant were
    mishandled, repeating this would walk the offset by 2.25 mm each time.
    """
    for head_type, start in ((HeadType.HT_384_D_70, -0.42),
                             (HeadType.HT_96_D_200, 1.30)):
        bravo = _bravo(head_type, start)
        try:
            head_y = _gripper_head_offsets(head_type)[1]
            offset = start
            for _ in range(3):
                current_y = TEACHPOINT_Y + offset + head_y
                monkeypatch.setattr(
                    type(bravo), "get_position",
                    lambda self, axis, _y=current_y: _y if axis is Axis.Y else 0.0,
                )
                offset = bravo.teach_gripper_y_offset(3)["y_offset"]
                assert offset == pytest.approx(start), (
                    f"{head_type.name}: offset drifted to {offset} from {start}"
                )
        finally:
            bravo.disconnect()


def test_a_real_correction_is_captured(monkeypatch):
    """Jogging 1.5 mm off the assumed grip point shifts the offset by 1.5 mm."""
    bravo = _bravo(HeadType.HT_384_D_70, -0.42)
    try:
        head_y = _gripper_head_offsets(HeadType.HT_384_D_70)[1]
        current_y = TEACHPOINT_Y + (-0.42) + head_y + 1.5
        monkeypatch.setattr(
            type(bravo), "get_position",
            lambda self, axis: current_y if axis is Axis.Y else 0.0,
        )

        res = bravo.teach_gripper_y_offset(3)

        assert res["y_offset"] == pytest.approx(-0.42 + 1.5)
        assert res["previous_y_offset"] == pytest.approx(-0.42)
    finally:
        bravo.disconnect()


def test_head_with_no_constant_stores_the_raw_delta(monkeypatch):
    """On a head with no offset the stored value is the plain measurement."""
    bravo = _bravo(HeadType.HT_96_D_200, 0.0)
    try:
        assert _gripper_head_offsets(HeadType.HT_96_D_200)[1] == pytest.approx(0.0)
        monkeypatch.setattr(
            type(bravo), "get_position",
            lambda self, axis: TEACHPOINT_Y + 0.75 if axis is Axis.Y else 0.0,
        )

        res = bravo.teach_gripper_y_offset(3)

        assert res["y_offset"] == pytest.approx(0.75)
    finally:
        bravo.disconnect()


def test_teaching_an_untaught_location_is_refused():
    """Without a teachpoint there is nothing to measure against."""
    from pybravo.deck.teachpoints import Teachpoints

    bravo = Bravo(profile=BravoProfile.default(), mode="simulation")
    bravo.connect()
    try:
        # get_teachpoint raises KeyError for an unset location; the operator
        # must get a readable message, not an opaque 500.
        bravo._teachpoints = Teachpoints()
        with pytest.raises(RuntimeError, match="no taught Y position"):
            bravo.teach_gripper_y_offset(5)
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_moving_to_an_empty_location_is_refused():
    """There is nothing to align the gripper against."""
    bravo = Bravo(profile=BravoProfile.default(), mode="simulation")
    bravo.connect()
    try:
        with pytest.raises(RuntimeError, match="No labware assigned to location 7"):
            await bravo.move_gripper_to_location(7)
    finally:
        bravo.disconnect()
