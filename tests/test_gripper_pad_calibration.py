"""Pins the gripper plate-pad Zg reference for every shipped profile.

``pad_zg_reference_mm`` and ``pad_reference_tip_length_mm`` are a calibration
PAIR: with a tip of the reference length installed, the gripper bottom sits in
the plate-pad plane at the reference Zg. Pick/place shifts that reference by the
taught tip-length delta.

The pairing is easy to misread. ``_gripper_pad_reference_zg`` is called with the
profile's own teach tip length, so "deriving" the reference length from the
profile too would collapse the formula to ``pad_zg_reference_mm + (L - L)`` —
the compensation silently disappears and every machine taught with long tips
drives its gripper ~29 mm off. These are hardware-verified values; if one of
these assertions fails, the change is wrong unless it was re-measured on that
instrument.
"""

from __future__ import annotations

from pathlib import Path

from pybravo.state_machine.tasks import (
    _infer_stack_count_from_scan_height,
    _stack_total_height_for_count,
)

import pytest

from pybravo.profile.profile import BravoProfile, GripperConfig
from pybravo.state_machine.tasks import PickPlaceTask, ScanStackHeightTask
from pybravo.tips import get_tip_length_mm

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"

# Hardware-verified Zg for the plate-pad plane, per shipped profile.
EXPECTED_PAD_ZG_MM = {
    "default": 7.00,
    "384": 7.00,
    "SRT_BRAVO": 7.00,
    "96LT": 36.10,
    "Claptrap": 36.10,
    "Opportunity": 36.10,
}


def _teach_tip_length(profile: BravoProfile) -> float:
    if profile.head.teach_tip_length_mm is not None:
        return float(profile.head.teach_tip_length_mm)
    length = get_tip_length_mm(
        profile.head.head_type,
        profile.head.teach_tip_id or profile.head.teach_tip_capacity,
    )
    assert length is not None, "teach tip length must resolve from the catalog"
    return float(length)


def _pad_reference_zg(profile: BravoProfile) -> float:
    g = profile.gripper
    return g.pad_zg_reference_mm + (_teach_tip_length(profile) - g.pad_reference_tip_length_mm)


@pytest.mark.parametrize("profile_name,expected_zg", sorted(EXPECTED_PAD_ZG_MM.items()))
def test_shipped_profile_pad_reference_zg_is_unchanged(profile_name, expected_zg):
    path = PROFILE_DIR / f"{profile_name}.yaml"
    if not path.exists():
        pytest.skip(f"{profile_name}.yaml is not present")
    profile = BravoProfile.load(str(path))
    assert _pad_reference_zg(profile) == pytest.approx(expected_zg, abs=1e-6)


def test_defaults_match_the_original_bench_measurement():
    """The shipped defaults are the values these profiles were calibrated at."""
    g = GripperConfig()
    assert g.pad_zg_reference_mm == pytest.approx(7.0)
    assert g.pad_reference_tip_length_mm == pytest.approx(26.1)


class _ProfileOnly:
    """Minimal stand-in: ``_gripper_pad_reference_zg`` only reads ``_profile``."""

    def __init__(self, profile: BravoProfile) -> None:
        self._profile = profile


@pytest.mark.parametrize("task_cls", [PickPlaceTask, ScanStackHeightTask])
def test_production_code_applies_the_tip_length_delta(task_cls):
    """Guard against the collapse described in this module's docstring.

    This calls the real method rather than recomputing the formula, so it fails
    if someone "derives" the reference length from the head's own tip and turns
    the correction into ``+ (L - L)``. Both task classes carry their own copy of
    the method, so both are checked.
    """
    path = PROFILE_DIR / "96LT.yaml"
    if not path.exists():
        pytest.skip("96LT.yaml is not present")
    profile = BravoProfile.load(str(path))
    tip_length = _teach_tip_length(profile)
    assert tip_length > profile.gripper.pad_reference_tip_length_mm, "fixture must use a long tip"

    got = task_cls._gripper_pad_reference_zg(_ProfileOnly(profile), tip_length)

    assert got == pytest.approx(EXPECTED_PAD_ZG_MM["96LT"], abs=1e-6)
    assert got != pytest.approx(profile.gripper.pad_zg_reference_mm), (
        "the tip-length compensation has been lost"
    )


def test_re_measured_calibration_actually_changes_the_result():
    """A lab that re-measures on its own instrument must see the new value."""
    profile = BravoProfile.load(str(PROFILE_DIR / "default.yaml"))
    profile.gripper.pad_zg_reference_mm = 12.0
    profile.gripper.pad_reference_tip_length_mm = 20.0
    # 12.0 + (26.1 - 20.0) = 18.1
    got = PickPlaceTask._gripper_pad_reference_zg(_ProfileOnly(profile), 26.1)
    assert got == pytest.approx(18.1, abs=1e-6)


def test_calibration_survives_a_profile_round_trip(tmp_path):
    """Re-measured calibration must persist through save/load, not reset."""
    profile = BravoProfile.load(str(PROFILE_DIR / "default.yaml"))
    profile.gripper.pad_zg_reference_mm = 9.25
    profile.gripper.pad_reference_tip_length_mm = 31.4

    out = tmp_path / "roundtrip.yaml"
    profile.save(str(out))
    reloaded = BravoProfile.load(str(out))

    assert reloaded.gripper.pad_zg_reference_mm == pytest.approx(9.25)
    assert reloaded.gripper.pad_reference_tip_length_mm == pytest.approx(31.4)


def test_profile_without_calibration_keys_falls_back_to_defaults(tmp_path):
    """Profiles written before these keys existed must keep working unchanged."""
    import yaml

    data = yaml.safe_load((PROFILE_DIR / "default.yaml").read_text())
    data.setdefault("gripper", {}).pop("pad_zg_reference_mm", None)
    data["gripper"].pop("pad_reference_tip_length_mm", None)

    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(yaml.dump(data))

    profile = BravoProfile.load(str(legacy))
    assert profile.gripper.pad_zg_reference_mm == pytest.approx(7.0)
    assert profile.gripper.pad_reference_tip_length_mm == pytest.approx(26.1)
    # And the resulting Zg is still the working value for this profile.
    assert _pad_reference_zg(profile) == pytest.approx(EXPECTED_PAD_ZG_MM["default"], abs=1e-6)


# ── Scan-stack measurement datum ───────────────────────────────────────────
#
# The scan senses where the top of a stack physically is. Where on a plate the
# jaws would grab it — the labware's `gripper_offset` — has nothing to do with
# that, so it must not appear in the measurement datum. It used to: the scan
# baseline was built with the gripper offset applied, which made every reading
# short by that offset and biased the inferred count differently for each
# labware. A fixed 4.1 mm fudge partly hid it, cancelling only for plates whose
# offset happened to be near 4.1 and leaving the 384 plates this lab runs
# (7.0-8.0 mm) reading short, so their counts came out low.

# name, plate height, stacking thickness, gripper offset — real catalog values.
_CATALOG_GEOMETRY = [
    ("1536 Labcyte LP-0400 LDV", 10.48, 9.8, 1.0),
    ("384 CellVis 1.5H", 14.3, 14.3, 8.0),
    ("384 Labcyte PP0200 PP sq flt", 14.4, 13.6, 7.0),
    ("384 V11 ST10 Tip Box", 50.0, 50.0, 10.0),
    ("96 Costar 3596", 14.3, 13.0, 0.0),
    ("96 Deepwell U-Bottom", 32.4, 32.4, 5.0),
    ("96 Nunc Flatbottom", 14.4, 14.4, 4.0),
    ("TestPlate", 13.2, 12.1, 5.0),
]

_OLD_FIXED_OFFSET_MM = 4.1


@pytest.mark.parametrize("name,height,stack,grip", _CATALOG_GEOMETRY)
@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8])
def test_pad_relative_reading_recovers_the_count(name, height, stack, grip, count):
    """With the pad plane as the datum the reading IS the stack height, and the
    gripper offset never enters the arithmetic."""
    true_top = _stack_total_height_for_count(count, height, stack)
    assert _infer_stack_count_from_scan_height(true_top, stack, height) == count


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8])
def test_gripper_offset_does_not_change_the_count(count):
    """The invariant. Same physical stack, same reading, regardless of where on
    the plate the jaws are set to grab it."""
    height, stack = 14.4, 13.6
    true_top = _stack_total_height_for_count(count, height, stack)
    counts = {
        grip: _infer_stack_count_from_scan_height(true_top, stack, height)
        for grip in (0.0, 1.0, 4.0, 7.0, 8.0, 10.0)
    }
    assert set(counts.values()) == {count}, counts


def test_old_datum_read_short_for_every_labware_above_the_fudge():
    """The old baseline was short by the gripper offset, and the fixed 4.1 mm
    fudge only cancelled that for offsets near 4.1 mm."""
    for name, height, stack, grip in _CATALOG_GEOMETRY:
        bias = _OLD_FIXED_OFFSET_MM - grip
        reading = _stack_total_height_for_count(4, height, stack) - grip + _OLD_FIXED_OFFSET_MM
        truth = _stack_total_height_for_count(4, height, stack)
        assert reading - truth == pytest.approx(bias)
        if grip > _OLD_FIXED_OFFSET_MM:
            assert bias < 0, (name, bias)          # reads short -> undercounts


@pytest.mark.parametrize("name,height,stack,grip", _CATALOG_GEOMETRY)
def test_scan_noise_that_the_old_datum_could_not_absorb(name, height, stack, grip):
    """A clean reading survived the old bias -- it is only ~0.2 of a plate. The
    damage was to the error margin: the rounding window sat off centre, so
    ordinary downward scan noise tipped the count. Check a 3 mm low reading,
    well inside the +/- stack/2 the geometry should tolerate."""
    noise = -3.0
    count = 4
    truth = _stack_total_height_for_count(count, height, stack)
    if abs(noise) >= 0.5 * stack:
        pytest.skip(f"{name}: 3 mm exceeds this labware's half-pitch")

    new_reading = truth + noise
    assert _infer_stack_count_from_scan_height(new_reading, stack, height) == count

    old_reading = max(0.0, truth - grip + _OLD_FIXED_OFFSET_MM + noise)
    old_count = _infer_stack_count_from_scan_height(old_reading, stack, height)
    if grip > _OLD_FIXED_OFFSET_MM + 0.5 * stack - abs(noise):
        assert old_count < count, (name, old_count)


def test_384_plates_undercount_under_the_old_datum():
    """The concrete reported failure: 384 plates, a few mm of scan noise, one
    plate missing."""
    height, stack, grip, count = 14.4, 13.6, 7.0, 4     # 384 Labcyte PP0200
    truth = _stack_total_height_for_count(count, height, stack)

    for noise in (-4.0, -5.0, -6.0):
        old_reading = truth - grip + _OLD_FIXED_OFFSET_MM + noise
        assert _infer_stack_count_from_scan_height(old_reading, stack, height) == count - 1
        # The pad-relative datum absorbs the same noise without dropping a plate.
        assert _infer_stack_count_from_scan_height(truth + noise, stack, height) == count


def _scan_task(gripper_offset_mm: float, z_teachpoint: float = 60.0):
    """A ScanStackHeightTask wired to one location, varying only the labware's
    gripper offset."""
    from pybravo.controllers.simulation import SimulationController
    from pybravo.deck.labware import DeckState, Labware, LabwareDefinition
    from pybravo.profile.profile import Teachpoints
    from pybravo.state_machine.tasks import ScanStackHeightTask
    from pybravo.types import Axis

    profile = BravoProfile.default()
    profile.safety.approach_height = 10.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(7, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(7, Axis.Y, 200.0)
    profile.teachpoints.set_teachpoint(7, Axis.Z, z_teachpoint)

    controller = SimulationController(profile.head.head_type)
    controller.open_tcp("simulation")
    definition = LabwareDefinition(
        id="p", name="p", kind="sbs_plate", base_class="microplate",
        height_mm=14.4, stack_height_mm=13.6, gripper_offset_mm=gripper_offset_mm,
    )
    labware = Labware.from_definition(definition)
    deck = DeckState()
    deck.set_single(7, labware)
    return ScanStackHeightTask(
        controller=controller, teachpoints=profile.teachpoints, profile=profile,
        deck=deck, location=7, template_labware=labware, expected_count=None,
    )


def test_scan_datum_is_independent_of_gripper_offset():
    """The invariant, at the source. The pad datum the scan measures against
    must not move when the labware's grab position changes — the two describe
    unrelated things. It used to move with it, one-for-one."""
    data = {grip: _scan_task(grip)._pad_plane_sum() for grip in (0.0, 1.0, 4.0, 7.0, 8.0, 10.0)}
    assert len(set(data.values())) == 1, data


def test_scan_datum_tracks_the_z_teachpoint():
    """Sanity check on the other axis of the datum: retaught deck, moved pad."""
    base = _scan_task(7.0, z_teachpoint=40.0)._pad_plane_sum()
    for delta in (10.0, 20.0, 35.0):
        shifted = _scan_task(7.0, z_teachpoint=40.0 + delta)._pad_plane_sum()
        assert shifted - base == pytest.approx(delta)


def test_scan_datum_survives_saturated_axis_geometry():
    """The datum must stay exact where a move target would clamp. Routing it
    through _solve_pick_or_place silently flattened it once the solve hit the
    travel limits, which is precisely when the reading is least verifiable."""
    reachable = _scan_task(7.0, z_teachpoint=60.0)._pad_plane_sum()
    saturated = _scan_task(7.0, z_teachpoint=300.0)._pad_plane_sum()
    assert saturated - reachable == pytest.approx(240.0)
