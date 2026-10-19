"""Versioned continuation for isolated, static-deck native simulations only.

These records establish simulator initial conditions. They are neither a
hardware restore mechanism nor permission to bypass collision checking.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, fields
from typing import Any

from pybravo.controllers.simulation import SimulationController
from pybravo.head_mode import PlateSelection, TipSelection, normalize_head_mode, selected_tip_wells
from pybravo.physics.assets import robot_assets_sha256
from pybravo.types import Axis, GripperDetectionState

SCHEMA_VERSION = "pybravo.simulation-checkpoint.v1"
SUPPORTED_NODES = frozenset({
    "flow/Start", "flow/End", "system/DockGripper", "tips/TipsOn",
    "tips/TipsOff", "liquid/Aspirate", "liquid/Dispense",
})
LIMITATIONS = ["Static deck only", "Linear native transfer workflows only",
               "No hardware restore", "Matching profile, catalog, methods, and robot geometry required"]
_STATE_FIELDS = (
    "positions", "initialized", "homed_axes", "head_type", "head_mode",
    "tip_selection", "tips_on_head", "tips_on_head_mode", "tips_on_head_selection",
    "tip_labware", "tip_definition_id", "attached_tip_length_mm", "plate_selection",
    "tipbox_inventory",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def validate_checkpoint_workflow(graph: dict, library: str = "") -> None:
    """Deliberately restrict v1 instead of dropping unrepresented state."""
    nodes = graph.get("nodes", [])
    if library or any(node.get("type") not in SUPPORTED_NODES for node in nodes):
        raise ValueError("Simulation checkpoints support only linear native transfer workflows without scripts or libraries")
    starts = [node for node in nodes if node.get("type") == "flow/Start"]
    ends = [node for node in nodes if node.get("type") == "flow/End"]
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("Checkpoint workflow requires exactly one Start and End")
    ids = {node["id"] for node in nodes}
    if len(ids) != len(nodes):
        raise ValueError("Checkpoint workflow node IDs must be unique")
    edges: dict[Any, Any] = {}
    output_links: dict[Any, Any] = {}
    incoming: set[Any] = set()
    for link in graph.get("links", []):
        if len(link) < 5 or link[1] not in ids or link[3] not in ids or link[2] != 0 or link[4] != 0:
            raise ValueError("Checkpoint workflow must have only linear flow links")
        if link[1] in edges or link[3] in incoming:
            raise ValueError("Checkpoint workflow cannot branch or merge")
        edges[link[1]] = link[3]
        output_links[link[1]] = link[0]
        incoming.add(link[3])
    for node in nodes:
        outputs = node.get("outputs") or []
        links = (outputs[0].get("links") or []) if outputs else []
        expected = [output_links[node["id"]]] if node["id"] in output_links else []
        if links != expected or any(output.get("links") for output in outputs[1:]):
            raise ValueError("Checkpoint workflow output links differ from its linear flow sequence")
    visited = set()
    node_id = starts[0]["id"]
    while node_id not in visited:
        visited.add(node_id)
        if node_id not in edges:
            break
        node_id = edges[node_id]
    if visited != ids or node_id != ends[0]["id"] or node_id in edges:
        raise ValueError("Checkpoint workflow must be one connected, acyclic Start-to-End sequence")


def _require_simulator(bravo):
    controller = bravo.controller
    if bravo.profile.connection.controller_type != "simulation" or not isinstance(controller, SimulationController):
        raise ValueError("Simulation checkpoints require an isolated simulation controller; hardware restore is forbidden")
    if controller._motion_guard is not None or bravo._engine.is_busy:
        raise ValueError("Simulation checkpoint requires an idle, unguarded simulator")
    if controller._plate_in_gripper or controller._last_error is not None:
        raise ValueError("Checkpoint v1 cannot represent held labware or a controller error")
    return controller


def _deck_state(bravo) -> dict:
    return {str(loc): [asdict(item) for item in stack.items]
            for loc, stack in sorted(bravo.deck._stacks.items()) if stack.items}


def _provenance(bravo) -> dict:
    from pybravo import liquid_classes, tips
    from pybravo.tip_offsets import get_tip_offset_table

    catalog = {}
    for stack in bravo.deck._stacks.values():
        for item in stack.items:
            definition = bravo.labware_catalog.get_definition(item.definition_id)
            if definition is None:
                raise ValueError("Checkpoint deck requires catalog-backed labware")
            catalog[item.definition_id] = asdict(definition)
    return {
        "profile_sha256": _digest(bravo.profile._to_dict()),
        "catalog_sha256": _digest(catalog),
        "robot_assets_sha256": robot_assets_sha256(),
        "methods_sha256": _digest({"tips": tips.list_tip_items(),
                                   "tip_offsets": [asdict(row) for row in get_tip_offset_table().entries],
                                   "liquid_methods": liquid_classes.load_store()}),
    }


def export_checkpoint(bravo, deck_config: dict) -> dict:
    controller = _require_simulator(bravo)
    state = bravo.get_state()
    if any(not inventory["freshness_tracked"] for inventory in state["tipbox_inventory"].values()):
        raise ValueError("Checkpoint v1 requires tracked tip freshness")
    if state["tips_on_head"] and (state["tips_on_head_mode"] is None or state["tips_on_head_selection"] is None):
        raise ValueError("Checkpoint mounted tips lack an exact mounted footprint")
    # All rack objects must still be on this static deck; no displaced inventory
    # may silently disappear from a seemingly complete checkpoint.
    deck_ids = {id(item) for stack in bravo.deck._stacks.values() for item in stack.items}
    if set(bravo._tipbox_rack_inventories) - deck_ids:
        raise ValueError("Checkpoint v1 cannot represent off-deck rack inventory")
    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "deck_config": deck_config,
        "deck_state": _deck_state(bravo),
        "provenance": _provenance(bravo),
        "state": {field: state[field] for field in _STATE_FIELDS},
        "controller": {
            "axes": {axis.name: asdict(controller._axes[axis]) for axis in Axis},
            "gripper_detected": controller._gripper_detected.name,
            "plate_in_gripper": controller._plate_in_gripper,
            "plate_sensor_present": controller._plate_sensor_present,
            "simulated_scan_height_mm": controller._simulated_scan_height_mm,
            "go_button_pressed": controller._go_button_pressed,
        },
    }
    # Reject non-finite values and non-JSON state before promising persistence.
    return json.loads(json.dumps(checkpoint, allow_nan=False))


def _mode(bravo, payload):
    if payload is None:
        return None
    mode = normalize_head_mode(bravo.profile.head.head_type, payload["subset_type"],
                               payload["subset_config"], payload["row_count"], payload["column_count"])
    if mode.to_dict() != payload:
        raise ValueError("Checkpoint contains an invalid head mode")
    return mode


def _selection(bravo, payload, cls):
    if payload is None:
        return None
    kwargs = {field.name: payload[field.name] for field in fields(cls) if field.name in payload}
    if any(type(value) is not int for key, value in kwargs.items() if key not in {"mirror_corner", "head_anchor"}):
        raise ValueError("Checkpoint selection coordinates must be integers")
    selection = cls(**kwargs)
    if selection.to_dict() != payload:
        raise ValueError("Checkpoint contains an invalid selection")
    item = bravo.deck.get_stack(selection.location).top
    if item is None:
        raise ValueError("Checkpoint selection references missing labware")
    rows, cols = int(item.metadata.get("rows", 0)), int(item.metadata.get("cols", 0))
    if cls is TipSelection:
        if selection.row_stride < 1 or selection.col_stride < 1:
            raise ValueError("Checkpoint tip strides must be positive")
        cells = selected_tip_wells(rows, cols, selection)
        if len(cells) != selection.row_count * selection.column_count:
            raise ValueError("Checkpoint tip footprint is incomplete")
    else:
        cells = [(selection.row, selection.col)]
    if any(not (0 <= row < rows and 0 <= col < cols) for row, col in cells):
        raise ValueError("Checkpoint selection is outside its labware")
    return selection


def restore_checkpoint(bravo, checkpoint: dict, deck_config: dict) -> None:
    """Restore after deck setup, before attaching the collision guard.

    Failure discards this isolated simulation; it never restores a physical
    instrument or runs homing/initialization motions.
    """
    controller = _require_simulator(bravo)
    if checkpoint.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported simulation checkpoint schema_version")
    if checkpoint.get("deck_config") != deck_config or checkpoint.get("deck_state") != _deck_state(bravo):
        raise ValueError("Simulation checkpoint deck or resolved geometry differs from the saved static deck")
    if checkpoint.get("provenance") != _provenance(bravo):
        raise ValueError("Simulation checkpoint provenance differs from the current profile, catalog, methods, or robot assets")
    state, control = checkpoint["state"], checkpoint["controller"]
    axes = control["axes"]
    if set(axes) != {axis.name for axis in Axis} or set(state["positions"]) != set(axes):
        raise ValueError("Checkpoint must record every native axis, including W")
    for name, axis_state in axes.items():
        position = axis_state["position"]
        if type(position) not in (int, float) or not math.isfinite(position) or state["positions"][name] != position:
            raise ValueError("Checkpoint axis positions must be consistent and finite")
        if type(axis_state["homed"]) is not bool or type(axis_state["motor_enabled"]) is not bool:
            raise ValueError("Checkpoint axis homing and motor flags must be boolean")
    controller._validate_zg_target(axes["Zg"]["position"])
    mode = _mode(bravo, state["head_mode"])
    mounted_mode = _mode(bravo, state["tips_on_head_mode"])
    selection = _selection(bravo, state["tip_selection"], TipSelection)
    mounted = _selection(bravo, state["tips_on_head_selection"], TipSelection)
    plates = {int(loc): _selection(bravo, value, PlateSelection) for loc, value in state["plate_selection"].items()}
    inventories = {}
    for raw_loc, inventory in state["tipbox_inventory"].items():
        loc = int(raw_loc)
        item = bravo._require_tip_box(loc, operation="Checkpoint restore")
        rows, cols = bravo._tipbox_dimensions(item)
        occupied = {tuple(map(int, cell.split(":"))) for cell in inventory["occupied"]}
        spent = {tuple(map(int, cell.split(":"))) for cell in inventory["spent"]}
        if not spent <= occupied or any(len(cell) != 2 or not (0 <= cell[0] < rows and 0 <= cell[1] < cols) for cell in occupied):
            raise ValueError("Checkpoint tip inventory contains impossible occupied or spent cells")
        if inventory["freshness_tracked"] is not True:
            raise ValueError("Checkpoint v1 requires tracked tip freshness")
        inventories[loc] = (item, occupied, spent)
    expected_racks = {int(loc) for loc in bravo.get_state()["tipbox_inventory"]}
    if set(inventories) != expected_racks:
        raise ValueError("Checkpoint must include every deck rack inventory")
    if type(state["tips_on_head"]) is not bool or type(state["initialized"]) is not bool:
        raise ValueError("Checkpoint robot flags must be boolean")
    if state["tips_on_head"]:
        length = state["attached_tip_length_mm"]
        if mounted is None or mounted_mode is None or not isinstance(length, (int, float)) or not math.isfinite(length) or length <= 0:
            raise ValueError("Checkpoint mounted tips lack their identity, geometry, or footprint")
        item, occupied, _ = inventories[mounted.location]
        if item.name != state["tip_labware"] or bravo._tip_id_for_labware(item) != state["tip_definition_id"]:
            raise ValueError("Checkpoint mounted tip identity differs from its source rack")
        cells = set(selected_tip_wells(*bravo._tipbox_dimensions(item), mounted))
        if cells & occupied or (mounted.row_count, mounted.column_count) != (mounted_mode.row_count, mounted_mode.column_count):
            raise ValueError("Checkpoint mounted tips conflict with rack inventory or head mode")
    elif any(state[key] is not None for key in ("tips_on_head_mode", "tips_on_head_selection", "attached_tip_length_mm")) or state["tip_labware"] or state["tip_definition_id"]:
        raise ValueError("Checkpoint detached tips have inconsistent mounted state")
    if control["plate_in_gripper"] is not False:
        raise ValueError("Checkpoint v1 cannot restore held labware")
    if any(type(control[key]) is not bool for key in ("plate_sensor_present", "go_button_pressed")):
        raise ValueError("Checkpoint controller flags must be boolean")
    scan_height = control["simulated_scan_height_mm"]
    if scan_height is not None and (type(scan_height) not in (int, float) or not math.isfinite(scan_height)):
        raise ValueError("Checkpoint scan height must be finite or null")
    homed = {Axis[name] for name in state["homed_axes"]}
    detected = GripperDetectionState[control["gripper_detected"]]
    # Direct software assignments preserve the W reference exactly; no moves,
    # homing, initialization, or active-instrument side effects occur here.
    for name, axis_state in axes.items():
        axis = controller._axes[Axis[name]]
        axis.position, axis.homed, axis.motor_enabled = axis_state["position"], axis_state["homed"], axis_state["motor_enabled"]
    controller._gripper_detected = detected
    controller._plate_sensor_present = control["plate_sensor_present"]
    controller._simulated_scan_height_mm = control["simulated_scan_height_mm"]
    controller._go_button_pressed = control["go_button_pressed"]
    bravo._initialized, bravo._homed_axes = state["initialized"], homed
    bravo._head_mode, bravo._tips_on_head_mode = mode, mounted_mode
    bravo._tip_selection, bravo._tips_on_head_selection = selection, mounted
    bravo._plate_selection = plates
    bravo._tips_on_head = state["tips_on_head"]
    bravo._tip_labware_name = state["tip_labware"]
    bravo._tip_definition_id = state["tip_definition_id"]
    bravo._attached_tip_length_mm = state["attached_tip_length_mm"]
    for loc, (item, occupied, spent) in inventories.items():
        bravo._tipbox_occupancy[loc], bravo._tipbox_spent[loc] = occupied, spent
        bravo._tipbox_untracked.discard(loc)
        bravo._remember_tipbox_inventory(loc, item)
    if export_checkpoint(bravo, deck_config) != checkpoint:
        raise ValueError("Simulation checkpoint did not round-trip exactly; continuation refused")
