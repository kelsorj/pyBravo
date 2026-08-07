# Workflows

A workflow is a protocol expressed as a node graph plus a deck configuration,
stored as a single JSON file. This page covers what a workflow contains, how to
build one in the designer, and how to simulate and run it.

> **Running a workflow moves a physical robot.** Simulate every workflow before
> you execute it, and re-simulate after every edit. Read [safety](safety.md)
> before executing anything on hardware.

---

## What a workflow is

Two things, saved together:

1. **A deck configuration** — which labware sits at each of the nine deck
   locations.
2. **A node graph** — the operations, in order, with branching and loops.

Execution starts at the Start node and follows the flow links from node to
node. Each task node is dispatched to the corresponding instrument operation;
flow-control nodes decide which link to follow next.

### Stored shape

Workflows are stored as one JSON file per workflow under `~/.pybravo/workflows/`.
The filename is derived from the workflow's ID, and the directory is created on
first use. The top level looks like this:

```json
{
  "id": "9f1c2e7a-…",
  "name": "Serial dilution",
  "description": "Optional summary",
  "created": "2026-04-12T10:00:00+00:00",
  "modified": "2026-04-12T14:30:00+00:00",
  "deck": {
    "1": {
      "labware_id": "…",
      "name": "…",
      "kind": "…",
      "base_class": "…",
      "wells": 96,
      "is_lidded": false,
      "is_sealed": false,
      "tip_definition_id": "…"
    }
  },
  "graph": {
    "nodes": [],
    "links": [],
    "groups": []
  }
}
```

- `id` is assigned on creation if absent, and reassigned on import so that
  importing a file twice cannot overwrite an existing workflow.
- `created` is set once; `modified` is rewritten on every save.
- `deck` is keyed by deck location as a string. Every field except
  `labware_id` has a default, and `tip_definition_id` is only meaningful for
  tip boxes.
- `graph` is the serialized node canvas: `nodes` (each with `id`, `type`,
  `pos`, `properties`, `inputs`, `outputs`), `links` (each an array of
  `[link_id, origin_id, origin_slot, target_id, target_slot, type]`), and
  `groups` (visual grouping only).

The designer stores two further top-level fields alongside these: `library`,
the workflow-level Python compiled once per run and shared by every Script
node, and `drafter_session_id` when the workflow originated from the LLM
drafter.

### The API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workflows` | List saved workflows |
| `GET` | `/api/workflows/{id}` | Load one workflow |
| `POST` | `/api/workflows` | Create a workflow |
| `PUT` | `/api/workflows/{id}` | Update a workflow |
| `DELETE` | `/api/workflows/{id}` | Delete a workflow |
| `POST` | `/api/workflows/import-json` | Import from an uploaded JSON file |
| `GET` | `/api/workflows/{id}/export` | Download as JSON |
| `POST` | `/api/workflows/{id}/simulate` | Run with no hardware |
| `POST` | `/api/workflows/{id}/execute` | Run on the instrument |
| `POST` | `/api/workflows/stop` | Abort the running workflow |

Malformed files in the workflows directory are skipped when listing rather
than failing the whole request.

---

## Node types

Nodes are grouped in the designer's left panel in the same categories used
here.

### Flow control

| Node | Behaviour |
|---|---|
| Start | Entry point. A workflow without one cannot run. |
| End | Stops the walk on that branch. |
| If / Else | Evaluates a condition against its `data` input and follows the `true` or `false` output. |
| Loop | Runs its `body` output `count` times, then continues from `done`. |
| Group | A visual container for tidying the canvas. It has no flow ports and is never executed. |

If / Else conditions are deliberately simple comparisons against the value
arriving on the `data` port, not general Python: `== "value"`, `!= "value"`,
`contains "substring"`, `> number` and `< number`. An empty condition is true
when the incoming value is truthy, and a comparison that cannot be evaluated
falls back to the same truthiness test.

Loop bodies also drive per-iteration property values — see
[Per-iteration and variable properties](#per-iteration-and-variable-properties)
below.

### Liquid handling

| Node | Operation | Key properties |
|---|---|---|
| Aspirate | `aspirate` | location, volume, liquid class, pipette technique, pre/post-aspirate volume, distance from bottom, dynamic tip extension, tip touch, anchor |
| Dispense | `dispense` | location, volume, liquid class, pipette technique, blowout volume, empty tips, distance from bottom, dynamic tip retraction, tip touch, anchor |
| Mix | `mix` | location, volume, cycles, liquid class, pipette technique, distance from bottom, anchor |

The `anchor` property is a well label such as `A1` or `D4`. It selects which
well the active head subset lines up with, and is applied through the same
plate-selection mechanism the control panel uses. An out-of-range, unreachable
or illegal anchor stops the node and raises the operator prompt.

### Tips

| Node | Operation | Key properties |
|---|---|---|
| Tips On | `tips_on` | location, head mode, tip anchor row/column |
| Tips Off | `tips_off` | location, tip anchor row/column |

The Tips On node carries a full head-mode object (subset type, orientation, row
count, column count), applied before the pickup runs. Tips Off does **not**
carry its own head mode — the head cannot reconfigure with tips fitted, so the
designer resolves the mode from the upstream Tips On node.

Head modes and tip-box anchor legality are explained in the
[user guide](user-guide.md#head-modes-and-tip-selection).

### Plate handling

| Node | Operation | Key properties |
|---|---|---|
| Pick/Place | `pick_place` | pick location, place location |
| Stack | `stack_plates` | source location, base location |
| Destack | `destack_plate` | source location, destination location |
| Mount | `mount_plates` | source location, base location |
| Unmount | `unmount_plate` | source location, destination location |
| Delid | `delid_plate` | location |
| Relid | `relid_plate` | location |

Mount is physically the same motion as Stack, but it flags the resulting pair
so that a later Pick/Place transports both plates as a single unit — the
arrangement you want for a filter plate sitting on a collection plate. Unmount
separates the pair again. Stack and Destack always move exactly one plate.

### Sensors

| Node | Operation | Key properties |
|---|---|---|
| Read Barcode | `read_barcode` | location, `store_as` |
| Scan Stack Height | `scan_stack_height` | location, expected count, `store_as` |

Both publish their reading on a data output port, which can feed an If / Else
or a Script node. `store_as` additionally writes the value into the workflow
blackboard under that name.

Scan Stack Height treats a blank, zero or negative `expected_count` as "no
expectation — just report what was measured". Any positive integer turns on
validation, and a mismatch raises the operator prompt.

### System

| Node | Operation |
|---|---|
| Initialize | `initialize` |
| Home | `home` (an `axes` property, default `X,Y,Z,W,G,Zg`) |
| Dock Gripper | `dock_gripper` |

### Logic

**Script** runs a block of Python during the run. It has a flow input and
output plus a `data` input and a `result` output, and three properties:
`script` (the source), `timeout` (seconds; `0` means no timeout), and
`store_as` (a blackboard key to mirror the result into).

Inside a script the following names are available:

- `data` — the value arriving on the input port, or `None`.
- `vars` — the workflow blackboard, a live dict that can be read and mutated
  and that persists for the whole run.
- `plates` — a live accessor over the deck. `plates[6]` returns the labware at
  location 6, `plates.get(6)` returns `None` instead of raising, `6 in plates`
  tests occupancy, and iterating yields `(location, labware)` pairs. Because it
  reads the live deck, a plate moved by an earlier node is visible here.
- `result` — assign to it to publish a value downstream.
- `log(...)` — writes to the server log.
- `prompt_user(message, default="")` — opens a dialog and blocks until the
  operator answers. Set the node's `timeout` to `0` when you use it, or the
  default 30-second limit will kill the script while the operator is still
  typing.
- `math`, `json` and `re`.

Builtins are restricted to a small allow-list. That list is a guard against
accidents, not a security boundary — a Script node runs code on the machine
hosting the server, so treat any workflow you did not write yourself as you
would any other untrusted script.

The designer's Script editor offers ready-made starters from the snippet
registry (`GET /api/script_snippets`), covering pass-through, writing to the
blackboard, classifying a barcode, operator prompts and barcode fallback. The
`Ask Operator` chip in the task palette is simply a Script node pre-filled with
the operator-prompt snippet.

The toolbar's `Library…` editor holds workflow-level Python that is compiled
once at the start of a run; every top-level binding it defines is merged into
every Script node's namespace. It is the right place for helper functions
shared across several scripts. **A library that fails to compile aborts the run
before any motion.**

---

## Building a workflow

Open `/designer`. The layout is a toolbar, a tab strip for multiple open
workflows, a left panel, the node canvas, and a right panel.

**Add nodes.** Drag a chip from the *Tasks* section of the left panel onto the
canvas, or right-click the canvas to add a node from the menu.

**Wire them.** Drag from a node's flow output to the next node's flow input.
If / Else has `true` and `false` outputs; Loop has `body` and `done`. Sensor
and Script nodes have a second, data-typed port that carries a value rather
than control flow — connect it to the `data` input of an If / Else or Script
node downstream.

**Configure the deck.** The *Deck* section of the left panel is the same 3×3
grid as the control panel. Click a position to assign labware; the header
summarises how many of the nine are configured. The deck is stored with the
workflow, so a workflow carries its own layout independent of whatever is
currently assigned on the instrument.

**Set properties.** Select a node and edit its fields in the right panel. Node
faces show an inline summary — location, volume, loop count, head-mode
footprint, tip anchor — so a graph can be read without opening every node.

**Housekeeping.** `Arrange` auto-lays-out the graph. Ctrl+G groups the selected
nodes into a collapsible Group node. The right panel also has a *Variables*
section that shows the live blackboard during a run.

Workflows are saved with `Save` / `Save As…` and reopened with `Load`. Both
`Simulate` and `Execute` save the workflow first, so the file on disk always
matches what ran.

### Per-iteration and variable properties

Two prefixes make node properties dynamic at run time:

- `iter:a,b,c` — inside a Loop, picks an element by the current iteration
  index, cycling if the list is shorter than the loop count. Outside a loop the
  string is left alone.
- `var:NAME` — looks the value up in the blackboard, including dotted paths
  such as `var:plate.barcode`. A missing key yields `None`. The strict form
  `var:!NAME` raises an error instead, which surfaces through the operator
  prompt.

Both are resolved into a fresh copy of the properties, so the saved graph is
never mutated. Strings without a prefix pass through untouched, which is why a
comma-bearing value like the Home node's `X,Y,Z` axis list is safe. Script
node properties are deliberately *not* expanded — the script body is expected
to read `data`, `vars` and `plates` itself.

---

## Simulating versus executing

**Simulate first. Every time.**

`POST /api/workflows/{id}/simulate` runs the graph against a simulated
instrument. No hardware is touched. If a real profile is loaded, the simulated
instrument reuses it, so the calibrated teachpoints and the current head, tip
and selection state drive the 3D preview and the geometry matches what a real
run would do. This is where you find missing plates, wrong locations, illegal
head modes and broken scripts — at no cost.

`POST /api/workflows/{id}/execute` runs the same graph against the connected
instrument. **Every task node commands real motion.** Execution is refused
unless an instrument is connected and its controller type is a real one; if the
instrument has not been initialized, the endpoint initializes it first, which
itself causes motion.

`POST /api/workflows/stop` aborts whichever run is in progress. It is also
wired to the designer's stop control.

Both paths run the same checks before anything starts:

- The designer validates the graph client-side: a Start node and an End node
  must exist, and it walks the graph tracking plate movements so that a node
  which picks from a location no plate will have reached is flagged before the
  run. Offending nodes are outlined in red on the canvas.
- The server validates every `liquid_class` and `pipette_technique` reference
  against the current machine, head and tip context. Unknown names return HTTP
  400 with a per-node breakdown, and the designer highlights and zooms to the
  offending nodes. This runs in simulate mode too, so stale references are
  caught during design rather than at the instrument.

During a run the server broadcasts progress over the state WebSocket: node
start, per-step progress, node completion, branch decisions taken, live axis
positions, blackboard updates, and the final complete-or-error event. The
designer uses these to highlight the running node, drive the progress bar and
scrubber, animate the 3D viewport, and populate the Variables panel. The
instrument's status light follows the run — steady while running, blinking on
error, and back to idle on clean completion.

The playback controls in the right panel (play, pause, stop, step, speed) apply
to simulation preview.

---

## Error handling during a run

Three interruptions can pause a run and wait for a person.

**Script errors.** When a Script node raises or times out, the run pauses and
the designer shows the error with three choices, resolved by
`POST /api/script_action`:

- `retry` — run the same script again.
- `edit_retry` — send corrected source in `new_source` and run that instead.
  The edit is written back into the graph, so it survives if the workflow is
  saved after the run.
- `abort` — stop the run.

The node stays paused until one of these arrives, so a transient failure can be
fixed and resumed without restarting the protocol.

**Operator prompts from a script.** `prompt_user()` blocks the script and opens
a dialog. The answer comes back via `POST /api/user_prompt_response` with the
`request_id`, the typed `value`, and a `cancelled` flag. `OK` returns the typed
string, `Ignore` returns an empty string and lets the script continue, and
`Cancel` raises an error inside the script that falls through to the
retry/edit/abort prompt above. Remember to set the node's `timeout` to `0`.

**Task failures.** When an instrument task step fails, the designer shows the
same operator prompt as the control panel, resolved through `POST /api/retry`,
`POST /api/ignore` or `POST /api/abort`. Ignoring a failed step continues with
the instrument in a state the software may no longer be tracking accurately —
see [safety](safety.md).

An unhandled error ends the run with an error event and leaves the status light
signalling that attention is needed.

---

## Import and export

**JSON.** `GET /api/workflows/{id}/export` downloads a workflow as formatted
JSON, and `POST /api/workflows/import-json` uploads one. Imported workflows are
given a fresh ID, so importing the same file twice produces two workflows
rather than overwriting anything. The designer's `Export` button downloads the
active tab, and `Import` reads a JSON file straight into a new tab without
touching the server.

**Legacy XML protocol files.** `POST /api/workflow/import` accepts a legacy XML
protocol file and returns the parsed result. Treat the output as a starting
point: review every node and deck assignment against the original protocol,
then simulate before you go anywhere near hardware.

---

## The optional LLM drafter

The designer's `Draft…` button can produce a first-pass workflow from a
description or from a paper.

- `POST /api/workflow/draft` takes a natural-language `prompt` and, optionally,
  the deck configuration of the current tab so the model can reuse the exact
  labware IDs already assigned. The drafter modal exposes this as a
  "Send the current tab's deck config as context" checkbox.
- `POST /api/workflow/draft_from_pdf` takes a PDF of a scientific paper. The
  document is parsed into structured paragraphs, the Methods section is turned
  into a set of grounded facts each tagged with its source paragraph, and those
  facts are converted into a workflow in which every non-structural node
  carries a citation back to the paragraph it came from. When a paper contains
  more than one candidate protocol, a picker shows the candidates alongside a
  page preview so you can choose.

Drafted workflows open in a new tab so you can inspect them before doing
anything else. Warnings and validation errors from the drafter are shown in the
modal.

**Requirements.** The drafter is an optional extra. Install it with
`pip install -e '.[llm]'` and set either `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY` in the environment. PDF drafting additionally needs a document
parsing service configured through `PYBRAVO_DOCLING_URL`. Without the extra the
endpoints return HTTP 501; without credentials, 503. See
[configuration](configuration.md).

**What it produces is a draft, not a validated protocol.** The model does not
know your instrument, your labware, your teachpoints, or whether the volumes it
wrote down are physically achievable with the tips you have fitted. It can
misread a paper, invent a step, put labware in the wrong location, or produce a
graph that is syntactically fine and scientifically wrong. Nothing about a
generated workflow has been checked against reality.

Every generated workflow **must** be reviewed node by node and simulated before
it is executed. Check the deck assignment, the volumes, the liquid classes, the
head mode, and the order of operations against your own protocol, and correct
what is wrong. Treat the draft as a rough first pass from an assistant who has
never seen your bench.

---

## See also

- [User guide](user-guide.md) — the control panel, head modes, labware, liquid
  classes.
- [Safety](safety.md) — read before executing anything.
- [Quickstart](quickstart.md) — a first workflow, end to end.
- [Configuration](configuration.md) — profiles and environment variables.
- [API reference](api-reference.md) — the full endpoint and event catalogue.
- [Architecture](architecture.md) — how the executor and task engine fit
  together.
- [Troubleshooting](troubleshooting.md) and [FAQ](faq.md).
