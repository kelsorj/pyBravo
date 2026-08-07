"""Pass 0 — segment a paper's Methods section into candidate protocols.

A scientific paper's Methods section often stacks multiple sub-protocols
end-to-end: bench chemistry (buffer prep, column packing) adjacent to
the actual automation step the lab cares about. Pass 1 (fact extraction)
used to see all of these at once and conflate their facts — the LLM
would emit a workflow describing the bench prep with an occasional
Bravo step mixed in, or just pick the wrong sub-protocol entirely
(see the PMC10716174 bead-prep failure in the test corpus).

This module runs *before* Pass 1 and splits the paper into discrete
:class:`ProtocolCandidate` records keyed by sub-heading. Each candidate
carries a heuristic score + a short LLM-refined rationale so the UI
can show a picker card with:

* title (verbatim sub-heading)
* first sentence (preview / tooltip)
* page range
* hint instruments mentioned ("Bravo", "Hamilton STAR", ...)
* volume-scale flag ("µL" | "mL" | "mixed")
* confidence 0..1
* rationale (why the LLM thinks it is or isn't an automation protocol)

The caller decides what to do with low-confidence candidates: the
picker may auto-select the top one when confidence is high and it
clearly beats the runner-up, else show the whole list for the user
to validate.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from pybravo.workflow.drafter.paper_parser import ParsedPaper, ParsedParagraph

logger = logging.getLogger(__name__)


# ── Rule-based shortlist heuristics ──────────────────────────────────

# Keyword → badge. Match is substring-insensitive on the paragraph text.
_INSTRUMENT_PATTERNS: dict[str, str] = {
    "bravo":              "Bravo",
    "agilent bravo":      "Bravo",
    "hamilton star":      "Hamilton STAR",
    "hamilton vantage":   "Hamilton Vantage",
    "hamilton microlab":  "Hamilton Microlab",
    "tecan evo":          "Tecan EVO",
    "tecan fluent":       "Tecan Fluent",
    "tecan freedom":      "Tecan Freedom",
    "opentrons":          "Opentrons",
    "liquid handler":     "Liquid handler",
    "liquid handling":    "Liquid handler",
    "automated":          "Automation",
}

# Volume-scale regexes
_UL_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:µL|uL|\u03bcL|microlit(?:er|re)s?)\b", re.IGNORECASE)
_ML_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mL|millilit(?:er|re)s?)\b", re.IGNORECASE)

# Plate / deck hints that reinforce the "this is automation" signal.
_PLATE_HINTS = (
    "96-well", "96 well", "384-well", "384 well", "1536-well",
    "pcr plate", "microplate", "sbs", "deep-well", "deep well",
    "tip box", "tip rack", "deck position", "deck slot",
)


class ProtocolCandidate(BaseModel):
    """One candidate protocol within a paper — typically a single
    sub-section under Methods."""

    title: str = Field(
        description="Sub-heading as it appears in the paper, e.g. "
                    "'Automated enrichment of newly synthesized proteins'."
    )
    paragraph_ids: list[str] = Field(
        description="Ordered list of paragraph ids that belong to this "
                    "candidate (the sub-heading itself, plus its body "
                    "paragraphs up to the next sibling sub-heading)."
    )
    first_sentence: str = Field(
        default="",
        description="First sentence of the candidate body — displayed "
                    "as a preview in the picker card.",
    )
    page_range: tuple[int | None, int | None] = Field(
        default=(None, None),
        description="(first_page, last_page) covered by this candidate. "
                    "Either may be None when Docling didn't surface a page number.",
    )
    hint_instruments: list[str] = Field(
        default_factory=list,
        description="Instruments explicitly named in the body "
                    "(Bravo, Hamilton STAR, Tecan, ...).",
    )
    volume_scale: Literal["µL", "mL", "mixed", "none"] = Field(
        default="none",
        description="'µL' = dominated by microlitre volumes; 'mL' = bench-"
                    "scale millilitre volumes; 'mixed' = both; 'none' = no "
                    "numeric volumes detected.",
    )
    plate_format_hints: list[str] = Field(
        default_factory=list,
        description="Plate-format phrases detected in the body "
                    "(96-well, PCR plate, ...).",
    )
    heuristic_score: float = Field(
        default=0.0,
        description="0..1 score from the rule-based scorer. Higher = more "
                    "likely to be an automated liquid-handling protocol.",
    )
    confidence: float = Field(
        default=0.0,
        description="Post-LLM-refinement confidence this candidate is the "
                    "automation sub-protocol worth drafting. 0..1.",
    )
    rationale: str = Field(
        default="",
        description="One-sentence rationale from the LLM pass (or the "
                    "heuristic fallback when the LLM is unavailable).",
    )
    body_chars: int = Field(
        default=0,
        description="Total character count of the candidate body — purely "
                    "diagnostic, useful when a candidate looks empty.",
    )


class PaperProtocols(BaseModel):
    """Pass-0 output: all candidate protocols found in a paper."""

    source_file: str = ""
    candidates: list[ProtocolCandidate] = Field(default_factory=list)
    segmenter_version: int = 1
    notes: str = ""           # free-form, e.g. "methods section not detected"


# ── Heuristic core ───────────────────────────────────────────────────


def _methods_paragraphs(parsed: ParsedPaper) -> list[ParsedParagraph]:
    """Return paragraphs that look like Methods content. Substring match
    on the section label — same criteria Pass 1 uses so the two passes
    stay consistent."""
    keep = []
    for p in parsed.paragraphs:
        sec = (p.section or "").lower()
        if any(k in sec for k in ("method", "experimental", "procedure", "protocol")):
            keep.append(p)
    return keep


def _group_by_subheading(
    paragraphs: list[ParsedParagraph],
) -> list[list[ParsedParagraph]]:
    """Split a flat paragraph list into sub-sections keyed on heading
    kind.

    The paper parser marks sub-section headings with ``kind="heading"``
    and a ``heading_level`` int. Each heading starts a new group; body
    paragraphs accumulate into the current group. If the stream starts
    mid-body (no heading yet), those early paragraphs become the first
    anonymous group.
    """
    groups: list[list[ParsedParagraph]] = []
    current: list[ParsedParagraph] = []
    for p in paragraphs:
        if p.kind == "heading":
            # New heading starts a new group. Push the previous one if
            # it had any body content.
            if current:
                groups.append(current)
            current = [p]
        else:
            current.append(p)
    if current:
        groups.append(current)
    return groups


def _score_group(body_text: str) -> tuple[float, dict[str, Any]]:
    """Rule-based score for a candidate sub-section.

    Signals considered:
      + µL volume mentions  (strong positive)
      + instrument names    (very strong positive)
      + plate-format hints  (positive)
      - mL-only volumes     (negative — bench chemistry)

    Returns (score 0..1, detail dict) where ``detail`` is the fields we
    want to surface on the candidate record.
    """
    text_lower = body_text.lower()

    # Instrument mentions
    instruments: list[str] = []
    for needle, label in _INSTRUMENT_PATTERNS.items():
        if needle in text_lower and label not in instruments:
            instruments.append(label)

    # Volume scale
    ul_count = len(_UL_RE.findall(body_text))
    ml_count = len(_ML_RE.findall(body_text))
    if ul_count and ml_count:
        scale: Literal["µL", "mL", "mixed", "none"] = "mixed"
    elif ul_count:
        scale = "µL"
    elif ml_count:
        scale = "mL"
    else:
        scale = "none"

    # Plate / deck hints
    plate_hints = [h for h in _PLATE_HINTS if h in text_lower]

    # Scoring (tunable — current weights reflect my reading of the
    # PMC10716174 failure case + a dozen positives from the test corpus).
    score = 0.0
    if instruments:
        score += 0.45 + 0.10 * min(len(instruments) - 1, 3)
    if scale == "µL":
        score += 0.30
    elif scale == "mixed":
        score += 0.15
    elif scale == "mL":
        score -= 0.15                                # bench chemistry penalty
    if plate_hints:
        score += 0.10 + 0.05 * min(len(plate_hints) - 1, 3)
    # A tiny amount of raw length helps — empty sections shouldn't win.
    if len(body_text) > 400:
        score += 0.05

    score = max(0.0, min(1.0, score))
    return score, {
        "hint_instruments": instruments,
        "volume_scale": scale,
        "plate_format_hints": plate_hints,
    }


def _first_sentence(text: str, cap: int = 220) -> str:
    """First sentence or ~220 chars, whichever comes first."""
    if not text:
        return ""
    # Cheap sentence split — our preview doesn't need linguistic accuracy.
    for sep in (". ", "? ", "! "):
        idx = text.find(sep)
        if 0 < idx < cap:
            return text[: idx + 1].strip()
    return (text[:cap] + "…") if len(text) > cap else text


def _candidate_from_group(
    group: list[ParsedParagraph],
    rank_hint: int,
) -> ProtocolCandidate:
    """Build a ProtocolCandidate record from a grouped paragraph list.

    The group starts with its heading (if any) followed by its body
    paragraphs.  Non-heading-only content becomes the body text for
    scoring.
    """
    title = ""
    body_paras: list[ParsedParagraph] = []
    for i, p in enumerate(group):
        if i == 0 and p.kind == "heading":
            title = p.text.strip()
        else:
            body_paras.append(p)

    # Fallback title when the group started mid-body.
    if not title:
        title = f"Untitled section {rank_hint}"

    body_text = "\n".join(p.text for p in body_paras)
    score, detail = _score_group(body_text)

    pages = [p.page_no for p in body_paras if p.page_no]
    page_range = (min(pages) if pages else None, max(pages) if pages else None)

    return ProtocolCandidate(
        title=title,
        paragraph_ids=[p.paragraph_id for p in group],
        first_sentence=_first_sentence(body_text),
        page_range=page_range,
        hint_instruments=detail["hint_instruments"],
        volume_scale=detail["volume_scale"],
        plate_format_hints=detail["plate_format_hints"],
        heuristic_score=score,
        confidence=score,                             # overwritten by LLM pass if we run one
        rationale="",                                 # ditto
        body_chars=len(body_text),
    )


# ── LLM refinement (optional) ────────────────────────────────────────


_LLM_ROLE = """You are an automated-protocol analyst. Given a short
summary of one sub-section pulled from a scientific paper's Methods,
decide whether it describes an AUTOMATED liquid-handling protocol a
lab robot (Bravo, Hamilton STAR, Tecan EVO/Fluent) could execute.

Output format: a JSON object with two fields:

  confidence (float 0..1)    — calibrated probability that this
                               sub-section is an automated liquid-
                               handling protocol worth drafting as
                               a workflow.
  rationale  (one sentence)  — why (instruments mentioned, volume
                               scale, plate format, etc.).

Hard rules:
- A sub-section describing bench chemistry (column packing, reagent
  synthesis, cell culture) at mL scale is NOT automation. Confidence < 0.2.
- A sub-section explicitly naming an automation instrument AND using
  µL volumes is very likely automation. Confidence > 0.8.
- Be calibrated — when in doubt, return a number close to 0.5 and
  explain the ambiguity in the rationale.

No prose outside the JSON. No markdown fences.
"""


class _LLMScoreResponse(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=280)


def _build_llm_user_prompt(cand: ProtocolCandidate, body_preview: str) -> str:
    bits = [
        f"Sub-section title: {cand.title}",
        f"Page range: {cand.page_range[0]}–{cand.page_range[1]}",
        f"Instruments mentioned (heuristic): {', '.join(cand.hint_instruments) or '(none)'}",
        f"Volume scale (heuristic): {cand.volume_scale}",
        f"Plate-format hints: {', '.join(cand.plate_format_hints) or '(none)'}",
        f"Heuristic pre-score: {cand.heuristic_score:.2f}",
        "",
        "Body preview (first 1500 chars):",
        body_preview[:1500],
    ]
    return "\n".join(bits)


async def _refine_with_llm(
    candidates: list[ProtocolCandidate],
    paragraphs_by_id: dict[str, str],
) -> None:
    """In-place: fill candidate.confidence + candidate.rationale via a
    per-candidate LLM call. Silently no-ops (keeps heuristic scores in
    confidence) when LLM deps / credentials aren't available.

    One call per candidate keeps the prompt short and structured-output
    constraint simple.
    """
    try:
        from pybravo.workflow.drafter.llm import _build_client, _llm_structured, _resolve_config
    except Exception:
        logger.info("drafter_segmenter_llm_unavailable", reason="import_failed")
        for c in candidates:
            c.rationale = _heuristic_rationale(c)
        return

    try:
        cfg = _resolve_config()
        client = _build_client(cfg.provider)
    except Exception as exc:
        logger.info("drafter_segmenter_llm_unavailable", reason=str(exc))
        for c in candidates:
            c.rationale = _heuristic_rationale(c)
        return

    for c in candidates:
        body_preview = "\n".join(
            paragraphs_by_id.get(pid, "") for pid in c.paragraph_ids
        )
        try:
            resp = _llm_structured(
                client, cfg,
                system=_LLM_ROLE,
                user=_build_llm_user_prompt(c, body_preview),
                response_model=_LLMScoreResponse,
            )
            c.confidence = float(resp.confidence)
            c.rationale = resp.rationale.strip()
        except Exception as exc:
            logger.warning("drafter_segmenter_llm_call_failed", title=c.title, exc_info=exc)
            c.rationale = _heuristic_rationale(c)


def _heuristic_rationale(c: ProtocolCandidate) -> str:
    """Fallback rationale used when the LLM refinement can't run."""
    bits: list[str] = []
    if c.hint_instruments:
        bits.append(f"mentions {', '.join(c.hint_instruments)}")
    if c.volume_scale == "µL":
        bits.append("µL-scale volumes")
    elif c.volume_scale == "mL":
        bits.append("mL-scale (likely bench prep)")
    elif c.volume_scale == "mixed":
        bits.append("mixed µL + mL volumes")
    if c.plate_format_hints:
        bits.append(f"plate format: {c.plate_format_hints[0]}")
    if not bits:
        return "no automation signals detected."
    return "Heuristic: " + "; ".join(bits) + "."


# ── Public entrypoint ────────────────────────────────────────────────


async def segment_paper(
    parsed: ParsedPaper,
    *,
    refine_with_llm: bool = True,
) -> PaperProtocols:
    """Split a parsed paper's Methods into candidate protocols.

    ``refine_with_llm=True`` runs a cheap per-candidate LLM classifier
    to calibrate confidence and emit a human-readable rationale. With
    ``False`` we skip the LLM entirely (heuristic score becomes
    confidence) — useful for offline tests and for fallback when the
    LLM provider is unreachable.
    """
    methods = _methods_paragraphs(parsed)
    if not methods:
        return PaperProtocols(
            source_file=parsed.source_name,
            candidates=[],
            notes="No Methods section detected by the parser.",
        )

    groups = _group_by_subheading(methods)
    # Drop trivially small groups (probably just a stray heading with
    # no body) — they can't be drafted into a workflow anyway.
    groups = [g for g in groups if sum(len(p.text) for p in g if p.kind != "heading") > 80]
    if not groups:
        return PaperProtocols(
            source_file=parsed.source_name,
            candidates=[],
            notes="Methods section detected but no sub-section had enough body to score.",
        )

    candidates = [_candidate_from_group(g, i + 1) for i, g in enumerate(groups)]

    if refine_with_llm and candidates:
        paragraphs_by_id = {p.paragraph_id: p.text for p in parsed.paragraphs}
        await _refine_with_llm(candidates, paragraphs_by_id)
    else:
        for c in candidates:
            c.rationale = _heuristic_rationale(c)

    # Sort descending by confidence — the picker shows them in this order.
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    return PaperProtocols(
        source_file=parsed.source_name,
        candidates=candidates,
    )


def autoselect_top(candidates: list[ProtocolCandidate]) -> int | None:
    """Return the index of a candidate to auto-select, or None if the
    picker should prompt the user.

    Heuristic: auto-pick when the top candidate is confidently above
    the 'probably automation' threshold and clearly beats #2.
    """
    if not candidates:
        return None
    top = candidates[0]
    if top.confidence < 0.80:
        return None
    if len(candidates) == 1:
        return 0
    runner = candidates[1]
    if top.confidence - runner.confidence >= 0.25:
        return 0
    return None
