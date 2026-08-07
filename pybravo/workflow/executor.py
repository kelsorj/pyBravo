"""
WorkflowExecutor — Walks a Litegraph node graph and dispatches tasks.

Converts each node type to its corresponding StateMachineTask, executes
through the StateMachineEngine, and broadcasts progress over WebSocket.
"""

from __future__ import annotations

import asyncio
import builtins as _builtins_module
import copy
import json
import math
import re
import uuid
from typing import Any, Callable

import structlog

from pybravo.deck.labware import Labware
from pybravo.tips import get_tip_length_mm

logger = structlog.get_logger(__name__)


# Sentinel for missing var lookups so callers can distinguish "key exists
# with value None" from "key not present at all" (relevant for var:!NAME
# strict resolution).
_MISSING = object()


class OperatorCancelled(Exception):
    """Raised inside a Script sandbox when the operator dismisses a
    prompt_user() modal with Cancel. Propagates up through _run_user_script
    to the standard script-error pause so the operator gets the usual
    Retry / Edit & Retry / Abort choices."""


# Accident-prevention allow-list for user-authored workflow scripts. This is
# NOT a security boundary — the user running the workflow already has full
# control of the Bravo via the REST API on the same machine. The allow-list
# catches accidental `open("/etc/passwd")` typos, not adversarial authors.
# `__import__` IS included so `import` statements work — scripts need stdlib
# modules (xml.etree, subprocess, pathlib, etc.) for real-world protocols.
# Names deliberately OMITTED: open, exec, eval, compile, input,
# getattr, setattr, delattr, globals, locals, vars (shadowed by the
# blackboard dict in the script namespace), exit, quit.
_SAFE_BUILTIN_NAMES = (
    "__import__",
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "chr",
    "complex", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "hash", "hex", "id", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    "True", "False", "None",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "ZeroDivisionError",
    "ArithmeticError", "LookupError", "StopIteration",
)
_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(_builtins_module, name)
    for name in _SAFE_BUILTIN_NAMES
    if hasattr(_builtins_module, name)
}


def _labware_to_dict(lw: Labware) -> dict[str, Any]:
    """Shallow-dict representation of a Labware instance for WebSocket frames.

    The live object stays in `self._vars` for subsequent scripts to use; this
    helper only renders a JSON-safe snapshot for broadcast to the UI.
    """
    return {
        "__type__": "Labware",
        "name": getattr(lw, "name", ""),
        "id": getattr(lw, "id", ""),
        "labware_type": getattr(lw, "labware_type", ""),
        "barcode": getattr(lw, "barcode", ""),
        "is_lidded": bool(getattr(lw, "is_lidded", False)),
        "is_sealed": bool(getattr(lw, "is_sealed", False)),
        "tags": dict(getattr(lw, "tags", {}) or {}),
    }


def _safe_json_snapshot(value: Any, _depth: int = 0) -> Any:
    """Recursively coerce a value into something `json.dumps` will accept.

    Used for every `workflow:vars_update` / `workflow:script_result` /
    `workflow:complete` payload so a user script stashing an arbitrary Python
    object into `vars` never breaks the WebSocket stream. Non-JSON-native
    types fall back to `repr(v)`; Labware instances render as their shallow
    dict. Cycle / depth guard prevents runaway recursion on self-referential
    structures.
    """
    if _depth > 6:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        # float NaN/Inf will still fail `json.dumps(..., allow_nan=False)`,
        # but default json.dumps accepts them — good enough.
        return value
    if isinstance(value, Labware):
        return _labware_to_dict(value)
    if isinstance(value, dict):
        return {
            str(k): _safe_json_snapshot(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json_snapshot(v, _depth + 1) for v in value]
    # Fallback: stringify. Covers custom objects, datetimes, paths, etc.
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


class _PlatesProxy:
    """Live accessor over `bravo._deck` exposed to workflow scripts as `plates`.

    - `plates[loc]` returns the top Labware instance; raises KeyError if empty.
    - `plates.get(loc, default=None)` returns default instead of raising.
    - `for loc, plate in plates:` iterates occupied deck positions.
    - `loc in plates` tests occupancy.

    The proxy holds a reference to the deck (not a snapshot), so pick/place in
    a later node is visible to subsequent script reads.
    """

    def __init__(self, deck: Any) -> None:
        self._deck = deck

    def _top(self, location: int) -> Labware | None:
        try:
            stack = self._deck.get_stack(int(location))
        except Exception:
            return None
        return stack.top if stack is not None else None

    def __getitem__(self, location: int) -> Labware:
        top = self._top(location)
        if top is None:
            raise KeyError(f"No plate at location {location}")
        return top

    def get(self, location: int, default: Any = None) -> Any:
        top = self._top(location)
        return top if top is not None else default

    def __iter__(self):
        # MIN_LOCATION..MAX_LOCATIONS inclusive (typed module exposes these)
        for loc in range(1, 10):
            top = self._top(loc)
            if top is not None:
                yield loc, top

    def __contains__(self, location: Any) -> bool:
        try:
            return self._top(int(location)) is not None
        except Exception:
            return False

    def __repr__(self) -> str:
        occupied = [loc for loc, _ in self]
        return f"<plates occupied={occupied}>"

# Maps Litegraph node type strings to their Bravo operation method names.
# The key is the Litegraph type (e.g., "liquid/Aspirate"),
# and the value is a (method_name, param_mapper) tuple.
NODE_TYPE_MAP: dict[str, str] = {
    "liquid/Aspirate": "aspirate",
    "liquid/Dispense": "dispense",
    "liquid/Mix": "mix",
    "tips/TipsOn": "tips_on",
    "tips/TipsOff": "tips_off",
    "plate/PickPlace": "pick_place",
    "plate/Stack": "stack_plates",
    "plate/Destack": "destack_plate",
    "plate/Mount":    "mount_plates",
    "plate/Unmount":  "unmount_plate",
    "plate/Delid": "delid_plate",
    "plate/Relid": "relid_plate",
    "sensor/ReadBarcode": "read_barcode",
    "sensor/ScanStackHeight": "scan_stack_height",
    "system/Initialize": "initialize",
    "system/Home": "home",
    "system/DockGripper": "dock_gripper",
}


_ITER_PREFIX = "iter:"
_VAR_PREFIX = "var:"


def _lookup_var(vars_dict: dict[str, Any], dotted_key: str, sentinel: Any) -> Any:
    """Walk a dotted key (`plate.barcode`) through nested dicts / attr-bearing objects.

    Returns `sentinel` on any missing segment. Tries dict lookup first, then
    attribute access, so both `vars["x"] = {"y": 1}` and `vars["x"] = obj` (with
    `obj.y`) work. Keeps the var: resolver uniform across plain dicts and
    user-stashed Labware / custom objects.
    """
    if not dotted_key:
        return sentinel
    cur: Any = vars_dict
    parts = dotted_key.split(".")
    # First segment must come from the vars dict itself.
    head, *rest = parts
    if isinstance(cur, dict):
        if head not in cur:
            return sentinel
        cur = cur[head]
    else:
        return sentinel
    for seg in rest:
        if cur is None:
            return sentinel
        if isinstance(cur, dict):
            if seg not in cur:
                return sentinel
            cur = cur[seg]
            continue
        # Fall back to attribute access (Labware, dataclasses, etc.)
        if hasattr(cur, seg):
            cur = getattr(cur, seg)
            continue
        return sentinel
    return cur


def _resolve_dynamic_properties(
    properties: dict[str, Any],
    loop_stack: list[int],
    vars_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expand `iter:v1,v2,...` AND `var:NAME` property values.

    Two-pass resolution:
      1. `iter:a,b,c` → pick element using innermost loop index (modulo cycling).
         Unchanged behavior from the prior `_resolve_iter_properties`.
      2. `var:NAME` → look up in `vars_dict` (supports dotted paths like
         `var:plate.barcode`). Missing keys yield None (permissive). The
         strict form `var:!NAME` raises RuntimeError if missing, which
         surfaces through the operator-prompt modal.

    Returns a NEW dict; the input is never mutated. Non-prefixed strings pass
    through unchanged so comma-bearing properties (`axes = "X,Y,Z"`) are safe.
    """
    if vars_dict is None:
        vars_dict = {}
    has_loop = bool(loop_stack)
    resolved: dict[str, Any] = {}
    for key, val in properties.items():
        # Pass 1 — iter: expansion.
        if isinstance(val, str) and val.startswith(_ITER_PREFIX):
            if has_loop:
                i = loop_stack[-1]
                parts = [p.strip() for p in val[len(_ITER_PREFIX):].split(",")]
                val = parts[i % len(parts)] if parts else val
            # If not in a loop, leave the iter:-prefixed string alone
            # (matches prior fast-path behavior).

        # Pass 2 — var: lookup (runs on the iter-resolved value).
        if isinstance(val, str) and val.startswith(_VAR_PREFIX):
            key_name = val[len(_VAR_PREFIX):]
            strict = key_name.startswith("!")
            if strict:
                key_name = key_name[1:]
                looked = _lookup_var(vars_dict, key_name, _MISSING)
                if looked is _MISSING:
                    raise RuntimeError(
                        f"Required workflow var {key_name!r} is not set"
                    )
                val = looked
            else:
                val = _lookup_var(vars_dict, key_name, None)

        resolved[key] = val
    return resolved


# Back-compat alias; some call-sites (and mental models) still reference the
# iter-only name. Keep it as a thin wrapper so no existing call breaks.
def _resolve_iter_properties(
    properties: dict[str, Any],
    loop_stack: list[int],
) -> dict[str, Any]:
    return _resolve_dynamic_properties(properties, loop_stack, vars_dict=None)


def _parse_anchor(anchor: str) -> tuple[int, int]:
    """Parse an anchor well label like 'A1', 'B2', 'D4' into absolute (row, col).

    Letter → row (A=0, B=1, ..., Z=25). Digit suffix → col (1-based input →
    0-based output). This matches the well-labeling convention users see in the
    Bravo diagnostics UI and in lab conversation (`"aspirate at A2"`).

    Raises ValueError on malformed input; the caller's error-handling path
    surfaces it through the operator-prompt modal.
    """
    if not isinstance(anchor, str):
        raise ValueError(f"Anchor must be a string like 'A1'; got {anchor!r}")
    s = anchor.strip().upper()
    if len(s) < 2 or not s[0].isalpha() or not s[1:].isdigit():
        raise ValueError(
            f"Invalid anchor {anchor!r}; expected letter+digit (e.g. 'A1', 'B2', 'D4')"
        )
    row = ord(s[0]) - ord("A")
    col = int(s[1:]) - 1
    if row < 0 or col < 0:
        raise ValueError(f"Invalid anchor {anchor!r}")
    return row, col


def _build_task_params(node_type: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Convert Litegraph node properties to Bravo method kwargs."""
    params: dict[str, Any] = {}

    if node_type in ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix"):
        params["location"] = int(properties.get("location", 1))
        params["volume"] = float(properties.get("volume", 0))
        if properties.get("liquid_class"):
            params["liquid_class"] = properties["liquid_class"]
        if properties.get("tip_touch") is not None:
            params["tip_touch"] = bool(properties["tip_touch"])
        if node_type == "liquid/Dispense" and properties.get("blowout"):
            params["blowout"] = float(properties["blowout"])
        if node_type == "liquid/Mix" and properties.get("cycles"):
            params["mix_cycles"] = int(properties["cycles"])

    elif node_type == "tips/TipsOn":
        params["location"] = int(properties.get("location", 1))
        # Head mode handled separately if needed

    elif node_type == "tips/TipsOff":
        params["location"] = int(properties.get("location", 1))

    elif node_type == "plate/PickPlace":
        params["from_location"] = int(properties.get("pick_location", 1))
        params["to_location"] = int(properties.get("place_location", 2))

    elif node_type == "plate/Stack":
        params["source_location"] = int(properties.get("source_location", 1))
        params["base_location"] = int(properties.get("base_location", 2))

    elif node_type == "plate/Destack":
        params["source_location"] = int(properties.get("source_location", 1))
        params["destination_location"] = int(properties.get("destination_location", 2))

    elif node_type == "plate/Mount":
        # Same property shape as Stack: source plate lands on the
        # base plate. Difference is in the task handler, which flags
        # the pair as mounted once placed.
        params["source_location"] = int(properties.get("source_location", 1))
        params["base_location"] = int(properties.get("base_location", 2))

    elif node_type == "plate/Unmount":
        # Same property shape as Destack: pull the mounted top plate
        # off and set it down at an empty pad. Task refuses to run on
        # a non-mounted pair.
        params["source_location"] = int(properties.get("source_location", 1))
        params["destination_location"] = int(properties.get("destination_location", 2))

    elif node_type in ("plate/Delid", "plate/Relid"):
        params["location"] = int(properties.get("location", 1))

    elif node_type in ("sensor/ReadBarcode", "sensor/ScanStackHeight"):
        params["location"] = int(properties.get("location", 1))
        if node_type == "sensor/ScanStackHeight":
            # Optional: blank / zero / negative means "no expectation; just
            # report whatever was measured". Any positive int triggers the
            # validation step which pops the retry/ignore/abort modal on
            # mismatch.
            raw_expected = properties.get("expected_count")
            if raw_expected not in (None, "", 0, "0"):
                try:
                    expected_int = int(raw_expected)
                    if expected_int > 0:
                        params["expected_count"] = expected_int
                except (TypeError, ValueError):
                    pass

    elif node_type == "system/Home":
        axes_str = properties.get("axes", "X,Y,Z,W,G,Zg")
        params["axes"] = [a.strip() for a in axes_str.split(",") if a.strip()]

    return params


class WorkflowExecutor:
    """Walks a serialized Litegraph graph and dispatches each node as a Bravo task."""

    def __init__(
        self,
        bravo: Any,
        graph_data: dict[str, Any],
        deck_config: dict[str, Any] | None = None,
        on_event: Callable[[dict], Any] | None = None,
        runtime_state: dict[str, Any] | None = None,
        preview_animation: bool = True,
        library_src: str = "",
    ) -> None:
        self.bravo = bravo
        self._deck_config = deck_config or {}
        # Workflow-level "Library" — Python defined once at run start, its
        # top-level bindings (functions, constants) merged into every Script
        # node's namespace. Populated by _compile_library() at execute()
        # entry; empty dict if the workflow has no library or the library
        # failed to compile (failures abort the run with workflow:error).
        self._library_src = library_src or ""
        self._library_ns: dict[str, Any] = {}
        self._runtime_state = copy.deepcopy(runtime_state or {})
        self._preview_animation = preview_animation
        self._nodes = {n["id"]: n for n in graph_data.get("nodes", [])}
        self._links = {}
        for link in graph_data.get("links", []):
            # Litegraph link format: [link_id, origin_id, origin_slot, target_id, target_slot, type]
            if len(link) >= 5:
                link_id, origin_id, origin_slot, target_id, target_slot = link[:5]
                self._links[link_id] = {
                    "origin_id": origin_id,
                    "origin_slot": origin_slot,
                    "target_id": target_id,
                    "target_slot": target_slot,
                }
        self._on_event = on_event
        self._aborted = False
        self._data_bus: dict[int, Any] = {}  # node_id -> output data value
        self._current_node_id: int | None = None
        self._tipbox_removed_cells: dict[str, set[str]] = {}
        # Stack of active loop iteration indices. Pushed on entry to each
        # Loop body iteration, popped on exit. `_resolve_dynamic_properties`
        # reads the top of this stack to expand `iter:v1,v2,...` values.
        self._loop_stack: list[int] = []
        # Workflow-scoped blackboard. Lives from workflow:start to
        # workflow:complete / workflow:error. Script nodes mutate it
        # directly (same live ref); sensor nodes with `store_as` write
        # their result here; any property value of the form `var:NAME`
        # resolves against this dict at dispatch time.
        self._vars: dict[str, Any] = {}
        # Script-error pause mechanism. When a Script node raises, the
        # executor emits `workflow:script_error` and waits on this event.
        # The operator's chosen action is stored in _script_action_*
        # before the event is set so _walk can read the decision.
        self._script_pause_event: asyncio.Event = asyncio.Event()
        self._script_action: str = ""          # "retry" | "edit_retry" | "abort"
        self._script_action_new_source: str = ""  # new script text for edit_retry
        # User-prompt pause mechanism. When a Script calls `prompt_user(...)`,
        # we open a Future keyed by request_id, emit `workflow:user_prompt`,
        # and block the sandbox thread on future.result(). The operator's
        # typed value arrives via POST /api/user_prompt_response, which calls
        # resolve_user_prompt() — that sets the future and the script resumes.
        self._user_prompts: dict[str, asyncio.Future] = {}
        # Captured at execute() entry so sandbox threads can submit coroutines
        # back to the main loop via run_coroutine_threadsafe.
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # Last-broadcast JSON snapshot, to suppress no-op vars_update
        # frames when an assignment leaves the observable state unchanged.
        self._vars_last_snapshot: str = ""
        # Workflow status-light state machine. Solid blue while a workflow
        # runs normally; switches to blinking yellow whenever a step fails
        # (engine raises, operator-prompt modal goes up); switches BACK to
        # solid blue as soon as the next step completes after an error
        # (i.e. the operator chose Retry and the retry succeeded). On
        # workflow completion the lights are returned to a solid green
        # idle. We track _light_in_error so we only emit transitions, not
        # on every step.
        self._light_in_error: bool = False

    @staticmethod
    def _selection_keys(selection: Any) -> set[str]:
        if selection is None:
            return set()
        row = int(getattr(selection, "row", 0))
        col = int(getattr(selection, "col", 0))
        row_count = max(1, int(getattr(selection, "row_count", 1)))
        column_count = max(1, int(getattr(selection, "column_count", 1)))
        return {
            f"{current_row}:{current_col}"
            for current_row in range(row, row + row_count)
            for current_col in range(col, col + column_count)
        }

    @staticmethod
    def _serialize_tipbox_removed_cells(cells: dict[str, set[str]]) -> dict[str, list[str]]:
        return {
            str(location): sorted(values)
            for location, values in sorted(cells.items(), key=lambda item: int(item[0]))
            if values
        }

    def _set_removed_tip_cells(self, location: int | None, selection: Any) -> None:
        if location is None or selection is None:
            return
        keys = self._selection_keys(selection)
        if not keys:
            return
        self._tipbox_removed_cells[str(int(location))] = set(keys)

    def _restore_tip_cells(self, location: int | None, selection: Any) -> None:
        if location is None or selection is None:
            return
        loc_key = str(int(location))
        current = set(self._tipbox_removed_cells.get(loc_key, set()))
        if not current:
            return
        current.difference_update(self._selection_keys(selection))
        if current:
            self._tipbox_removed_cells[loc_key] = current
        else:
            self._tipbox_removed_cells.pop(loc_key, None)

    def _apply_node_head_mode(self, head_mode_payload: Any) -> None:
        """Apply a per-workflow-node head_mode override (if present) to
        sim_bravo. The payload may be a dict with subset_type / subset_config
        / row_count / column_count (same shape as the runtime snapshot).
        Silently no-ops if the payload is empty or malformed."""
        if not head_mode_payload or not isinstance(head_mode_payload, dict):
            return
        try:
            self.bravo.set_head_mode(
                head_mode_payload.get("subset_type"),
                head_mode_payload.get("subset_config"),
                head_mode_payload.get("row_count"),
                head_mode_payload.get("column_count"),
            )
        except Exception:
            pass

    def _apply_runtime_snapshot(self) -> None:
        if not self._runtime_state:
            return

        head_mode = self._runtime_state.get("head_mode") or {}
        try:
            self.bravo.set_head_mode(
                head_mode.get("subset_type"),
                head_mode.get("subset_config"),
                head_mode.get("row_count"),
                head_mode.get("column_count"),
            )
        except Exception:
            pass

        tip_selection = self._runtime_state.get("tip_selection") or {}
        tip_location = tip_selection.get("location")
        if tip_location is not None:
            try:
                self.bravo.set_tip_selection(
                    int(tip_location),
                    int(tip_selection.get("row", 0)),
                    int(tip_selection.get("col", 0)),
                )
            except Exception:
                self.bravo._tip_selection = None

        plate_selection = self._runtime_state.get("plate_selection") or {}
        for raw_location, selection in plate_selection.items():
            try:
                self.bravo.set_plate_selection(
                    int(raw_location),
                    int(selection.get("row", 0)),
                    int(selection.get("col", 0)),
                )
            except Exception:
                self.bravo._plate_selection.pop(int(raw_location), None)

        self.bravo._tips_on_head = bool(self._runtime_state.get("tips_on_head"))
        self.bravo._tip_labware_name = str(self._runtime_state.get("tip_labware") or "")
        self.bravo._tip_definition_id = str(self._runtime_state.get("tip_definition_id") or "")
        attached_tip_length_mm = self._runtime_state.get("attached_tip_length_mm")
        self.bravo._attached_tip_length_mm = (
            None if attached_tip_length_mm is None else float(attached_tip_length_mm)
        )
        self.bravo._tips_on_head_mode = None
        self.bravo._tips_on_head_selection = None
        if self.bravo._tips_on_head:
            mounted_mode_payload = self._runtime_state.get("tips_on_head_mode") or head_mode
            try:
                self.bravo._tips_on_head_mode = self.bravo.set_head_mode(
                    mounted_mode_payload.get("subset_type"),
                    mounted_mode_payload.get("subset_config"),
                    mounted_mode_payload.get("row_count"),
                    mounted_mode_payload.get("column_count"),
                )
                self.bravo.set_head_mode(
                    head_mode.get("subset_type"),
                    head_mode.get("subset_config"),
                    head_mode.get("row_count"),
                    head_mode.get("column_count"),
                )
            except Exception:
                self.bravo._tips_on_head_mode = self.bravo._head_mode

            mounted_selection = self._runtime_state.get("tips_on_head_selection") or {}
            mounted_location = mounted_selection.get("location")
            if mounted_location is not None and self.bravo._tips_on_head_mode is not None:
                try:
                    stack = self.bravo._deck.get_stack(int(mounted_location))
                    labware = None if stack is None else stack.top
                    if labware is not None:
                        self.bravo._tips_on_head_selection = self.bravo._selection_from_clicked_tip(
                            int(mounted_location),
                            labware,
                            self.bravo._tips_on_head_mode,
                            int(mounted_selection.get("row", 0)),
                            int(mounted_selection.get("col", 0)),
                            purpose="pickup",
                        )
                except Exception:
                    self.bravo._tips_on_head_selection = None

        self._tipbox_removed_cells = {}
        if self.bravo._tips_on_head and self.bravo._tips_on_head_selection is not None:
            self._set_removed_tip_cells(
                getattr(self.bravo._tips_on_head_selection, "location", None),
                self.bravo._tips_on_head_selection,
            )

    def _current_runtime_event_state(self) -> dict[str, Any]:
        active_tip_capacity_ul = self._runtime_state.get("active_tip_capacity_ul")
        if active_tip_capacity_ul is None:
            try:
                active_tip_capacity_ul = self.bravo.active_tip_capacity_ul()
            except Exception:
                active_tip_capacity_ul = None
        return {
            "type": "workflow:runtime_state",
            "head_type": getattr(self.bravo.profile.head.head_type, "name", None),
            "head_mode": None if self.bravo._head_mode is None else self.bravo._head_mode.to_dict(),
            "tip_selection": None if self.bravo._tip_selection is None else self.bravo._tip_selection.to_dict(),
            "plate_selection": {
                str(location): selection.to_dict()
                for location, selection in sorted(self.bravo._plate_selection.items())
            },
            "tips_on_head": bool(self.bravo._tips_on_head),
            "tips_on_head_mode": None if self.bravo._tips_on_head_mode is None else self.bravo._tips_on_head_mode.to_dict(),
            "tips_on_head_selection": None if self.bravo._tips_on_head_selection is None else self.bravo._tips_on_head_selection.to_dict(),
            "tip_labware_name": self.bravo._tip_labware_name or "",
            "tip_definition_id": self.bravo._tip_definition_id or "",
            "attached_tip_length_mm": self.bravo._attached_tip_length_mm,
            "active_tip_capacity_ul": active_tip_capacity_ul,
            "tipbox_removed_cells": self._serialize_tipbox_removed_cells(self._tipbox_removed_cells),
        }

    async def _setup_deck(self) -> None:
        """Configure the Bravo deck from the workflow's deck configuration.

        Each deck-config list entry represents one plate in the stack at that
        location, ordered bottom-first. The first entry goes through
        `bravo.set_labware` so all its side effects (tipbox occupancy,
        metadata, deck_updated event) fire exactly once; subsequent entries
        are appended via `deck.add` so a multi-plate stack survives intact.
        Previously every entry called set_labware, whose `set_single` replaces
        the stack — only the last entry survived, turning a 4-plate stack
        into a 1-plate stack.
        """
        from pybravo.deck.labware import Labware

        for loc_str, items in self._deck_config.items():
            loc = int(loc_str)
            # items can be a single dict (old format) or a list (stack format)
            stack = items if isinstance(items, list) else [items]
            for idx, item in enumerate(stack):
                labware_id = item.get("labware_id", "")
                if not labware_id:
                    continue
                if idx == 0:
                    try:
                        self.bravo.set_labware(
                            loc,
                            labware_id,
                            is_lidded=item.get("is_lidded", False),
                            is_sealed=item.get("is_sealed", False),
                        )
                    except (ValueError, Exception) as exc:
                        logger.warning(
                            "Could not set labware at location %d: %s (continuing anyway)",
                            loc, exc,
                        )
                        # Even if the catalog doesn't know this labware, we still
                        # want simulation to proceed — set a minimal placeholder.
                        try:
                            placeholder = Labware(
                                id=labware_id,
                                name=item.get("name", labware_id),
                                height=float(item.get("height_mm", 14.4)),
                                width=float(item.get("width_mm", 85.48)),
                                length=float(item.get("length_mm", 127.76)),
                                labware_type=item.get("kind", "sbs_plate"),
                                wells=int(item.get("wells", 0)),
                            )
                            self.bravo._deck.set_single(loc, placeholder)
                        except Exception as inner_exc:
                            logger.warning("Placeholder labware also failed: %s", inner_exc)
                else:
                    # Stack entry #2..N — push onto the existing stack rather
                    # than replacing it. Prefer the catalog-backed instance so
                    # heights / dimensions are accurate.
                    pushed = None
                    try:
                        defn = self.bravo._labware_catalog.get_definition(labware_id)
                        if defn is not None:
                            pushed = Labware.from_definition(
                                defn,
                                is_lidded=item.get("is_lidded", False),
                                is_sealed=item.get("is_sealed", False),
                            )
                    except Exception as exc:
                        logger.warning(
                            "Could not resolve labware %s at location %d: %s",
                            labware_id, loc, exc,
                        )
                    if pushed is None:
                        try:
                            pushed = Labware(
                                id=labware_id,
                                name=item.get("name", labware_id),
                                height=float(item.get("height_mm", 14.4)),
                                width=float(item.get("width_mm", 85.48)),
                                length=float(item.get("length_mm", 127.76)),
                                labware_type=item.get("kind", "sbs_plate"),
                                wells=int(item.get("wells", 0)),
                            )
                        except Exception as exc:
                            logger.warning(
                                "Stacked placeholder at location %d failed: %s",
                                loc, exc,
                            )
                            continue
                    try:
                        self.bravo._deck.add(loc, pushed)
                    except Exception as exc:
                        logger.warning(
                            "Could not push stack entry #%d at location %d: %s",
                            idx + 1, loc, exc,
                        )

    def abort(self) -> None:
        """Request abort of the running workflow."""
        self._aborted = True

    def resolve_script_error(self, action: str, new_source: str = "") -> bool:
        """Resolve a pending script-error pause.

        Called from the ``/api/script_action`` REST endpoint when the operator
        chooses Retry, Edit & Retry, or Abort in the script-error modal.

        Returns ``True`` if a pause was actually pending and we unblocked it.
        """
        if self._script_pause_event.is_set():
            return False  # nothing pending
        self._script_action = action
        self._script_action_new_source = new_source
        self._script_pause_event.set()
        return True

    async def _open_user_prompt(
        self,
        request_id: str,
        node_id: int,
        message: str,
        default: str,
    ) -> str:
        """Register a Future for an operator prompt, emit the WS event, and
        await the answer. Called from the sandbox thread via
        run_coroutine_threadsafe — runs on the main event loop so the
        emit/await can actually progress."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._user_prompts[request_id] = fut
        await self._emit({
            "type": "workflow:user_prompt",
            "request_id": request_id,
            "node_id": node_id,
            "message": message,
            "default": default,
        })
        try:
            return await fut
        finally:
            self._user_prompts.pop(request_id, None)

    def resolve_user_prompt(
        self,
        request_id: str,
        value: str = "",
        cancelled: bool = False,
    ) -> bool:
        """Resolve a pending prompt_user() pause.

        Called from the ``/api/user_prompt_response`` REST endpoint when the
        operator submits the modal. On cancel, the sandbox's prompt_user()
        call raises OperatorCancelled, which falls through to the existing
        script-error pause (Retry / Edit & Retry / Abort).

        Returns True if a pause was actually pending and we unblocked it.
        """
        fut = self._user_prompts.get(request_id)
        if fut is None or fut.done():
            return False
        if cancelled:
            fut.set_exception(OperatorCancelled("Operator cancelled prompt"))
        else:
            fut.set_result(str(value))
        return True

    # ── Status-light state machine ───────────────────────────────────────
    # Three commanded states the operator can read at a glance:
    #   RUNNING  → solid BLUE         period_ms=0,   duty=1.0
    #   ERROR    → blinking YELLOW    period_ms=500, duty=0.5
    #   IDLE     → solid GREEN        period_ms=0,   duty=1.0
    # Each is a single light-set command; we never blink BLUE (operators
    # have learned to read blink-vs-solid as ok-vs-attention).

    def _set_workflow_light(self, mode: str) -> None:
        """Best-effort: set the status light to one of running|error|idle.
        Failures (no controller, lights unsupported) are swallowed so the
        light state never blocks workflow progress."""
        try:
            from pybravo.protocol.commands import LightCommandData
            from pybravo.types import LightColor
            if mode == "running":
                cmd = LightCommandData(light=LightColor.BLUE,
                                       period_ms=0, duty_cycle=1.0)
            elif mode == "error":
                cmd = LightCommandData(light=LightColor.YELLOW,
                                       period_ms=500, duty_cycle=0.5)
            elif mode == "idle":
                cmd = LightCommandData(light=LightColor.GREEN,
                                       period_ms=0, duty_cycle=1.0)
            else:
                return
            ctrl = getattr(self.bravo, "controller", None)
            if ctrl is None:
                return
            set_light = getattr(ctrl, "set_light", None)
            if set_light is None:
                return
            set_light(cmd)
        except Exception as exc:
            logger.debug("workflow light set %s failed: %s", mode, exc)

    def _compile_library(self) -> tuple[bool, str]:
        """Compile + exec the workflow-level library. Returns (ok, error_msg).

        The library namespace is restricted to the same accident-prevention
        allow-list used for Script nodes, plus `log` / `math` / `json` / `re`.
        It deliberately does NOT have access to `vars` / `plates` / `data` /
        `prompt_user` — those are per-node runtime bindings, not library
        concerns. If a helper needs the blackboard or the deck, it should
        accept them as arguments (keeps helpers pure and testable).
        """
        src = (self._library_src or "").strip()
        if not src:
            self._library_ns = {}
            return True, ""
        ns: dict[str, Any] = {
            "__builtins__": _SAFE_BUILTINS,
            "log": logger.info,
            "math": math,
            "json": json,
            "re": re,
        }
        try:
            compiled = compile(self._library_src, "<workflow-library>", "exec")
            exec(compiled, ns, ns)  # noqa: S102 — opt-in user scripting
        except Exception as exc:
            self._library_ns = {}
            return False, f"{type(exc).__name__}: {exc}"
        # Export every top-level binding that isn't a dunder (skip
        # __builtins__, module internals). log/math/json/re intentionally
        # stay exported so helpers can reference them and so that Scripts
        # that rely on the library having pre-imported something still
        # work even if the script itself doesn't re-import.
        self._library_ns = {
            k: v for k, v in ns.items() if not k.startswith("__")
        }
        return True, ""

    async def execute(self) -> None:
        """Execute the workflow from the Start node."""
        # Capture the main loop so sandbox threads (prompt_user) can submit
        # coroutines back via asyncio.run_coroutine_threadsafe.
        self._main_loop = asyncio.get_running_loop()
        # Compile the workflow-level library ONCE at run start so every
        # Script node sees the same helpers. A library compile failure
        # aborts the run before any motion.
        ok, err = self._compile_library()
        if not ok:
            await self._emit({
                "type": "workflow:error",
                "error": f"Library failed to compile: {err}",
            })
            return
        start = self._find_start_node()
        if not start:
            await self._emit({"type": "workflow:error", "error": "No Start node found"})
            return

        # Ensure connected (auto-connect in simulation mode if needed)
        if not self.bravo.is_connected:
            try:
                self.bravo.connect()
                logger.info("Auto-connected bravo for workflow simulation")
            except Exception as exc:
                await self._emit({"type": "workflow:error", "error": f"Failed to connect: {exc}"})
                return

        # Hook into the state machine engine's on_step_complete callback so we
        # can broadcast the real hardware positions after each step. Only
        # installed in execute mode — in simulate/preview mode, the
        # _animate_task_motion path (run from _walk BEFORE each task) is the
        # authoritative position stream, and layering a second stream from
        # the state-machine engine's on_step callback on top produces the
        # "moves repeating 2x" artifact in the 3D viewport.
        engine = self.bravo._engine
        self._step_event_loop = asyncio.get_event_loop()
        def _on_step(task_name: str, step_name: str) -> None:
            """Synchronous callback from the state-machine engine — runs in
            a worker thread.  Schedule position broadcast on the event loop."""
            # In preview/simulation mode the _animate_task_motion path is the
            # authoritative position stream; skip emission here to avoid the
            # "every move animates twice" artifact in the 3D viewport.
            if self._preview_animation:
                return
            try:
                raw = self.bravo.get_all_positions()
                if raw:
                    from pybravo.types import Axis
                    AXIS_NAMES = {0: "X", 1: "Y", 2: "Z", 3: "W", 4: "G", 5: "Zg"}
                    positions = {}
                    for key, val in raw.items():
                        if isinstance(key, Axis):
                            positions[key.name] = val
                        elif isinstance(key, int):
                            positions[AXIS_NAMES.get(key, str(key))] = val
                        else:
                            positions[str(key)] = val
                    logger.info(
                        "hw_step_positions",
                        task=task_name,
                        step=step_name,
                        Z=round(positions.get("Z", 0), 2),
                        Zg=round(positions.get("Zg", 0), 2),
                        X=round(positions.get("X", 0), 2),
                        Y=round(positions.get("Y", 0), 2),
                        G=round(positions.get("G", 0), 2),
                    )
                    asyncio.run_coroutine_threadsafe(
                        self._emit({
                            "type": "workflow:positions",
                            "positions": positions,
                        }),
                        self._step_event_loop,
                    )
                    asyncio.run_coroutine_threadsafe(
                        self._emit({
                            "type": "workflow:node_step",
                            "node_id": self._current_node_id,
                            "step_name": step_name,
                        }),
                        self._step_event_loop,
                    )
            except Exception as e:
                logger.warning("on_step position read failed: %s", e)
        engine.set_step_handler(_on_step)

        # Light state machine: every successful step that runs while we're
        # in the error state means the operator chose Retry and the retry
        # succeeded. Restore solid blue. (Step success during normal flow
        # is a no-op.)
        prev_step_handler = _on_step
        def _on_step_with_light(task_name: str, step_name: str) -> None:
            try:
                if self._light_in_error:
                    self._light_in_error = False
                    self._set_workflow_light("running")
            except Exception:
                pass
            prev_step_handler(task_name, step_name)
        engine.set_step_handler(_on_step_with_light)

        # Engine fires this when a step raises (BEFORE the operator
        # resolves with Retry/Ignore/Abort). Latch error state and blink
        # yellow until the next successful step or workflow end.
        def _on_engine_error(task_error) -> None:
            try:
                if not self._light_in_error:
                    self._light_in_error = True
                    self._set_workflow_light("error")
            except Exception:
                pass
        engine.set_error_handler(_on_engine_error)

        # Set up deck labware from workflow config
        await self._setup_deck()
        self._apply_runtime_snapshot()

        # Solid blue = "workflow running normally". Set BEFORE the start
        # event so any UI listener sees consistent state from t=0.
        self._set_workflow_light("running")

        await self._emit({"type": "workflow:start"})
        await self._emit(self._current_runtime_event_state())
        # Seed the designer Variables panel with the (empty) blackboard so
        # stale state from a previous run is cleared.
        await self._emit_vars_update(force=True)
        # Give the WebSocket client a moment to connect and receive events
        await asyncio.sleep(0.3)

        try:
            await self._walk(start["id"], 0)  # slot 0 = flow output
        except Exception as exc:
            await self._emit({
                "type": "workflow:error",
                "error": str(exc),
                "vars": _safe_json_snapshot(self._vars),
            })
            # Workflow ended in an unhandled error — leave the lights
            # blinking yellow so the operator sees something needs
            # attention before navigating away.
            self._set_workflow_light("error")
            return
        finally:
            # Remove step + error handlers so they don't fire for
            # non-workflow tasks.
            engine.set_step_handler(None)
            engine.set_error_handler(None)

        # Clean completion — green idle so the operator can tell the run
        # is done from across the room.
        self._set_workflow_light("idle")
        await self._emit({
            "type": "workflow:complete",
            "status": "ok",
            "vars": _safe_json_snapshot(self._vars),
        })

    async def _emit_vars_update(self, force: bool = False) -> None:
        """Broadcast the current workflow blackboard snapshot.

        Diff-aware: skips no-op frames where the serialized snapshot hasn't
        changed since the last broadcast. Pass `force=True` to always emit
        (used for the initial seed at workflow start).
        """
        snapshot = _safe_json_snapshot(self._vars)
        try:
            serialized = json.dumps(snapshot, sort_keys=True, default=repr)
        except Exception:
            serialized = repr(snapshot)
        if not force and serialized == self._vars_last_snapshot:
            return
        self._vars_last_snapshot = serialized
        await self._emit({"type": "workflow:vars_update", "vars": snapshot})

    async def _run_user_script(
        self,
        source: str,
        data: Any,
        timeout: float,
        node_id: int | None = None,
    ) -> Any:
        """Execute user Python in a restricted namespace on a worker thread.

        Exposes `data`, `vars` (live blackboard dict), `plates` (live deck
        proxy), `result` (starts None; script assigns to publish), `log`,
        `prompt_user(message, default="")` (opens an operator dialog and
        blocks until the operator answers — returns the typed string, or
        raises OperatorCancelled on Cancel), and `math`/`json`/`re` modules.
        Builtins are the accident-prevention allow-list `_SAFE_BUILTINS` —
        not a security boundary, just a guard against typos like
        `open("/etc/passwd")`.

        Scripts run synchronously on `asyncio.to_thread` so `while True: pass`
        doesn't starve the event loop; `asyncio.wait_for` applies the
        per-node timeout (non-positive timeout = no timeout).
        """
        # Capture the main loop for the prompt_user() bridge. _main_loop is
        # set at execute() entry; fall back to the current loop if someone
        # calls _run_user_script outside a full execute() (e.g. tests).
        main_loop = self._main_loop or asyncio.get_running_loop()
        prompt_node_id = node_id if node_id is not None else -1

        def prompt_user(message: str = "", default: str = "") -> str:
            """Blocking operator prompt. Submits a coroutine back to the
            main loop, then blocks this worker thread on its Future until
            the operator responds via /api/user_prompt_response."""
            request_id = uuid.uuid4().hex
            coro = self._open_user_prompt(
                request_id, prompt_node_id, str(message), str(default),
            )
            fut = asyncio.run_coroutine_threadsafe(coro, main_loop)
            return fut.result()  # propagates OperatorCancelled if cancelled

        def _runner() -> Any:
            # Library bindings FIRST, then per-node runtime names override
            # so a helper named `vars` or `data` in the library can't
            # shadow the blackboard / upstream data.
            ns: dict[str, Any] = {
                "__builtins__": _SAFE_BUILTINS,
            }
            ns.update(self._library_ns)
            ns.update({
                "data": data,
                "vars": self._vars,
                "plates": _PlatesProxy(self.bravo._deck),
                "result": None,
                "log": logger.info,
                "prompt_user": prompt_user,
                "math": math,
                "json": json,
                "re": re,
            })
            compiled = compile(source or "", "<workflow-script>", "exec")
            exec(compiled, ns, ns)  # noqa: S102 — opt-in user scripting
            return ns.get("result")

        if timeout and timeout > 0:
            return await asyncio.wait_for(asyncio.to_thread(_runner), timeout=timeout)
        return await asyncio.to_thread(_runner)

    async def _walk(self, from_node_id: int, from_slot: int) -> None:
        """Follow the flow connection from a node's output slot."""
        if self._aborted:
            return

        next_node = self._follow_flow(from_node_id, from_slot)
        if not next_node:
            return

        node = next_node
        node_type = node.get("type", "")
        node_id = node["id"]
        properties = node.get("properties", {})
        self._current_node_id = node_id

        await self._emit({
            "type": "workflow:node_start",
            "node_id": node_id,
            "task_name": node_type,
        })

        # ── Flow control nodes ────────────────────────────────────────
        if node_type == "flow/End":
            await self._emit({"type": "workflow:node_complete", "node_id": node_id, "status": "ok"})
            return

        if node_type == "flow/IfElse":
            condition = properties.get("condition", "")
            data_value = self._get_input_data(node)
            result = self._evaluate_condition(condition, data_value)
            await self._emit({
                "type": "workflow:branch",
                "node_id": node_id,
                "condition": condition,
                "result": result,
            })
            await self._emit({"type": "workflow:node_complete", "node_id": node_id, "status": "ok"})
            # Follow true (slot 0) or false (slot 1)
            await self._walk(node_id, 0 if result else 1)
            return

        if node_type == "flow/Loop":
            count = int(properties.get("count", 1))
            for i in range(count):
                if self._aborted:
                    return
                await self._emit({
                    "type": "workflow:node_step",
                    "node_id": node_id,
                    "step_index": i,
                    "step_name": f"iteration {i + 1}/{count}",
                })
                # Push this iteration's index so `_resolve_dynamic_properties`
                # can expand `iter:...` values for any node inside the body.
                # The finally-pop guarantees the stack unwinds cleanly even
                # on abort or mid-body exception propagation.
                self._loop_stack.append(i)
                try:
                    # Execute loop body (slot 0)
                    await self._walk(node_id, 0)
                finally:
                    self._loop_stack.pop()
            await self._emit({"type": "workflow:node_complete", "node_id": node_id, "status": "ok"})
            # Follow done output (slot 1)
            await self._walk(node_id, 1)
            return

        # ── Script node ───────────────────────────────────────────────
        # Runs user Python in a restricted namespace, publishes `result` to
        # the data bus (so downstream IfElse on the `result` port works
        # unchanged), and optionally mirrors `result` into the blackboard
        # under the `store_as` key so any later node can read it via
        # `var:NAME` property interpolation. The script has full read/write
        # access to `vars` (the live blackboard dict) and `plates` (live
        # deck accessor), so mutations persist across the run.
        if node_type == "logic/Script":
            # Script properties are NOT iter:/var:-expanded — the code IS
            # the iteration variant. The script body decides how to read
            # `data`, `vars`, `plates` per iteration.
            script_src = str(properties.get("script", "") or "")
            try:
                timeout_val = float(properties.get("timeout", 30) or 0)
            except (TypeError, ValueError):
                timeout_val = 30.0
            store_as = str(properties.get("store_as", "") or "").strip()
            data_in = self._get_input_data(node)

            # Retry loop: on error, pause for operator decision (Retry /
            # Edit & Retry / Abort) via the script-error modal. The loop
            # re-executes the (potentially edited) script until it succeeds
            # or the operator aborts.
            result_val = None
            while True:
                try:
                    result_val = await self._run_user_script(
                        script_src, data_in, timeout_val, node_id=node_id,
                    )
                    break  # success — exit retry loop
                except asyncio.TimeoutError:
                    err_msg = f"Script timed out after {timeout_val}s"
                    err_type = "TimeoutError"
                except Exception as exc:
                    err_msg = str(exc)
                    err_type = type(exc).__name__

                # ── Error: pause and ask operator ────────────────────
                logger.error(
                    "Script node %s error (%s): %s", node_id, err_type, err_msg,
                )
                # Reset the pause event so we can await it.
                self._script_pause_event.clear()
                self._script_action = ""
                self._script_action_new_source = ""
                # Emit the error event — frontend shows the modal.
                await self._emit({
                    "type": "workflow:script_error",
                    "node_id": node_id,
                    "error_type": err_type,
                    "error": err_msg,
                    "script": script_src,
                    "choices": ["retry", "edit_retry", "abort"],
                })
                # NOTE: do NOT emit workflow:node_complete here — the
                # frontend's node_complete handler auto-dismisses modals.
                # The script_error handler already highlights the node red.
                # Wait for operator response (comes via resolve_script_error).
                await self._script_pause_event.wait()

                action = self._script_action
                if action == "abort":
                    logger.info("Operator aborted script node %s", node_id)
                    await self._emit({
                        "type": "workflow:task_aborted",
                        "node_id": node_id,
                        "error": f"Operator aborted: {err_type}: {err_msg}",
                    })
                    self._aborted = True
                    raise RuntimeError(
                        f"Script aborted by operator: {err_type}: {err_msg}"
                    )
                elif action == "edit_retry":
                    new_src = self._script_action_new_source
                    if new_src:
                        script_src = new_src
                        # Persist the edit back into the graph so it survives
                        # if the workflow is saved after the run.
                        node["properties"]["script"] = new_src
                    logger.info(
                        "Operator edited and retrying script node %s", node_id,
                    )
                else:  # "retry"
                    logger.info("Operator retrying script node %s", node_id)
                # Re-highlight as running before re-executing.
                await self._emit({
                    "type": "workflow:node_start",
                    "node_id": node_id,
                    "task_name": "Script (retry)",
                })
                # Loop continues → re-executes the script.

            self._data_bus[node_id] = result_val
            if store_as:
                self._vars[store_as] = result_val
            # Script may have mutated `vars` and/or `plates` directly — emit
            # a fresh snapshot (diff-aware; no-op if nothing changed).
            await self._emit_vars_update()
            await self._emit({
                "type": "workflow:script_result",
                "node_id": node_id,
                "result": _safe_json_snapshot(result_val),
            })
            await self._emit({"type": "workflow:node_complete", "node_id": node_id, "status": "ok"})
            await self._walk(node_id, 0)
            return

        # Expand per-iteration AND per-blackboard overrides before anything
        # downstream reads `properties`. Two-pass resolution:
        #   1. `iter:a,b,c`   → per-loop-iteration pick (unchanged behavior)
        #   2. `var:NAME`     → lookup in self._vars (supports dotted paths
        #                       like `var:plate.barcode` via _lookup_var)
        # Both passes return a NEW dict; the stored graph on `self._nodes`
        # is never mutated.
        properties = _resolve_dynamic_properties(
            properties, self._loop_stack, self._vars,
        )

        # Per-node plate anchor selection. Aspirate/Dispense/Mix nodes carry an
        # `anchor` property (e.g. "A1", "B2", "D4", or "iter:A1,A2,B1,B2" which
        # has already been collapsed above). We translate it to absolute
        # (row, col) and push it through the same `set_plate_selection` API
        # the diagnostics UI uses via `PUT /api/plate_selection`. Bravo's
        # `_effective_plate_selection` cache-check (bravo.py:1608) then picks
        # up this value when building `PlateSelection` for the task.
        #
        # `set_plate_selection` raises RuntimeError on out-of-bounds /
        # unreachable / illegal-for-head-mode anchors; `_parse_anchor` raises
        # ValueError on malformed input. Both surface through the normal
        # operator-prompt modal path.
        if node_type in ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix"):
            anchor_val = properties.get("anchor")
            if anchor_val:  # empty / missing → keep pre-feature auto-pick
                loc = int(properties.get("location", 1))
                row, col = _parse_anchor(str(anchor_val))
                self.bravo.set_plate_selection(loc, row, col)

        # Per-node head_mode + tip-box anchor for tips_on / tips_off. The
        # animation path (below) also applies these but only runs in simulate
        # mode — applying here too lets execute mode (preview_animation=False)
        # honor the workflow's head_mode/anchor instead of the bravo's
        # leftover global state.
        if node_type in ("tips/TipsOn", "tips/TipsOff"):
            self._apply_node_head_mode(properties.get("head_mode"))
            anchor_row = properties.get("anchor_row", properties.get("tip_anchor_row"))
            anchor_col = properties.get("anchor_col", properties.get("tip_anchor_col"))
            if node_type == "tips/TipsOn" and (anchor_row is not None or anchor_col is not None):
                try:
                    self.bravo.set_tip_selection(
                        int(properties.get("location", 1)),
                        int(anchor_row or 0),
                        int(anchor_col or 0),
                    )
                except Exception:
                    pass

        # Animate the motion in 3D FIRST (before executing the actual task).
        # Skip in execute mode — the real task broadcasts live `positions`
        # events that drive the viewport, so the preview animation just doubles
        # cycle time.
        if self._preview_animation:
            await self._animate_task_motion(node_type, properties)

        # ── Task nodes ────────────────────────────────────────────────
        method_name = NODE_TYPE_MAP.get(node_type)
        result: Any = None
        if method_name:
            params = _build_task_params(node_type, properties)
            try:
                method = getattr(self.bravo, method_name, None)
                if method:
                    result = await method(**params)
                    # Store data output for sensor nodes
                    if node_type == "sensor/ReadBarcode" and result:
                        barcode = str(result.get("barcode") or "")
                        self._data_bus[node_id] = barcode
                        # Surface error results with a visible log line. A
                        # ReadBarcode that returns {status: "error", ...}
                        # previously looked identical to a success-with-
                        # empty-string in this block (both produced
                        # barcode="", location=None), which hid the real
                        # failure from the operator. Emit the error to
                        # the workflow event stream too so the UI can
                        # display it.
                        status = str(result.get("status") or "")
                        err_msg = str(result.get("message") or "")
                        if status == "error":
                            logger.warning(
                                "ReadBarcode at node %s returned error: %s",
                                node_id, err_msg or "(no message)",
                            )
                            await self._emit({
                                "type": "workflow:task_warning",
                                "node_id": node_id,
                                "warning": f"Barcode read failed: {err_msg}",
                            })
                        # Attach the barcode to the live plate instance at
                        # this deck location so it travels with the plate
                        # across subsequent pick/place operations. Then emit a
                        # workflow event so the UI can display the result.
                        # Fall back to the requested location from params if
                        # the reader failed and didn't echo one back.
                        location = result.get("location")
                        if location is None:
                            location = params.get("location")
                        labware_name = ""
                        if barcode and location is not None:
                            try:
                                stack = self.bravo._deck.get_stack(int(location))
                                top = stack.top if stack is not None else None
                                if top is not None:
                                    top.barcode = barcode
                                    labware_name = getattr(top, "name", "") or ""
                            except Exception:
                                pass
                        await self._emit({
                            "type": "workflow:barcode_read",
                            "node_id": node_id,
                            "location": location,
                            "labware_name": labware_name,
                            "barcode": barcode,
                            "error": err_msg if status == "error" else "",
                        })
                        # Declarative store-to-blackboard: if the node has
                        # `store_as: "plate_id"`, the scanned barcode is
                        # written to `vars["plate_id"]` so downstream nodes
                        # can reference it as `var:plate_id`. No Python
                        # script needed for the common "scan → remember"
                        # case.
                        store_as = str(properties.get("store_as", "") or "").strip()
                        if store_as:
                            self._vars[store_as] = barcode
                            await self._emit_vars_update()
                    elif node_type == "sensor/ScanStackHeight" and result:
                        height_val = result.get("height", 0)
                        self._data_bus[node_id] = height_val
                        store_as = str(properties.get("store_as", "") or "").strip()
                        if store_as:
                            self._vars[store_as] = height_val
                            await self._emit_vars_update()
                else:
                    logger.warning("Unknown bravo method", method=method_name)
            except Exception as exc:
                # With universal operator-prompt coverage, the engine now
                # pauses on every step failure for Retry/Ignore/Abort. If an
                # exception still bubbles up here it means either the operator
                # chose Abort, or an unexpected internal fault occurred. In
                # simulate/preview mode we forgive (animation was the point);
                # in execute mode we stop the workflow.
                if self._preview_animation:
                    logger.warning("Task execution error (preview/sim, continuing): %s", exc)
                    await self._emit({
                        "type": "workflow:task_warning",
                        "node_id": node_id,
                        "warning": str(exc),
                    })
                else:
                    logger.error("Task execution aborted: %s", exc)
                    await self._emit({
                        "type": "workflow:task_aborted",
                        "node_id": node_id,
                        "error": str(exc),
                    })
                    self._aborted = True
                    raise

            # In execute mode, also honor a clean ABORTED status returned
            # from the Bravo wrapper (some methods swallow abort and return
            # normally with a {status: "aborted"} dict). Stop the workflow.
            if (
                not self._preview_animation
                and isinstance(result, dict)
                and result.get("status") == "aborted"
            ):
                logger.warning("Task reported aborted status; halting workflow: %s", result.get("message"))
                await self._emit({
                    "type": "workflow:task_aborted",
                    "node_id": node_id,
                    "error": str(result.get("message") or "aborted"),
                })
                self._aborted = True
                return

        await self._emit({"type": "workflow:node_complete", "node_id": node_id, "status": "ok"})

        # Brief pause so the frontend can see node transitions in simulation
        if self._preview_animation:
            await asyncio.sleep(0.5)

        # Follow flow output (slot 0 for task nodes)
        await self._walk(node_id, 0)

    # ── Graph Navigation ──────────────────────────────────────────────

    def _find_start_node(self) -> dict | None:
        for node in self._nodes.values():
            if node.get("type") == "flow/Start":
                return node
        return None

    def _follow_flow(self, from_node_id: int, from_slot: int) -> dict | None:
        """Find the node connected to the given output slot of from_node."""
        node = self._nodes.get(from_node_id)
        if not node:
            return None
        outputs = node.get("outputs", [])
        if from_slot >= len(outputs):
            return None
        output = outputs[from_slot]
        link_ids = output.get("links", [])
        if not link_ids:
            return None
        # Follow first link (flow connections should have exactly one)
        link = self._links.get(link_ids[0])
        if not link:
            return None
        return self._nodes.get(link["target_id"])

    def _get_input_data(self, node: dict) -> Any:
        """Get data value from the data input port of a node."""
        inputs = node.get("inputs", [])
        for inp in inputs:
            if inp.get("type") == "string" or inp.get("name") == "data":
                link_id = inp.get("link")
                if link_id is None:
                    continue
                link = self._links.get(link_id)
                if link:
                    return self._data_bus.get(link["origin_id"])
        return None

    def _evaluate_condition(self, condition: str, data_value: Any) -> bool:
        """Evaluate a simple condition expression."""
        if not condition:
            return bool(data_value)
        data_str = str(data_value or "")
        try:
            if "==" in condition:
                _, expected = condition.split("==", 1)
                return data_str.strip() == expected.strip().strip('"').strip("'")
            if "!=" in condition:
                _, expected = condition.split("!=", 1)
                return data_str.strip() != expected.strip().strip('"').strip("'")
            if "contains" in condition:
                _, substring = condition.split("contains", 1)
                return substring.strip().strip('"').strip("'") in data_str
            if ">" in condition:
                _, threshold = condition.split(">", 1)
                return float(data_str) > float(threshold.strip())
            if "<" in condition:
                _, threshold = condition.split("<", 1)
                return float(data_str) < float(threshold.strip())
        except (ValueError, TypeError):
            pass
        return bool(data_value)

    # ── Events ────────────────────────────────────────────────────────

    # ── Kinematic Constants (from types.py and tasks.py) ─────────────
    _GRIPPER_THICKNESS = 18.7452
    _GRIPPER_TO_BASE_GAP = 0.79
    _GRIPPER_RECESS = -20.0
    _ZG_MAX_PLATE = 100.0
    _GRIP_TARGET = 9.0
    _GRIP_OPEN = 0.0
    _Z_CLEARANCE = 10.0

    @property
    def _Z_SAFE(self) -> float:
        """Read z_safe_position from the Bravo profile (matches real state machine)."""
        try:
            val = float(self.bravo.profile.safety.z_safe_position or 0.0)
            return val
        except Exception:
            return 0.0

    def _get_tip_length(self) -> float:
        """Teach tip length: the stored value, else derived from head + teach tip."""
        head = self.bravo.profile.head
        if head.teach_tip_length_mm is not None:
            return float(head.teach_tip_length_mm)
        length = get_tip_length_mm(
            head.head_type, head.teach_tip_id or head.teach_tip_capacity
        )
        if length is None:
            raise RuntimeError(
                f"Teach tip length is not configured for {head.head_type.name} "
                f"with {head.teach_tip_id or head.teach_tip_capacity}."
            )
        return float(length)

    def _gripper_pad_ref_zg(self) -> float:
        """Calibrated Zg when the gripper pad contacts the plate.

        Reads the same profile calibration pair the state machine uses, so the
        simulator cannot drift from real motion.
        """
        g = self.bravo.profile.gripper
        return g.pad_zg_reference_mm + (
            self._get_tip_length() - g.pad_reference_tip_length_mm
        )

    def _solve_pick_place_zg(self, loc: int, stack_height: float, gripper_offset: float) -> tuple[float, float]:
        """Solve Z/Zg for a pick or place position (disposable head formula).

        Matches the real state-machine ``_solve_pick_or_place``: Z stays at
        the safe position and Zg extends to reach the plate.  Only when the
        computed Zg exceeds its range does Z move to compensate.
        """
        _, _, z_tp = self._get_location_xy(loc)
        # Head stays at safe-Z; only the gripper extends down.
        z_current = self._Z_SAFE

        new_zg = z_tp - z_current + self._gripper_pad_ref_zg() - gripper_offset - stack_height

        # Clamp/distribute between Z and Zg
        if new_zg > self._ZG_MAX_PLATE:
            z = z_current + new_zg - self._ZG_MAX_PLATE
            zg = self._ZG_MAX_PLATE
        elif new_zg < self._GRIPPER_RECESS:
            z = z_current + new_zg - self._GRIPPER_RECESS
            zg = self._GRIPPER_RECESS
        else:
            z = z_current
            zg = new_zg

        z_final = max(0.0, min(z, 150.0))
        zg_final = max(-20.0, min(zg, 105.0))
        logger.info(
            "solve_pick_place_zg",
            loc=loc,
            z_tp=z_tp,
            z_safe_position=z_current,
            gripper_pad_ref=self._gripper_pad_ref_zg(),
            gripper_offset=gripper_offset,
            stack_height=stack_height,
            new_zg=new_zg,
            z_final=z_final,
            zg_final=zg_final,
        )
        return (z_final, zg_final)

    def _get_labware_params(self, loc: int) -> dict:
        """Get labware height/gripper_offset for a deck location from the workflow config."""
        items = self._deck_config.get(str(loc), [])
        if isinstance(items, dict):
            items = [items]
        if not items:
            return {"height": 14.4, "stack_height": 14.4, "gripper_offset": 6.0, "well_depth": 10.0}
        # Use catalog definition if available
        top = items[-1] if isinstance(items, list) else items
        labware_id = top.get("labware_id", "")
        cat_def = None
        try:
            cat_def = next((d for d in self.bravo.labware_catalog.list_definitions() if d.id == labware_id), None)
        except Exception:
            pass
        if cat_def:
            return {
                "height": cat_def.height_mm or 14.4,
                "stack_height": cat_def.stack_height_mm or cat_def.height_mm or 14.4,
                "gripper_offset": cat_def.gripper_offset_mm or 6.0,
                "well_depth": cat_def.well_depth_mm or 10.0,
            }
        return {
            "height": float(top.get("height_mm", 14.4)),
            "stack_height": float(top.get("stack_height_mm", top.get("height_mm", 14.4))),
            "gripper_offset": float(top.get("gripper_offset_mm", 6.0)),
            "well_depth": float(top.get("well_depth_mm", 10.0)),
        }

    def _get_stack_support_height(self, loc: int) -> float:
        """Height of all items below the top plate at a location."""
        items = self._deck_config.get(str(loc), [])
        if isinstance(items, dict):
            items = [items]
        if len(items) <= 1:
            return 0.0
        total = 0.0
        for item in items[:-1]:
            total += float(item.get("stack_height_mm", item.get("height_mm", 14.4)))
        return total

    def _get_stacking_height(self, loc: int) -> float:
        """Total stacking height at a location (what a new plate would sit on)."""
        items = self._deck_config.get(str(loc), [])
        if isinstance(items, dict):
            items = [items]
        total = 0.0
        for item in items:
            total += float(item.get("stack_height_mm", item.get("height_mm", 14.4)))
        return total

    def _get_gripper_y_offset(self) -> float:
        """Gripper Y offset from profile."""
        try:
            y_off = self.bravo.profile.gripper.y_offset or 0.0
            return float(y_off)
        except Exception:
            return 0.0

    def _get_location_xy(self, loc: int) -> tuple[float, float, float]:
        """Get X, Y, Z teachpoint for a deck location."""
        # Try to read from the bravo's teachpoints via the proper API
        try:
            from pybravo.types import Axis
            tp = self.bravo._teachpoints
            x = tp.get_teachpoint(loc, Axis.X)
            y = tp.get_teachpoint(loc, Axis.Y)
            z = tp.get_teachpoint(loc, Axis.Z)
            return (float(x), float(y), float(z if z is not None else 60.0))
        except Exception:
            pass
        # Fallback: compute from grid (96ch defaults)
        row = (loc - 1) // 3
        col = (loc - 1) % 3
        return (5.79 + col * 186.69, 5.98 + row * 109.093, 60.0)

    def _get_labware_metadata(self, loc: int) -> dict:
        """Return the catalog metadata dict for the top labware at *loc*.

        Prefers the live ``sim_bravo._deck`` metadata (same source the real
        state machine uses) so well geometry stays consistent with hardware.
        """
        # Prefer the live deck's metadata — this is what AspirateTask/DispenseTask
        # read in the real state machine, and it's already populated by _setup_deck.
        try:
            stack = self.bravo._deck.get_stack(loc)
            if stack is not None and stack.top is not None:
                md = getattr(stack.top, "metadata", None) or {}
                if md:
                    return dict(md)
        except Exception:
            pass
        # Fallback: resolve via workflow deck_config + labware catalog.
        items = self._deck_config.get(str(loc), [])
        if isinstance(items, dict):
            items = [items]
        if not items:
            return {}
        top = items[-1] if isinstance(items, list) else items
        labware_id = top.get("labware_id", "")
        try:
            cat_def = next(
                (d for d in self.bravo.labware_catalog.list_definitions() if d.id == labware_id),
                None,
            )
            if cat_def:
                return cat_def.to_summary()
        except Exception:
            pass
        return dict(top)

    @staticmethod
    def _parse_quadrant(value: Any) -> tuple[int, int]:
        """Map a 384-head-in-1536-plate quadrant to (row_offset, col_offset) in
        units of the destination plate's well pitch.

        Supported forms: ``"A1"``/``"A2"``/``"B1"``/``"B2"`` (case-insensitive)
        or the integers ``1-4`` (1→A1, 2→A2, 3→B1, 4→B2). Returns ``(0, 0)``
        for anything else.
        """
        if value is None:
            return 0, 0
        s = str(value).strip().upper()
        mapping = {
            "A1": (0, 0), "A2": (0, 1), "B1": (1, 0), "B2": (1, 1),
            "1": (0, 0), "2": (0, 1), "3": (1, 0), "4": (1, 1),
        }
        return mapping.get(s, (0, 0))

    def _get_tip_xy(
        self,
        loc: int,
        *,
        purpose: str = "pickup",
        head_mode_override: Any | None = None,
    ) -> tuple[float, float]:
        """Compute the head X,Y for a tips_on/tips_off at *loc* - matches the
        real state machine (``TipsOnTask._tip_xy``):

            x = teach_x + tipbox_offset_x - head_offset_x
            y = teach_y + tipbox_offset_y - head_offset_y
        """
        teach_x, teach_y, _ = self._get_location_xy(loc)
        try:
            from pybravo.deck.geometry import tipbox_anchor_offset_from_teachpoint_mm
            from pybravo.head_mode import tip_task_head_offsets_mm

            stack = self.bravo._deck.get_stack(loc)
            labware = stack.top if stack is not None else None
            if labware is None:
                return teach_x, teach_y
            head_mode = head_mode_override or self.bravo._head_mode
            head_type = self.bravo._profile.head.head_type
            try:
                tip_selection = self.bravo._effective_tip_selection(
                    loc,
                    labware,
                    head_mode,
                    purpose=purpose,
                )
            except Exception:
                tip_selection = None
            if tip_selection is None:
                return teach_x, teach_y
            tipbox_off_x, tipbox_off_y = tipbox_anchor_offset_from_teachpoint_mm(
                labware.metadata, tip_selection,
            )
            head_off_x, head_off_y = tip_task_head_offsets_mm(head_type, head_mode)
            return teach_x + tipbox_off_x - head_off_x, teach_y + tipbox_off_y - head_off_y
        except Exception:
            return teach_x, teach_y

    def _get_well_xy(self, loc: int, row: int = 0, col: int = 0, *, command: str = "Aspirate") -> tuple[float, float]:
        """Compute the head X,Y for well (row, col) — matches the real state
        machine (``AspirateTask._well_xy`` / ``DispenseTask._well_xy``):

            x = teach_x + well_offset_x - head_mode_offset_x
            y = teach_y + well_offset_y - head_mode_offset_y

        This reuses the sim_bravo's live deck + head_mode + plate_selection
        so the calculation stays in sync with hardware.
        """
        teach_x, teach_y, _ = self._get_location_xy(loc)
        try:
            from pybravo.deck.geometry import well_center_offset_from_teachpoint_mm
            from pybravo.head_mode import head_mode_offsets_mm

            # Resolve labware, head_mode, and plate_selection from sim_bravo.
            stack = self.bravo._deck.get_stack(loc)
            labware = stack.top if stack is not None else None
            if labware is None:
                return teach_x, teach_y
            head_mode = self.bravo._head_mode
            head_type = self.bravo._profile.head.head_type
            try:
                plate_selection = self.bravo._effective_plate_selection(
                    loc, labware, head_mode, command_name=command,
                )
            except Exception:
                plate_selection = None
            ps_row = int(plate_selection.row) if plate_selection is not None else row
            ps_col = int(plate_selection.col) if plate_selection is not None else col
            off_x, off_y = well_center_offset_from_teachpoint_mm(
                labware.metadata, row=ps_row, col=ps_col,
            )
            head_off_x, head_off_y = head_mode_offsets_mm(head_type, head_mode)
            return teach_x + off_x - head_off_x, teach_y + off_y - head_off_y
        except Exception:
            return teach_x, teach_y

    def _tip_change_event(
        self,
        *,
        tips_on: bool,
        location: int,
        head_mode: Any,
        tip_selection: Any | None,
        tip_labware_name: str,
        attached_tip_length_mm: float | None = None,
        active_tip_capacity_ul: float | None = None,
        tip_definition_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._current_runtime_event_state()
        state["type"] = "workflow:tips_change"
        state["tips_on"] = bool(tips_on)
        state["location"] = int(location)
        state["head_mode"] = None if head_mode is None else head_mode.to_dict()
        state["tip_selection"] = None if tip_selection is None else tip_selection.to_dict()
        state["tip_labware_name"] = tip_labware_name or state.get("tip_labware_name") or ""
        state["tips_on_head"] = bool(tips_on)
        state["tips_on_head_mode"] = None if not tips_on or head_mode is None else head_mode.to_dict()
        state["tips_on_head_selection"] = None if not tips_on or tip_selection is None else tip_selection.to_dict()
        if attached_tip_length_mm is not None:
            state["attached_tip_length_mm"] = float(attached_tip_length_mm)
        if active_tip_capacity_ul is not None:
            state["active_tip_capacity_ul"] = float(active_tip_capacity_ul)
        if tip_definition_id is not None:
            state["tip_definition_id"] = str(tip_definition_id)
        return state

    async def _animate_task_motion(self, node_type: str, properties: dict) -> None:
        """Generate and broadcast a realistic motion sequence for the 3D viewport."""
        logger.info(
            "animate_task_motion",
            node_type=node_type,
            z_safe=self._Z_SAFE,
            gripper_recess=self._GRIPPER_RECESS,
        )
        loc = (properties.get("location")
               or properties.get("pick_location")
               or properties.get("source_location"))
        if loc is None:
            await self._emit_positions()
            return

        loc = int(loc)
        target_x, target_y, target_z = self._get_location_xy(loc)
        step_delay = 0.4  # seconds between animation keyframes

        if node_type == "sensor/ScanStackHeight":
            # Use real kinematics: head lowers to teachpoint Z, gripper scans from there
            scan_z, scan_zg = self._solve_pick_place_zg(loc, 0.0, 0.0)
            mid_zg = self._GRIPPER_RECESS + (scan_zg - self._GRIPPER_RECESS) * 0.5
            steps = [
                {"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0},   # Move XY, gripper nested
                {"X": target_x, "Y": target_y, "Z": scan_z, "Zg": mid_zg, "G": 0, "W": 0},                        # Lower head, begin scan
                {"X": target_x, "Y": target_y, "Z": scan_z, "Zg": scan_zg, "G": 0, "W": 0},                       # Scan down to deck
                {"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0},    # Retract
            ]
        elif node_type in ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix"):
            from pybravo.state_machine.tasks import _build_liquid_z_geometry

            stack = self.bravo._deck.get_stack(loc)
            labware = stack.top if stack is not None else None
            distance_from_bottom = float(properties.get("distance_from_bottom", properties.get("dist_bottom", 1.0)))
            geometry = _build_liquid_z_geometry(
                teachpoints=self.bravo._teachpoints,
                location=loc,
                labware=labware,
                head_type=self.bravo.profile.head.head_type,
                teach_tip_length_mm=self.bravo.profile.head.teach_tip_length_mm,
                attached_tip_length_mm=self.bravo._attached_tip_length_mm,
                tips_on_head=bool(self.bravo._tips_on_head),
                distance_from_bottom_mm=distance_from_bottom,
            )
            z_top_plate = geometry.top_plane_head_z
            z_liquid = geometry.target_head_z
            volume = float(properties.get("volume", 50))
            row = int(properties.get("anchor_row", properties.get("row", 0)))
            col = int(properties.get("anchor_col", properties.get("col", properties.get("column", 0))))
            q_row, q_col = self._parse_quadrant(properties.get("quadrant"))
            _cmd_name = {
                "liquid/Aspirate": "Aspirate",
                "liquid/Dispense": "Dispense",
                "liquid/Mix": "Mix",
            }.get(node_type, "Aspirate")
            # Apply per-node overrides: head_mode + anchor. These overwrite
            # the runtime-snapshot defaults so the simulator matches what the
            # workflow graph actually specifies. `_get_well_xy` then reads
            # this updated plate_selection via `_effective_plate_selection`.
            self._apply_node_head_mode(properties.get("head_mode"))
            if labware is not None:
                try:
                    self.bravo.set_plate_selection(int(loc), row, col)
                except Exception:
                    pass
            target_x, target_y = self._get_well_xy(loc, row=row, col=col, command=_cmd_name)
            if q_row or q_col:
                # 384-head on 1536 plate: quadrant shifts anchor by one
                # destination-plate well pitch (2.25 mm for 1536).
                try:
                    from pybravo.deck.geometry import well_geometry_from_metadata
                    metadata = self._get_labware_metadata(loc)
                    geom = well_geometry_from_metadata(metadata)
                    target_x += q_col * geom.pitch_x_mm
                    target_y += q_row * geom.pitch_y_mm
                except Exception:
                    target_x += q_col * 2.25
                    target_y += q_row * 2.25
            logger.debug(
                "liquid_motion",
                node_type=node_type,
                loc=loc,
                teach_xyz=(target_x, target_y, target_z),
                quadrant=properties.get("quadrant"),
                quadrant_offset=(q_row, q_col),
                labware_height=geometry.labware_height_mm,
                well_depth=geometry.well_depth_mm,
                distance_from_bottom=distance_from_bottom,
                z_top_plate=z_top_plate,
                z_liquid=z_liquid,
                tip_delta=geometry.tip_delta_mm,
            )
            steps = [
                {"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0},   # Safe Z, move XY
                {"X": target_x, "Y": target_y, "Z": z_top_plate, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0},     # Lower Z to plate rim
                {"X": target_x, "Y": target_y, "Z": z_liquid, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0},        # Lower to liquid
                {"X": target_x, "Y": target_y, "Z": z_liquid, "Zg": self._GRIPPER_RECESS, "G": 0, "W": volume},   # Aspirate/Dispense (W moves)
                {"X": target_x, "Y": target_y, "Z": z_top_plate, "Zg": self._GRIPPER_RECESS, "G": 0, "W": volume}, # Retract from well
                {"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": volume}, # Safe Z
            ]
            if node_type == "liquid/Mix":
                # Mix adds repeated aspirate/dispense cycles at liquid level
                cycles = int(properties.get("cycles", 3))
                mix_steps = []
                for c in range(cycles):
                    mix_steps.append({"X": target_x, "Y": target_y, "Z": z_liquid, "Zg": self._GRIPPER_RECESS, "G": 0, "W": volume})
                    mix_steps.append({"X": target_x, "Y": target_y, "Z": z_liquid, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0})
                steps = steps[:3] + mix_steps + steps[4:]
        elif node_type in ("tips/TipsOn", "tips/TipsOff"):
            is_on = node_type == "tips/TipsOn"
            from pybravo.tips import get_tip_id_for_capacity, get_tip_length_mm

            stack = self.bravo._deck.get_stack(loc)
            labware = stack.top if stack is not None else None
            # Apply per-node overrides before resolving effective selection.
            # Also accept `tip_anchor_row`/`tip_anchor_col` (designer node
            # property names) — they're equivalent to `anchor_row`/`anchor_col`.
            self._apply_node_head_mode(properties.get("head_mode"))
            node_anchor_row = properties.get(
                "anchor_row",
                properties.get("tip_anchor_row", properties.get("row")),
            )
            node_anchor_col = properties.get(
                "anchor_col",
                properties.get("tip_anchor_col", properties.get("col", properties.get("column"))),
            )
            if is_on and labware is not None and (node_anchor_row is not None or node_anchor_col is not None):
                try:
                    self.bravo.set_tip_selection(
                        int(loc),
                        int(node_anchor_row or 0),
                        int(node_anchor_col or 0),
                    )
                except Exception:
                    pass
            head_mode = self.bravo._head_mode if is_on else (self.bravo._tips_on_head_mode or self.bravo._head_mode)
            purpose = "pickup" if is_on else "return"
            try:
                tip_selection = None if labware is None else self.bravo._effective_tip_selection(
                    loc,
                    labware,
                    head_mode,
                    purpose=purpose,
                )
            except Exception:
                tip_selection = None

            # Real formula: deck_surface_z = teachpoint_z + teach_tip_length and
            # tips_on_z = deck_surface_z - top_labware.height. Support adapters
            # underneath the tipbox are not part of the press target.
            teach_tip_length = self._get_tip_length()
            deck_surface_z = target_z + teach_tip_length
            labware_height = float(getattr(labware, "height", 0.0) or self._get_labware_params(loc).get("height", 50.0))
            # Resolve which labware_id we actually looked up for diagnostics.
            _items = self._deck_config.get(str(loc), [])
            if isinstance(_items, dict):
                _items = [_items]
            _top_labware_id = _items[-1].get("labware_id") if _items else None
            tips_z = deck_surface_z - labware_height
            if not is_on:
                # Tips-off ejects slightly above the tipbox top (real state machine),
                # using the per-(head, tip box) offset when one is configured.
                try:
                    if labware is not None:
                        tips_off_z_offset = float(
                            self.bravo._resolve_tip_offsets(labware).tips_off_z_offset
                        )
                    else:
                        tips_off_z_offset = float(self.bravo.profile.safety.tips_off_z_offset)
                except Exception:
                    tips_off_z_offset = 10.0
                tips_z = tips_z - tips_off_z_offset
            target_x, target_y = self._get_tip_xy(loc, purpose=purpose, head_mode_override=head_mode)
            logger.debug(
                "tips_motion",
                node_type=node_type,
                loc=loc,
                labware_id=_top_labware_id,
                teach_xyz=(target_x, target_y, target_z),
                teach_tip_length=teach_tip_length,
                deck_surface_z=deck_surface_z,
                labware_height=labware_height,
                tips_z_unclamped=tips_z,
            )
            tips_z = max(0.0, min(tips_z, 150.0))
            # Extra press/eject travel
            press_z = min(tips_z + 5.0, 150.0)
            if is_on:
                self._set_removed_tip_cells(loc, tip_selection)
            else:
                self._restore_tip_cells(loc, tip_selection)
            tip_labware_name = getattr(labware, "name", "") or ""
            metadata = getattr(labware, "metadata", {}) or {}
            mounted_tip_capacity_ul = metadata.get("disposable_tip_capacity_ul")
            mounted_tip_definition_id = str(metadata.get("tip_definition_id") or "").strip()
            if not mounted_tip_definition_id:
                mounted_tip_definition_id = str(
                    get_tip_id_for_capacity(self.bravo.profile.head.head_type, mounted_tip_capacity_ul) or ""
                ).strip()
            mounted_tip_length = (
                metadata.get("tip_length_mm")
                or metadata.get("disposable_tip_length_mm")
                or metadata.get("attached_tip_length_mm")
            )
            if mounted_tip_length is None and mounted_tip_definition_id:
                mounted_tip_length = get_tip_length_mm(
                    self.bravo.profile.head.head_type,
                    mounted_tip_definition_id,
                )
            if mounted_tip_length is None and mounted_tip_capacity_ul is not None:
                mounted_tip_length = get_tip_length_mm(
                    self.bravo.profile.head.head_type,
                    mounted_tip_capacity_ul,
                )
            if mounted_tip_length is None:
                mounted_tip_length = self._get_tip_length()
            mounted_tip_length_mm = float(mounted_tip_length)
            main_tip_event = self._tip_change_event(
                tips_on=is_on,
                location=loc,
                head_mode=head_mode,
                tip_selection=tip_selection,
                tip_labware_name=tip_labware_name,
                attached_tip_length_mm=mounted_tip_length_mm if is_on else self.bravo._attached_tip_length_mm,
                active_tip_capacity_ul=None if mounted_tip_capacity_ul is None else float(mounted_tip_capacity_ul),
                tip_definition_id=mounted_tip_definition_id or (self.bravo._tip_definition_id if not is_on else ""),
            )
            # Visual sequencing: for tips_on we must NOT show tips attached
            # during the descent (the runtime snapshot may start with
            # tips_on_head=True). Clear first, descend empty, attach after
            # retract. For tips_off, keep tips attached through the descent
            # and have them disappear at the eject step (before retract).
            if is_on:
                pre_clear_event = self._tip_change_event(
                    tips_on=False,
                    location=loc,
                    head_mode=head_mode,
                    tip_selection=None,
                    tip_labware_name=tip_labware_name,
                )
                steps_with_events = [
                    (None, pre_clear_event),                                                                                    # Clear visual tips before descent
                    ({"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),    # Move XY (head empty)
                    ({"X": target_x, "Y": target_y, "Z": tips_z, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),          # Lower head to tip box top
                    ({"X": target_x, "Y": target_y, "Z": press_z, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),         # Press into tips
                    ({"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),    # Retract (head still empty visually)
                    (None, main_tip_event),                                                                                     # Tips appear attached after retract
                ]
            else:
                steps_with_events = [
                    ({"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),    # Move XY (tips still attached)
                    ({"X": target_x, "Y": target_y, "Z": tips_z, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),          # Lower to eject height
                    ({"X": target_x, "Y": target_y, "Z": press_z, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),         # Eject
                    (None, main_tip_event),                                                                                     # Tips disappear at eject
                    ({"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": 0, "W": 0}, None),    # Retract (head empty)
                ]
            for i, (pos, evt) in enumerate(steps_with_events):
                if evt:
                    await self._emit(evt)
                if pos:
                    await self._emit({"type": "workflow:positions", "positions": pos})
                    await self._emit({
                        "type": "workflow:node_step",
                        "node_id": self._current_node_id,
                        "step_index": i,
                        "step_name": f"step {i + 1}/{len(steps_with_events)}",
                    })
                    await asyncio.sleep(step_delay)
            return  # Skip the generic step loop below
        elif node_type in ("plate/PickPlace", "plate/Stack", "plate/Destack"):
            # PickPlace: `place_location`. Stack: `base_location`. Destack:
            # `destination_location`.
            place_loc = int(
                properties.get("place_location")
                or properties.get("base_location")
                or properties.get("destination_location")
                or loc
            )
            place_x, place_y, _ = self._get_location_xy(place_loc)
            gripper_y_off = self._get_gripper_y_offset()

            # Real kinematic calculations
            src_params = self._get_labware_params(loc)
            src_support = self._get_stack_support_height(loc)
            gripper_offset = src_params["gripper_offset"]
            logger.info(
                "pick_place_params",
                pick_loc=loc,
                place_loc=place_loc,
                src_params=src_params,
                src_support=src_support,
                gripper_offset=gripper_offset,
            )

            pick_z, pick_zg = self._solve_pick_place_zg(loc, src_support, gripper_offset)
            dest_stacking = self._get_stacking_height(place_loc)
            place_z, place_zg = self._solve_pick_place_zg(place_loc, dest_stacking, gripper_offset)

            # Carry height: clear both source and dest stacks + Z_CLEARANCE
            carry_stack = max(src_support, dest_stacking) + self._Z_CLEARANCE
            carry_z, carry_zg = self._solve_pick_place_zg(loc, carry_stack, gripper_offset)

            steps_with_events = [
                # 1. Safe Z, open gripper
                ({"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, "move_to_safe_start"),
                # 2. Nest gripper
                ({"X": target_x, "Y": target_y, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, "nest_gripper"),
                # 3. Move XY to pick (with gripper Y offset)
                ({"X": target_x, "Y": target_y + gripper_y_off, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, "move_xy_to_pick"),
                # 4. Lower to pick height
                ({"X": target_x, "Y": target_y + gripper_y_off, "Z": pick_z, "Zg": pick_zg, "G": self._GRIP_OPEN, "W": 0}, "lower_to_pick"),
                # 5. Grip
                ({"X": target_x, "Y": target_y + gripper_y_off, "Z": pick_z, "Zg": pick_zg, "G": self._GRIP_TARGET, "W": 0}, "grip_plate"),
            ]
            # Capture the plate identity (barcode + labware name) at the
            # source before the pick so the event can carry it even though
            # `_deck` is read-only in preview mode.
            pick_barcode = ""
            pick_labware_name = ""
            try:
                src_stack = self.bravo._deck.get_stack(loc)
                src_top = src_stack.top if src_stack is not None else None
                if src_top is not None:
                    pick_barcode = getattr(src_top, "barcode", "") or ""
                    pick_labware_name = getattr(src_top, "name", "") or ""
            except Exception:
                pass

            steps_with_events_full = [(p, None, n) for p, n in steps_with_events]
            # 6. Plate pick event
            steps_with_events_full.append((None, {"type": "workflow:plate_pick", "from_location": loc, "to_location": place_loc, "barcode": pick_barcode, "labware_name": pick_labware_name}, "plate_picked"))
            # 7. Lift to carry height
            steps_with_events_full.append(
                ({"X": target_x, "Y": target_y + gripper_y_off, "Z": carry_z, "Zg": carry_zg, "G": self._GRIP_TARGET, "W": 0}, None, "lift_to_carry"))
            # 8. Move XY to place
            steps_with_events_full.append(
                ({"X": place_x, "Y": place_y + gripper_y_off, "Z": carry_z, "Zg": carry_zg, "G": self._GRIP_TARGET, "W": 0}, None, "move_xy_to_place"))
            # 9. Lower to place height
            steps_with_events_full.append(
                ({"X": place_x, "Y": place_y + gripper_y_off, "Z": place_z, "Zg": place_zg, "G": self._GRIP_TARGET, "W": 0}, None, "lower_to_place"))
            # 10. Release
            steps_with_events_full.append(
                ({"X": place_x, "Y": place_y + gripper_y_off, "Z": place_z, "Zg": place_zg, "G": self._GRIP_OPEN, "W": 0}, None, "release_plate"))
            # 11. Place event
            steps_with_events_full.append((None, {"type": "workflow:plate_place", "to_location": place_loc, "barcode": pick_barcode, "labware_name": pick_labware_name}, "plate_placed"))
            # 12. Retract gripper
            steps_with_events_full.append(
                ({"X": place_x, "Y": place_y + gripper_y_off, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, None, "retract"))

            for i, (pos, evt, step_name) in enumerate(steps_with_events_full):
                if evt:
                    await self._emit(evt)
                if pos:
                    await self._emit({"type": "workflow:positions", "positions": pos})
                    await self._emit({
                        "type": "workflow:node_step",
                        "node_id": self._current_node_id,
                        "step_index": i,
                        "step_name": step_name,
                    })
                    await asyncio.sleep(step_delay)
            return  # Skip the generic step loop below
        elif node_type in ("plate/Delid", "plate/Relid"):
            # Delid/relid uses gripper to remove/replace lid on top of plate stack
            lw_params = self._get_labware_params(loc)
            lid_height = float(lw_params.get("height", 14.4))
            stack_support = self._get_stack_support_height(loc)
            gripper_offset = lw_params.get("gripper_offset", 6.0)
            gripper_y_off = self._get_gripper_y_offset()
            # Lid grip position: at the top of the stack
            lid_z, lid_zg = self._solve_pick_place_zg(loc, stack_support, gripper_offset)
            # Carry position: lifted above the stack
            carry_z, carry_zg = self._solve_pick_place_zg(loc, stack_support + lid_height + self._Z_CLEARANCE, gripper_offset)
            if node_type == "plate/Delid":
                steps_with_events = [
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": lid_z, "Zg": lid_zg, "G": self._GRIP_OPEN, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": lid_z, "Zg": lid_zg, "G": self._GRIP_TARGET, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": carry_z, "Zg": carry_zg, "G": self._GRIP_TARGET, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, None),
                ]
            else:
                steps_with_events = [
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_TARGET, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": carry_z, "Zg": carry_zg, "G": self._GRIP_TARGET, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": lid_z, "Zg": lid_zg, "G": self._GRIP_TARGET, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": lid_z, "Zg": lid_zg, "G": self._GRIP_OPEN, "W": 0}, None),
                    ({"X": target_x, "Y": target_y + gripper_y_off, "Z": self._Z_SAFE, "Zg": self._GRIPPER_RECESS, "G": self._GRIP_OPEN, "W": 0}, None),
                ]
            for i, (pos, evt) in enumerate(steps_with_events):
                if evt:
                    await self._emit(evt)
                if pos:
                    await self._emit({"type": "workflow:positions", "positions": pos})
                    await self._emit({
                        "type": "workflow:node_step",
                        "node_id": self._current_node_id,
                        "step_index": i,
                        "step_name": f"step {i + 1}/{len(steps_with_events)}",
                    })
                    await asyncio.sleep(step_delay)
            return
        elif node_type == "system/Home":
            steps = [
                {"X": 0, "Y": 0, "Z": 0, "Zg": -20, "G": 0, "W": 0},
            ]
        else:
            # Generic: just move to location
            steps = [
                {"X": target_x, "Y": target_y, "Z": 0, "Zg": -20, "G": 0, "W": 0},
            ]

        for i, positions in enumerate(steps):
            await self._emit({
                "type": "workflow:positions",
                "positions": positions,
            })
            await self._emit({
                "type": "workflow:node_step",
                "node_id": self._current_node_id,
                "step_index": i,
                "step_name": f"step {i + 1}/{len(steps)}",
            })
            await asyncio.sleep(step_delay)

    async def _emit_positions(self) -> None:
        """Broadcast current axis positions so the 3D viewport can update."""
        try:
            raw = self.bravo.get_all_positions()
            if raw:
                # Normalize keys to axis name strings (X, Y, Z, W, G, Zg)
                from pybravo.types import Axis
                AXIS_NAMES = {0: "X", 1: "Y", 2: "Z", 3: "W", 4: "G", 5: "Zg"}
                positions = {}
                for key, val in raw.items():
                    if isinstance(key, Axis):
                        positions[key.name] = val
                    elif isinstance(key, int):
                        positions[AXIS_NAMES.get(key, str(key))] = val
                    else:
                        positions[str(key)] = val
                await self._emit({
                    "type": "workflow:positions",
                    "positions": positions,
                })
        except Exception:
            pass  # Don't let position reads break the workflow

    async def _emit(self, event: dict) -> None:
        """Emit a workflow event."""
        logger.debug("workflow_event", **event)
        if self._on_event:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
