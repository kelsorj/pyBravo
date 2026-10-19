"""A model-authored action sequence must pass catalog and state checks."""

from __future__ import annotations

import ast
from copy import deepcopy

import pytest

from pybravo.evals.text2wetlab.action_ir import (
    ActionPlan,
    ActionPlanError,
    LabwareFacts,
    ModuleFacts,
    PipetteFacts,
    SourceSpan,
    compile_actions,
    validate_action_evidence,
)

LABWARE = {
    "synthetic_96_tiprack_20ul": LabwareFacts(
        frozenset({"A1", "B1"}), is_tiprack=True, tip_capacity_ul=20,
    ),
    "synthetic_source": LabwareFacts(frozenset({"A1", "A2"})),
    "synthetic_target": LabwareFacts(frozenset({"A1", "A2"})),
}
PIPETTES = {"synthetic_p20": PipetteFacts(1, 20, 1)}
INSTRUCTION = "Synthetic test: run only the explicitly listed actions in order."
SPANS = {"task": SourceSpan("instruction", 0, len(INSTRUCTION))}


def _draft() -> dict:
    return {
        "labware": [
            {"id": "tips", "load_name": "synthetic_96_tiprack_20ul", "slot": 1},
            {"id": "source", "load_name": "synthetic_source", "slot": 2},
            {"id": "target", "load_name": "synthetic_target", "slot": 3},
        ],
        "pipettes": [{
            "id": "small", "model": "synthetic_p20", "mount": "left",
            "tip_rack_ids": ["tips"],
        }],
        "actions": [
            {"kind": "pickup", "pipette": "small"},
            {"kind": "aspirate", "pipette": "small", "labware": "source",
             "well": "A1", "volume_ul": 5},
            {"kind": "dispense", "pipette": "small", "labware": "target",
             "well": "A1", "volume_ul": 5},
            {"kind": "drop", "pipette": "small"},
        ],
    }


def _compile(draft: dict, *, labware=LABWARE, pipettes=PIPETTES,
             modules=None, refill_authorized=False) -> str:
    # Fixtures focus on mechanical checks; evidence behavior is tested below.
    cited = deepcopy(draft)
    for action in cited["actions"]:
        action.setdefault("evidence_refs", ["task"])
        if action["kind"] == "for_each":
            for item in action["actions"]:
                item.setdefault("evidence_refs", ["task"])
    return compile_actions(
        cited, labware_catalog=labware, pipette_catalog=pipettes,
        source_spans=SPANS, instruction=INSTRUCTION, module_catalog=modules,
        refill_authorized=refill_authorized,
    )


def test_explicit_actions_lower_to_fixed_primitives_without_inferred_steps():
    draft = _draft()
    code = _compile(draft)
    ast.parse(code)
    assert code.count(".pick_up_tip()") == 1
    assert code.count(".aspirate(") == 1
    assert code.count(".dispense(") == 1
    assert code.count(".drop_tip()") == 1
    assert code.index(".aspirate(") < code.index(".dispense(")
    assert ActionPlan.model_json_schema()["additionalProperties"] is False


def test_model_authored_top_dispense_is_explicit_and_aspiration_stays_in_well():
    draft = _draft()
    draft["actions"][2]["location"] = "top"
    code = _compile(draft)
    assert ".dispense(5.0, lw_2.wells_by_name()['A1'].top())" in code
    assert ".aspirate(5.0, lw_1.wells_by_name()['A1'])" in code

    draft["actions"][1]["location"] = "top"
    with pytest.raises(ActionPlanError, match="cannot aspirate from the well rim"):
        _compile(draft)


def test_comment_records_exact_model_authored_note_without_pausing():
    draft = _draft()
    draft["actions"].insert(0, {
        "kind": "comment", "message": "Synthetic manual sealing step.",
    })
    code = _compile(draft)
    assert "protocol.comment('Synthetic manual sealing step.')" in code
    assert "protocol.pause(" not in code


@pytest.mark.parametrize("volume,diagnostic", [
    (0.5, "outside"),
    (21, "outside"),
])
def test_rejects_strokes_outside_trusted_pipette_and_tip_range(volume, diagnostic):
    draft = _draft()
    draft["actions"][1]["volume_ul"] = volume
    with pytest.raises(ActionPlanError, match=diagnostic):
        _compile(draft)


def test_tracks_tip_contents_across_distinct_aspirate_and_dispense_actions():
    draft = _draft()
    draft["actions"][2]["volume_ul"] = 6
    with pytest.raises(ActionPlanError, match="dispenses more"):
        _compile(draft)
    draft = _draft()
    draft["actions"].insert(2, deepcopy(draft["actions"][1]))
    draft["actions"][2]["volume_ul"] = 16
    with pytest.raises(ActionPlanError, match="tip capacity"):
        _compile(draft)


def test_requires_tip_and_checks_tip_supply_before_compilation():
    draft = _draft()
    draft["actions"].pop(0)
    with pytest.raises(ActionPlanError, match="without an attached tip"):
        _compile(draft)
    draft = _draft()
    draft["actions"].extend(deepcopy(draft["actions"]))
    draft["actions"].extend(deepcopy(draft["actions"][:4]))
    with pytest.raises(ActionPlanError, match="fresh-tip supply"):
        _compile(draft)


def test_rejects_unloaded_well_and_unknown_catalog_entries():
    draft = _draft()
    draft["actions"][1]["well"] = "H12"
    with pytest.raises(ActionPlanError, match="invalid liquid well"):
        _compile(draft)
    draft = _draft()
    draft["labware"][1]["load_name"] = "invented_source"
    with pytest.raises(ActionPlanError, match="absent from the trusted catalog"):
        _compile(draft)


def test_rejects_multichannel_geometry_and_shared_tiprack():
    draft = _draft()
    multi = {"synthetic_p20": PipetteFacts(1, 20, 8)}
    with pytest.raises(ActionPlanError, match="multichannel compatible"):
        _compile(draft, pipettes=multi)
    draft = _draft()
    draft["pipettes"].append({
        "id": "other", "model": "synthetic_p20", "mount": "right",
        "tip_rack_ids": ["tips"],
    })
    with pytest.raises(ActionPlanError, match="multiple pipettes"):
        _compile(draft)


def test_rejects_model_supplied_extra_code_or_motion_fields():
    draft = _draft()
    draft["actions"][1]["python"] = "import os"
    with pytest.raises(ActionPlanError, match="Extra inputs are not permitted"):
        _compile(draft)


def _mapped_draft() -> dict:
    draft = _draft()
    draft["actions"] = [{
        "kind": "for_each",
        "bindings": [
            {"source_well": "A1", "target_well": "A1"},
            {"source_well": "A2", "target_well": "A2"},
        ],
        "actions": [
            {"kind": "pickup", "pipette": "small"},
            {"kind": "aspirate", "pipette": "small", "labware": "source",
             "well": "$source_well", "volume_ul": 5},
            {"kind": "dispense", "pipette": "small", "labware": "target",
             "well": "$target_well", "volume_ul": 5},
            {"kind": "drop", "pipette": "small"},
        ],
    }]
    return draft


def test_well_mapping_expands_only_explicit_model_supplied_pairs():
    code = _compile(_mapped_draft())
    ast.parse(code)
    assert code.count(".pick_up_tip()") == 2
    assert code.count(".aspirate(") == 2
    assert code.count(".dispense(") == 2
    assert code.count(".drop_tip()") == 2
    assert "lw_1.wells_by_name()['A1']" in code
    assert "lw_1.wells_by_name()['A2']" in code
    assert "lw_2.wells_by_name()['A1']" in code
    assert "lw_2.wells_by_name()['A2']" in code


def test_mapping_errors_retain_binding_index_for_model_repair():
    draft = _mapped_draft()
    draft["actions"][0]["bindings"][1]["target_well"] = "H12"
    with pytest.raises(ActionPlanError, match=r"actions\[0\]\.bindings\[1\]\.actions\[2\]"):
        _compile(draft)
    draft = _mapped_draft()
    draft["actions"][0]["actions"][1]["well"] = "$missing"
    with pytest.raises(ActionPlanError, match="unknown well binding"):
        _compile(draft)
    draft = _mapped_draft()
    draft["actions"][0]["bindings"][1].pop("target_well")
    with pytest.raises(ActionPlanError, match="must supply the same"):
        _compile(draft)


def test_mapping_rejects_unused_names_and_excessive_expansion():
    draft = _mapped_draft()
    draft["actions"][0]["actions"][2]["well"] = "A1"
    with pytest.raises(ActionPlanError, match="unused well bindings"):
        _compile(draft)
    draft = _mapped_draft()
    loop = draft["actions"][0]
    loop["bindings"] = [{"source_well": "A1", "target_well": "A1"}] * 384
    loop["actions"].extend({"kind": "delay", "seconds": 1} for _ in range(28))
    with pytest.raises(ActionPlanError, match="expansion limit"):
        _compile(draft)


def test_eight_channel_loop_accepts_only_trusted_full_column_anchors():
    draft = _mapped_draft()
    facts = {
        "synthetic_96_tiprack_20ul": LabwareFacts(
            frozenset(f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)),
            is_tiprack=True, tip_capacity_ul=20, multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1", "A2"}),
        ),
        "synthetic_source": LabwareFacts(
            frozenset(f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)),
            multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1", "A2"}),
        ),
        "synthetic_target": LabwareFacts(
            frozenset(f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)),
            multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1", "A2"}),
        ),
    }
    multi = {"synthetic_p20": PipetteFacts(1, 20, 8)}
    code = _compile(draft, labware=facts, pipettes=multi)
    assert code.count(".pick_up_tip()") == 2
    draft["actions"][0]["bindings"][1]["target_well"] = "B2"
    with pytest.raises(ActionPlanError, match="invalid multichannel column anchor"):
        _compile(draft, labware=facts, pipettes=multi)


def _thermocycler_draft() -> tuple[dict, dict[str, ModuleFacts]]:
    draft = _draft()
    draft["modules"] = [{"id": "tc", "model": "trusted_tc"}]
    draft["labware"][2].pop("slot")
    draft["labware"][2]["module_id"] = "tc"
    draft["actions"] = [
        {"kind": "open_lid", "module": "tc"},
        *draft["actions"][:3],
        {"kind": "drop", "pipette": "small"},
        {"kind": "close_lid", "module": "tc"},
        {"kind": "set_lid_temperature", "module": "tc", "celsius": 75},
        {"kind": "set_block_temperature", "module": "tc", "celsius": 42,
         "hold_seconds": 30},
        {"kind": "open_lid", "module": "tc"},
    ]
    modules = {"trusted_tc": ModuleFacts(
        "thermocycler", fixed_occupied_slots=frozenset({7, 8, 10, 11}),
        compatible_labware_load_names=frozenset({"synthetic_target"}),
        temperature_range_c=(4, 99), lid_temperature_range_c=(37, 110),
    )}
    return draft, modules


def test_trusted_module_footprint_and_state_lower_to_sdk_methods():
    draft, modules = _thermocycler_draft()
    code = _compile(draft, modules=modules)
    ast.parse(code)
    assert "protocol.load_module('trusted_tc')" in code
    assert "mod_0.load_labware('synthetic_target')" in code
    assert "mod_0.open_lid()" in code
    assert "mod_0.close_lid()" in code
    assert "mod_0.set_lid_temperature(75.0)" in code
    assert "mod_0.set_block_temperature(42.0, hold_time_seconds=30.0)" in code
    draft["labware"].append({"id": "colliding", "load_name": "synthetic_source", "slot": 8})
    with pytest.raises(ActionPlanError, match="slot 8 is assigned more than once"):
        _compile(draft, modules=modules)


def test_thermocycler_lid_and_temperature_bounds_are_hard_gates():
    draft, modules = _thermocycler_draft()
    draft["actions"].pop(0)
    with pytest.raises(ActionPlanError, match="without an open lid"):
        _compile(draft, modules=modules)
    draft, modules = _thermocycler_draft()
    draft["actions"][-2]["celsius"] = 105
    with pytest.raises(ActionPlanError, match="exceeds trusted limits"):
        _compile(draft, modules=modules)
    draft, modules = _thermocycler_draft()
    draft["modules"][0]["slot"] = 7
    with pytest.raises(ActionPlanError, match="must not specify a slot"):
        _compile(draft, modules=modules)


def test_module_labware_requires_trusted_compatibility():
    draft, modules = _thermocycler_draft()
    modules["trusted_tc"] = ModuleFacts(
        "thermocycler", fixed_occupied_slots=frozenset({7, 8, 10, 11}),
        temperature_range_c=(4, 99), lid_temperature_range_c=(37, 110),
    )
    with pytest.raises(ActionPlanError, match="no trusted compatibility"):
        _compile(draft, modules=modules)


def test_magnet_and_temperature_actions_require_correct_trusted_module_kind():
    draft = _draft()
    draft["modules"] = [
        {"id": "mag", "model": "trusted_magnet", "slot": 4},
        {"id": "temp", "model": "trusted_temperature", "slot": 5},
    ]
    modules = {
        "trusted_magnet": ModuleFacts(
            "magnetic", allowed_slots=frozenset({4}),
            magnet_height_range_mm=(0, 20),
        ),
        "trusted_temperature": ModuleFacts(
            "temperature", allowed_slots=frozenset({5}),
            temperature_range_c=(4, 95),
        ),
    }
    draft["actions"] = [
        {"kind": "set_temperature", "module": "temp", "celsius": 4},
        {"kind": "magnet_engage", "module": "mag", "height_from_base_mm": 6},
        {"kind": "delay", "seconds": 10},
        {"kind": "magnet_disengage", "module": "mag"},
    ]
    code = _compile(draft, modules=modules)
    assert "mod_1.set_temperature(4.0)" in code
    assert "mod_0.engage(height_from_base=6.0)" in code
    assert "mod_0.disengage()" in code
    draft["actions"][1]["height_from_base_mm"] = 30
    with pytest.raises(ActionPlanError, match="exceeds trusted limits"):
        _compile(draft, modules=modules)
    draft["actions"][1]["height_from_base_mm"] = 6
    draft["actions"][1]["module"] = "temp"
    with pytest.raises(ActionPlanError, match="not supported by module"):
        _compile(draft, modules=modules)


def test_refill_requires_task_authorization_and_actual_rack_exhaustion():
    draft = _mapped_draft()  # Two pickups exhaust the trusted two-tip rack.
    draft["actions"].extend([
        {"kind": "refill_tips", "pipette": "small",
         "message": "Operator: replace all used tips with fresh tips."},
        {"kind": "pickup", "pipette": "small"},
        {"kind": "aspirate", "pipette": "small", "labware": "source",
         "well": "A1", "volume_ul": 5},
        {"kind": "dispense", "pipette": "small", "labware": "target",
         "well": "A1", "volume_ul": 5},
        {"kind": "drop", "pipette": "small"},
    ])
    with pytest.raises(ActionPlanError, match="no task-authorized"):
        _compile(draft)
    code = _compile(draft, refill_authorized=True)
    assert code.count(".pick_up_tip()") == 3
    assert code.index("protocol.pause('Operator: replace") < code.index(".reset_tipracks()")
    draft["actions"].insert(0, draft["actions"][1])
    with pytest.raises(ActionPlanError, match="before its tip racks are exhausted"):
        _compile(draft, refill_authorized=True)


def test_refill_cannot_reset_a_rack_while_tip_remains_attached():
    draft = _draft()
    draft["actions"] = [
        {"kind": "pickup", "pipette": "small"},
        {"kind": "drop", "pipette": "small"},
        {"kind": "pickup", "pipette": "small"},
        {"kind": "refill_tips", "pipette": "small", "message": "Load fresh tips."},
    ]
    with pytest.raises(ActionPlanError, match="cannot refill while"):
        _compile(draft, refill_authorized=True)


def test_every_action_and_loop_body_resolves_to_exact_task_or_paper_spans():
    instruction = "Transfer liquid from the supplied source to the target."
    paper = "Hold the completed plate at 42 C for 30 seconds."
    spans = {
        "transfer": SourceSpan("instruction", 0, len(instruction)),
        "hold": SourceSpan("paper", 0, len(paper)),
    }
    draft = _mapped_draft()
    draft["actions"][0]["evidence_refs"] = ["transfer"]
    for item in draft["actions"][0]["actions"]:
        item["evidence_refs"] = ["transfer"]
    draft["actions"].append({
        "kind": "delay", "seconds": 30, "evidence_refs": ["hold"],
    })
    plan = ActionPlan.model_validate(draft)
    resolved = validate_action_evidence(
        plan, source_spans=spans, instruction=instruction, paper=paper,
    )
    assert len(resolved) == 6  # Loop, four body actions, then a paper-cited delay.
    assert resolved[0].action_path == "actions[0]"
    assert resolved[0].quote == instruction
    assert resolved[-1].action_path == "actions[1]"
    assert resolved[-1].source == "paper"
    assert resolved[-1].quote == paper
    code = compile_actions(
        draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
        source_spans=spans, instruction=instruction, paper=paper,
    )
    assert "protocol.delay(seconds=30.0)" in code


def test_missing_and_forged_action_evidence_cannot_compile():
    draft = _draft()
    with pytest.raises(ActionPlanError, match="evidence_refs"):
        compile_actions(
            draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
            source_spans=SPANS, instruction=INSTRUCTION,
        )
    for item in draft["actions"]:
        item["evidence_refs"] = ["invented"]
    with pytest.raises(ActionPlanError, match="unknown source span"):
        compile_actions(
            draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
            source_spans=SPANS, instruction=INSTRUCTION,
        )
    for item in draft["actions"]:
        item["evidence_refs"] = ["task"]
    draft["actions"][1]["evidence_refs"] = ["task", "task"]
    with pytest.raises(ActionPlanError, match="repeats an evidence reference"):
        compile_actions(
            draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
            source_spans=SPANS, instruction=INSTRUCTION,
        )


def test_unavailable_paper_and_out_of_bounds_spans_are_rejected():
    draft = _draft()
    for item in draft["actions"]:
        item["evidence_refs"] = ["paper"]
    with pytest.raises(ValueError, match="outside its supplied source"):
        compile_actions(
            draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
            source_spans={"paper": SourceSpan("paper", 0, 5)},
            instruction=INSTRUCTION,
        )
    with pytest.raises(ValueError, match="outside its supplied source"):
        compile_actions(
            draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
            source_spans={"paper": SourceSpan("instruction", 0, 1000)},
            instruction=INSTRUCTION,
        )


def test_loop_body_requires_its_own_evidence_reference():
    draft = _mapped_draft()
    draft["actions"][0]["evidence_refs"] = ["task"]
    with pytest.raises(ActionPlanError, match="evidence_refs"):
        compile_actions(
            draft, labware_catalog=LABWARE, pipette_catalog=PIPETTES,
            source_spans=SPANS, instruction=INSTRUCTION,
        )


def _catalog_selector_draft() -> dict:
    draft = _mapped_draft()
    loop = draft["actions"][0]
    loop.pop("bindings")
    loop["selector"] = {
        "kind": "catalog_wells", "relation": "zip",
        "series": [
            {"binding": "source_well", "labware": "source", "mode": "all"},
            {"binding": "target_well", "labware": "target", "mode": "all"},
        ],
    }
    return draft


def test_catalog_selector_compacts_ninety_six_explicit_well_mappings():
    draft = _catalog_selector_draft()
    ordered = tuple(f"{row}{column}" for column in range(1, 13)
                    for row in "ABCDEFGH")
    facts = {
        "synthetic_96_tiprack_20ul": LabwareFacts(
            frozenset(ordered), ordered_wells=ordered, is_tiprack=True,
            tip_capacity_ul=20,
        ),
        "synthetic_source": LabwareFacts(frozenset(ordered), ordered_wells=ordered),
        "synthetic_target": LabwareFacts(frozenset(ordered), ordered_wells=ordered),
    }
    code = _compile(draft, labware=facts)
    assert code.count(".pick_up_tip()") == 96
    assert code.count(".aspirate(") == 96
    assert code.count(".dispense(") == 96
    assert "lw_1.wells_by_name()['H12']" in code
    assert "lw_2.wells_by_name()['H12']" in code
    assert code.index("lw_1.wells_by_name()['A1']") < code.index("lw_1.wells_by_name()['H12']")


def test_catalog_selector_uses_model_named_columns_in_selected_order():
    draft = _catalog_selector_draft()
    series = draft["actions"][0]["selector"]["series"]
    for item in series:
        item["mode"] = "column_anchors"
        item["columns"] = [2, 1]
    ordered = tuple(f"{row}{column}" for column in (1, 2)
                    for row in "ABCDEFGH")
    anchors = frozenset({"A1", "A2"})
    facts = {
        name: LabwareFacts(
            frozenset(ordered), ordered_wells=ordered,
            is_tiprack=name == "synthetic_96_tiprack_20ul",
            tip_capacity_ul=20 if name == "synthetic_96_tiprack_20ul" else None,
            multichannel_compatible=True,
            multichannel_anchor_wells=anchors,
        ) for name in LABWARE
    }
    multi = {"synthetic_p20": PipetteFacts(1, 20, 8)}
    code = _compile(draft, labware=facts, pipettes=multi)
    assert code.count(".pick_up_tip()") == 2
    assert code.index("lw_1.wells_by_name()['A2']") < code.index("lw_1.wells_by_name()['A1']")
    series[1]["columns"] = [99]
    with pytest.raises(ActionPlanError, match="absent columns"):
        _compile(draft, labware=facts, pipettes=multi)


def test_catalog_selector_expands_model_selected_wells_within_columns():
    draft = _catalog_selector_draft()
    for item in draft["actions"][0]["selector"]["series"]:
        item["mode"] = "wells_in_columns"
        item["columns"] = [3, 1]
    ordered = tuple(f"{row}{column}" for column in (1, 2, 3)
                    for row in "ABCDEFGH")
    facts = {
        name: LabwareFacts(
            frozenset(ordered), ordered_wells=ordered,
            is_tiprack=name == "synthetic_96_tiprack_20ul",
            tip_capacity_ul=20 if name == "synthetic_96_tiprack_20ul" else None,
        ) for name in LABWARE
    }
    code = _compile(draft, labware=facts)
    assert code.count(".pick_up_tip()") == 16
    assert code.index("lw_1.wells_by_name()['A3']") < code.index(
        "lw_1.wells_by_name()['H3']"
    ) < code.index("lw_1.wells_by_name()['A1']")
    draft["actions"][0]["selector"]["series"][0]["columns"] = []
    with pytest.raises(ActionPlanError, match="needs explicit model-selected columns"):
        _compile(draft, labware=facts)


def test_catalog_selector_rejects_ambiguous_or_wrong_labware_relations():
    draft = _catalog_selector_draft()
    ordered = ("A1", "A2")
    facts = {
        "synthetic_96_tiprack_20ul": LABWARE["synthetic_96_tiprack_20ul"],
        "synthetic_source": LabwareFacts(frozenset(ordered), ordered_wells=ordered),
        "synthetic_target": LabwareFacts(frozenset(ordered), ordered_wells=ordered),
    }
    draft["actions"][0]["bindings"] = [{"source_well": "A1", "target_well": "A1"}]
    with pytest.raises(ActionPlanError, match="exactly one"):
        _compile(draft, labware=facts)
    draft["actions"][0].pop("bindings")
    draft["actions"][0]["actions"][1]["labware"] = "target"
    with pytest.raises(ActionPlanError, match="different labware"):
        _compile(draft, labware=facts)
    draft["actions"][0]["actions"][1]["labware"] = "source"
    draft["actions"][0]["selector"]["series"][1]["columns"] = [1]
    with pytest.raises(ActionPlanError, match="cannot zip unequal"):
        _compile(draft, labware=facts)


def test_catalog_selector_requires_trusted_ordered_wells():
    with pytest.raises(ActionPlanError, match="needs trusted ordered catalog wells"):
        _compile(_catalog_selector_draft())


def test_catalog_selector_same_name_relation_ignores_different_catalog_order():
    draft = _catalog_selector_draft()
    draft["actions"][0]["selector"]["relation"] = "same_name"
    facts = {
        "synthetic_96_tiprack_20ul": LABWARE["synthetic_96_tiprack_20ul"],
        "synthetic_source": LabwareFacts(
            frozenset({"A1", "A2"}), ordered_wells=("A1", "A2")),
        "synthetic_target": LabwareFacts(
            frozenset({"A1", "A2"}), ordered_wells=("A2", "A1")),
    }
    code = _compile(draft, labware=facts)
    assert code.index("lw_2.wells_by_name()['A1']") < code.index(
        "lw_2.wells_by_name()['A2']"
    )
