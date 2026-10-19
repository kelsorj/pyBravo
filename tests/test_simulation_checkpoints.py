"""Checkpoint continuation preserves native state without replaying motion."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pybravo.state_machine.engine import TaskStatus
from pybravo.types import Axis
from pybravo.workflow.checkpoints import (
    SCHEMA_VERSION, export_checkpoint, restore_checkpoint, validate_checkpoint_workflow,
)
from pybravo.workflow.executor import WorkflowExecutor
from tests.test_bravo_init import _make_workflow_executor_bravo


def _graph():
    return {"nodes": [
        {"id": 1, "type": "flow/Start", "outputs": [{"links": [1]}]},
        {"id": 2, "type": "system/DockGripper", "outputs": [{"links": [2]}]},
        {"id": 3, "type": "flow/End"},
    ], "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]]}


@pytest.fixture
async def pair(monkeypatch):
    source, deck = _make_workflow_executor_bravo()
    target, _ = _make_workflow_executor_bravo()
    for bravo in (source, target):
        await WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck)._setup_deck()
        bravo.set_head_mode("single_barrel", "back_left")

        async def complete(task):
            task.status = TaskStatus.COMPLETED

        monkeypatch.setattr(bravo._engine, "execute", complete)
    yield source, target, deck
    source.disconnect()
    target.disconnect()


async def test_mounted_tip_and_nonzero_w_roundtrip_without_initializing_or_motion(pair, monkeypatch):
    source, target, deck = pair
    source.set_tip_selection(2, 15, 23)
    await source.tips_on(2)
    source.controller._axes[Axis.W].position = 4.25
    source.controller._axes[Axis.X].motor_enabled = True
    source.controller._axes[Axis.Y].homed = False
    source._homed_axes = {Axis.X, Axis.Z}
    source._initialized = True
    checkpoint = export_checkpoint(source, deck)
    monkeypatch.setattr(target, "initialize", AsyncMock(side_effect=AssertionError("must not initialize")))
    monkeypatch.setattr(target.controller, "move", lambda *a, **k: pytest.fail("must not move"))
    restore_checkpoint(target, checkpoint, deck)
    assert export_checkpoint(target, deck) == checkpoint
    assert target.controller.get_position(Axis.W) == 4.25
    assert target._tipbox_inventory_owners[2] is target.deck.get_stack(2).top
    assert target._tipbox_occupancy[2] == source._tipbox_occupancy[2]
    assert target._tips_on_head_selection == source._tips_on_head_selection
    assert target.controller._axes[Axis.Y].homed is False


async def test_returned_tip_stays_spent_after_another_restore(pair):
    source, target, deck = pair
    source.set_tip_selection(2, 15, 23)
    await source.tips_on(2)
    await source.tips_off(2)
    checkpoint = export_checkpoint(source, deck)
    restore_checkpoint(target, checkpoint, deck)
    assert target._spent_tip_wells(2) == {(15, 23)}
    with pytest.raises(RuntimeError, match="spent tips|No legal fresh tip anchors"):
        await target.tips_on(2)
    assert export_checkpoint(target, deck) == checkpoint


@pytest.mark.parametrize("change, expected", [
    (lambda cp: cp.update(schema_version="future"), "schema_version"),
    (lambda cp: cp["provenance"].update(profile_sha256="changed"), "provenance"),
    (lambda cp: cp["provenance"].update(robot_assets_sha256="changed"), "provenance"),
    (lambda cp: cp["deck_config"]["5"][0].update(is_lidded=True), "deck"),
    (lambda cp: cp["deck_state"]["5"][0].update(height=999), "geometry"),
    (lambda cp: cp["controller"]["axes"]["W"].update(position=float("nan")), "finite"),
    (lambda cp: cp["controller"]["axes"].pop("W"), "every native axis"),
    (lambda cp: cp["state"]["tipbox_inventory"]["2"]["spent"].append("99:99"), "impossible"),
    (lambda cp: cp["state"]["tipbox_inventory"]["2"].update(freshness_tracked=False), "freshness"),
    (lambda cp: cp["state"]["tipbox_inventory"].clear(), "every deck rack"),
])
async def test_invalid_checkpoint_is_refused(pair, change, expected):
    source, target, deck = pair
    cp = export_checkpoint(source, deck)
    change(cp)
    with pytest.raises((ValueError, KeyError), match=expected):
        restore_checkpoint(target, cp, deck)


async def test_changed_catalog_and_profile_refuse_restore(pair):
    source, target, deck = pair
    cp = export_checkpoint(source, deck)
    target.profile.gripper.y_offset += 1
    with pytest.raises(ValueError, match="provenance"):
        restore_checkpoint(target, cp, deck)


async def test_hardware_profile_refuses_both_export_and_restore(pair):
    source, target, deck = pair
    cp = export_checkpoint(source, deck)
    target.profile.connection.controller_type = "agile"
    with pytest.raises(ValueError, match="hardware restore is forbidden"):
        restore_checkpoint(target, cp, deck)
    with pytest.raises(ValueError, match="hardware restore is forbidden"):
        export_checkpoint(target, deck)


def test_graph_contract_rejects_scripts_cycles_and_disconnected_nodes():
    validate_checkpoint_workflow(_graph())
    graph = _graph()
    graph["nodes"][1]["type"] = "logic/Script"
    with pytest.raises(ValueError, match="linear native"):
        validate_checkpoint_workflow(graph)
    graph = _graph()
    graph["links"].append([3, 3, 0, 1, 0, -1])
    graph["nodes"][2]["outputs"] = [{"links": [3]}]
    with pytest.raises(ValueError, match="acyclic"):
        validate_checkpoint_workflow(graph)
    graph = _graph()
    graph["nodes"].append({"id": 4, "type": "liquid/Aspirate"})
    with pytest.raises(ValueError, match="connected"):
        validate_checkpoint_workflow(graph)


def test_capture_is_not_supported_by_unchecked_executor():
    with pytest.raises(ValueError, match="only for checked simulation"):
        WorkflowExecutor(object(), _graph(), capture_checkpoint=True)


async def test_capabilities_and_optional_request_contract(monkeypatch):
    from pybravo.web import server

    assert (await server.simulation_checkpoint_capabilities())["schema_version"] == SCHEMA_VERSION
    run = AsyncMock(return_value={"status": "started"})
    monkeypatch.setattr(server, "_run_designer_workflow", run)
    await server.simulate_designer_workflow("old")
    run.assert_awaited_once_with("old", mode="simulate")
    run.reset_mock()
    payload = server.SimulationCheckpointRequest(capture_checkpoint=True, checkpoint={"saved": True})
    await server.simulate_designer_workflow("new", payload)
    run.assert_awaited_once_with("new", mode="simulate", checkpoint_options={
        "capture_checkpoint": True, "checkpoint": {"saved": True},
    })


async def test_executor_emits_initial_and_final_checkpoints_only_on_success(monkeypatch):
    from tests.test_collision_runtime import _Probe
    from tests.test_executor_physics_runtime import _executor

    bravo, executor, events = _executor(monkeypatch, _Probe())
    executor._capture_checkpoint = True
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:complete", events[-1]
        assert events[-1]["simulation_checkpoint"]["schema_version"] == SCHEMA_VERSION
        start = next(event for event in events if event["type"] == "workflow:start")
        assert start["simulation_checkpoint"]["state"]["positions"] == start["positions"]
    finally:
        bravo.disconnect()


async def test_restore_failure_is_terminal_before_start_and_has_no_checkpoint(monkeypatch):
    from tests.test_collision_runtime import _Probe
    from tests.test_executor_physics_runtime import _executor

    bravo, executor, events = _executor(monkeypatch, _Probe())
    executor._capture_checkpoint = True
    executor._checkpoint = {"schema_version": "invalid"}
    try:
        await executor.execute()
        assert events[-1]["type"] == "workflow:error"
        assert "schema_version" in events[-1]["error"]
        assert not any(event["type"] == "workflow:start" for event in events)
        assert "simulation_checkpoint" not in events[-1]
    finally:
        bravo.disconnect()
