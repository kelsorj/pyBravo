"""LLM invocation + retry loop for the workflow drafter.

Imports ``instructor`` / ``anthropic`` / ``openai`` lazily so the
schema, prompt, and validator modules can load (and be tested) without
the LLM packages installed. The server endpoint returns a clean 501
with an install hint if the deps are missing.

Provider choice is driven by env vars, in priority order:

1. ``ANTHROPIC_API_KEY`` — use Claude 3.5 Sonnet.
2. ``OPENAI_API_KEY`` — use GPT-4o.

When both are set, Anthropic wins (per plan recommendation). A
``PYBRAVO_DRAFTER_PROVIDER`` env var overrides the default ("anthropic"
or "openai") for A/B testing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog

from pybravo.workflow.drafter.facts import (
    PASS1_SYSTEM_PROMPT,
    ExtractedFact,
    PaperFacts,
    build_pass1_user_prompt,
)
from pybravo.workflow.drafter.paper_parser import ParsedPaper
from pybravo.workflow.drafter.prompt import build_system_prompt
from pybravo.workflow.drafter.schema import DraftedWorkflow
from pybravo.workflow.drafter.validator import (
    ValidationIssue,
    format_issues_for_repair,
    validate_drafted_workflow,
)

logger = structlog.get_logger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────


class LLMDrafterError(RuntimeError):
    """Base class for drafter failures surfaced to the API layer."""


class MissingLLMDependencyError(LLMDrafterError):
    """``instructor`` + ``anthropic`` / ``openai`` aren't installed."""


class NoLLMCredentialsError(LLMDrafterError):
    """No ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY`` in the environment."""


# ── Config ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DrafterConfig:
    """Resolved provider + model + retry config for one draft call."""

    provider: str  # "anthropic" | "openai"
    model: str
    max_tokens: int = 4096
    temperature: float = 0.1
    max_repair_attempts: int = 2


def _resolve_config() -> DrafterConfig:
    """Pick the provider/model based on env vars.

    Environment knobs:
        PYBRAVO_DRAFTER_PROVIDER — "anthropic" | "openai"
        PYBRAVO_DRAFTER_MODEL    — override the default model id
        PYBRAVO_DRAFTER_MAX_TOKENS / _TEMPERATURE / _REPAIR_ATTEMPTS
    """
    forced = os.environ.get("PYBRAVO_DRAFTER_PROVIDER", "").strip().lower()
    anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    if forced == "anthropic" and anthropic_key:
        provider = "anthropic"
    elif forced == "openai" and openai_key:
        provider = "openai"
    elif anthropic_key:
        provider = "anthropic"
    elif openai_key:
        provider = "openai"
    else:
        raise NoLLMCredentialsError(
            "No LLM credentials found. Set ANTHROPIC_API_KEY (Claude) or "
            "OPENAI_API_KEY (GPT-4o) in the environment before calling "
            "/api/workflow/draft."
        )

    default_model = {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o",
    }[provider]
    model = os.environ.get("PYBRAVO_DRAFTER_MODEL", default_model)

    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    return DrafterConfig(
        provider=provider,
        model=model,
        max_tokens=_int_env("PYBRAVO_DRAFTER_MAX_TOKENS", 4096),
        temperature=_float_env("PYBRAVO_DRAFTER_TEMPERATURE", 0.1),
        max_repair_attempts=_int_env("PYBRAVO_DRAFTER_REPAIR_ATTEMPTS", 2),
    )


# ── Lazy client factory ───────────────────────────────────────────────


def _load_instructor():
    """Import instructor + the chosen SDK at call time only."""
    try:
        import instructor  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingLLMDependencyError(
            "The `instructor` package is required for /api/workflow/draft. "
            "Install it: pip install 'pybravo[llm]' (or: pip install "
            "instructor anthropic openai)."
        ) from exc
    return instructor


def _build_client(provider: str):
    instructor = _load_instructor()
    if provider == "anthropic":
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingLLMDependencyError(
                "The `anthropic` package is required for provider=anthropic. "
                "pip install anthropic"
            ) from exc
        return instructor.from_anthropic(anthropic.Anthropic())
    elif provider == "openai":
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MissingLLMDependencyError(
                "The `openai` package is required for provider=openai. "
                "pip install openai"
            ) from exc
        return instructor.from_openai(openai.OpenAI())
    else:
        raise LLMDrafterError(f"Unknown provider: {provider!r}")


# ── Draft entrypoint ──────────────────────────────────────────────────


@dataclass
class DraftResult:
    """What ``draft_workflow`` returns to the API layer."""

    workflow: DraftedWorkflow
    issues: list[ValidationIssue]
    attempts: int
    provider: str
    model: str

    def designer_payload(self) -> dict[str, Any]:
        """Shape the API response so the designer can deserialize directly."""
        return {
            "workflow": self.workflow.to_designer_json(),
            "warnings": [str(i) for i in self.issues if i.severity == "warning"],
            "errors": [str(i) for i in self.issues if i.severity == "error"],
            "meta": {
                "provider": self.provider,
                "model": self.model,
                "attempts": self.attempts,
            },
        }


async def draft_workflow(
    prompt: str,
    *,
    current_deck: dict[str, Any] | None = None,
    config: DrafterConfig | None = None,
) -> DraftResult:
    """End-to-end draft: system prompt → LLM → validate → (optional) repair.

    The repair loop re-sends the previous (invalid) draft to the LLM
    with a formatted issue list, asking it to fix the errors. Capped
    at ``config.max_repair_attempts`` retries. Warnings do not trigger
    retries; only errors do.

    Raises:
        MissingLLMDependencyError: ``instructor`` / SDK not installed.
        NoLLMCredentialsError: no ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``.
        LLMDrafterError: LLM call itself failed.
    """
    cfg = config or _resolve_config()
    client = _build_client(cfg.provider)
    system_prompt = build_system_prompt(current_deck=current_deck)

    messages: list[dict[str, str]] = [
        {"role": "user", "content": prompt},
    ]

    last_wf: DraftedWorkflow | None = None
    last_issues: list[ValidationIssue] = []

    for attempt in range(cfg.max_repair_attempts + 1):
        logger.info(
            "drafter_llm_call",
            provider=cfg.provider,
            model=cfg.model,
            attempt=attempt + 1,
            of_max=cfg.max_repair_attempts + 1,
        )
        try:
            if cfg.provider == "anthropic":
                # Anthropic's SDK requires system as a top-level param.
                wf = client.messages.create(
                    model=cfg.model,
                    max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    system=system_prompt,
                    messages=messages,
                    response_model=DraftedWorkflow,
                )
            else:
                # OpenAI: system goes in the messages array.
                wf = client.chat.completions.create(
                    model=cfg.model,
                    max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    messages=[{"role": "system", "content": system_prompt}, *messages],
                    response_model=DraftedWorkflow,
                )
        except Exception as exc:
            # Instructor wraps schema-violation retries internally; what
            # bubbles up here is typically network / auth / context-
            # length error. Surface with the attempt number.
            raise LLMDrafterError(
                f"LLM call failed on attempt {attempt + 1}: {exc}"
            ) from exc

        last_wf = wf
        last_issues = validate_drafted_workflow(wf)
        error_issues = [i for i in last_issues if i.severity == "error"]
        if not error_issues:
            logger.info("drafter_draft_ok", attempt=attempt + 1, warnings=len(last_issues))
            return DraftResult(
                workflow=wf,
                issues=last_issues,
                attempts=attempt + 1,
                provider=cfg.provider,
                model=cfg.model,
            )

        logger.info(
            "drafter_draft_errors",
            attempt=attempt + 1,
            errors=[i.code for i in error_issues],
        )
        if attempt == cfg.max_repair_attempts:
            # Out of retries — return the last draft with errors attached.
            return DraftResult(
                workflow=wf,
                issues=last_issues,
                attempts=attempt + 1,
                provider=cfg.provider,
                model=cfg.model,
            )

        # Append the failed draft + repair instructions for the next attempt.
        messages.append({
            "role": "assistant",
            "content": wf.model_dump_json(),
        })
        messages.append({
            "role": "user",
            "content": format_issues_for_repair(last_issues),
        })

    # Unreachable — the loop either returns or raises.
    assert last_wf is not None
    return DraftResult(
        workflow=last_wf,
        issues=last_issues,
        attempts=cfg.max_repair_attempts + 1,
        provider=cfg.provider,
        model=cfg.model,
    )


# ══════════════════════════════════════════════════════════════════════
# Phase 3: two-pass PDF → workflow
# ══════════════════════════════════════════════════════════════════════


def _llm_structured(
    client: Any,
    cfg: DrafterConfig,
    *,
    system: str,
    user: str,
    response_model: Any,
    max_tokens: int | None = None,
) -> Any:
    """One-shot structured-output call, provider-agnostic.

    Lifted out of ``draft_workflow`` so Passes 1 & 2 of the PDF
    pipeline can reuse the same client plumbing without dragging in
    the repair-loop. Returns the parsed Pydantic instance.
    """
    mt = max_tokens or cfg.max_tokens
    if cfg.provider == "anthropic":
        return client.messages.create(
            model=cfg.model,
            max_tokens=mt,
            temperature=cfg.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_model=response_model,
        )
    else:
        return client.chat.completions.create(
            model=cfg.model,
            max_tokens=mt,
            temperature=cfg.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_model=response_model,
        )


async def extract_facts(
    parsed: ParsedPaper,
    *,
    section_hint: str = "methods",
    config: DrafterConfig | None = None,
) -> PaperFacts:
    """Pass 1: parsed paper → grounded fact list.

    Pulls paragraphs whose detected section matches ``section_hint``
    (substring match, e.g. "methods" hits "Materials and Methods",
    "experimental" catches Agilent-style docs). Falls back to *all*
    paragraphs if the section filter matches nothing — better to over-
    send than to silently return empty.

    Raises the same exception family as ``draft_workflow``.
    """
    cfg = config or _resolve_config()
    client = _build_client(cfg.provider)

    # Collect passages to feed the LLM. Heading-kind items are included
    # alongside paragraphs so the LLM can anchor facts to "4.5. Manual
    # preparation" when that's what a sentence refers back to.
    section_needle = section_hint.strip().lower()
    passages: list[dict[str, Any]] = []
    for p in parsed.paragraphs:
        sec = (p.section or "").lower()
        if section_needle and section_needle not in sec and "experimental" not in sec and "protocol" not in sec:
            continue
        passages.append({
            "paragraph_id": p.paragraph_id,
            "text": p.text,
            "page": p.page_no,
            "section": p.section,
            "kind": p.kind,
        })
    if not passages:
        # No Methods section detected — fall back to every non-reference,
        # non-acknowledgement paragraph.
        excluded = ("references", "acknowledgements", "acknowledgments")
        for p in parsed.paragraphs:
            if (p.section or "").lower() in excluded:
                continue
            passages.append({
                "paragraph_id": p.paragraph_id,
                "text": p.text,
                "page": p.page_no,
                "section": p.section,
                "kind": p.kind,
            })

    user_prompt = build_pass1_user_prompt(passages, source_file=parsed.source_name)

    logger.info(
        "drafter_pass1_call",
        provider=cfg.provider, model=cfg.model,
        passage_count=len(passages),
        total_chars=sum(len(p["text"]) for p in passages),
    )
    try:
        facts = _llm_structured(
            client, cfg,
            system=PASS1_SYSTEM_PROMPT,
            user=user_prompt,
            response_model=PaperFacts,
        )
    except Exception as exc:
        raise LLMDrafterError(f"Pass 1 (fact extraction) failed: {exc}") from exc

    # Enforce the "paragraph_id must be one we supplied" rule post-hoc;
    # Instructor's schema doesn't catch this. Drop any hallucinated refs.
    valid_ids = {p["paragraph_id"] for p in passages}
    kept: list[ExtractedFact] = []
    dropped = 0
    for f in facts.facts:
        if f.paragraph_id in valid_ids:
            kept.append(f)
        else:
            dropped += 1
            logger.warning(
                "pass1 dropped fact with hallucinated paragraph_id",
                fact_id=f.fact_id, paragraph_id=f.paragraph_id,
            )
    facts.facts = kept
    if dropped:
        logger.info("pass1_facts_kept", kept=len(kept), dropped=dropped)
    facts.source_file = parsed.source_name or facts.source_file
    return facts


def _pass2_user_prompt(facts: PaperFacts, current_deck: dict[str, Any] | None) -> str:
    """Build the Pass-2 user message. System prompt is the normal
    drafter prompt (same node catalog / labware / snippet context)."""
    lines: list[str] = []
    if facts.source_file:
        lines.append(f"Source paper: {facts.source_file}")
    if facts.summary:
        lines.append(f"Protocol summary (from Pass 1): {facts.summary}")
    lines.append("")
    lines.append(
        "Pass 1 extracted the following grounded facts from the paper. "
        "Turn them into an OpenBravo workflow JSON. EVERY non-structural "
        "node (anything that isn't flow/Start, flow/End, flow/Loop, "
        "flow/IfElse, or flow/Frame) MUST include a `source_citation` "
        "field whose `fact_id` matches one of the fact_ids below, and "
        "whose `paragraph_id` matches that same fact's paragraph_id. "
        "NEVER invent a fact_id not in this list, NEVER leave a non-"
        "structural node without a source_citation."
    )
    lines.append("")
    lines.append("Facts (fact_id | paragraph_id | kind | text):")
    for f in facts.facts:
        bits: list[str] = []
        if f.volume_ul is not None:
            bits.append(f"volume={f.volume_ul} uL")
        if f.duration_s is not None:
            bits.append(f"duration={f.duration_s} s")
        if f.temperature_c is not None:
            bits.append(f"temp={f.temperature_c} C")
        if f.speed_rpm is not None:
            bits.append(f"speed={f.speed_rpm} rpm")
        if f.deck_location is not None:
            bits.append(f"deck={f.deck_location}")
        if f.step_order is not None:
            bits.append(f"order={f.step_order}")
        suffix = f"  [{'; '.join(bits)}]" if bits else ""
        lines.append(f"  {f.fact_id} | {f.paragraph_id} | {f.kind} | {f.text}{suffix}")
    lines.append("")
    lines.append(
        "Emit a single DraftedWorkflow JSON. Remember the citation "
        "requirement on every action / liquid / plate / tips / sensor / "
        "system / logic node."
    )
    return "\n".join(lines)


async def draft_workflow_from_facts(
    facts: PaperFacts,
    *,
    current_deck: dict[str, Any] | None = None,
    config: DrafterConfig | None = None,
) -> DraftResult:
    """Pass 2: grounded facts → drafted workflow with citations.

    Uses the same repair-loop shape as :func:`draft_workflow` — if the
    LLM emits a schema-valid but semantically-broken workflow (missing
    tips, orphan nodes, etc.), we resend with the issue list and ask
    for a fix. Up to ``cfg.max_repair_attempts`` retries.
    """
    cfg = config or _resolve_config()
    client = _build_client(cfg.provider)

    system_prompt = build_system_prompt(current_deck=current_deck)
    user_prompt = _pass2_user_prompt(facts, current_deck)

    # Bolt the citation requirement onto the normal system prompt — the
    # prompt.py file is a single source of truth for the node catalog
    # etc., and we don't want Pass 2 diverging from that.
    system_prompt = system_prompt + "\n\n" + _PASS2_CITATION_CLAUSE

    messages: list[dict[str, str]] = [{"role": "user", "content": user_prompt}]
    last_wf: DraftedWorkflow | None = None
    last_issues: list[ValidationIssue] = []

    for attempt in range(cfg.max_repair_attempts + 1):
        logger.info(
            "drafter_pass2_call",
            provider=cfg.provider, model=cfg.model,
            attempt=attempt + 1,
            of_max=cfg.max_repair_attempts + 1,
            fact_count=len(facts.facts),
        )
        try:
            if cfg.provider == "anthropic":
                wf = client.messages.create(
                    model=cfg.model, max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature, system=system_prompt,
                    messages=messages, response_model=DraftedWorkflow,
                )
            else:
                wf = client.chat.completions.create(
                    model=cfg.model, max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    messages=[{"role": "system", "content": system_prompt}, *messages],
                    response_model=DraftedWorkflow,
                )
        except Exception as exc:
            raise LLMDrafterError(f"Pass 2 (workflow from facts) failed: {exc}") from exc

        last_wf = wf
        # Pass the facts' paragraph/fact IDs into the validator so it
        # can check citation sanity alongside the normal graph checks.
        valid_fact_ids = {f.fact_id for f in facts.facts}
        valid_paragraph_ids = {f.paragraph_id for f in facts.facts}
        last_issues = validate_drafted_workflow(
            wf,
            valid_fact_ids=valid_fact_ids,
            valid_paragraph_ids=valid_paragraph_ids,
        )
        error_issues = [i for i in last_issues if i.severity == "error"]
        if not error_issues:
            logger.info("drafter_pass2_ok", attempt=attempt + 1, warnings=len(last_issues))
            return DraftResult(
                workflow=wf, issues=last_issues,
                attempts=attempt + 1, provider=cfg.provider, model=cfg.model,
            )

        logger.info(
            "drafter_pass2_errors",
            attempt=attempt + 1,
            errors=[i.code for i in error_issues],
        )
        if attempt == cfg.max_repair_attempts:
            return DraftResult(
                workflow=wf, issues=last_issues,
                attempts=attempt + 1, provider=cfg.provider, model=cfg.model,
            )
        messages.append({"role": "assistant", "content": wf.model_dump_json()})
        messages.append({"role": "user", "content": format_issues_for_repair(last_issues)})

    assert last_wf is not None
    return DraftResult(
        workflow=last_wf, issues=last_issues,
        attempts=cfg.max_repair_attempts + 1,
        provider=cfg.provider, model=cfg.model,
    )


_PASS2_CITATION_CLAUSE = """## Pass-2 citation requirement (overrides any ambiguity above)

You are receiving a pre-extracted list of FACTS, each with a `fact_id`
and a `paragraph_id`. Every non-structural node you emit MUST include
a `source_citation` whose `fact_id` AND `paragraph_id` match one of the
facts in the input list. Structural-only nodes (`flow/Start`,
`flow/End`, `flow/Loop`, `flow/IfElse`, `flow/Frame`) MAY omit
source_citation.

Do not fabricate new facts. If the facts list doesn't contain enough
information to specify a parameter (e.g. the paper said "a small
volume" and Pass 1 left volume_ul null), pick the closest-reasonable
value AND record the fact_id of the source fact so the operator can
audit.
"""


async def draft_workflow_from_paper(
    parsed: ParsedPaper,
    *,
    current_deck: dict[str, Any] | None = None,
    config: DrafterConfig | None = None,
) -> tuple[PaperFacts, DraftResult]:
    """End-to-end: parsed paper → facts (Pass 1) → drafted workflow (Pass 2).

    Returns both the facts list and the DraftResult so the API layer
    can expose them to the UI (the facts list is useful for debugging
    and for displaying "here's what the LLM saw" next to each drafted
    node).
    """
    cfg = config or _resolve_config()
    facts = await extract_facts(parsed, config=cfg)
    if not facts.facts:
        # No actionable facts → return an empty workflow with the
        # summary as the description. Don't spend a Pass 2 call on
        # zero input.
        from pybravo.workflow.drafter.schema import DraftedGraph, DraftedNode
        empty_wf = DraftedWorkflow(
            name="Draft (no actionable protocol found)",
            description=facts.summary or "No liquid-handling protocol detected in the paper.",
            deck={},
            graph=DraftedGraph(
                nodes=[
                    DraftedNode(id=1, type="flow/Start", pos=[0.0, 0.0], properties={}),
                    DraftedNode(id=2, type="flow/End", pos=[0.0, 0.0], properties={}),
                ],
                links=[],
            ),
        )
        return facts, DraftResult(
            workflow=empty_wf,
            issues=[],
            attempts=1,
            provider=cfg.provider,
            model=cfg.model,
        )
    result = await draft_workflow_from_facts(
        facts, current_deck=current_deck, config=cfg,
    )
    return facts, result
