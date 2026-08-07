# Workflow Editor Design Spec

## Context

PyBravo can connect to the Bravo liquid handler and perform individual tasks (aspirate, dispense, tips on/off, pick/place, etc.) via the existing web UI. Users need a way to chain these tasks into complex, multi-step workflows with conditional branching, simulate them in 3D before committing to real hardware, and then execute them — all from a single interface.

## Overview

A new page (`/workflow`) on the existing FastAPI server providing a node-based workflow editor with an embedded 3D simulation viewport. Users build workflows by dragging task nodes onto a canvas, connecting them, configuring parameters, and previewing execution in the 3D digital twin before running on real hardware.

## Layout

Three-panel layout with a floating 3D viewport:

```
┌──────────┬─────────────────────────────┬──────────────┐
│ Left     │  Center                     │  Right       │
│ Panel    │  Node Canvas                │  Properties  │
│          │  (Litegraph.js)             │  Panel       │
│ ┌──────┐ │                             │              │
│ │Tasks ▼│ │   [Start]                  │  Node config │
│ │chips  │ │      │                     │  fields      │
│ └──────┘ │   [Tips On (1)]             │              │
│ ┌──────┐ │      │                     │──────────────│
│ │Deck  ▼│ │   [Aspirate (2)]          │  Simulation  │
│ │3x3   │ │      │                     │  Playback    │
│ │grid   │ │   [Dispense (3)]  ┌──────┐│  Controls    │
│ └──────┘ │      │             │ 3D   ││              │
│          │   [End]            │ View ││              │
│          │                    └──────┘│              │
└──────────┴─────────────────────────────┴──────────────┘
```

### Left Panel — Collapsible Sections

Both sections have a chevron toggle to collapse/expand.

**Tasks section** — Draggable chips grouped by category:
- Liquid Handling: Aspirate, Dispense, Mix
- Tips: Tips On, Tips Off
- Plate Handling: Pick/Place, Stack, Destack, Delid, Relid
- Sensors: Read Barcode, Scan Stack Height
- Flow Control: If/Else, Loop
- System: Initialize, Home, Dock Gripper

**Deck section** (collapsible) — 3x3 grid matching physical deck layout (positions 1-9). Click a position to assign labware from the definitions catalog. Assigned positions show labware name with color coding. Empty positions show dashed borders. When collapsed, shows a summary line: "Deck (6/9 configured)".

### Center — Node Canvas

Built on **Litegraph.js** (dependency-free, ~80KB, HTML5 Canvas).

Features:
- Pan, zoom, grid snapping
- Right-click context menu to add nodes
- Delete key to remove nodes
- Undo/redo (Ctrl+Z / Ctrl+Shift+Z)
- Minimap in corner

Node rendering:
- Typed input/output ports: "flow" ports (execution order) + data ports (barcode strings, heights)
- Inline summary on node face (e.g., "Aspirate — Loc 3, 50μL")
- Border color matches deck position color
- Currently executing node highlighted during simulation/execution
- Special shapes: Start (green), End (red), If/Else (two outputs labeled true/false), Loop (body + done outputs)

### Right Panel — Properties + Playback

**Properties** (top) — Dynamic form fields based on selected node type:
- Location: dropdown limited to deck positions with compatible labware
- Volume: numeric input with μL unit
- Liquid class: dropdown from profile definitions
- Booleans: toggle switches (tip touch, dynamic tip extension, etc.)
- Wells: grid picker or range input (e.g., A1:H12)
- Condition expressions: text input for If/Else nodes

**Simulation playback** (bottom):
- Play / Pause / Stop / Step-forward buttons
- Speed slider: instant (snap) through 1x, 2x, 4x, 8x, 16x
- Progress indicator: "Step 3 of 12 — Aspirating..."
- Progress bar

### Floating 3D Viewport

A draggable, resizable panel over the canvas containing the Three.js robot scene.

- Title bar with minimize / maximize / fullscreen controls
- Same URDF robot model, joint mapping, and motion interpolation as main page
- Labware rendered on deck according to workflow's deck configuration
- During playback: robot animates through each task's motion steps
- Independent of main page's deck state (uses workflow's own deck config)

## Node Types

| Node | Parameters | Input Ports | Output Ports |
|------|-----------|-------------|--------------|
| Start | — | — | flow |
| End | — | flow | — |
| Initialize | — | flow | flow |
| Home | axes (multi-select) | flow | flow |
| Tips On | location, head_mode, subset_config | flow | flow |
| Tips Off | location | flow | flow |
| Aspirate | location, volume, liquid_class, tip_touch, quadrant, wells | flow | flow |
| Dispense | location, volume, liquid_class, tip_touch, blowout, quadrant, wells | flow | flow |
| Mix | location, volume, cycles, liquid_class | flow | flow |
| Pick/Place | pick_location, place_location | flow | flow |
| Stack | source_location, target_location | flow | flow |
| Destack | source_location, target_location | flow | flow |
| Delid | location | flow | flow |
| Relid | location | flow | flow |
| Read Barcode | location | flow | flow, data (barcode string) |
| Scan Stack Height | location | flow | flow, data (height mm) |
| Dock Gripper | — | flow | flow |
| If/Else | condition expression | flow, data | true flow, false flow |
| Loop | count or condition | flow | body flow, done flow |

### Head Mode (Tips On)

The Tips On node includes a head mode selector that determines which subset of tips are picked up:

- **Full Head** — all barrels pick up tips (default)
- **Row Mode** — select which row(s) to use (e.g., row 3)
- **Column Mode** — select which column(s) to use (e.g., column 5)
- **Rectangle Mode** — select a rectangular sub-region (rows x columns)
- **Single Tips** — select individual tip positions

In the properties panel, the head mode appears as a dropdown. When a mode other than "Full Head" is selected, a secondary selector appears:
- Row/Column mode: numeric picker for which row or column
- Rectangle mode: row count + column count inputs
- Single tips: grid picker showing all tip positions

The selected head mode is shown on the node summary (e.g., "Tips On — Loc 1, Row 3"). The head mode set by Tips On carries forward to subsequent Aspirate/Dispense/Mix nodes until changed by another Tips On or Tips Off.

### Quadrant Selection

When the head density differs from the target plate density, the user must select a quadrant to address the correct subset of wells:

- **384-head → 1536-well plate:** Choose quadrant A1, A2, B1, or B2 (each quadrant maps the 384 tips to every-other-well in a 2x2 sub-grid)
- **96-head → 384-well plate:** Same quadrant options (A1, A2, B1, B2)
- **Same density (e.g., 96-head → 96-well):** Quadrant selector is hidden/disabled — not applicable

In the properties panel, the quadrant field appears as a 2x2 button grid when the head/plate density mismatch is detected. The selected quadrant is shown on the node's inline summary (e.g., "Aspirate — Loc 3, 50μL, Q:A1"). The editor determines whether to show the quadrant selector by comparing the active head type (from profile) against the labware definition's well count at the selected deck location.

### If/Else Condition Expressions

Conditions are simple comparison expressions evaluated at runtime:

- `barcode == "ABC123"` — string equality from Read Barcode data port
- `barcode != ""` — non-empty check
- `height > 25.0` — numeric comparison from Scan Stack Height data port
- `barcode contains "CTRL"` — substring match

The data input port receives values from upstream data output ports (Read Barcode, Scan Stack Height). The condition expression references the incoming data by its source type name (`barcode`, `height`).

## Data Model

### Workflow JSON

```json
{
  "id": "uuid-string",
  "name": "Serial Dilution",
  "description": "Optional description",
  "created": "2026-04-12T10:00:00Z",
  "modified": "2026-04-12T14:30:00Z",
  "deck": {
    "1": {
      "labware_id": "opentrons_96_tiprack",
      "is_lidded": false,
      "is_sealed": false,
      "tip_definition_id": "d200"
    },
    "2": {
      "labware_id": "corning_96_wellplate_360ul_flat",
      "is_lidded": false,
      "is_sealed": false
    },
    "7": {
      "labware_id": "agilent_1_trash"
    }
  },
  "graph": {
    "nodes": [],
    "links": [],
    "groups": []
  }
}
```

### Storage

- **Location:** `~/.pybravo/workflows/` (configurable via profile)
- **Format:** One JSON file per workflow, filename derived from workflow name
- **Auto-save:** Drafts saved to browser `localStorage` every 30 seconds
- **Import/Export:** Download/upload JSON files for sharing

## Backend

### New API Endpoints

All under the existing FastAPI server:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/workflows` | List saved workflows (name, id, modified date) |
| GET | `/api/workflows/{id}` | Load a workflow |
| POST | `/api/workflows` | Save new workflow |
| PUT | `/api/workflows/{id}` | Update existing workflow |
| DELETE | `/api/workflows/{id}` | Delete a workflow |
| POST | `/api/workflows/import` | Upload a JSON workflow file |
| GET | `/api/workflows/{id}/export` | Download workflow as JSON file |
| POST | `/api/workflows/{id}/simulate` | Start simulation run |
| POST | `/api/workflows/{id}/execute` | Start real hardware execution |
| POST | `/api/workflows/stop` | Abort current simulation or execution |

### Workflow Executor

New module: `pybravo/workflow/executor.py`

**Class: `WorkflowExecutor`**

Responsibilities:
1. **Graph walking** — starts at Start node, follows flow connections
2. **Node dispatch** — for each task node, creates the corresponding `StateMachineTask` with parameters from the node config, executes via `StateMachineEngine`
3. **Branching** — If/Else nodes evaluate a condition and follow the matching output port
4. **Loops** — Loop nodes re-enter their body subgraph N times or until a condition
5. **State broadcast** — sends current node ID + step progress over WebSocket
6. **Error handling** — abort/retry/ignore pattern surfaced to the workflow editor UI

**WebSocket events** (new types):

```
workflow:node_start   { node_id, task_name, step_count }
workflow:node_step    { node_id, step_index, step_name }
workflow:node_complete { node_id, status }
workflow:branch       { node_id, condition, result, next_node_id }
workflow:complete     { status, duration_s }
workflow:error        { node_id, step_name, error, actions: [abort, retry, ignore] }
```

### Execution Modes

- **Simulate** — uses `SimulationController`, 3D preview only, no hardware
- **Execute** — uses connected controller (Agile/Darwin), 3D shows real-time positions

Toggle in the toolbar. Execute mode requires an active instrument connection.

## Frontend Architecture

### New Files

```
frontend/
  workflow.html              — Main workflow editor page
  src/
    robot-scene.js           — Extracted reusable 3D scene module
    workflow.js              — Workflow editor logic (canvas, panels, WebSocket)
    nodes/                   — Litegraph node type definitions
      task-nodes.js          — All task node types
      flow-nodes.js          — If/Else, Loop, Start, End
```

### 3D Module Extraction

Extract from `main.js` into `robot-scene.js`:
- URDF loading and joint mapping
- Labware rendering (plates, tip boxes, stacks)
- Motion interpolation (exponential lerp)
- Camera setup and OrbitControls
- Tip visualization on head
- Deck slot anchor calculation

Both `main.js` and `workflow.js` import from `robot-scene.js`. The workflow editor creates its own scene instance with its own deck configuration.

### Litegraph.js Integration

- Load via ESM from CDN (consistent with existing Three.js pattern)
- Register custom node types for each task
- Custom rendering callback for node appearance (colors, inline summaries)
- Serialize/deserialize graph to/from workflow JSON
- Event hooks: node selected → update properties panel, node added → validate deck compatibility

## Verification Plan

1. **Page loads** — navigate to `/workflow`, verify three-panel layout renders
2. **Deck configuration** — click deck positions, assign labware, verify color coding propagates to node dropdowns
3. **Node creation** — drag task chips onto canvas, verify nodes appear with correct ports
4. **Node connections** — connect flow ports between nodes, verify edges render
5. **Properties editing** — select a node, change parameters, verify inline summary updates
6. **Save/Load** — save a workflow, reload page, load it back, verify graph + deck state restored
7. **Import/Export** — export to JSON file, re-import, verify identical
8. **3D viewport** — verify robot scene renders in floating panel, can pan/zoom/orbit
9. **Simulation playback** — run a simple workflow (tips on → aspirate → dispense → tips off), verify 3D robot animates through motions and active node highlights on canvas
10. **Speed control** — adjust speed slider during simulation, verify animation speed changes
11. **Branching** — create If/Else workflow with Read Barcode, simulate, verify correct branch taken
12. **Execution** — with instrument connected, execute a simple workflow, verify real robot moves match 3D visualization
13. **Collapsible panels** — collapse deck section, verify it minimizes to summary line and canvas gains space
