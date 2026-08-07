"""Pass 1 of the PDF→workflow pipeline: ground facts in paragraphs.

A scientific paper's Methods section is prose — dense with volumes,
concentrations, temperatures, durations, and equipment references, often
stitched together across multiple paragraphs. Asking an LLM to one-shot
this into a valid OpenBravo workflow JSON is a bad deal: the model has
to simultaneously read for comprehension, track quantities, pick node
types from our vocabulary, AND generate valid structured JSON. Failure
modes compound.

The two-pass architecture splits the work:

1. **Pass 1 (this module)** — Read the Methods paragraphs, emit a flat
   list of :class:`ExtractedFact` records. Each fact is grounded in a
   specific `paragraph_id` from the parser, names what *kind* of fact
   it is (material / action / parameter / ...), and carries structured
   numeric fields where present (volume_ul, temperature_c, duration_s,
   etc.). No workflow structure, no node types — just facts.

2. **Pass 2 (llm.py: `draft_workflow_from_facts`)** — Consume the facts
   list + OpenBravo schema and emit a :class:`DraftedWorkflow` where
   every non-structural node's ``source_citation.fact_id`` points at
   the fact that spawned it. This pass doesn't re-read the paper; it
   reasons only over the structured facts.

Splitting this way closes off the "LLM hallucinated a 10 µL that
nobody wrote" failure mode — Pass 2 can't cite a fact that doesn't
exist, so if a parameter isn't in the facts list, it isn't in the
draft.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Fact schema ───────────────────────────────────────────────────────


FactKind = Literal[
    "material",       # "384-well Labcyte LP-0400 plate"
    "reagent",        # "5 mg/mL proteinase K"
    "equipment",      # "Agilent Bravo with Peltier thermal station"
    "action",         # "aspirate 10 uL from the source plate"
    "parameter",      # "centrifuge at 2000 x g for 5 min"
    "condition",      # "incubate at 37 C for 30 min"
    "labware_layout", # "source in slot 5, dest in slot 6"
    "note",           # anything else worth preserving (caveats, branches)
]


class ExtractedFact(BaseModel):
    """A single grounded statement pulled from one paragraph.

    Kept flat on purpose: Pass 2 reasons over a list of these, and
    nested structures would fight structured-output constraints. Where
    structure helps (volume_ul, duration_s, temperature_c), it lives as
    a top-level optional field; everything else lives in ``text``.
    """

    fact_id: str = Field(
        description=(
            "Unique identifier assigned during extraction, e.g. 'f-1'. "
            "Pass 2 uses this to cite back into the facts list when "
            "emitting drafted-node source_citation.fact_id."
        ),
    )
    paragraph_id: str = Field(
        description=(
            "Stable paragraph id from the parser (e.g. 'p-399'). MUST be "
            "one of the paragraph_ids present in the input passages — "
            "never invent one."
        ),
    )
    kind: FactKind = Field(
        description="Tight vocabulary — see the FactKind literal.",
    )
    text: str = Field(
        description=(
            "One short sentence summarizing the fact in canonical form. "
            "Max ~200 chars. Example: 'Aspirate 10 uL from source plate "
            "at slot 5 using 384-channel head.'"
        ),
    )
    volume_ul: float | None = Field(
        default=None,
        description="Volume in microliters, if the fact mentions one. Never guess.",
    )
    duration_s: float | None = Field(
        default=None,
        description="Duration in seconds, if mentioned. Convert minutes/hours.",
    )
    temperature_c: float | None = Field(
        default=None,
        description="Temperature in Celsius, if mentioned.",
    )
    speed_rpm: float | None = Field(
        default=None,
        description="Spin / shake / centrifuge rate, if mentioned. Convert x g when possible.",
    )
    deck_location: int | None = Field(
        default=None,
        description=(
            "Deck slot number (1-9 on a Bravo) if the paper names one. "
            "If the paper just says 'source plate' without a slot, leave null."
        ),
    )
    step_order: int | None = Field(
        default=None,
        description=(
            "Reading-order rank of this fact as an action/step. Optional — "
            "Pass 2 will re-sort if the facts are out of order."
        ),
    )


class PaperFacts(BaseModel):
    """The full Pass-1 output for one paper."""

    source_file: str = Field(
        default="",
        description="Original PDF filename, carried through for logging / display.",
    )
    summary: str = Field(
        default="",
        max_length=500,
        description=(
            "One-to-two sentence plain-English summary of the protocol "
            "described in the paper. Sets context for Pass 2."
        ),
    )
    facts: list[ExtractedFact] = Field(
        description=(
            "All facts extracted from the Methods section. Can be "
            "empty if the paper doesn't describe a protocol."
        ),
    )

    def actions(self) -> list[ExtractedFact]:
        """Facts of kind 'action' — the core steps Pass 2 will turn
        into workflow nodes."""
        return [f for f in self.facts if f.kind == "action"]

    def by_kind(self, kind: FactKind) -> list[ExtractedFact]:
        return [f for f in self.facts if f.kind == kind]


# ── Pass-1 prompt ──────────────────────────────────────────────────────


_PASS1_ROLE = """You are a molecular-biology-savvy protocol extractor. You
read the Methods/Experimental section of a scientific paper and emit a
structured list of FACTS about the liquid-handling protocol it describes.

## Your output

A single JSON object matching the PaperFacts schema: a `summary` field
plus a `facts` list. Each fact grounds in EXACTLY ONE paragraph by its
stable paragraph_id (the IDs are provided below).

## Hard rules

1. Every fact's `paragraph_id` MUST be one of the IDs I provide. Never
   invent a new paragraph id.
2. If the paper doesn't mention a numeric quantity, leave the
   corresponding optional field null. NEVER guess. "A small volume"
   is not 10 uL; it's null with an action kind and text describing
   the ambiguity.
3. Prefer canonical units: uL for volumes, seconds for durations, C
   for temperatures, rpm for rotation (convert x g when feasible).
4. Each fact's `text` is one sentence. Long compound steps = multiple
   facts sharing the same paragraph_id.
5. `kind` is a tight vocabulary — use the closest match and put
   ambiguous content in `note`. Do not invent new kinds.
6. `fact_id` values are `f-1`, `f-2`, ... in the order you emit them.
7. Omit ambient / computational / sequencing / staining steps that
   don't involve liquid handling. Keep only what a Bravo robot would
   actually do (transfers, tip changes, plate movements, read steps).
8. If the Methods section is not actually a liquid-handling protocol
   (e.g. it's a computational analysis), return an empty facts list
   and a summary explaining why.
9. A single Methods section often bundles MULTIPLE sub-protocols —
   bench chemistry (coupling reactions, reagent preparation, column
   packing) sitting next to the ACTUAL automation step. You MUST
   privilege the AUTOMATED sub-protocol:
   * Prefer paragraphs that mention an instrument by name ("Bravo",
     "Hamilton", "Hamilton STAR", "Tecan", "Tecan EVO", "Tecan Fluent",
     "liquid handler", "robot", "automated"). These anchor the
     sub-section that should drive the workflow.
   * Prefer paragraphs whose volumes are in MICROLITRES (µL / uL).
     A paragraph that mixes 5 mL / 10 mL / 45 mL volumes is almost
     certainly bench chemistry — EXCLUDE its facts unless it feeds
     a later automated step.
   * Prefer paragraphs that name plate formats ("96-well PCR plate",
     "384-well", "PCR plate on the deck") or deck positions.
   * When in doubt between two sub-sections, pick the one with the
     HIGHEST density of the signals above. The other sub-section is
     probably bench prep the operator will do manually.
   This bias is not optional — a drafted workflow that describes the
   bench-chemistry step instead of the automation step is useless.

## Tone

No prose outside the JSON. No markdown fences. No commentary.
"""


def build_pass1_user_prompt(
    passages: list[dict[str, Any]],
    *,
    source_file: str = "",
) -> str:
    """Assemble the user-message text for Pass 1.

    Args:
        passages: one entry per paragraph, each with keys
                  ``paragraph_id``, ``text``, ``page`` (optional),
                  ``section`` (optional), ``kind`` (optional).
        source_file: original filename, rendered in the prompt header.

    Returns:
        A plain-text user message ready for the LLM client.
    """
    lines: list[str] = []
    if source_file:
        lines.append(f"Source paper: {source_file}")
        lines.append("")
    lines.append(
        "Below are the Methods/Experimental paragraphs from the paper. "
        "Each has a stable `paragraph_id` you MUST use verbatim when "
        "citing it in the `paragraph_id` field of any fact you emit."
    )
    lines.append("")
    for p in passages:
        pid = p.get("paragraph_id", "?")
        page = p.get("page")
        kind = p.get("kind", "paragraph")
        section = p.get("section", "")
        header = f"[{pid}]"
        meta_bits: list[str] = []
        if page is not None:
            meta_bits.append(f"p.{page}")
        if section:
            meta_bits.append(section)
        if kind != "paragraph":
            meta_bits.append(kind)
        if meta_bits:
            header += "  (" + ", ".join(meta_bits) + ")"
        lines.append(header)
        lines.append(p.get("text", "").strip())
        lines.append("")
    lines.append(
        "Emit the PaperFacts JSON now. Remember: every fact's "
        "paragraph_id MUST match one of the bracketed ids above."
    )
    return "\n".join(lines)


# Exposed for the orchestrator.
PASS1_SYSTEM_PROMPT: str = _PASS1_ROLE
