"""Post-generation sanity check on a DraftedWorkflow.

Schema-level hallucination (invalid node types, wrong value types) is
already caught by Pydantic when the LLM response is parsed. This
module catches what Pydantic can't:

* Graph topology (exactly one Start, at least one End, no orphans, no
  dangling link endpoints).
* Physical sanity of property references (locations 1-9, barcode reads
  target plates that exist on the deck, etc.).
* Unit-like sanity (volumes in a plausible range).

Returns a list of :class:`ValidationIssue` — empty means the draft
passes. The drafter's retry loop re-prompts the LLM with the issue
list when non-empty; beyond a retry budget the issues surface to the
operator as warnings without blocking the draft from opening in a tab.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pybravo.workflow.drafter.schema import DraftedWorkflow


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a drafted workflow."""

    severity: str  # "error" | "warning"
    code: str       # short machine-readable identifier
    message: str    # human-readable; included verbatim in repair prompts
    node_id: int | None = None

    def __str__(self) -> str:
        prefix = f"[{self.severity.upper()} {self.code}]"
        if self.node_id is not None:
            prefix += f" node#{self.node_id}"
        return f"{prefix} {self.message}"


# ── Property-key expectations per node type ───────────────────────────
# Used by _check_required_properties. Keys listed here are *required*;
# anything else the LLM includes is forwarded untouched so per-node
# type validation in the runtime (executor) catches deeper issues.

_REQUIRED_PROPERTIES: dict[str, tuple[str, ...]] = {
    "plate/PickPlace": ("pick_location", "place_location"),
    # NOTE: Stack uses `base_location` everywhere else — designer
    # node, executor, bravo API, 3D viewer.  The validator previously
    # required `target_location`, which no caller ever produced and
    # so silently never triggered MISSING_PROPERTY errors for Stack
    # nodes drafted without the right key.  Fixed to match reality.
    "plate/Stack":   ("source_location", "base_location"),
    "plate/Destack": ("source_location", "destination_location"),
    "plate/Mount":   ("source_location", "base_location"),
    "plate/Unmount": ("source_location", "destination_location"),
    "plate/Delid":   ("location",),
    "plate/Relid":   ("location",),
    "liquid/Aspirate": ("location", "volume", "liquid_class"),
    "liquid/Dispense": ("location", "volume", "liquid_class"),
    "liquid/Mix":      ("location", "volume", "liquid_class"),
    "tips/TipsOn":     ("location",),
    "tips/TipsOff":    ("location",),
    "sensor/ReadBarcode":     ("location",),
    "sensor/ScanStackHeight": ("location",),
    "flow/Loop":       ("count",),
    "logic/Script":    ("script",),
}


_LOCATION_PROPERTY_KEYS: tuple[str, ...] = (
    "location",
    "pick_location",
    "place_location",
    "source_location",
    "destination_location",
    "target_location",
    "base_location",
)


def _as_location_int(value: Any) -> int | None:
    """Coerce a location property into an int 1-9, or None if not a plain int.

    Accepts ``iter:1,2,3,4`` (runtime expands these from the loop index)
    by returning ``None`` — the validator skips physical-existence checks
    on iter: references; the runtime handles them.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.startswith("iter:") or value.startswith("var:"):
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


# ── Individual checks ─────────────────────────────────────────────────


def _check_start_end_counts(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    starts = [n for n in wf.graph.nodes if n.type == "flow/Start"]
    ends = [n for n in wf.graph.nodes if n.type == "flow/End"]
    if len(starts) == 0:
        issues.append(ValidationIssue(
            severity="error", code="NO_START",
            message="Workflow has no flow/Start node. Add exactly one.",
        ))
    elif len(starts) > 1:
        issues.append(ValidationIssue(
            severity="error", code="MULTIPLE_START",
            message=f"Workflow has {len(starts)} flow/Start nodes; exactly one is required.",
        ))
    if len(ends) == 0:
        issues.append(ValidationIssue(
            severity="error", code="NO_END",
            message="Workflow has no flow/End node. Add at least one.",
        ))
    return issues


def _check_unique_ids(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen_ids: set[int] = set()
    for n in wf.graph.nodes:
        if n.id in seen_ids:
            issues.append(ValidationIssue(
                severity="error", code="DUPLICATE_NODE_ID",
                message=f"Duplicate node id {n.id}. Every node id must be unique.",
                node_id=n.id,
            ))
        seen_ids.add(n.id)
    seen_links: set[int] = set()
    for link in wf.graph.links:
        if link.id in seen_links:
            issues.append(ValidationIssue(
                severity="error", code="DUPLICATE_LINK_ID",
                message=f"Duplicate link id {link.id}. Every link id must be unique.",
            ))
        seen_links.add(link.id)
    return issues


def _check_link_endpoints(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    node_ids = {n.id for n in wf.graph.nodes}
    for link in wf.graph.links:
        if link.origin_id not in node_ids:
            issues.append(ValidationIssue(
                severity="error", code="DANGLING_LINK_ORIGIN",
                message=(
                    f"Link {link.id} originates at node {link.origin_id} "
                    "which does not exist in the graph."
                ),
            ))
        if link.target_id not in node_ids:
            issues.append(ValidationIssue(
                severity="error", code="DANGLING_LINK_TARGET",
                message=(
                    f"Link {link.id} targets node {link.target_id} "
                    "which does not exist in the graph."
                ),
            ))
    return issues


def _check_required_properties(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for n in wf.graph.nodes:
        required = _REQUIRED_PROPERTIES.get(n.type, ())
        for key in required:
            if key not in n.properties:
                issues.append(ValidationIssue(
                    severity="error", code="MISSING_PROPERTY",
                    message=f"Node of type {n.type} is missing required property '{key}'.",
                    node_id=n.id,
                ))
    return issues


def _check_location_sanity(
    wf: DraftedWorkflow,
    strict_deck: bool,
) -> list[ValidationIssue]:
    """Every location property must be 1-9.

    If ``strict_deck`` is True, locations referenced by nodes that expect
    labware (PickPlace, Aspirate, Dispense, ReadBarcode, ...) must also
    exist in ``wf.deck``. We leave this off by default because a draft
    workflow may legitimately prepare a deck stack up-front (tip boxes
    starting empty, destacked plates appearing at run time) — deferring
    deep physical checks to the runtime's live deck state.
    """
    issues: list[ValidationIssue] = []
    for n in wf.graph.nodes:
        for key in _LOCATION_PROPERTY_KEYS:
            if key not in n.properties:
                continue
            loc = _as_location_int(n.properties[key])
            if loc is None:
                continue  # iter: / var: — skip
            if not 1 <= loc <= 9:
                issues.append(ValidationIssue(
                    severity="error", code="LOCATION_OUT_OF_RANGE",
                    message=(
                        f"Property '{key}' on node {n.id} ({n.type}) references "
                        f"location {loc}; only 1-9 are valid deck positions."
                    ),
                    node_id=n.id,
                ))
                continue
            if strict_deck and key in ("pick_location", "source_location") and str(loc) not in wf.deck:
                issues.append(ValidationIssue(
                    severity="warning", code="LOCATION_EMPTY",
                    message=(
                        f"Node {n.id} ({n.type}) picks from location {loc} "
                        "but the deck configuration has nothing there at "
                        "workflow start. This may be fine if an upstream "
                        "node places labware there first; otherwise a "
                        "runtime error will fire."
                    ),
                    node_id=n.id,
                ))
    return issues


def _check_volume_sanity(wf: DraftedWorkflow) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for n in wf.graph.nodes:
        if n.type not in ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix"):
            continue
        vol = n.properties.get("volume")
        # Allow var: / iter: string placeholders.
        if isinstance(vol, str) and (vol.startswith("var:") or vol.startswith("iter:")):
            continue
        if vol is None:
            continue  # _check_required_properties already caught this
        try:
            v = float(vol)
        except (TypeError, ValueError):
            issues.append(ValidationIssue(
                severity="error", code="VOLUME_NOT_NUMERIC",
                message=f"Volume on node {n.id} is not a number: {vol!r}.",
                node_id=n.id,
            ))
            continue
        if v <= 0:
            issues.append(ValidationIssue(
                severity="error", code="VOLUME_NOT_POSITIVE",
                message=f"Volume on node {n.id} must be > 0 uL; got {v}.",
                node_id=n.id,
            ))
        elif v > 5000:
            # Only flag volumes that are almost certainly a unit error
            # (> 5 mL).  Legitimate bulk-reagent steps (e.g. 1–2 mL
            # washes, 1200 µL deep-well plates) sit between 200–5000 µL,
            # and scientific papers may describe protocols at that scale
            # even when they can't be executed on a Bravo as-is.
            issues.append(ValidationIssue(
                severity="warning", code="VOLUME_HIGH",
                message=(
                    f"Volume on node {n.id} is {v} uL (>{v/1000:.0f} mL). "
                    "Verify the unit — if the paper cited millilitres this "
                    "value needs to be converted before running."
                ),
                node_id=n.id,
            ))
    return issues


_LIQUID_TYPES = ("liquid/Aspirate", "liquid/Dispense", "liquid/Mix")


def _check_tips_lifecycle(wf: DraftedWorkflow) -> list[ValidationIssue]:
    """Every liquid action MUST be bracketed by tips on / tips off.

    Two checks:

    1. Coarse — if ANY aspirate/dispense/mix exists in the workflow, at
       least one tips/TipsOn AND at least one tips/TipsOff MUST exist
       somewhere in the graph. Missing either is a hard error.
    2. Linear-flow walk from Start, tracking tip state along each path.
       Hitting a liquid action while tips_on is False is a hard error;
       that catches the case of a workflow that happens to have a
       TipsOn node but in the wrong place (e.g. after the dispense).

    Loops and branches: when the flow enters a Loop body, the initial
    state at the body's entry is whatever it was at the Loop node; the
    walker treats the loop body as an unrolled linear segment for
    checking purposes. This misses some clever cross-iteration tip
    reuse patterns but catches every case of "forgot tips entirely"
    and "put them in the wrong order", which is the 99% failure mode.
    """
    issues: list[ValidationIssue] = []
    nodes_by_id = {n.id: n for n in wf.graph.nodes}
    liquid_ids = [n.id for n in wf.graph.nodes if n.type in _LIQUID_TYPES]
    if not liquid_ids:
        return issues  # no liquid actions → tips not required

    has_tips_on = any(n.type == "tips/TipsOn" for n in wf.graph.nodes)
    has_tips_off = any(n.type == "tips/TipsOff" for n in wf.graph.nodes)
    if not has_tips_on:
        issues.append(ValidationIssue(
            severity="error", code="MISSING_TIPS_ON",
            message=(
                "The workflow has aspirate/dispense/mix nodes but no "
                "tips/TipsOn node. Add a tips/TipsOn before the first "
                "liquid action."
            ),
        ))
    if not has_tips_off:
        issues.append(ValidationIssue(
            severity="error", code="MISSING_TIPS_OFF",
            message=(
                "The workflow has aspirate/dispense/mix nodes but no "
                "tips/TipsOff node. Add a tips/TipsOff after the last "
                "liquid action so tips are ejected."
            ),
        ))

    # Linear walk (flow-link adjacency, -1 type links only).
    adj: dict[int, list[int]] = {}
    for link in wf.graph.links:
        if link.link_type != -1:
            continue
        adj.setdefault(link.origin_id, []).append(link.target_id)

    start_nodes = [n for n in wf.graph.nodes if n.type == "flow/Start"]
    if not start_nodes:
        return issues
    # Walk once, tracking tip state per visited node. State transitions:
    #   TipsOn  -> tips_on = True
    #   TipsOff -> tips_on = False
    # A node visited twice with conflicting states is flagged once only.
    state: dict[int, bool] = {}  # node_id -> tips_on at entry
    stack: list[tuple[int, bool]] = [(start_nodes[0].id, False)]
    reported: set[int] = set()
    while stack:
        nid, tips_on = stack.pop()
        prior = state.get(nid)
        if prior is True and tips_on is False:
            # Conflicting states across paths — we don't try to reconcile;
            # if any path has tips off at a liquid action, we'll flag it.
            tips_on = False
        elif prior is False and tips_on is True:
            tips_on = False
        elif prior is not None:
            continue  # already visited with same state
        state[nid] = tips_on
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        if node.type in _LIQUID_TYPES and not tips_on and nid not in reported:
            issues.append(ValidationIssue(
                severity="error", code="LIQUID_WITHOUT_TIPS",
                message=(
                    f"Node {nid} ({node.type}) executes without tips attached. "
                    "Insert a tips/TipsOn upstream of this node on the flow path."
                ),
                node_id=nid,
            ))
            reported.add(nid)
        # Compute outgoing tip state based on this node's type.
        if node.type == "tips/TipsOn":
            out_state = True
        elif node.type == "tips/TipsOff":
            out_state = False
        else:
            out_state = tips_on
        for tgt in adj.get(nid, []):
            stack.append((tgt, out_state))
    return issues


def _check_start_end_reachability(wf: DraftedWorkflow) -> list[ValidationIssue]:
    """Warn on orphan nodes not reachable from Start via flow links.

    Script / sensor data-only connections are fine; this only walks
    flow-typed links (link_type == -1 in LiteGraph). Frame nodes have
    no flow ports and are expected to be orphans in the flow graph.
    """
    issues: list[ValidationIssue] = []
    start_nodes = [n for n in wf.graph.nodes if n.type == "flow/Start"]
    if not start_nodes:
        return issues  # already reported by _check_start_end_counts
    # Build flow adjacency
    adj: dict[int, list[int]] = {}
    for link in wf.graph.links:
        if link.link_type != -1:
            continue
        adj.setdefault(link.origin_id, []).append(link.target_id)
    reachable: set[int] = set()
    stack = [start_nodes[0].id]
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        for tgt in adj.get(nid, []):
            stack.append(tgt)
    for n in wf.graph.nodes:
        if n.type == "flow/Frame":
            continue  # Frames have no flow ports by design
        if n.id not in reachable:
            issues.append(ValidationIssue(
                severity="warning", code="ORPHAN_NODE",
                message=(
                    f"Node {n.id} ({n.type}) is not reachable from flow/Start "
                    "via flow links. It will never execute."
                ),
                node_id=n.id,
            ))
    return issues


# ── Entrypoint ────────────────────────────────────────────────────────


# ── Citation coverage (Phase 3: PDF → workflow pipeline) ──────────────


_STRUCTURAL_NODE_TYPES: tuple[str, ...] = (
    "flow/Start", "flow/End", "flow/Loop", "flow/IfElse", "flow/Frame",
)


def _check_citations(
    wf: DraftedWorkflow,
    *,
    valid_fact_ids: set[str] | None,
    valid_paragraph_ids: set[str] | None,
) -> list[ValidationIssue]:
    """When the drafter was fed a PaperFacts list (Pass 2 of the PDF
    pipeline), every non-structural node MUST carry a source_citation
    whose fact_id and paragraph_id both reference that fact list.

    No-op when both id sets are None — that means this validator was
    invoked on a NL-drafted workflow where citations aren't expected.
    """
    if valid_fact_ids is None and valid_paragraph_ids is None:
        return []
    issues: list[ValidationIssue] = []
    facts = valid_fact_ids or set()
    paras = valid_paragraph_ids or set()
    for n in wf.graph.nodes:
        if n.type in _STRUCTURAL_NODE_TYPES:
            continue
        c = n.source_citation
        if c is None:
            issues.append(ValidationIssue(
                severity="error", code="MISSING_CITATION",
                message=(
                    f"Non-structural node of type {n.type} has no "
                    "source_citation. Every drafted-from-paper node "
                    "must cite the fact_id + paragraph_id it came from."
                ),
                node_id=n.id,
            ))
            continue
        if facts and c.fact_id and c.fact_id not in facts:
            issues.append(ValidationIssue(
                severity="error", code="UNKNOWN_FACT_ID",
                message=(
                    f"source_citation.fact_id={c.fact_id!r} on node {n.id} "
                    "is not in the facts list Pass 1 produced. Never cite "
                    "a fact that wasn't extracted."
                ),
                node_id=n.id,
            ))
        if paras and c.paragraph_id not in paras:
            issues.append(ValidationIssue(
                severity="error", code="UNKNOWN_PARAGRAPH_ID",
                message=(
                    f"source_citation.paragraph_id={c.paragraph_id!r} on "
                    f"node {n.id} is not in the paper. Use only IDs that "
                    "appear in the extracted facts list."
                ),
                node_id=n.id,
            ))
    return issues


def validate_drafted_workflow(
    wf: DraftedWorkflow,
    *,
    strict_deck: bool = False,
    valid_fact_ids: set[str] | None = None,
    valid_paragraph_ids: set[str] | None = None,
) -> list[ValidationIssue]:
    """Run every check on a drafted workflow.

    Args:
        strict_deck: defaults to False. When True, every location
            referenced by a pick/aspirate/etc. node must already have
            labware on the deck at workflow start. Useful from test
            harnesses; too strict for mid-workflow drafts.
        valid_fact_ids / valid_paragraph_ids: supplied by Pass 2 of the
            PDF pipeline. When both are present, every non-structural
            node must carry a source_citation whose ids reference these
            sets. When both are None, citation checking is skipped
            (NL-prompt drafts don't require citations).
    """
    issues: list[ValidationIssue] = []
    issues.extend(_check_start_end_counts(wf))
    issues.extend(_check_unique_ids(wf))
    issues.extend(_check_link_endpoints(wf))
    issues.extend(_check_required_properties(wf))
    issues.extend(_check_location_sanity(wf, strict_deck=strict_deck))
    issues.extend(_check_volume_sanity(wf))
    issues.extend(_check_tips_lifecycle(wf))
    issues.extend(_check_start_end_reachability(wf))
    issues.extend(_check_citations(
        wf,
        valid_fact_ids=valid_fact_ids,
        valid_paragraph_ids=valid_paragraph_ids,
    ))
    return issues


def format_issues_for_repair(issues: list[ValidationIssue]) -> str:
    """Render an issues list as a repair prompt fragment.

    Used by the drafter's retry loop: the LLM sees the previous draft
    plus this block and is asked to fix each issue. Only ERROR-severity
    issues are included; warnings don't block the draft.
    """
    errors = [i for i in issues if i.severity == "error"]
    if not errors:
        return ""
    lines = ["The previous draft had the following problems — please fix them:"]
    for i in errors:
        lines.append(f"  - {i}")
    return "\n".join(lines)
