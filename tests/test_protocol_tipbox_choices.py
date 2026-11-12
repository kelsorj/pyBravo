"""Tip-box recommendations require catalog evidence, not names or tip capacity."""
from copy import deepcopy

import pytest

from pybravo.bravo import Bravo
from pybravo.deck.labware import InMemoryLabwareCatalog, Labware, LabwareDefinition
from pybravo.profile.profile import BravoProfile
from pybravo.tips import TipDefinition
from pybravo.types import HeadType
from pybravo.workflow.protocols import context
from pybravo.workflow.protocols.tipbox_choices import compatible_tipbox_choices


def catalog():
    tips = [
        {"id": "st_10ul", "label": "10 uL short tip", "capacity_ul": 10.0, "length_mm": 19.9,
         "compatible_heads": ["HT_384_D_70", "HT_96_D_70", "HT_16_D_ST"], "kind": "tip"},
        {"id": "lt_250ul", "label": "250 uL long tip", "capacity_ul": 250.0, "length_mm": 55.2,
         "compatible_heads": ["HT_96_D_200", "HT_8_D_LT"], "kind": "tip"},
    ]
    racks = [
        {"id": "rack-384-st", "name": "384 short-tip box", "kind": "tip_box", "base_class": "tip_box",
         "rows": 16, "cols": 24, "wells": 384, "spacing_x_mm": 4.5, "spacing_y_mm": 4.5,
         "tip_definition_id": "st_10ul", "supported_tip_ids": ["st_10ul"]},
        {"id": "rack-96-st", "name": "96 short-tip box", "kind": "tip_box", "base_class": "tip_box",
         "rows": 8, "cols": 12, "wells": 96, "spacing_x_mm": 9.0, "spacing_y_mm": 9.0,
         "tip_definition_id": "st_10ul", "supported_tip_ids": ["st_10ul"]},
        {"id": "rack-96-lt", "name": "96 long-tip box", "kind": "tip_box", "base_class": "tip_box",
         "rows": 8, "cols": 12, "wells": 96, "spacing_x_mm": 9.0, "spacing_y_mm": 9.0,
         "tip_definition_id": "lt_250ul", "supported_tip_ids": ["lt_250ul"]},
    ]
    return racks, tips


def ids(choices):
    return [(choice["labware_id"], choice["tip_definition_id"]) for choice in choices]


def test_full_head_format_and_st_lt_are_both_required():
    racks, tips = catalog()
    assert ids(compatible_tipbox_choices("HT_384_D_70", racks, tips)) == [("rack-384-st", "st_10ul")]
    assert ids(compatible_tipbox_choices("HT_96_D_70", racks, tips)) == [("rack-384-st", "st_10ul"), ("rack-96-st", "st_10ul")]
    assert ids(compatible_tipbox_choices("HT_96_D_200", racks, tips)) == [("rack-96-lt", "lt_250ul")]
    # Renaming an incompatible rack never changes its geometry or tip mapping.
    racks[2]["name"] = "384 ST perfect match"
    assert ids(compatible_tipbox_choices("HT_384_D_70", racks, tips)) == [("rack-384-st", "st_10ul")]


@pytest.mark.parametrize("field,value", [
    ("rows", 0), ("cols", None), ("wells", 96), ("spacing_x_mm", 9.0),
    ("spacing_y_mm", float("nan")), ("rows", 16.5), ("wells", True),
])
def test_incomplete_or_contradictory_geometry_is_excluded(field, value):
    racks, tips = catalog()
    racks[0][field] = value
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []


@pytest.mark.parametrize("changes", [
    {"compatible_heads": []}, {"compatible_heads": ["HT_96_D_200"]},
    {"compatible_heads": ["HT_384_D_70"], "supported_head_types": ["HT_96_D_200"]},
    {"capacity_ul": 0},
    {"capacity_ul": float("inf")}, {"kind": "cartridge"},
])
def test_tip_definition_must_explicitly_support_head_and_have_known_geometry(changes):
    racks, tips = catalog()
    tips[0].update(changes)
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []


def test_rack_must_link_tip_definition_without_capacity_or_name_guessing():
    racks, tips = catalog()
    racks[0].update(tip_definition_id="", supported_tip_ids=[], disposable_tip_capacity_ul=10.0)
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []
    racks[0]["supported_tip_ids"] = ["st_10ul"]
    assert ids(compatible_tipbox_choices("HT_384_D_70", racks, tips)) == [("rack-384-st", "st_10ul")]
    racks[0].update(tip_definition_id="st_10ul", supported_tip_ids=["lt_250ul"])
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []


def test_declared_tip_box_base_class_is_authoritative_but_name_is_not():
    racks, tips = catalog()
    racks[0]["kind"] = "sbs_plate"
    assert ids(compatible_tipbox_choices("HT_384_D_70", racks, tips)) == [("rack-384-st", "st_10ul")]
    racks[0]["base_class"] = "microplate"
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []


def test_narrow_heads_use_actual_catalog_geometry_without_inventing_rack_format():
    racks, tips = catalog()
    assert ids(compatible_tipbox_choices("HT_16_D_ST", racks, tips)) == [("rack-384-st", "st_10ul")]
    assert ids(compatible_tipbox_choices("HT_8_D_LT", racks, tips)) == [("rack-96-lt", "lt_250ul")]
    racks[0]["rows"] = 8
    racks[0]["wells"] = 8 * 24
    assert compatible_tipbox_choices("HT_16_D_ST", racks, tips) == []


def test_choices_are_deterministic_and_do_not_modify_catalog_or_fill_defaults():
    racks, tips = catalog()
    original = deepcopy((racks, tips))
    expected = compatible_tipbox_choices(HeadType.HT_384_D_70, racks, tips)
    assert expected == compatible_tipbox_choices(int(HeadType.HT_384_D_70), reversed(racks), reversed(tips))
    assert (racks, tips) == original
    assert expected[0]["tip_capacity_ul"] == 10.0
    assert expected[0]["tip_length_mm"] == 19.9
    assert expected[0]["rows"] * expected[0]["cols"] == 384
    for head in ("not-a-head", "HT_96_ASSAYMAP", "HT_384_PINTOOL", True):
        assert compatible_tipbox_choices(head, racks, tips) == []


def test_conflicting_duplicate_catalog_ids_are_not_recommended():
    racks, tips = catalog()
    racks.append({**racks[0], "spacing_x_mm": 9.0})
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []
    racks, tips = catalog()
    tips.append({**tips[0], "compatible_heads": ["HT_96_D_200"]})
    assert compatible_tipbox_choices("HT_384_D_70", racks, tips) == []


def test_context_exposes_filtered_choices_and_invalidates_when_tip_definition_changes(monkeypatch):
    racks, tips = catalog()
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile, mode="simulation")
    bravo._labware_catalog = InMemoryLabwareCatalog([LabwareDefinition(**rack) for rack in racks])
    definition = TipDefinition(tip_id="st_10ul", label="10 uL short tip", capacity_ul=10.0,
                               length_mm=19.9, source="test", compatible_heads=("HT_384_D_70",))
    monkeypatch.setattr(context, "get_tip_definitions_for_head", lambda head: [definition])
    first = context.machine_context(bravo)
    assert ids(first["tipbox_choices"]) == [("rack-384-st", "st_10ul")]
    assert first["tipbox_choices_reason"] == ""
    # Selecting a channel subset must not make 96-format racks suitable for a 384 head.
    bravo.set_head_mode("single_barrel", "back_left")
    assert context.machine_context(bravo)["tipbox_choices"] == first["tipbox_choices"]
    definition = TipDefinition(tip_id="st_10ul", label="10 uL short tip", capacity_ul=10.0,
                               length_mm=None, source="test", compatible_heads=("HT_384_D_70",))
    second = context.machine_context(bravo)
    assert second["tipbox_choices"][0]["execution_ready"] is False
    assert second["tipbox_choices"][0]["missing_metadata"] == ["tip_length"]
    assert second["tipbox_choices"][0]["tip_length_mm"] is None
    assert second["context_hash"] != first["context_hash"]


def test_real_snapshot_384_rack_has_recorded_st10_pair_and_grid():
    from pathlib import Path

    import yaml

    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    snapshot = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml").read_text())
    rack = next(row for row in snapshot["labware"] if row["id"] == "lw-4914769d0af7")
    original = deepcopy(rack)
    _, tips = catalog()
    choices = compatible_tipbox_choices("HT_384_D_70", [rack], tips)
    candidates = tipbox_catalog_candidates("HT_384_D_70", [rack], tips)
    assert ids(choices) == [("lw-4914769d0af7", "st_10ul")]
    assert (choices[0]["rows"], choices[0]["cols"]) == (16, 24)
    assert candidates == []
    assert rack == original


def test_checked_in_tipbox_links_agree_across_runtime_editor_and_seed():
    import json
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1] / "config"
    snapshot = yaml.safe_load((root / "labware_catalog.snapshot.yaml").read_text())["labware"]
    editor = yaml.safe_load((root / "labware_editor.yaml").read_text())["labware_types"]
    seed = json.loads((root / "labware_types.seed.json").read_text())
    expected = {
        "lw-4914769d0af7": ((16, 24), "st_10ul", 10.0),
        "lw-b0704e550d2a": ((8, 12), "lt_200ul", 200.0),
    }
    for identity, (grid, tip_id, capacity) in expected.items():
        for rows, key in ((snapshot, "id"), (editor, "labware_type_id"), (seed, "labware_type_id")):
            rack = next(row for row in rows if row[key] == identity)
            wells = rack.get("well_dimensions_mm", rack)
            assert rack["base_class"] == "tip_box"
            assert (wells["rows"], wells["cols"]) == grid
            assert float(wells["disposable_tip_capacity_ul"]) == capacity
            assert rack["tip_definition_id"] == tip_id
            assert rack["supported_tip_ids"] == (["st_10ul", "st_70ul"] if tip_id == "st_10ul" else [tip_id])


def test_checked_in_provisional_box_keeps_preview_and_unconfirmed_links_across_stores():
    import json
    from dataclasses import asdict
    from pathlib import Path

    import yaml

    from pybravo.tips import load_tip_definitions

    root = Path(__file__).resolve().parents[1] / "config"
    snapshot = yaml.safe_load((root / "labware_catalog.snapshot.yaml").read_text())["labware"]
    editor = yaml.safe_load((root / "labware_editor.yaml").read_text())["labware_types"]
    seed = json.loads((root / "labware_types.seed.json").read_text())
    identity = "lw-96st-provisional"
    preview = "/labware-assets/lw-96st-provisional/96_ST_Tip_Box_Provisional.gltf"
    definitions = []
    for rows, key in ((snapshot, "id"), (editor, "labware_type_id"), (seed, "labware_type_id")):
        matching = [row for row in rows if row[key] == identity]
        assert len(matching) == 1
        rack = matching[0]
        wells = rack.get("well_dimensions_mm", rack)
        model = rack["model_3d"]
        assert (model["url"] if isinstance(model, dict) else model) == preview
        assert rack["base_class"] == "tip_box"
        # The display name is editable in the labware editor, so the stores may
        # differ in its suffix. The provisional flag and the description below
        # carry the unconfirmed state, not the name.
        assert rack["name"].startswith("96 ST Tip Box")
        assert "visual review only" in rack["description"]
        assert "No tip types are approved" in rack["description"]
        assert (wells["rows"], wells["cols"], rack["wells"]) == (8, 12, 96)
        assert (wells["spacing_x_mm"], wells["spacing_y_mm"]) == (9.0, 9.0)
        assert float(wells["disposable_tip_capacity_ul"]) == 0.0
        assert rack["tip_definition_id"] == ""
        assert rack["supported_tip_ids"] == []
        assert rack["provisional"] is True
        assert rack["compatible_head_types"] == ["HT_96_D_70", "HT_96_D_70_S2"]
        definition = LabwareDefinition(**rack) if key == "id" else LabwareDefinition.from_mongo(rack)
        assert definition.provisional is True
        assert definition.compatible_head_types == ["HT_96_D_70", "HT_96_D_70_S2"]
        assert definition.model_3d == preview
        definitions.append(definition.to_summary())

    # A previewable catalog row must not become a compatible consumable pair in
    # any of the three persisted forms, even for heads matching its proposed grid.
    tips = [asdict(tip) for tip in load_tip_definitions()]
    for rack in definitions:
        for head in HeadType:
            assert compatible_tipbox_choices(head, [rack], tips) == []
        # Adding a known consumable must not itself approve provisional geometry.
        rack.update(tip_definition_id="st_10ul", supported_tip_ids=["st_10ul", "st_70ul"])
        assert compatible_tipbox_choices("HT_96_D_70", [rack], tips) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_provisional_snapshot_box_blocks_direct_tip_commands_before_execution(linked):
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import yaml

    snapshot = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml").read_text())
    row = next(row for row in snapshot["labware"] if row["id"] == "lw-96st-provisional")
    if linked:
        row.update(tip_definition_id="st_10ul", supported_tip_ids=["st_10ul"])
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_96_D_70
    bravo = Bravo(profile=profile, mode="simulation")
    bravo._engine = SimpleNamespace(execute=AsyncMock())
    rack = Labware.from_definition(LabwareDefinition(**row))
    bravo.deck.set_single(1, rack)
    initial_metadata = deepcopy(rack.metadata)

    with pytest.raises(RuntimeError, match="Tip selection cannot use provisional tip box"):
        bravo.set_tip_selection(1, 0, 0)
    with pytest.raises(RuntimeError, match="Tips on cannot use provisional tip box"):
        await bravo.tips_on(1)
    assert not bravo._tips_on_head
    assert bravo._tip_selection is None

    bravo._tips_on_head = True
    with pytest.raises(RuntimeError, match="Tips off cannot use provisional tip box"):
        await bravo.tips_off(1)
    assert bravo._tips_on_head is True
    assert rack.metadata == initial_metadata
    bravo._engine.execute.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"spacing_x_mm": 9.0}, {"spacing_y_mm": 0}, {"wells": 96},
    {"rows": 8, "cols": 48}, {"kind": "sbs_plate", "base_class": "microplate"},
])
def test_catalog_candidates_exclude_wrong_pitch_format_or_recorded_rack_type(changes):
    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    racks, tips = catalog()
    rack = {**racks[0], "rows": 0, "cols": 0, "tip_definition_id": "", "supported_tip_ids": [], **changes}
    assert tipbox_catalog_candidates("HT_384_D_70", [rack], tips) == []


def test_candidates_separate_complete_choices_from_missing_tip_metadata_and_known_mismatch():
    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    racks, tips = catalog()
    assert tipbox_catalog_candidates("HT_384_D_70", racks, tips) == []
    tips[0].update(length_mm=None, compatible_heads=[])
    candidate = tipbox_catalog_candidates("HT_384_D_70", racks, tips)[0]
    assert candidate["missing_metadata"] == ["head_compatibility", "tip_length"]
    tips[0]["compatible_heads"] = ["HT_96_D_200"]
    assert tipbox_catalog_candidates("HT_384_D_70", racks, tips) == []
    racks[0]["tip_definition_id"] = "unknown"
    racks[0]["supported_tip_ids"] = ["unknown"]
    assert tipbox_catalog_candidates("HT_384_D_70", racks, tips)[0]["missing_metadata"] == ["tip_definition"]


def test_candidate_count_is_bounded_and_narrow_head_never_invents_grid():
    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    racks, tips = catalog()
    incomplete = [{**racks[0], "id": f"incomplete-{i:02d}", "rows": 0, "cols": 0} for i in range(30)]
    candidates = tipbox_catalog_candidates("HT_16_D_ST", incomplete, tips, limit=999)
    assert len(candidates) == 20
    assert all(row["rows"] == row["cols"] == 0 and row["verified"] is False for row in candidates)
    assert len(tipbox_catalog_candidates("HT_16_D_ST", incomplete, tips, limit=2)) == 2
    incomplete[0]["wells"] = 8
    assert tipbox_catalog_candidates("HT_16_D_ST", incomplete[:1], tips) == []


def test_context_publishes_candidates_separately_from_verified_tipbox_choices(monkeypatch):
    racks, tips = catalog()
    racks[0].update(rows=0, cols=0, tip_definition_id="", supported_tip_ids=[])
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile, mode="simulation")
    bravo._labware_catalog = InMemoryLabwareCatalog([LabwareDefinition(**rack) for rack in racks])
    monkeypatch.setattr(context, "get_tip_definitions_for_head", lambda head: [])
    capabilities = context.machine_context(bravo)
    assert capabilities["tipbox_choices"] == []
    assert capabilities["tipbox_catalog_candidates"][0]["labware_id"] == "rack-384-st"
    assert capabilities["tipbox_catalog_candidates"][0]["missing_metadata"] == ["rows_cols", "tip_link"]


def test_snapshot_calibration_excludes_long_tip_candidates_for_short_tip_heads():
    from dataclasses import asdict
    from pathlib import Path

    import yaml

    from pybravo.tip_offsets import load_tip_offset_table
    from pybravo.tips import load_tip_definitions
    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    root = Path(__file__).resolve().parents[1]
    snapshot = yaml.safe_load((root / "config" / "labware_catalog.snapshot.yaml").read_text())["labware"]
    offsets = [asdict(entry) for entry in load_tip_offset_table(root / "config" / "tip_offsets.yaml").entries]
    tips = [asdict(tip) for tip in load_tip_definitions()]
    provisional_id = "lw-96st-provisional"
    short = tipbox_catalog_candidates("HT_96_D_70", snapshot, tips, tip_offsets=offsets)
    assert [row["labware_id"] for row in short] == [provisional_id]
    assert short[0]["verified"] is False
    assert short[0]["missing_metadata"] == ["provisional_approval", "tip_link"]
    assert tipbox_catalog_candidates("HT_96_D_70_S2", snapshot, tips, tip_offsets=offsets) == short
    for head in ("HT_384_D_70", "HT_16_D_ST"):
        assert tipbox_catalog_candidates(head, snapshot, tips, tip_offsets=offsets) == []
        assert ids(compatible_tipbox_choices(head, snapshot, tips)) == [("lw-4914769d0af7", "st_10ul"), ("lw-4914769d0af7", "st_70ul")]
    long = tipbox_catalog_candidates("HT_96_D_200", snapshot, tips, tip_offsets=offsets)
    # Matching pitch alone cannot override the provisional rack's declared heads.
    assert long == []
    long_choices = compatible_tipbox_choices("HT_96_D_200", snapshot, tips)
    assert ids(long_choices) == [("lw-b0704e550d2a", "lt_200ul")]
    assert long_choices[0]["execution_ready"] is False


def test_calibration_filters_by_exact_rack_identity_not_st_lt_name_substrings():
    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    racks, tips = catalog()
    rack = {**racks[1], "name": "A perfectly short-tip ST box", "rows": 0, "cols": 0,
            "tip_definition_id": "", "supported_tip_ids": []}
    offsets = [{"head_type": "HT_96_D_200", "tipbox_id": rack["id"], "tips_off_z_offset": 25.0}]
    assert tipbox_catalog_candidates("HT_96_D_70", [rack], tips, tip_offsets=offsets) == []
    # A differently identified LT box's calibration says nothing about this rack.
    offsets[0]["tipbox_id"] = "other-rack"
    assert len(tipbox_catalog_candidates("HT_96_D_70", [rack], tips, tip_offsets=offsets)) == 1
    # Calibration can also match the exact normalized identity name.
    offsets[0]["tipbox"] = "  A   PERFECTLY short-tip ST box  "
    assert tipbox_catalog_candidates("HT_96_D_70", [rack], tips, tip_offsets=offsets) == []


def test_explicit_rack_tip_link_can_establish_compatibility_beyond_existing_calibration():
    from pybravo.workflow.protocols.tipbox_choices import tipbox_catalog_candidates
    racks, tips = catalog()
    rack = {**racks[1], "rows": 0, "cols": 0}
    offsets = [{"head_type": "HT_96_D_200", "tipbox_id": rack["id"]}]
    result = tipbox_catalog_candidates("HT_96_D_70", [rack], tips, tip_offsets=offsets)
    assert result[0]["missing_metadata"] == ["rows_cols"]
    assert compatible_tipbox_choices("HT_96_D_70", [rack], tips) == []


@pytest.mark.parametrize("head_name", ["HT_96_D_70", "HT_96_D_70_S2"])
def test_provisional_box_is_visible_for_review_but_blocked_until_tip_links_confirmed(monkeypatch, head_name):
    from pybravo.workflow.protocols.compiler import ProtocolCompilationError, compile_plan
    from pybravo.workflow.protocols.validation import validate_plan
    from tests.test_protocol_compiler import protocol_fixture

    # Synthetic proposed geometry/model: deliberately no consumable association.
    # Recording a name, pitch, capacity or preview asset must not confirm a rack.
    rack = LabwareDefinition(
        id="provisional-96-st", name="Provisional 96 ST box — confirm visually",
        kind="sbs_plate", base_class="tip_box", rows=8, cols=12, wells=96,
        spacing_x_mm=9.0, spacing_y_mm=9.0, disposable_tip_capacity_ul=30.0,
        tip_definition_id="", supported_tip_ids=[],
        description="Unconfirmed visual draft; no physical use until catalog review.",
        model_3d="/labware-assets/provisional-96-st/preview.gltf",
    )
    profile = BravoProfile.default()
    profile.head.head_type = HeadType[head_name]
    bravo = Bravo(profile=profile, mode="simulation")
    bravo._labware_catalog = InMemoryLabwareCatalog([rack])
    tip = TipDefinition(tip_id="st_30ul", label="ST30", capacity_ul=30.0, length_mm=26.1,
                        source="test", compatible_heads=(head_name,))
    monkeypatch.setattr(context, "get_tip_definitions_for_head", lambda head: [tip])
    proposed = context.machine_context(bravo)
    assert proposed["labware"][0]["model_3d"] == rack.model_3d
    assert proposed["labware"][0]["description"] == rack.description
    assert proposed["tipbox_choices"] == []
    assert proposed["tipbox_catalog_candidates"] == [{
        "labware_id": rack.id, "labware_name": rack.name, "rows": 8, "cols": 12, "wells": 96,
        "spacing_x_mm": 9.0, "spacing_y_mm": 9.0, "verified": False, "missing_metadata": ["tip_link"],
    }]
    plan, setup, validation_context, sources = protocol_fixture()
    validation_context.update(head_type=head_name, tip_definitions=proposed["tip_definitions"])
    validation_context["labware"][1] = rack.to_summary()
    for material in plan["materials"][2:]:
        material.update(labware_id=rack.id, tip_definition_id="st_30ul")
    report = validate_plan(plan, setup, validation_context, sources=sources)
    assert "tip_rack_compatibility" in {issue["code"] for issue in report["issues"]}
    with pytest.raises(ProtocolCompilationError):
        compile_plan(plan, setup, validation_context, sources=sources)

    # Explicitly record confirmation in this isolated catalog, not in live data.
    rack.supported_tip_ids = ["st_30ul"]
    confirmed = context.machine_context(bravo)
    assert ids(confirmed["tipbox_choices"]) == [(rack.id, "st_30ul")]
    assert confirmed["tipbox_catalog_candidates"] == []
    assert confirmed["context_hash"] != proposed["context_hash"]
    validation_context["labware"][1] = rack.to_summary()
    assert validate_plan(plan, setup, validation_context, sources=sources)["valid"]
