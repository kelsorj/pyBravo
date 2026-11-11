"""Synthetic action plans must lower without inventing Bravo setup or science."""

import json
from copy import deepcopy

import httpx
import pytest

from pybravo.evals.text2wetlab.action_ir import LabwareFacts, PipetteFacts, SourceSpan
from pybravo.evals.text2wetlab.bravo_lowering import lower_actions_to_designer
from pybravo.web import server
from pybravo.workflow.storage import WorkflowStorage

INSTRUCTION = "Pick up tips. Transfer 5 uL from A1 to B1. Drop tips."
SPANS = {"sentence": SourceSpan("instruction", 0, len(INSTRUCTION))}


def _sources():
    wells = frozenset({"A1", "A2", "B1", "B2"})
    return ({"ot2_tiprack": LabwareFacts(frozenset({"A1", "A2"}), is_tiprack=True,
                                         tip_capacity_ul=200.0),
             "ot2_plate": LabwareFacts(wells)},
            {"p300_single": PipetteFacts(1.0, 300.0, 1)})


def _context():
    return {
        "context_hash": "synthetic-catalog", "head_type": "HT_8_D_LT",
        "machine_id": "synthetic-bravo", "head_max_volume_ul": 200.0,
        "profile": {"teachpoints": {str(slot): {"x": float(slot), "y": 1.0, "z": 2.0}
                                    for slot in range(1, 10)}},
        "labware": [
            {"id": "rack", "name": "Synthetic tip rack", "kind": "tip_box",
             "base_class": "tip_box", "rows": 8, "cols": 12, "wells": 96,
             "spacing_x_mm": 9.0, "spacing_y_mm": 9.0,
             "height_mm": 20.0, "width_mm": 120.0, "length_mm": 80.0},
            {"id": "plate", "name": "Synthetic plate", "kind": "sbs_plate",
             "base_class": "microplate", "rows": 8, "cols": 12, "wells": 96,
             "spacing_x_mm": 9.0, "spacing_y_mm": 9.0, "well_depth_mm": 10.0,
             "height_mm": 20.0, "width_mm": 120.0, "length_mm": 80.0},
            {"id": "trash", "name": "Synthetic waste", "kind": "waste",
             "base_class": "waste", "wells": 0,
             "height_mm": 20.0, "width_mm": 120.0, "length_mm": 80.0},
        ],
        "tipbox_choices": [{"labware_id": "rack", "tip_definition_id": "tip-200",
                            "execution_ready": True, "tip_capacity_ul": 200.0}],
        "liquid_classes": [{"liquid_class_id": "lc-200", "name": "Synthetic 200 uL method",
                            "head_type": "HT_8_D_LT", "machine_id": "synthetic-bravo",
                            "tip_id": "tip-200", "tip_capacity_ul": 200.0,
                            "status": "qualified", "execution_ready": True}],
    }


def _setup():
    return {
        "labware": {
            "tips": {"labware_id": "rack", "slot": 1, "tip_definition_id": "tip-200"},
            "source": {"labware_id": "plate", "slot": 2},
            "target": {"labware_id": "plate", "slot": 3},
        },
        "pickup": {"actions[0]": {"rack_id": "tips", "tip_anchor_row": 0,
                                   "tip_anchor_col": 0}},
        "disposal": {"labware_id": "trash", "slot": 4},
        "liquid_class_id": "lc-200", "distance_from_bottom_mm": 1.0,
        "head_mode": {"subset_type": "single_barrel", "subset_config": "back_left"},
    }


def _plan():
    return {
        "labware": [
            {"id": "tips", "load_name": "ot2_tiprack", "slot": 1},
            {"id": "source", "load_name": "ot2_plate", "slot": 2},
            {"id": "target", "load_name": "ot2_plate", "slot": 3},
        ],
        "pipettes": [{"id": "pip", "model": "p300_single", "mount": "left",
                      "tip_rack_ids": ["tips"]}],
        "actions": [
            {"kind": "pickup", "pipette": "pip", "evidence_refs": ["sentence"]},
            {"kind": "aspirate", "pipette": "pip", "labware": "source",
             "well": "A1", "volume_ul": 5.0, "evidence_refs": ["sentence"]},
            {"kind": "dispense", "pipette": "pip", "labware": "target",
             "well": "B1", "volume_ul": 5.0, "evidence_refs": ["sentence"]},
            {"kind": "drop", "pipette": "pip", "evidence_refs": ["sentence"]},
        ],
    }


def _lower(plan, setup=None, context=None, source_catalogs=None):
    labware, pipettes = source_catalogs or _sources()
    return lower_actions_to_designer(
        plan, setup=setup or _setup(), bravo_context=context or _context(),
        source_labware_catalog=labware, source_pipette_catalog=pipettes,
        source_spans=SPANS, instruction=INSTRUCTION,
        model_provenance={"model": "synthetic-qwen", "source_id": "synthetic-plan"},
    )


def test_direct_actions_preserve_exact_wells_volumes_order_and_provenance():
    result = _lower(_plan())
    assert result.status == "blocked"
    assert [item["code"] for item in result.blockers] == ["PHYSICAL_DECK_INVENTORY_UNCONFIRMED"]
    assert result.workflow["library"] == ""
    assert result.workflow["protocol_generated_draft"] is True
    assert result.workflow["protocol_draft_status"] == "unreviewed"
    assert result.workflow["protocol_generated_provenance"]["source_plan_sha256"] == result.source_plan_sha256
    nodes = result.workflow["graph"]["nodes"]
    assert [node["type"] for node in nodes] == [
        "flow/Start", "tips/TipsOn", "liquid/Aspirate", "liquid/Dispense",
        "tips/TipsOff", "flow/End",
    ]
    assert [node["properties"]["volume"] for node in nodes[2:4]] == [5.0, 5.0]
    assert [node["properties"]["anchor"] for node in nodes[2:4]] == ["A1", "B1"]
    assert [node["properties"]["location"] for node in nodes[1:5]] == [1, 2, 3, 4]
    assert nodes[2]["properties"]["_action_provenance"]["action_path"] == "actions[1]"
    assert nodes[2]["properties"]["_protocol_step_id"] == "unreviewed-node-3"
    assert nodes[2]["properties"]["_source_citation"]["paragraph_id"] == "sentence"
    assert len(result.workflow["graph"]["links"]) == len(nodes) - 1
    assert result.source_plan_sha256
    assert result.bravo_context_hash == "synthetic-catalog"
    assert any("strict Bravo simulation" in item for item in result.review_requirements)


def test_unsupported_module_actions_keep_ordered_manual_handoffs_and_group_blockers():
    plan = _plan()
    plan["modules"] = [{"id": "thermal", "model": "thermocycler"}]
    plan["actions"][2:2] = [
        {"kind": "set_block_temperature", "module": "thermal", "celsius": 37.0,
         "hold_seconds": 120.0, "evidence_refs": ["sentence"]},
        {"kind": "open_lid", "module": "thermal", "evidence_refs": ["sentence"]},
    ]
    result = _lower(plan)
    assert result.status == "blocked"
    assert result.workflow is not None
    nodes = result.workflow["graph"]["nodes"]
    assert [node["type"] for node in nodes[2:6]] == [
        "liquid/Aspirate", "system/Manual", "system/Manual", "liquid/Dispense",
    ]
    assert "37.0" in nodes[3]["properties"]["message"]
    assert "120.0" in nodes[3]["properties"]["message"]
    module_blockers = [item for item in result.blockers
                       if item["code"] == "OT2_MODULE_OPERATION_UNSUPPORTED"]
    assert len(module_blockers) == 1
    assert [item["path"] for item in module_blockers[0]["occurrences"]] == ["actions[2]", "actions[3]"]


def test_explicit_repetition_keeps_model_selected_well_pairing():
    plan = _plan()
    plan["actions"][1:3] = [{
        "kind": "for_each", "evidence_refs": ["sentence"],
        "bindings": [{"destination_well": "B1"}, {"destination_well": "B2"}],
        "actions": [
            {"kind": "aspirate", "pipette": "pip", "labware": "source",
             "well": "A1", "volume_ul": 5.0, "evidence_refs": ["sentence"]},
            {"kind": "dispense", "pipette": "pip", "labware": "target",
             "well": "$destination_well", "volume_ul": 5.0,
             "evidence_refs": ["sentence"]},
        ],
    }]
    result = _lower(plan)
    assert result.status == "blocked"
    nodes = result.workflow["graph"]["nodes"]
    liquid = [node for node in nodes if node["type"].startswith("liquid/")]
    assert [(node["type"], node["properties"]["anchor"], node["properties"]["volume"])
            for node in liquid] == [
                ("liquid/Aspirate", "A1", 5.0), ("liquid/Dispense", "B1", 5.0),
                ("liquid/Aspirate", "A1", 5.0), ("liquid/Dispense", "B2", 5.0),
            ]
    assert liquid[-1]["properties"]["_action_provenance"]["action_path"] == (
        "actions[1].bindings[1].actions[1]"
    )


def test_absent_binding_and_unverified_catalog_pair_block_graph():
    plan = _plan()
    setup = _setup()
    del setup["pickup"]["actions[0]"]
    context = _context()
    context["tipbox_choices"] = []
    result = _lower(plan, setup=setup, context=context)
    assert result.status == "blocked"
    assert result.workflow is None
    assert {item["code"] for item in result.blockers} >= {"TIP_PICKUP_BINDING_MISSING"}
    setup = _setup()
    result = _lower(plan, setup=setup, context=context)
    assert result.workflow is None
    assert {item["code"] for item in result.blockers} >= {"TIP_HEAD_PAIR_UNVERIFIED"}


def test_source_channels_or_stroke_volume_cannot_be_silently_changed():
    plan = _plan()
    sources = _sources()
    sources[1]["p300_single"] = PipetteFacts(1.0, 300.0, 8)
    result = _lower(plan, source_catalogs=sources)
    assert result.workflow is None
    assert any(item["code"] == "SOURCE_CHANNEL_MAPPING_UNCONFIRMED" for item in result.blockers)
    plan = deepcopy(plan)
    plan["actions"][1]["volume_ul"] = 250.0
    plan["actions"][2]["volume_ul"] = 250.0
    result = _lower(plan)
    assert result.workflow is None
    assert any(item["code"] == "STROKE_VOLUME_OUTSIDE_CATALOG" for item in result.blockers)


def test_comment_is_visible_and_blocked_because_manual_node_pauses():
    plan = _plan()
    plan["actions"].insert(2, {"kind": "comment", "message": "Record external observation",
                               "evidence_refs": ["sentence"]})
    result = _lower(plan)
    assert result.status == "blocked"
    assert result.workflow is not None
    manual = result.workflow["graph"]["nodes"][3]
    assert manual["type"] == "system/Manual"
    assert "Record external observation" in manual["properties"]["message"]
    comment_blockers = [item for item in result.blockers
                        if item["code"] == "OT2_COMMENT_HAS_NO_BRAVO_LOG_NODE"]
    assert len(comment_blockers) == 1
    assert comment_blockers[0]["occurrences"][0]["path"] == "actions[2]"


def test_catalog_class_does_not_claim_qualification_or_physical_inventory():
    context = _context()
    context["liquid_classes"][0].pop("status")
    context["liquid_classes"][0].pop("execution_ready")
    result = _lower(_plan(), context=context)
    assert result.workflow is not None
    assert result.status == "blocked"
    assert {item["code"] for item in result.blockers} == {
        "LIQUID_CLASS_QUALIFICATION_UNVERIFIED",
        "PHYSICAL_DECK_INVENTORY_UNCONFIRMED",
    }
    assert result.workflow["protocol_generated_draft"] is True


def test_missing_physical_catalog_dimensions_or_profile_teachpoint_blocks_graph():
    context = _context()
    context["labware"][1].pop("height_mm")
    context["profile"]["teachpoints"].pop("2")
    result = _lower(_plan(), context=context)
    assert result.workflow is None
    assert {item["code"] for item in result.blockers} >= {
        "BRAVO_LABWARE_FOOTPRINT_UNVERIFIED", "BRAVO_TEACHPOINT_UNVERIFIED",
    }


def test_explicit_catalog_head_restriction_blocks_graph():
    context = _context()
    context["labware"][1]["compatible_head_types"] = ["HT_96_D_200"]
    result = _lower(_plan(), context=context)
    assert result.workflow is None
    assert any(item["code"] == "BRAVO_LABWARE_HEAD_INCOMPATIBLE"
               for item in result.blockers)


@pytest.mark.asyncio
async def test_lowered_graph_stays_unreviewed_after_load_and_save(tmp_path, monkeypatch):
    result = _lower(_plan())
    storage = WorkflowStorage(tmp_path / "workflows")
    saved = storage.create_generated_draft(
        result.workflow, provenance=result.workflow["protocol_generated_provenance"],
        issues=list(result.blockers),
    )
    monkeypatch.setattr(server, "_get_workflow_storage", lambda: storage)
    monkeypatch.setattr(server, "_bravo", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                 base_url="http://test") as client:
        loaded = (await client.get(f"/api/workflows/{saved['id']}")).json()
        assert loaded["protocol_generated_draft"] is True
        assert loaded["protocol_draft_status"] == "unreviewed"
        assert loaded["graph"]["nodes"][2]["properties"]["_protocol_step_id"] == "unreviewed-node-3"
        assert loaded["protocol_draft_issues"][0]["code"] == "PHYSICAL_DECK_INVENTORY_UNCONFIRMED"

        async def assert_rehearsal_only():
            # An unreviewed draft may enter the isolated simulator, never hardware.
            denied = await client.post(f"/api/workflows/{saved['id']}/execute")
            assert denied.status_code == 409
            assert "unreviewed draft" in denied.json()["detail"]
            # This synthetic draft names labware and a liquid class the real
            # catalog does not have, so its rehearsal stops at preflight validation.
            rehearsal = await client.post(f"/api/workflows/{saved['id']}/simulate")
            assert rehearsal.status_code == 400, rehearsal.text
            assert rehearsal.json()["detail"]["invalid_nodes"]

        await assert_rehearsal_only()
        loaded["name"] = "Edited unreviewed lowering"
        updated = await client.put(f"/api/workflows/{saved['id']}", json=loaded)
        assert updated.status_code == 200
        assert updated.json()["protocol_generated_draft"] is True
        assert updated.json()["protocol_draft_status"] == "unreviewed"
        stripped = deepcopy(loaded)
        stripped.pop("protocol_generated_draft")
        stripped.pop("id")
        assert (await client.post("/api/workflows", json=stripped)).status_code == 409
        stripped.pop("protocol_generated_provenance")
        stripped.pop("protocol_generated_root_id")
        stripped.pop("protocol_draft_status")
        assert (await client.post("/api/workflows", json=stripped)).status_code == 409
        assert (await client.post("/api/workflows", json=result.workflow)).status_code == 409
        saved_without_marker = deepcopy(storage.get_workflow(saved["id"]))
        for key in ("protocol_generated_draft", "protocol_generated_provenance",
                    "protocol_generated_root_id", "protocol_draft_status"):
            saved_without_marker.pop(key)
        (tmp_path / "workflows" / f"{saved['id']}.json").write_text(
            json.dumps(saved_without_marker), encoding="utf-8",
        )
        await assert_rehearsal_only()
