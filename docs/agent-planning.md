# Agent planning on Marvin

## Product target

A scientist gives Bravo a liquid-handling objective, answers a few experimental
questions, and receives a checked protocol ready for a final review and Run
action in the same workspace. This is the target for familiar tasks covered by
reviewed local methods and known labware. New liquids, labware or techniques can
require additional characterization. Question reduction must come from stored
knowledge and current instrument evidence, rather than guessed experimental facts.

The wrapper belongs to the device and serves multiple kinds of agent. Qwen is
the optional assistant behind the built-in chat. External agents should be able
to use their own models and submit typed task chains directly. All clients must
share the same task service, device state, method selection, readiness checks,
execution ownership and run evidence.

The intended interfaces are an MCP endpoint for compatible agent clients, the
existing HTTP API for other integrations, and the built-in conversation. MCP
can be mounted inside the pyBravo web application; a separately deployed daemon
is an operational choice. The current stdio MCP adapter is an initial local
interface. A network MCP endpoint and unified preparation service are not yet
implemented.

The next service operation is preparation: resolve the request against saved
setups, applicable reviewed methods and protocol recipes; compose a typed plan;
validate it; rehearse it in isolated simulation; and repair issues supported by
known facts. Return a persisted preparation with a readiness state, proposed
deck/run sheet, provenance and only the remaining questions or blockers. Answers
resume the same preparation. Any relevant change to the plan or instrument
state invalidates the corresponding readiness evidence.

Preparation should use these distinctions:

- Known device facts and still-current observations are retrieved automatically.
- Routine choices with applicable reviewed defaults are proposed together for
  confirmation, including deck layout and methods.
- Missing experimental facts or consequential ambiguities become focused,
  deduplicated questions with their affected steps identified.
- A blocked or failed rehearsal returns a specific repair or explanation, rather
  than a generic list of every unset schema field.

Historical human protocols become reviewed cases with input conditions,
equipment, labware, tips, fluids, volume ranges, contamination policies, expected
task chains and available execution/performance evidence. They supply reusable
recipes and regression cases. A protocol document alone is not proof of measured
liquid delivery or carryover performance.

Acceptance for the stacking proof of concept is: start from the four-plate,
5 µL diluent plus 5 µL sample request; retrieve a saved compatible setup and
reviewed methods; collect the genuinely missing experimental answers in a few
turns; automatically produce validation and rehearsal evidence; and present one
final review with a Run action. Exercise this through both the built-in chat and
an independent MCP client, with the same resulting plan and readiness rules.
Readiness means a compiled plan, applicable methods, sufficient confirmed supply
and tips, checked geometry, and current deck confirmation. Multiple clients may
plan, but a single device execution owner controls motion. The current work
implements the conversation and planning/review foundation, not this complete
preparation-to-run acceptance scenario.

## Current implementation

Protocol Assistant at `/protocol-assistant` combines a conversation, visible
ordered task cards, experimental setup, validation and native physical rehearsal.
The **Stacking example** button prepares a request for separate 5 µL diluent
and sample additions to two 384-well destination plates. Edit the request before
sending it. The example is planning guidance; it supplies no physical inventory,
qualified liquid method or measured labware dimensions.

## Suggest, inspect, accept, test

The browser now runs an actual local tool-calling agent. It sends requests to
the configured Qwen Chat Completions server on Sparky; pyBravo hosts the allowed
tools and sends their results back to the model until it replies. A separate
MCP daemon is not needed for the browser. The stdio MCP entry point below is
for external clients.

The conversation can answer questions without proposing a new plan. Qwen can
inspect the current protocol, read machine capabilities, search labware, draft
a protocol, make targeted typed edits, validate, and rehearse in isolated
simulation. It has no approval, hardware dispatch, shell or arbitrary-code tool.
Every turn begins with a fresh protocol inspection, including calculated nominal
volume totals. Totals explicitly depend on the selected channels, or on a
visible full-head assumption when that choice remains open; they exclude dead
volume and method overages and do not establish sufficient source supply.
The sidebar streams real tool activity. Proposed steps appear in the main
workspace as edits arrive. **Stop** cancels the active turn; **New protocol**
starts a clean conversation while keeping the earlier session saved.

An editing turn produces a suggestion without changing the saved protocol. Inspect
**changed fields** and the suggested steps in the main workspace, then accept or reject it.
You can keep chatting to refine a pending suggestion or answer its questions;
intermediate versions do not need to be accepted. The next suggestion supersedes
the previous one against the same saved revision. Pending suggestions also
resume from the page URL after a reload.
Acceptance saves a new draft revision and invalidates previous validation,
simulation and approval. Rejection leaves the saved record unchanged. A
suggestion cannot overwrite concurrent edits or changes to instrument context.
Save direct form edits before requesting or accepting a suggestion.
Source, destination, volume, anchor and plate-move fields are also editable
directly on the task cards. The detailed editor remains available for other
properties. A question-only turn preserves existing validation and approval;
its user text is retained separately until a later scientific edit cites it.

The agent returns control to the scientist when validation needs missing
measurements or other experimental input. It does not keep trying to fill those
values until a tool budget is exhausted. If final reply generation fails after
a typed draft was prepared, the suggestion is retained with a visible warning.

If the sidebar says the server needs a restart, the page has loaded new HTML
while an older Python process is still serving its API. Restart the web server
and reload the page. `/api/protocols/agent-status` identifies the tool-agent
backend and configured model. Native tool calling must be enabled on the model
server; the deployed `qwen` endpoint was verified to return real `tool_calls`.

Requests such as **Use the same tips, I don't care about carryover** or
**Use fresh tips for every transfer** let Qwen propose a targeted tip-policy
edit without regenerating the task chain.
A reuse request remains visible with its original reason and still undergoes
inventory, capacity, method and simulation checks. It never combines additions
from different sources into one aspiration. Plate movements release tips under
the existing compiler policy; the next destination plate therefore needs a
fresh replacement set even when tips are reused between its liquid additions.

Use the existing setup editor for measured volumes, exact labware/tip identities,
head scope and liquid methods. Validate, run strict simulation, and inspect the
reported failures. A follow-up message receives the saved validation issues and
simulation report as repair context. Review and accept the correction, then
repeat the checks. Scientist approval and release to Designer use the existing
reviewed execution flow. The **Deck camera** view uses the configured vision
service; opening it does not start cameras or change instrument configuration.

## Discovery

`GET /api/protocols/agent-tasks` returns `pybravo.agent-tasks` version 1.0.0:

- Active model-selectable tasks, with parameters, availability, preconditions,
  effects and deterministic compiler targets from the Bravo Capability Manifest.
- Planning skill contents and content hashes, including stacked-plate processing.
- The `ProtocolPlan` JSON Schema and discovery/workflow links.
- An instrument context hash and a digest for the complete discovery document.

The task dictionary is derived from the existing manifest rather than another
independent operation list. Skills and recipes guide composition. The validator
and compiler enforce actual requirements. Discovery does not establish loaded
labware, liquid volumes, tip freshness or method qualification.

## External agents

Run the web server in the intended profile, then connect a stdio MCP client to:

```sh
.venv/bin/python -m pybravo.workflow.protocols.mcp_agent
```

`PYBRAVO_MCP_BASE_URL` selects the running server (default
`http://127.0.0.1:8000`). The existing `mcp_discovery` entry point remains read-only.
The planning entry point adds these tools to its discovery tools:

| Tool | Behavior |
| --- | --- |
| `bravo_get_task_catalog` | Read tasks, skills and plan schema |
| `bravo_inspect_runtime` | Read the existing software runtime state |
| `bravo_create_protocol` | Save source text; return source paragraph IDs |
| `bravo_get_protocol` | Inspect source, revision, plan, checks and run records |
| `bravo_propose_plan` | Stage an agent-composed typed task chain and return validation feedback |
| `bravo_suggest_changes` | Ask the configured local model to stage a suggestion |
| `bravo_accept_suggestion`, `bravo_reject_suggestion` | Resolve a suggestion without executing hardware |
| `bravo_validate_protocol` | Check the exact supplied revision |
| `bravo_simulate_protocol` | Start isolated native-task collision rehearsal; poll the protocol for completion |

An external planner can create a source session, cite its paragraph IDs in a
`ProtocolPlan`, stage that plan, inspect returned validation issues, and revise
it. Proposed plans must leave `decisions` empty; existing recorded scientist
decisions are preserved. Only an accepted plan becomes the session's current
draft. A returned proposal is never an execution grant. The MCP server exposes
neither scientist approval nor hardware dispatch. Structured HTTP validation
errors are retained as tool repair feedback.

HTTP endpoints use `/api/protocols`:

| Request | Result |
| --- | --- |
| `GET /agent-status` | Loaded agent backend and configured model connection |
| `POST /agent-chat` | `{message, session_id?, revision?, proposal_id?}` → newline-delimited tool activity, live draft preview, and a natural reply or staged suggestion; `proposal_id` refines a pending suggestion |
| `POST /chat-proposals` | `{message, session_id?, revision?}` → staged candidate and preview |
| `GET /chat-proposals/{proposal_id}` | Resume a pending suggestion, including across a page reload |
| `POST /{id}/plan-proposals` | `{revision, plan}` → staged candidate, validation and preview |
| `POST /chat-proposals/{proposal_id}/accept` | Save the exact server-held candidate if its base revision and context still match |
| `POST /chat-proposals/{proposal_id}/reject` | Mark rejected; leave the saved protocol unchanged |
| `POST /{id}/validate` or `/simulate` | Optional `{revision}` pins the requested check; MCP always supplies it |

Proposals persist in the protocol store's `proposals/` directory. Resolved
proposals cannot be applied again. Normal session history retains accepted
changes and the superseded review evidence.

## Current boundary and next milestones

This is the planning and review integration for the stacking proof of concept.
Runtime inspection exposes existing software state, not a new persistent,
observation-reconciled semantic twin. Hardware dispatch still happens explicitly
in Designer after review. Simulation checks modeled geometry and accounting;
it does not qualify carryover, liquid delivery or grasp reliability.

The next state-engine work is to persist task-level completion evidence and
uncertain outcomes across restarts, reconcile physical observations before
resuming an interrupted action, and bind each dispatched command block to the
checked runtime revision. Historical protocols can then become reviewed cases
with expected plans, operating conditions, traces and measured outcomes. That
corpus should retain separate labels for structural correctness, mechanical
rehearsal and measured liquid performance.
