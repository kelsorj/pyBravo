"""Agent discovery and narrow, explicit conversational setup edits.

Catalogs describe possibilities. They never assert physical inventory or turn
planning instructions into executable code.
"""

from __future__ import annotations

import re
from hashlib import sha256

from .agent_skills import _ROOT
from .capabilities import build_capability_manifest
from .models import ProtocolPlan, ScientistDecision
from .store import digest


def task_catalog(context: dict) -> dict:
    manifest = build_capability_manifest(context)
    skills = []
    for path in sorted(_ROOT.glob("*/SKILL.md")):
        content = path.read_text(encoding="utf-8")
        skills.append({"id": path.parent.name, "sha256": sha256(content.encode()).hexdigest(), "content": content})
    body = {
        "standard": "pybravo.agent-tasks", "schema_version": "1.0.0",
        "context_hash": manifest["context_hash"],
        "tasks": manifest["assistant_operations"], "skills": skills,
        "plan_schema": ProtocolPlan.model_json_schema(),
        "workflow": ["inspect", "propose", "accept_draft", "validate", "simulate", "review", "release"],
        "endpoints": {"propose": "/api/protocols/chat-proposals",
                      "capabilities": "/api/protocols/capabilities",
                      "recipes": "/api/protocols/recipes",
                      "runtime": "/api/state"},
        "runtime_evidence": "Runtime deck and liquid state are software records; physical confirmation is separate.",
        "execution": "Accepting a suggestion saves a draft. Existing scientist review and release checks govern hardware.",
    }
    return {**body, "digest": digest(body)}


def explicit_tip_edit(message: str, current_plan: dict | None, setup: dict) -> tuple[ProtocolPlan, dict] | None:
    """Handle a complete tip-policy-only message without regenerating the plan.

    Full matching avoids treating quoted examples, negation, or another requested
    operation as permission to reuse. General language still goes to the model.
    """
    if not current_plan:
        return None
    text = re.sub(r"\s+", " ", message.strip().casefold()).rstrip(".! ")
    reuse = re.fullmatch(
        r"(?:please )?(?:use|reuse) (?:the )?same tips?[,;. ]+i (?:do not|don't|don’t) care about carryover", text,
    )
    fresh = re.fullmatch(r"(?:please )?use fresh tips? (?:for|before) (?:each|every) transfer", text)
    if not reuse and not fresh:
        return None
    plan = ProtocolPlan.model_validate(current_plan)
    policy = "reuse_all" if reuse else "fresh_each_step"
    updated = {**setup, "tip_strategy": policy, "tip_reuse_reason": message if reuse else None}
    paths = {"/setup/tip_strategy", "/setup/tip_reuse_reason"}
    plan.decisions = [decision for decision in plan.decisions if decision.path not in paths]
    for field in ("tip_strategy", "tip_reuse_reason"):
        plan.decisions.append(ScientistDecision(path="/setup/" + field, value=updated[field], reason=message))
    plan.questions = [question for question in plan.questions if question.path not in paths]
    return plan, updated
