"""Pydantic schema the LLM is constrained to emit.

Matches the runtime LiteGraph JSON shape that ``deserializeWorkflow`` in
the designer consumes (``frontend/designer.html``) — so a valid
``DraftedWorkflow`` can be round-tripped into the designer as a new tab
without any shape translation.

The strict ``Literal`` enum on ``DraftedNode.type`` is the single most
important guardrail: it eliminates the largest class of hallucination
(invented node types) at schema-validation time.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Supported node types ──────────────────────────────────────────────
# Kept in sync with ``NODE_TYPE_MAP`` in pybravo/workflow/executor.py plus
# the flow/* node types that executor doesn't dispatch but the designer
# understands. Adding a new node type means updating both places.

SUPPORTED_NODE_TYPES: tuple[str, ...] = (
    "flow/Start",
    "flow/End",
    "flow/Loop",
    "flow/IfElse",
    "flow/Frame",
    "plate/PickPlace",
    "plate/Stack",
    "plate/Destack",
    "plate/Mount",
    "plate/Unmount",
    "plate/Delid",
    "plate/Relid",
    "liquid/Aspirate",
    "liquid/Dispense",
    "liquid/Mix",
    "tips/TipsOn",
    "tips/TipsOff",
    "sensor/ReadBarcode",
    "sensor/ScanStackHeight",
    "system/Initialize",
    "system/Home",
    "system/DockGripper",
    "logic/Script",
)

NodeType = Literal[
    "flow/Start",
    "flow/End",
    "flow/Loop",
    "flow/IfElse",
    "flow/Frame",
    "plate/PickPlace",
    "plate/Stack",
    "plate/Destack",
    "plate/Mount",
    "plate/Unmount",
    "plate/Delid",
    "plate/Relid",
    "liquid/Aspirate",
    "liquid/Dispense",
    "liquid/Mix",
    "tips/TipsOn",
    "tips/TipsOff",
    "sensor/ReadBarcode",
    "sensor/ScanStackHeight",
    "system/Initialize",
    "system/Home",
    "system/DockGripper",
    "logic/Script",
]


# ── Deck ──────────────────────────────────────────────────────────────


class DraftedDeckItem(BaseModel):
    """One labware in a deck-location stack (bottom-first order)."""

    labware_id: str = Field(
        description="Catalog id, e.g. 'lw-3918306f45b8'. Must exist in the labware catalog."
    )
    name: str = Field(
        default="",
        description="Human-readable label, e.g. '1536 Labcyte LP-0400 LDV'.",
    )
    kind: str = Field(
        default="sbs_plate",
        description="Catalog `kind` — sbs_plate / tube_rack / tip_box.",
    )
    base_class: str = Field(
        default="",
        description="Catalog `base_class` — microplate / tip_box / etc.",
    )
    wells: int = Field(default=0, ge=0)
    is_lidded: bool = False
    is_sealed: bool = False


# ── Nodes + links ─────────────────────────────────────────────────────


class SourceCitation(BaseModel):
    """Provenance for a drafted node — which passage of the source paper
    (or other input) justified this node.

    Populated when a workflow is drafted from a PDF (Phase 3 pipeline).
    Absent for NL-prompt drafts and for structural nodes (Start/End/
    Loop scaffolding) that don't map to any specific paragraph.

    The designer renders a small badge on any node that has this field
    populated; clicking opens a side panel showing the paragraph text.
    """

    paragraph_id: str = Field(
        description=(
            "Stable id of the paragraph this node was derived from, "
            "assigned by the paper parser (e.g. 'p-399'). The id is "
            "opaque — the UI uses it to look up the full text from the "
            "parsed-paper payload cached alongside the drafted workflow."
        ),
    )
    excerpt: str = Field(
        default="",
        max_length=500,
        description=(
            "Short excerpt (<=500 chars) of the source passage, for "
            "tooltip display in the designer. Can be empty — UI will "
            "fall back to looking up the full paragraph by id."
        ),
    )
    page: int | None = Field(
        default=None,
        description="Page number in the source PDF, if known.",
    )
    fact_id: str = Field(
        default="",
        description=(
            "Intermediate fact id from the extraction pipeline's Pass 1 "
            "(e.g. 'f-12'). Empty for nodes drafted without the "
            "two-pass facts pipeline. Kept for debugging / auditing."
        ),
    )


class DraftedNode(BaseModel):
    """One node in the workflow graph."""

    id: int = Field(description="Unique within the graph; positive integer.", ge=1)
    type: NodeType = Field(
        description="One of the supported node types. NEVER invent a type not in this enum."
    )
    title: str = Field(
        default="",
        description="Optional human-readable name shown on the node header.",
    )
    pos: list[float] = Field(
        default_factory=lambda: [0.0, 0.0],
        min_length=2,
        max_length=2,
        description=(
            "Canvas position as [x, y] pixels. Arrange will overwrite these "
            "so sensible defaults or [0, 0] are fine."
        ),
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-node-type parameters. Required keys vary by type — see the "
            "node catalog in the system prompt. Examples: "
            "PickPlace requires `pick_location` and `place_location` (ints 1-9); "
            "Aspirate requires `location`, `volume`, `liquid_class`."
        ),
    )
    source_citation: SourceCitation | None = Field(
        default=None,
        description=(
            "Provenance when this node was derived from a paper PDF. "
            "Populate for every action node drafted from paper text; "
            "leave null for structural nodes (Start/End/Loop/Frame) "
            "that aren't tied to a specific passage."
        ),
    )


class DraftedLink(BaseModel):
    """A flow or data link between two node slots.

    Serializes to LiteGraph's 6-tuple format:
    ``[link_id, origin_id, origin_slot, target_id, target_slot, type]``
    where ``type`` is ``-1`` for flow links and a string like ``"string"``
    for typed data links.
    """

    id: int = Field(ge=1)
    origin_id: int = Field(ge=1)
    origin_slot: int = Field(ge=0)
    target_id: int = Field(ge=1)
    target_slot: int = Field(ge=0)
    link_type: int = Field(
        default=-1,
        description=(
            "Always -1 for flow links (the common case in drafts). Typed data "
            "links between sensor output slots and Script data inputs are rare "
            "in drafted workflows and can be wired up by the operator after."
        ),
    )

    def to_tuple(self) -> list[Any]:
        """Emit the LiteGraph 6-tuple the designer expects."""
        return [
            self.id,
            self.origin_id,
            self.origin_slot,
            self.target_id,
            self.target_slot,
            self.link_type,
        ]


class DraftedGraph(BaseModel):
    """Graph portion of the workflow — nodes + links."""

    last_node_id: int = Field(default=0, ge=0)
    last_link_id: int = Field(default=0, ge=0)
    nodes: list[DraftedNode]
    links: list[DraftedLink] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    version: float = Field(default=0.4)

    @model_validator(mode="after")
    def _sync_last_ids(self) -> "DraftedGraph":
        """If the LLM leaves last_*_id at zero, fill them from the content."""
        if self.last_node_id == 0 and self.nodes:
            self.last_node_id = max(n.id for n in self.nodes)
        if self.last_link_id == 0 and self.links:
            self.last_link_id = max(link.id for link in self.links)
        return self


# ── Top-level workflow ────────────────────────────────────────────────


class DraftedWorkflow(BaseModel):
    """A full workflow draft produced by the LLM.

    Matches the JSON shape consumed by ``deserializeWorkflow`` in the
    designer (frontend/designer.html), so a valid instance round-trips
    into a new tab with no translation.
    """

    name: str = Field(default="Untitled Draft", max_length=120)
    description: str = Field(default="", max_length=2000)
    deck: dict[str, list[DraftedDeckItem]] = Field(
        default_factory=dict,
        description=(
            "Map of deck-location-string (\"1\"..\"9\") to stack of labware. "
            "Stack is bottom-first: index 0 is the plate sitting on the deck, "
            "subsequent entries are plates stacked on top. Empty or missing "
            "location = nothing there."
        ),
    )
    graph: DraftedGraph
    library: str = Field(
        default="",
        description=(
            "Optional workflow-level Library Python code. Functions defined "
            "here are injected into every Script node's namespace at run "
            "start. Leave empty if no helpers are needed."
        ),
    )

    @field_validator("deck")
    @classmethod
    def _deck_locations_are_1_to_9(
        cls, value: dict[str, list[DraftedDeckItem]]
    ) -> dict[str, list[DraftedDeckItem]]:
        for key in value:
            try:
                loc = int(key)
            except ValueError as exc:
                raise ValueError(f"Deck location key must be an integer string; got {key!r}") from exc
            if not 1 <= loc <= 9:
                raise ValueError(f"Deck location must be between 1 and 9; got {loc}")
        return value

    def to_designer_json(self) -> dict[str, Any]:
        """Render as the exact JSON shape the designer loads.

        Three shape-level transforms happen here, not in the Pydantic
        dump:

        * ``graph.links`` becomes a list of 6-tuples (LiteGraph native),
          not the nested-dict DraftedLink schema we use for LLM constraint.
        * ``pos`` is a plain list rather than a tuple.
        * **Each node gets its ``inputs`` / ``outputs`` arrays populated
          with slot metadata + the link IDs that reference each slot.**
          LiteGraph's ``graph.configure()`` doesn't cross-wire the links
          onto nodes automatically — without this the nodes land on the
          canvas unconnected even though the links exist in the JSON.
        """
        data = self.model_dump(mode="json")
        data["graph"]["links"] = [link.to_tuple() for link in self.graph.links]
        # LiteGraph keeps positions as two-element lists.
        for node in data["graph"]["nodes"]:
            node["pos"] = list(node.get("pos", [0, 0]))
            # LiteGraph's configure() copies `properties` verbatim but drops
            # unknown top-level fields. Tuck source_citation into properties
            # under an underscored key so it survives the serialize/load
            # cycle — and therefore survives Save As, Load, Duplicate tab,
            # and lives through a designer reload.
            citation = node.pop("source_citation", None)
            if citation:
                node.setdefault("properties", {})
                node["properties"]["_source_citation"] = citation
        _populate_node_slots(data["graph"]["nodes"], self.graph.links)
        return data


# ── Slot catalog for each supported node type ─────────────────────────
# Keyed by node type. Each entry has "inputs" and "outputs" lists
# mirroring how LiteGraph registers slots on the node constructor
# (see registerNodeTypes() in frontend/designer.html). Each slot is
# (name, slot_type) where slot_type is -1 for flow or a string like
# "string"/"number" for typed data slots.
_FLOW_IN = [("flow", -1)]
_FLOW_OUT = [("flow", -1)]

_NODE_SLOTS: dict[str, dict[str, list[tuple[str, int | str]]]] = {
    "flow/Start":              {"inputs": [],                                       "outputs": _FLOW_OUT},
    "flow/End":                {"inputs": _FLOW_IN,                                 "outputs": []},
    "flow/Loop":               {"inputs": _FLOW_IN,                                 "outputs": [("body", -1), ("done", -1)]},
    "flow/IfElse":             {"inputs": [("flow", -1), ("data", "string")],       "outputs": [("true", -1), ("false", -1)]},
    "flow/Frame":              {"inputs": [],                                       "outputs": []},
    "plate/PickPlace":         {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "plate/Stack":             {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "plate/Destack":           {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "plate/Mount":             {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "plate/Unmount":           {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "plate/Delid":             {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "plate/Relid":             {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "liquid/Aspirate":         {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "liquid/Dispense":         {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "liquid/Mix":              {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "tips/TipsOn":             {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "tips/TipsOff":            {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "sensor/ReadBarcode":      {"inputs": _FLOW_IN,                                 "outputs": [("flow", -1), ("barcode", "string")]},
    "sensor/ScanStackHeight":  {"inputs": _FLOW_IN,                                 "outputs": [("flow", -1), ("height", "number")]},
    "system/Initialize":       {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "system/Home":             {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "system/DockGripper":      {"inputs": _FLOW_IN,                                 "outputs": _FLOW_OUT},
    "logic/Script":            {"inputs": [("flow", -1), ("data", "string")],       "outputs": [("flow", -1), ("result", "string")]},
}


def _populate_node_slots(
    nodes: list[dict[str, Any]],
    links: list["DraftedLink"],
) -> None:
    """Populate each node dict's ``inputs`` / ``outputs`` arrays in-place.

    LiteGraph's ``graph.configure()`` reads:
      node.outputs[slot_index].links = [link_id, ...]
      node.inputs[slot_index].link   = link_id

    Without these, the LLink objects in the top-level ``graph.links``
    array exist but the nodes don't "know" they're connected, so no
    wires render. This function is the source of truth for that
    cross-wiring when generating workflows from outside the designer.
    """
    # Seed each node with empty slot arrays according to its type.
    by_id: dict[int, dict[str, Any]] = {}
    for n in nodes:
        slots = _NODE_SLOTS.get(n.get("type", ""))
        if slots is None:
            # Unknown type — leave as-is; validation already flagged it.
            continue
        n["inputs"] = [
            {"name": name, "type": t, "link": None}
            for name, t in slots["inputs"]
        ]
        n["outputs"] = [
            {"name": name, "type": t, "links": [], "slot_index": i}
            for i, (name, t) in enumerate(slots["outputs"])
        ]
        by_id[n["id"]] = n

    # Wire each link onto its origin's output-slot.links + target's input-slot.link.
    for link in links:
        origin = by_id.get(link.origin_id)
        target = by_id.get(link.target_id)
        if origin and 0 <= link.origin_slot < len(origin.get("outputs", [])):
            origin["outputs"][link.origin_slot]["links"].append(link.id)
        if target and 0 <= link.target_slot < len(target.get("inputs", [])):
            target["inputs"][link.target_slot]["link"] = link.id
