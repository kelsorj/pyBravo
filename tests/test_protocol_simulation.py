"""Compiled liquid workflows run real task logic on a simulation controller."""

from __future__ import annotations

import asyncio
from dataclasses import fields

import pytest

from pybravo import liquid_classes
from pybravo.bravo import Bravo
from pybravo.deck.labware import InMemoryLabwareCatalog, LabwareDefinition
from pybravo.workflow.executor import WorkflowExecutor, _build_task_params
from pybravo.workflow.protocols.compiler import compile_plan
from tests.test_protocol_compiler import protocol_fixture


def liquid_workflow(monkeypatch):
    plan, setup, context, sources = protocol_fixture()
    context["head_type"] = "HT_96_D_70"
    context["tip_definitions"] = [{"id": "st_30ul", "capacity_ul": 30.0, "length_mm": 26.1, "compatible_heads": ["HT_96_D_70"]}]
    rack = context["labware"][1]
    rack.update(tip_definition_id="st_30ul", supported_tip_ids=["st_30ul"], height_mm=50.0,
                disposable_tip_capacity_ul=30.0)
    context["labware"][0].update(height_mm=14.4)
    for row in context["labware"]:
        row.update(length_mm=127.76, width_mm=85.48, offset_x_mm=4.5, offset_y_mm=4.5)
    setup["head_mode"] = {"subset_type": "column", "subset_config": "back_left", "column_count": 1}
    plan["steps"][0]["repeat"] = 2
    plan["steps"][0]["source_values"].append({"field": "repeat", "value": 2.0, "unit": "count", "paragraph_id": "p1"})
    # Two explicitly available columns exercise preservation of partial racks.
    # Columns 1-2 are the ones a back-left single column can reach at deck
    # position 3 inside the configured X range; columns 11-12 would need
    # X 473 mm against a 390 mm limit and are rejected by the reachability check.
    plan["materials"][2]["available_tips"] = [f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)]
    workflow = compile_plan(plan, setup, context, sources=sources)
    bravo = Bravo(mode="simulation")
    field_names = {field.name for field in fields(LabwareDefinition)}
    bravo._labware_catalog = InMemoryLabwareCatalog([
        LabwareDefinition(**{key: val for key, val in row.items() if key in field_names})
        for row in context["labware"]])
    monkeypatch.setattr(liquid_classes, "get_liquid_class", lambda *args, **kwargs: {
        "name": "Water", "tip_id": "st_30ul", "tip_capacity_ul": 30.0,
        "aspirate": {}, "dispense": {}, "equation": {},
    })
    return bravo, workflow


@pytest.mark.asyncio
async def test_compiled_repeated_transfer_uses_exact_tips_and_runs_all_tasks(monkeypatch):
    bravo, workflow = liquid_workflow(monkeypatch)
    events = []

    async def event(payload):
        events.append(payload)

    executor = WorkflowExecutor(bravo, workflow["graph"], deck_config=workflow["deck"],
                                preview_animation=False, strict_validation=True, on_event=event)
    try:
        await asyncio.wait_for(executor.execute(), timeout=20)
        assert not [e for e in events if e["type"] in {"workflow:error", "workflow:task_warning"}], events
        assert events[-1]["type"] == "workflow:complete" and events[-1]["status"] == "ok"
        assert bravo._occupied_tip_wells(3) == set()
        assert len(bravo._occupied_tip_wells(4)) == 16
        assert not bravo._tips_on_head
        assert sum(e.get("task_name") == "liquid/Aspirate" for e in events) == 2
        assert sum(e.get("task_name") == "liquid/Dispense" for e in events) == 2
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_strict_task_failure_never_passes_or_waits_for_ignore(monkeypatch):
    bravo, workflow = liquid_workflow(monkeypatch)
    events = []

    async def event(payload):
        events.append(payload)

    async def fail(**kwargs):
        raise RuntimeError("Simulated liquid operation failed")

    monkeypatch.setattr(bravo, "aspirate", fail)
    executor = WorkflowExecutor(bravo, workflow["graph"], deck_config=workflow["deck"],
                                preview_animation=False, strict_validation=True, on_event=event)
    try:
        await asyncio.wait_for(executor.execute(), timeout=10)
        assert any(e["type"] == "workflow:error" for e in events)
        assert not any(e["type"] == "workflow:complete" for e in events)
        assert not any(e.get("task_name") == "liquid/Dispense" for e in events)
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_manual_checkpoint_requires_exact_confirmation_and_aborts_cleanly():
    bravo = Bravo(mode="simulation")
    graph = {"nodes": [
        {"id": 1, "type": "flow/Start", "outputs": [{"links": [1]}]},
        {"id": 2, "type": "system/Manual", "properties": {"message": "Inspect the plate", "duration_s": 2},
         "outputs": [{"links": [2]}]},
        {"id": 3, "type": "flow/End"}], "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]]}
    events = []
    executor = None

    async def event(payload):
        events.append(payload)
        if payload["type"] == "workflow:user_prompt":
            assert payload["required_confirmation"] is True
            assert "2 seconds" in payload["message"]
            executor.resolve_user_prompt(payload["request_id"], value="")

    executor = WorkflowExecutor(bravo, graph, preview_animation=False, on_event=event)
    try:
        await asyncio.wait_for(executor.execute(), timeout=3)
        assert any(e["type"] == "workflow:error" for e in events)
        assert not any(e["type"] == "workflow:complete" for e in events)
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_strict_unknown_labware_and_python_are_rejected_before_tasks():
    bravo = Bravo(mode="simulation")
    try:
        executor = WorkflowExecutor(bravo, {"nodes": []}, strict_validation=True, library_src="raise RuntimeError('executed')")
        with pytest.raises(ValueError, match="does not execute Python"):
            await executor.execute()
        executor = WorkflowExecutor(bravo, {"nodes": []}, strict_validation=True,
                                    deck_config={"1": {"labware_id": "invented"}})
        with pytest.raises(RuntimeError, match="Unknown or invalid labware"):
            await executor._setup_deck()
    finally:
        bravo.disconnect()


@pytest.mark.parametrize("node_type,method", [("liquid/Aspirate", Bravo.aspirate),
    ("liquid/Dispense", Bravo.dispense), ("liquid/Mix", Bravo.mix)])
def test_approved_pipetting_height_reaches_correct_task_argument(node_type, method):
    import inspect
    properties = {"location": 2, "volume": 10, "distance_from_bottom": 2.5, "cycles": 4,
                  "pipette_technique": "Approved", "pre_aspirate_volume": 1, "post_aspirate_volume": 2,
                  "blowout_volume": 3, "empty_tips": True, "dynamic_tip_extension": 0.5,
                  "dynamic_tip_retraction": 0.6}
    kwargs = _build_task_params(node_type, properties)
    inspect.signature(method).bind(None, **kwargs)
    if node_type == "liquid/Mix":
        assert kwargs["aspirate_distance"] == kwargs["dispense_distance"] == 2.5
        assert kwargs["mix_cycles"] == 4
    else:
        assert kwargs["distance_from_bottom"] == 2.5
    assert kwargs["pipette_technique"] == "Approved"


@pytest.mark.asyncio
async def test_rejected_head_mode_aborts_before_tip_pickup(monkeypatch):
    bravo, workflow = liquid_workflow(monkeypatch)
    events = []

    async def event(payload):
        events.append(payload)

    def rejected(*args, **kwargs):
        raise RuntimeError("Head cannot adopt requested footprint")

    monkeypatch.setattr(bravo, "set_head_mode", rejected)
    executor = WorkflowExecutor(bravo, workflow["graph"], deck_config=workflow["deck"],
                                preview_animation=False, strict_validation=True, on_event=event)
    try:
        await executor.execute()
        assert any(e["type"] == "workflow:error" and "footprint" in e["error"] for e in events)
        assert len(bravo._occupied_tip_wells(3)) == 16
        assert not any(e.get("task_name") == "liquid/Aspirate" for e in events)
    finally:
        bravo.disconnect()
