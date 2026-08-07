"""Assemble the system prompt for the LLM drafter.

The prompt has seven sections, in this order:

1. Role framing + hard rules ("never invent a node type", etc).
2. Node-type catalog — every supported type and its required properties.
3. Labware catalog excerpt — id → name → base_class, filtered to
   microplates + tip boxes by default to keep context tight.
4. Deck state — the workflow currently open in the designer (so the LLM
   can refer to "the 384 PP plate at loc 7" by exact labware_id).
5. Library snippets — the pre-authored snippet registry so the LLM
   prefers the Ask-Operator / Barcode-fallback / Kaldor-send patterns
   over open-coding similar logic.
6. Few-shot exemplars — 3 full workflows in the target JSON shape.
7. The operator's natural-language description.

Assembly is deterministic. The resulting prompt is ~3-4k tokens before
exemplars, ~5-7k including them — well within the 200k window of
Claude 3.5 / Sonnet 4 / GPT-4o.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from pybravo.workflow.drafter.schema import SUPPORTED_NODE_TYPES

# ── Hand-written node-catalog docstrings ──────────────────────────────
# Grouped so the LLM sees the affordance structure, not a flat list.
# Properties are what the executor reads off the node at dispatch time
# (see NODE_TYPE_MAP in pybravo/workflow/executor.py + _build_task_params
# at the same site).

_NODE_CATALOG: tuple[tuple[str, dict[str, Any]], ...] = (
    # (type, descriptor)
    ("flow/Start", {
        "desc": "Entry point. Every workflow has exactly one. No properties. Output slot 0 is flow.",
        "required": (),
        "optional": (),
    }),
    ("flow/End", {
        "desc": "Exit point. At least one required. No properties. Input slot 0 is flow.",
        "required": (),
        "optional": (),
    }),
    ("flow/Loop", {
        "desc": (
            "Executes its body flow (output slot 0) `count` times, then "
            "continues via `done` flow (output slot 1). Nodes inside the "
            "body can reference the current iteration index via `iter:v1,v2,...` "
            "property values (expands to the Nth comma-separated value on "
            "iteration N-1)."
        ),
        "required": ("count",),
        "optional": (),
    }),
    ("flow/IfElse", {
        "desc": (
            "Branches flow on a condition. True path = output slot 0, "
            "False path = output slot 1. The `data` input accepts the "
            "value to test; the `condition` property is a Python "
            "expression evaluated against it."
        ),
        "required": ("condition",),
        "optional": (),
    }),
    ("flow/Frame", {
        "desc": (
            "Visual-only container that groups related nodes. No flow "
            "ports. `properties.member_ids` is the list of node ids it "
            "wraps. Used to tidy up the canvas; not traversed at runtime."
        ),
        "required": ("title", "member_ids"),
        "optional": ("color", "collapsed", "expanded_bbox"),
    }),
    ("plate/PickPlace", {
        "desc": "Grip + move a plate from one deck location to another.",
        "required": ("pick_location", "place_location"),
        "optional": (),
    }),
    ("plate/Stack", {
        "desc": (
            "Move a plate from `source_location` onto a growing stack whose "
            "base is `base_location`. `target_location` names the final "
            "resting position; typically equal to base_location unless "
            "the stack is being built elsewhere."
        ),
        "required": ("source_location", "base_location", "target_location"),
        "optional": (),
    }),
    ("plate/Destack", {
        "desc": (
            "Remove the top plate from the stack at `source_location` and "
            "place it at `destination_location`. `target_location` is the "
            "final resting position (usually equals destination_location)."
        ),
        "required": ("source_location", "destination_location", "target_location"),
        "optional": (),
    }),
    ("plate/Delid", {
        "desc": "Remove a lid from the plate at `location`.",
        "required": ("location",),
        "optional": (),
    }),
    ("plate/Relid", {
        "desc": "Replace a previously-removed lid on the plate at `location`.",
        "required": ("location",),
        "optional": (),
    }),
    ("liquid/Aspirate", {
        "desc": (
            "Aspirate `volume` uL from the plate at `location` using the "
            "specified `liquid_class`. `anchor` selects the starting well "
            "(e.g. \"A1\"); optional `quadrant` selects a 1536 quadrant."
        ),
        "required": ("location", "volume", "liquid_class"),
        "optional": (
            "pipette_technique", "pre_aspirate_volume", "post_aspirate_volume",
            "distance_from_bottom", "dynamic_tip_extension", "tip_touch",
            "anchor", "quadrant", "wells",
        ),
    }),
    ("liquid/Dispense", {
        "desc": "Dispense `volume` uL at `location`. Same parameter shape as Aspirate.",
        "required": ("location", "volume", "liquid_class"),
        "optional": (
            "pipette_technique", "blowout_volume", "empty_tips",
            "distance_from_bottom", "dynamic_tip_retraction", "tip_touch",
            "anchor", "quadrant", "wells",
        ),
    }),
    ("liquid/Mix", {
        "desc": "Aspirate + dispense in place to mix.",
        "required": ("location", "volume", "liquid_class"),
        "optional": ("cycles", "distance_from_bottom", "anchor"),
    }),
    ("tips/TipsOn", {
        "desc": (
            "Pick up tips from a tip box. `location` is the tip-box deck "
            "position (use `iter:1,2,3,4` to cycle through multiple boxes "
            "on successive loop iterations). `head_mode` is a dict with "
            "`subset_type` (all_barrels|row|column|rectangle|single_barrel), "
            "`subset_config` (back_left|back_right|front_left|front_right), "
            "and optional `row_count`/`column_count`. `tip_anchor_row` / "
            "`tip_anchor_col` choose which cells of the tip box are picked "
            "up (0-indexed; ignored when using the whole head)."
        ),
        "required": ("location",),
        "optional": ("head_mode", "tip_anchor_row", "tip_anchor_col"),
    }),
    ("tips/TipsOff", {
        "desc": (
            "Eject tips at `location` — either back into a tip box or "
            "into a tip trash bin. The head_mode is INHERITED from the "
            "most recent upstream Tips On node (head can't reconfigure "
            "mid-cycle), so DO NOT emit `head_mode` on Tips Off. "
            "`tip_anchor_row` / `tip_anchor_col` choose which cells of "
            "the destination box receive the tips (0-indexed); default "
            "to the upstream pickup anchor."
        ),
        "required": ("location",),
        "optional": ("tip_anchor_row", "tip_anchor_col"),
    }),
    ("sensor/ReadBarcode", {
        "desc": (
            "Read the barcode of the plate currently at `location`. "
            "`store_as` optionally names a vars[...] key to write the "
            "scanned barcode into (e.g. store_as=\"plateFivebc\" makes "
            "the scanned value available as vars[\"plateFivebc\"] to "
            "downstream Script nodes)."
        ),
        "required": ("location",),
        "optional": ("store_as",),
    }),
    ("sensor/ScanStackHeight", {
        "desc": (
            "Probe a stack at `location` with the gripper plate sensor "
            "to verify the expected number of plates. `expected_count` "
            "triggers an operator-prompt retry/ignore/abort modal if the "
            "measured count differs."
        ),
        "required": ("location",),
        "optional": ("expected_count", "store_as"),
    }),
    ("logic/Script", {
        "desc": (
            "Runs user-authored Python at this point in the flow. Sees "
            "`data` (upstream input), `vars` (blackboard dict), `plates` "
            "(live deck accessor), `result` (assign to publish), `log`, "
            "`prompt_user(msg, default=\"\")` (opens an operator-input "
            "modal — set timeout=0 when using it), and any helpers from "
            "the workflow Library. `store_as` optionally writes the "
            "script's `result` into vars[that_key]."
        ),
        "required": ("script",),
        "optional": ("timeout", "store_as"),
    }),
    ("system/Initialize", {
        "desc": "Initialize the robot (home axes, verify hardware). Usually first node after Start.",
        "required": (),
        "optional": (),
    }),
    ("system/Home", {
        "desc": "Home the specified axes.",
        "required": (),
        "optional": ("axes",),
    }),
    ("system/DockGripper", {
        "desc": "Return the gripper to its nesting position.",
        "required": (),
        "optional": (),
    }),
)


def _format_node_catalog() -> str:
    lines = [
        "## Node type catalog",
        "Every `type` value MUST be one of these exact strings — do not "
        "invent variants. Required properties MUST be present; optional "
        "ones can be omitted.",
        "",
    ]
    for type_name, d in _NODE_CATALOG:
        lines.append(f"### `{type_name}`")
        lines.append(d["desc"])
        if d["required"]:
            lines.append(f"**Required:** {', '.join(d['required'])}")
        if d["optional"]:
            lines.append(f"**Optional:** {', '.join(d['optional'])}")
        lines.append("")
    return "\n".join(lines)


# ── Labware catalog excerpt ───────────────────────────────────────────


def _load_labware_catalog(
    catalog_path: Path | None = None,
    base_classes: Iterable[str] = ("microplate", "tip_box"),
) -> list[dict[str, Any]]:
    """Return a filtered summary of the labware catalog.

    Only returns the fields that matter to the drafter (id / name /
    base_class / wells / is_lidded / is_sealed). Full mechanical
    dimensions are irrelevant to NL → JSON translation.
    """
    if catalog_path is None:
        catalog_path = Path(__file__).resolve().parent.parent.parent.parent / "config" / "labware_catalog.snapshot.yaml"
    if not catalog_path.exists():
        return []
    try:
        import yaml
    except ImportError:
        return []
    with catalog_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    filtered: list[dict[str, Any]] = []
    for entry in data.get("labware", []) or []:
        base = entry.get("base_class", "")
        if base not in base_classes:
            continue
        filtered.append({
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "base_class": base,
            "wells": entry.get("wells", 0),
        })
    return filtered


def _format_labware_catalog(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = [
        "## Labware catalog (available labware_ids for deck placement)",
        "Use these exact `labware_id` strings in any deck entry. Do not "
        "invent new ones — the executor looks them up by id in the "
        "catalog at run time.",
        "",
        "| labware_id | name | base_class | wells |",
        "|---|---|---|---|",
    ]
    for e in entries:
        lines.append(f"| `{e['id']}` | {e['name']} | {e['base_class']} | {e['wells']} |")
    lines.append("")
    return "\n".join(lines)


# ── Snippet-registry excerpt ──────────────────────────────────────────


def _format_snippet_registry() -> str:
    """Describe the pre-authored Script snippets the LLM can leverage.

    We paraphrase each snippet's purpose rather than dumping full code
    — the full code lives in the exemplars and in the Library on
    actually-run workflows. Prompt-token-budget matters.
    """
    try:
        from pybravo.workflow.script_snippets import get_snippets
    except ImportError:
        return ""
    snippets = get_snippets()
    if not snippets:
        return ""
    lines = [
        "## Available Script-node snippet patterns",
        "When a Script node needs to do one of these, emit the snippet's "
        "code verbatim — don't reinvent.",
        "",
    ]
    for s in snippets:
        lines.append(f"- **{s.get('label', s.get('id', '?'))}** ({s.get('category', '?')}): {s.get('description', '')}")
    lines.append("")
    return "\n".join(lines)


# ── Exemplars ─────────────────────────────────────────────────────────


_EXEMPLARS_DIR = Path(__file__).resolve().parent / "exemplars"


def _load_exemplars() -> list[dict[str, Any]]:
    """Load all exemplar workflows, sorted by filename so order is stable."""
    if not _EXEMPLARS_DIR.exists():
        return []
    exemplars: list[dict[str, Any]] = []
    for p in sorted(_EXEMPLARS_DIR.glob("*.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                exemplars.append(json.load(f))
        except Exception:
            continue
    return exemplars


def _format_exemplars(exemplars: list[dict[str, Any]]) -> str:
    if not exemplars:
        return ""
    lines = [
        "## Exemplar workflows (target JSON shape)",
        "Your output MUST match this JSON structure exactly. These are "
        "real valid drafts — mimic field names, nesting, link tuple "
        "ordering, node positioning style, etc.",
        "",
    ]
    for ex in exemplars:
        name = ex.get("name", "unnamed")
        desc = ex.get("description", "")
        lines.append(f"### Example: {name}")
        if desc:
            lines.append(f"*{desc}*")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(ex, indent=2))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# ── Hard rules ────────────────────────────────────────────────────────


_ROLE_AND_RULES = """You are the pyBravo workflow drafter. Your job is to
translate a lab operator's natural-language description of an experiment
into a valid pyBravo workflow JSON.

## Hard rules (non-negotiable)

1. The `type` field of every node MUST be one of the exact strings in
   the "Node type catalog" section below. Never invent a new type,
   never misspell an existing one, never use a type that isn't listed.
2. Every workflow MUST have exactly one `flow/Start` node and at least
   one `flow/End` node. Flow MUST originate at Start and reach End.
3. Deck-location property values (`location`, `pick_location`,
   `source_location`, etc.) MUST be integers in 1-9, OR the special
   string `iter:v1,v2,...` (for loop iteration), OR `var:NAME` (for
   blackboard lookup). Never use 0, 10+, or arbitrary strings.
4. Volumes are in microliters (uL), as numbers (int or float).
   0 < volume <= 200 for single-channel heads. Never emit mL.
5. `labware_id` values MUST come from the Labware catalog section. If
   the operator names a labware not in the catalog, choose the closest
   match and include a brief note in `description`.
6. Node `id` and link `id` values MUST be unique positive integers
   within the graph.
7. Link tuples connect slot 0 of one node's outputs to slot 0 of the
   next node's inputs, using `link_type: -1` for flow. (Typed data
   slots use "string", "number", etc.)
8. If the operator asks for something you cannot do (e.g. a non-
   existent node type, a location outside 1-9, an unsupported head
   mode), produce the closest-feasible draft and note the deviation
   in `description` — never invent new schema fields.
9. Prefer the pre-authored Script snippets (see catalog below) over
   writing new Python. Snippets are vetted; novel code isn't.
10. `pos` can be left at [0, 0] for all nodes — the designer's
    Auto-Arrange button will lay them out properly.
11. Every `liquid/Aspirate`, `liquid/Dispense`, and `liquid/Mix` MUST
    be flanked by tips: a `tips/TipsOn` before the first liquid action
    in a tip lifecycle, and a `tips/TipsOff` after the last one. The
    only exception is when the operator EXPLICITLY says "skip tips"
    or "tips already on" — otherwise always include them. If the
    operator doesn't name a tip-box location, default to `location: 1`.

## Output format

Emit ONLY a single JSON object matching the DraftedWorkflow schema.
No prose, no code fences, no commentary outside the JSON.
"""


# ── Public assembler ──────────────────────────────────────────────────


def build_system_prompt(
    *,
    current_deck: dict[str, Any] | None = None,
    include_exemplars: bool = True,
) -> str:
    """Assemble the full system prompt.

    Args:
        current_deck: The deck configuration currently loaded in the
            designer (from the active tab). Forwarded to the LLM so it
            can reuse exact labware_ids already on the deck. Pass None
            to omit (the LLM gets only the catalog, not the live deck).
        include_exemplars: Skip the few-shot JSON dumps for tiny tests.

    Returns:
        A single string ready for the ``system`` role of an Anthropic
        or OpenAI chat call.
    """
    sections: list[str] = [_ROLE_AND_RULES, _format_node_catalog()]

    labware = _load_labware_catalog()
    sections.append(_format_labware_catalog(labware))

    if current_deck:
        sections.append("## Current deck configuration")
        sections.append(
            "The designer currently has this deck loaded. Reuse these "
            "exact labware entries when the operator's description "
            "refers to \"the plate at loc N\" or similar. Overwrite "
            "only when the description clearly implies a different "
            "plate there."
        )
        sections.append("```json")
        sections.append(json.dumps(current_deck, indent=2))
        sections.append("```")
        sections.append("")

    sections.append(_format_snippet_registry())

    if include_exemplars:
        sections.append(_format_exemplars(_load_exemplars()))

    sections.append(
        "Allowed node type values for the `type` field (also listed in "
        "the catalog above): "
        + ", ".join(f"`{t}`" for t in SUPPORTED_NODE_TYPES)
    )

    return "\n\n".join(s for s in sections if s)
