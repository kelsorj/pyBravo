"""Tests for per-(head, tip box) Tips On / Tips Off offset resolution."""

from __future__ import annotations

import textwrap

import pytest

from pybravo.bravo import Bravo
from pybravo.deck.labware import Labware, LabwareDefinition
from pybravo.deck.teachpoints import Teachpoints
from pybravo.head_mode import TipSelection, normalize_head_mode
from pybravo.profile.profile import BravoProfile
from pybravo.tip_offsets import (
    TipOffsetEntry,
    TipOffsetTable,
    _normalize_head,
    load_tip_offset_table,
)
from pybravo.types import TIPBOX_JOG_TOLERANCE, Axis, HeadType
from tests.test_bravo_init import RecordingSimulationController

# ---------------------------------------------------------------------------
# Resolver / loader unit tests
# ---------------------------------------------------------------------------

LT200 = TipOffsetEntry(
    head_type="HT_96_D_200",
    tipbox="96 V11 LT200 Tip Box 06880.002",
    tipbox_id="lw-b0704e550d2a",
    tips_off_z_offset=25.0,
    tips_off_w_position=-35.0,
    tips_on_jog_tolerance=12.0,
    tips_on_z_offset=0.0,
)


def _resolve(table, head, **kw):
    return table.resolve(
        head,
        default_z_offset=15.0,
        default_w_position=-15.0,
        **kw,
    )


def test_resolve_falls_back_to_defaults_when_no_entry():
    table = TipOffsetTable([])
    resolved = _resolve(table, HeadType.HT_96_D_200, tipbox_name="Anything")
    assert resolved.matched is False
    assert resolved.tips_off_z_offset == 15.0
    assert resolved.tips_off_w_position == -15.0
    assert resolved.tips_on_jog_tolerance == TIPBOX_JOG_TOLERANCE
    assert resolved.tips_on_z_offset == 0.0
    assert resolved.source == "profile defaults"


def test_resolve_matches_by_name_case_and_whitespace_insensitive():
    table = TipOffsetTable([LT200])
    resolved = _resolve(
        table,
        HeadType.HT_96_D_200,
        tipbox_name="  96 v11  lt200 tip box 06880.002 ",
    )
    assert resolved.matched is True
    assert resolved.tips_off_z_offset == 25.0
    assert resolved.tips_off_w_position == -35.0
    assert resolved.tips_on_jog_tolerance == 12.0


def test_resolve_matches_by_id_when_name_differs():
    table = TipOffsetTable([LT200])
    resolved = _resolve(
        table,
        HeadType.HT_96_D_200,
        tipbox_name="renamed in the catalog",
        tipbox_id="lw-b0704e550d2a",
    )
    assert resolved.matched is True
    assert resolved.tips_off_z_offset == 25.0


def test_resolve_head_mismatch_falls_back():
    table = TipOffsetTable([LT200])
    resolved = _resolve(
        table,
        HeadType.HT_384_D_70,
        tipbox_name="96 V11 LT200 Tip Box 06880.002",
    )
    assert resolved.matched is False
    assert resolved.tips_off_z_offset == 15.0


def test_resolve_partial_fields_fall_back_per_field():
    entry = TipOffsetEntry(
        head_type="HT_96_D_200",
        tipbox="Partial Box",
        tips_off_z_offset=22.0,  # only this set; others None
    )
    table = TipOffsetTable([entry])
    resolved = _resolve(table, HeadType.HT_96_D_200, tipbox_name="Partial Box")
    assert resolved.matched is True
    assert resolved.tips_off_z_offset == 22.0
    assert resolved.tips_off_w_position == -15.0  # fell back to default
    assert resolved.tips_on_jog_tolerance == TIPBOX_JOG_TOLERANCE
    assert resolved.tips_on_z_offset == 0.0


def test_normalize_head_accepts_enum_name_and_int():
    assert _normalize_head(HeadType.HT_96_D_200) == "HT_96_D_200"
    assert _normalize_head("ht_96_d_200") == "HT_96_D_200"
    assert _normalize_head(5) == "HT_96_D_200"  # IntEnum value
    assert _normalize_head("") == ""


def test_load_missing_file_returns_empty_table(tmp_path):
    table = load_tip_offset_table(tmp_path / "does_not_exist.yaml")
    assert table.entries == []


def test_load_from_yaml_parses_entries(tmp_path):
    path = tmp_path / "tip_offsets.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            offsets:
              - head_type: HT_96_D_200
                tipbox: 96 V11 LT200 Tip Box 06880.002
                tipbox_id: lw-b0704e550d2a
                tips_off_z_offset: 25.0
                tips_off_w_position: -35.0
                tips_on_jog_tolerance: 12.0
              - head_type: ht_384_d_70
                tipbox: 384 V11 ST10 Tip Box 10734.102
                tips_off_z_offset: 10.0
            """
        ),
        encoding="utf-8",
    )
    table = load_tip_offset_table(path)
    assert len(table.entries) == 2

    lt = table.find(HeadType.HT_96_D_200, tipbox_name="96 V11 LT200 Tip Box 06880.002")
    assert lt is not None and lt.tips_on_jog_tolerance == 12.0

    # Head type strings are normalized to the canonical enum name on load.
    st = table.find("HT_384_D_70", tipbox_name="384 V11 ST10 Tip Box 10734.102")
    assert st is not None and st.tips_off_z_offset == 10.0
    assert st.tips_off_w_position is None  # unset -> None -> resolves to default


def test_load_malformed_root_returns_empty(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    assert load_tip_offset_table(path).entries == []


# ---------------------------------------------------------------------------
# Integration tests through the Bravo facade
# ---------------------------------------------------------------------------


def _patch_table(monkeypatch, entries):
    table = TipOffsetTable(entries)
    monkeypatch.setattr("pybravo.bravo.get_tip_offset_table", lambda **_: table)
    return table


@pytest.mark.asyncio
async def test_tips_on_uses_per_box_press_tolerance(monkeypatch):
    _patch_table(
        monkeypatch,
        [
            TipOffsetEntry(
                head_type="HT_384_D_70",
                tipbox="30 uL Tip Box",
                tips_on_jog_tolerance=9.0,
            )
        ],
    )
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    profile.safety.tip_press_dwell_time = 0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 123.0)
    tp.set_teachpoint(1, Axis.Y, 45.0)
    tp.set_teachpoint(1, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(tipbox))

    await bravo.tips_on(1)

    assert len(controller.jog_calls) == 1
    # Press window comes from the per-box entry...
    assert controller.jog_calls[0].tolerance == pytest.approx(9.0)
    # ...but the press target geometry is unchanged (tips_on_z_offset defaulted to 0).
    assert controller.jog_calls[0].max_position == pytest.approx(36.1, abs=1e-6)


@pytest.mark.asyncio
async def test_tips_on_z_offset_shifts_press_target(monkeypatch):
    _patch_table(
        monkeypatch,
        [
            TipOffsetEntry(
                head_type="HT_384_D_70",
                tipbox="30 uL Tip Box",
                tips_on_jog_tolerance=8.0,
                tips_on_z_offset=2.0,
            )
        ],
    )
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    profile.safety.tip_press_dwell_time = 0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 123.0)
    tp.set_teachpoint(1, Axis.Y, 45.0)
    tp.set_teachpoint(1, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(tipbox))

    await bravo.tips_on(1)

    assert controller.jog_calls[0].tolerance == pytest.approx(8.0)
    # base target 36.1 + tips_on_z_offset 2.0
    assert controller.jog_calls[0].max_position == pytest.approx(38.1, abs=1e-6)


@pytest.mark.asyncio
async def test_tips_off_uses_per_box_z_and_w(monkeypatch):
    _patch_table(
        monkeypatch,
        [
            TipOffsetEntry(
                head_type="HT_384_D_70",
                tipbox="Tip Trash",
                tips_off_z_offset=25.0,
                tips_off_w_position=-35.0,
            )
        ],
    )
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    # Profile defaults deliberately differ from the per-box override below.
    profile.safety.tips_off_w_position = -11.0
    profile.safety.tips_off_z_offset = 10.0
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 222.0)
    tp.set_teachpoint(2, Axis.Y, 33.0)
    tp.set_teachpoint(2, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tip_trash = LabwareDefinition(
        id="tip-trash",
        name="Tip Trash",
        kind="tip_trash",
        base_class="tip_trash",
        height_mm=5.0,
    )
    bravo.deck.set_single(2, Labware.from_definition(tip_trash))
    bravo._set_tip_state(
        labware_name="30 uL Tip Box",
        tip_length_mm=26.1,
        head_mode=normalize_head_mode(profile.head.head_type, "all_barrels", "front_left"),
        tip_selection=TipSelection(location=1, row=0, col=0),
    )

    await bravo.tips_off(2)

    # deck_surface = 60 + 26.1 = 86.1; box_top = 86.1 - 5.0 = 81.1;
    # eject Z = 81.1 - 25.0 (per-box) = 56.1
    eject_z = [m for call in controller.move_calls for m in call if m.axis == Axis.Z]
    assert any(m.position == pytest.approx(56.1, abs=1e-6) for m in eject_z)
    # Ejector throw uses the per-box W target, not the profile default of -11.
    w_moves = [m for call in controller.move_calls for m in call if m.axis == Axis.W]
    assert w_moves[0].position == pytest.approx(-35.0)
    assert w_moves[-1].position == pytest.approx(0.0)
