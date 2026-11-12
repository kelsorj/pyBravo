"""Agent planning tools over the existing reviewed Protocol Assistant API.

Run ``python -m pybravo.workflow.protocols.mcp_agent`` beside the web server.
Tools can save drafts and request isolated simulation. Hardware execution and
scientist approval remain in the existing application flow.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import mcp_discovery as discovery
from .models import ProtocolPlan


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Arguments):
    pass


class Session(Arguments):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class Revision(Session):
    revision: int = Field(ge=1)


class Text(Arguments):
    text: str = Field(min_length=1, max_length=200000)
    name: str = Field(default="Agent protocol", min_length=1, max_length=300)


class Chat(Arguments):
    message: str = Field(min_length=1, max_length=200000)
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    revision: int | None = Field(default=None, ge=1)


class Plan(Revision):
    plan: ProtocolPlan


class Proposal(Arguments):
    proposal_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


# Name, schema, read-only, description. No arbitrary URL or HTTP-method tool.
_SPECS = [
    ("bravo_get_task_catalog", Empty, True, "Discover typed Bravo tasks, planning skills, and the plan JSON schema."),
    ("bravo_inspect_runtime", Empty, True, "Read software runtime state. Deck records and modeled liquid quantities are not independent physical observations."),
    ("bravo_create_protocol", Text, False, "Save source text as an unapproved protocol session; returns paragraph IDs for citing a typed plan."),
    ("bravo_get_protocol", Session, True, "Inspect a session's revision, source passages, plan, validation, simulation evidence and recorded runs."),
    ("bravo_propose_plan", Plan, False, "Stage a typed task chain against a saved revision; returns deterministic validation feedback. Leave decisions empty."),
    ("bravo_suggest_changes", Chat, False, "Ask the configured local model for a staged protocol suggestion without changing the saved draft."),
    ("bravo_accept_suggestion", Proposal, False, "Accept a staged suggestion as an unapproved draft. Rejects changed session revisions or instrument context."),
    ("bravo_reject_suggestion", Proposal, False, "Reject a staged suggestion; leaves the saved draft unchanged."),
    ("bravo_validate_protocol", Revision, False, "Validate the specified saved revision and persist repair diagnostics; no hardware motion."),
    ("bravo_simulate_protocol", Revision, False, "Start isolated native-task physical simulation for the specified revision. Poll bravo_get_protocol for terminal status and evidence."),
]


def dispatch(message: dict[str, Any], fetch: Callable = discovery._fetch) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "id" not in message:
        return discovery.dispatch(message, fetch)
    request_id = message["id"]
    if message.get("method") == "initialize":
        result = discovery.dispatch(message, fetch)
        if "result" not in result:
            return result
        result["result"]["serverInfo"] = {"name": "pybravo-agent-planning", "version": "0.1.0"}
        return result
    if message.get("method") == "tools/list":
        result = discovery.dispatch(message, fetch)
        if "result" not in result:
            return result
        tools = result["result"]["tools"]
        return discovery._result(request_id, {"tools": tools + [
            {"name": name, "description": description, "inputSchema": schema.model_json_schema(),
             "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                             "idempotentHint": read_only, "openWorldHint": False}}
            for name, schema, read_only, description in _SPECS
        ]})
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return discovery._error(request_id, -32602, "params must be an object")
    name = params.get("name")
    spec = next((item for item in _SPECS if item[0] == name), None)
    if message.get("method") != "tools/call" or spec is None:
        return discovery.dispatch(message, fetch)
    try:
        args = spec[1].model_validate(params.get("arguments", {})).model_dump(exclude_none=True)
    except ValidationError as exc:
        return discovery._error(request_id, -32602, str(exc))
    identity = args.pop("session_id", None)
    if identity is not None and not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
        return discovery._error(request_id, -32602, "Invalid session identifier")
    payload = None
    if name == "bravo_get_task_catalog":
        path = "/api/protocols/agent-tasks"
    elif name == "bravo_inspect_runtime":
        path = "/api/state"
    elif name == "bravo_create_protocol":
        path, payload = "/api/protocols/from-text", args
    elif name == "bravo_get_protocol":
        path = "/api/protocols/" + identity
    elif name == "bravo_propose_plan":
        path, payload = "/api/protocols/" + identity + "/plan-proposals", args
    elif name == "bravo_suggest_changes":
        if identity is not None:
            args["session_id"] = identity
        path, payload = "/api/protocols/chat-proposals", args
    elif name in {"bravo_accept_suggestion", "bravo_reject_suggestion"}:
        action = "accept" if name == "bravo_accept_suggestion" else "reject"
        path, payload = "/api/protocols/chat-proposals/" + args["proposal_id"] + "/" + action, {}
    else:
        action = "validate" if name == "bravo_validate_protocol" else "simulate"
        path, payload = "/api/protocols/" + identity + "/" + action, args
    try:
        data = fetch(path, payload)
    except HTTPError as exc:
        # Validation and simulation return structured repair diagnostics on 409.
        # Preserve those details instead of reducing them to an HTTP status.
        body = exc.read(discovery.MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        return discovery._result(request_id, {"isError": True, "content": [
            {"type": "text", "text": body or str(exc)},
        ]})
    except (OSError, ValueError) as exc:
        return discovery._result(request_id, {"isError": True, "content": [{"type": "text", "text": str(exc)}]})
    return discovery._result(request_id, {"content": [{"type": "text", "text": json.dumps(data, allow_nan=False)}]})


def main() -> None:
    for line in sys.stdin:
        try:
            response = dispatch(json.loads(line))
        except json.JSONDecodeError:
            response = discovery._error(None, -32700, "Parse error")
        if response is not None:
            sys.stdout.write(json.dumps(response, allow_nan=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
