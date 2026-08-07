# OpenBravo HTTP & WebSocket API Reference

OpenBravo exposes a FastAPI backend (`pybravo/web/server.py`) that drives Bravo liquid-handling
robots, manages deck and labware configuration, stores profiles, and runs the workflow designer.

- **Base URL:** `http://localhost:8000` (the server binds `0.0.0.0:8000` by default; see `run_server()`)
- **Interactive docs:** `http://localhost:8000/docs` (Swagger UI), `http://localhost:8000/redoc`,
  raw schema at `http://localhost:8000/openapi.json`
- **CORS:** all origins, methods, and headers are allowed.

## Content types

| Direction | Type | Used by |
|---|---|---|
| Request | `application/json` | Most `POST`/`PUT`/`PATCH` endpoints (Pydantic models) |
| Request | `multipart/form-data` | File uploads (labware assets, PDF and JSON imports) |
| Request | query string | A handful of endpoints take scalars as query parameters — noted per route |
| Response | `application/json` | Default for every `/api/*` route |
| Response | `text/html` | Page routes (`/designer`, `/labware-editor`, …) |
| Response | `image/png`, `image/jpeg` | Camera previews and rendered PDF pages |

## Errors

Four error shapes are produced, all JSON:

| Condition | Status | Body |
|---|---|---|
| `RuntimeError` (e.g. not connected, no address configured) | `400` | `{"error": "<message>"}` |
| `BravoError` (hardware/protocol failure) | `400` | `{"error": "<message>", "error_type": "<ErrorType name>"}` |
| `HTTPException` raised by a handler | as raised | `{"detail": <string or object>}` |
| Request body fails validation | `422` | `{"detail": [<pydantic errors>]}` |
| Any unhandled exception | `500` | `{"error": "Internal server error"}` |

Common `HTTPException` statuses in this codebase: `400` (bad argument), `404` (unknown id /
feature disabled), `409` (conflicting state — already connected, name already taken),
`500`/`501`/`502`/`503` (upstream service unavailable or not installed).

A few handlers return `{"status": "error", "message": …}` with HTTP `200` instead of raising —
notably `POST /api/execute_command` for an unrecognised command name.

---

## ⚠️ Endpoints that move physical hardware

This software drives a robot with a moving gantry, pipette head, and gripper. **Every endpoint in
the table below can cause physical motion.** Before calling any of them, confirm the deck is clear,
the enclosure is safe, and nobody's hands are inside the working envelope.

| Endpoint | What moves |
|---|---|
| `POST /api/initialize` | Full initialization sequence including homing |
| `POST /api/home` | All axes home, then travel to park positions |
| `POST /api/home_axis` | One axis homes |
| `POST /api/move` | One axis to an absolute position |
| `POST /api/jog` | One axis by a relative step |
| `POST /api/tip_force_jog` | Z axis under force control |
| `POST /api/move_to_location` | X/Y/Z travel to a taught deck location |
| `POST /api/move_safe_z` | Z retracts to the safe height |
| `POST /api/aspirate` | Travel + plunger motion |
| `POST /api/dispense` | Travel + plunger motion |
| `POST /api/tips_on` | Head presses into a tip box |
| `POST /api/tips_off` | Head ejects/returns tips |
| `POST /api/gripper/open` | Gripper jaws |
| `POST /api/gripper/close` | Gripper jaws |
| `POST /api/gripper/dock` | Gripper jaws + gripper Z |
| `POST /api/pick_place` | Full labware transfer between two locations |
| `POST /api/execute_command` | Dispatches to any of the above task APIs |
| `POST /api/workflows/{workflow_id}/execute` | Runs an entire workflow graph against live hardware |
| `POST /api/motor/enable`, `/disable`, `/enable_all`, `/disable_all` | No commanded motion, but disabling a servo can let a loaded axis drop under gravity |

**Destructive or disruptive endpoints** (no motion, but data loss or service interruption):
`POST /api/shutdown`, `DELETE /api/workflows/{id}`, `DELETE /labware/types/{id}`,
`DELETE /labware/classes/{id}`, `DELETE /liquid-classes/{id}`,
`DELETE /pipette-techniques/{id}`, `DELETE /tips/{id}`, `DELETE /api/deck/{location}/labware`,
`POST /api/profile/rename`, and `POST /api/profile/import_reg` /
`POST /api/profile/import_dat` when called with `overwrite: true`.

---

## Table of contents

- [Connection & Lifecycle](#connection--lifecycle)
- [Motion](#motion)
- [Teachpoints](#teachpoints)
- [Head & Tips](#head--tips)
- [State & Diagnostics](#state--diagnostics)
- [Vision](#vision)
- [Labware](#labware)
- [Liquid Classes & Pipette Techniques](#liquid-classes--pipette-techniques)
- [Deck](#deck)
- [Profiles](#profiles)
- [Device Discovery](#device-discovery)
- [Workflows](#workflows)
- [Workflow Drafting (LLM)](#workflow-drafting-llm)
- [HTML pages and static mounts](#html-pages-and-static-mounts)
- [WebSocket: `/ws/state`](#websocket-wsstate)

---

## Conventions

**Axis names** (`X`, `Y`, `Z`, `W`, `G`, `Zg`) are matched case-insensitively. `W` is the plunger,
`G` the gripper jaws, `Zg` the gripper vertical axis. An unknown name returns `400`.

**Speed levels** are `FAST`, `MED`, `SLOW`, `HOMING`, `SAFE` (case-insensitive). Omitting `speed`
uses `MED`. An unknown value returns `400`.

**Deck locations** are integers `1`–`9`.

**Head types** are `HeadType` enum names, e.g. `HT_96_D_200`, `HT_96_D_70`, `HT_384_D_70`,
`HT_96_F_50`, `HT_96_ASSAYMAP`, `HT_96_PINTOOL`, `HT_384_PINTOOL`, `HT_1536_PINTOOL`,
`HT_UNKNOWN`.

**Controller types** referenced across the API: `simulation`, `agile`, `agile_7612`, `agile_srt`,
`darwin_native`, `darwin_serial`.

**"Requires a connection"** below means the endpoint calls into the controller and will fail with
`400 {"error": "Not connected"}` (or an equivalent `RuntimeError`) if no transport is open.

---

## Connection & Lifecycle

### POST `/api/connect`

Opens the configured controller transport and persists the resulting connection settings back into
the active profile. Any omitted field falls back to the value already stored in the profile. This
endpoint does **not** run initialization — call `POST /api/initialize` for that.

Request body — `ConnectRequest`:

| Field | Type | Notes |
|---|---|---|
| `controller_type` | `string \| null` | Falls back to the profile value |
| `address` | `string \| null` | IP address; required for `agile`, `agile_7612`, `agile_srt`, `darwin_native` |
| `serial_port` | `string \| null` | Required for `darwin_serial` |

Response: `{"status": "connected", "controller": "<controller_type>"}`

Errors: `400` when no address/serial port is configured for the selected controller type, or when
the socket cannot be opened.

### POST `/api/disconnect`

Closes the active controller connection.

Response: `{"status": "disconnected"}`

### POST `/api/shutdown`

**Destructive.** Best-effort disconnect, then hard-exits the server process shortly after the
response is flushed. Intended for a UI "Quit" button; it bypasses the graceful shutdown path.

Response: `{"status": "shutting_down"}`

### POST `/api/initialize`

⚠️ **Moves hardware.** Runs the device initialization sequence using the active profile, connecting
first if no session is open. Initialization homes axes and drives the robot to a known state.

Request body: none.

Response: `{"status": "initialized", "controller": "<controller_type>"}`

Errors: `400` when no address/serial port is configured, or the connection fails.

### POST `/api/home`

⚠️ **Moves hardware.** Retracts to safe Z, docks the gripper when one is present, homes the machine
axes, and moves the homed axes to their park positions. Requires a connection.

Response: `{"status": "homed", "axes": ["X", "Y", ...]}`

### POST `/api/abort`

Signals the task engine to abort the currently running operation.

Response: `{"status": "aborted", "accepted": <bool>}`

### POST `/api/retry`

Retries the current failed state-machine step.

Response: `{"status": "retried", "accepted": <bool>}`

### POST `/api/ignore`

Ignores the current task error and continues to the next step.

Response: `{"status": "ignored", "accepted": <bool>}`

---

## Motion

Every endpoint in this section requires an active hardware connection and moves the robot.

### POST `/api/move`

⚠️ **Moves hardware.** Absolute single-axis move in engineering units.

Request body — `MoveRequest`:

| Field | Type | Default |
|---|---|---|
| `axis` | `string` | required |
| `position` | `float` | required |
| `velocity` | `float` | `0.0` (controller default) |
| `acceleration` | `float` | `0.0` (controller default) |

Response: `{"status": "moved", "axis": "<axis>", "position": <float>}`

### POST `/api/jog`

⚠️ **Moves hardware.** Relative single-axis jog, used for manual positioning and teaching.

Request body — `JogRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `axis` | `string` | required | |
| `step` | `float` | required | Magnitude; sign is taken from `direction` |
| `direction` | `int` | `1` | `>= 0` positive, negative otherwise |
| `speed` | `string \| null` | `MED` | Speed level name |
| `peak_current` | `float \| null` | `null` | When set, performs a force-limited jog |

Response: `{"status": "jogged", "axis": "<axis>", "step": <signed float>, "position": <float>}`

### POST `/api/tip_force_jog`

⚠️ **Moves hardware.** Experimental force-controlled jog with position-based settle detection, used
for diagnosing tip-pickup behaviour. Reads the current position, computes `current + step` as the
travel limit, and drives the axis under a current cap.

Request body — `JogRequest` (same shape as `/api/jog`; `peak_current` defaults to `0.10` here).

Response:

```json
{"status": "jogged", "axis": "Z", "step": -2.0, "position": 12.3,
 "start": 14.3, "max_position": 12.3, "peak_current": 0.1}
```

Errors: `400` when the active controller does not implement `tip_force_jog`.

### POST `/api/home_axis`

⚠️ **Moves hardware.** Homes a single axis.

Request body — `AxisRequest`: `{"axis": "<axis>"}`

Response: `{"status": "homed", "axis": "<axis>"}`

### POST `/api/motor/enable`

Enables servo power for one axis. Request body — `AxisRequest`. Response:
`{"status": "enabled", "axis": "<axis>"}`

⚠️ Enabling or disabling a servo changes whether a loaded axis is held in place.

### POST `/api/motor/disable`

Disables servo power for one axis so it can be repositioned by hand. Request body — `AxisRequest`.
Response: `{"status": "disabled", "axis": "<axis>"}`

⚠️ A disabled vertical axis can drop under gravity.

### POST `/api/motor/enable_all`

Enables servo power on every axis. No request body. Response: `{"status": "all_enabled"}`

### POST `/api/motor/disable_all`

Disables servo power on every axis. No request body. Response: `{"status": "all_disabled"}`

⚠️ Disables the vertical axes as well — support the head before calling.

### POST `/api/move_to_location`

⚠️ **Moves hardware.** Sends the head to a taught deck location: retract Z to safe height, move X/Y
to the taught coordinates, then lower Z to the taught position or to an approach height above it.
The location must already have a valid teachpoint.

Request body — `MoveToLocationRequest`:

| Field | Type | Default |
|---|---|---|
| `location` | `int` | required |
| `approach_height` | `float` | `0.0` |
| `only_move_z` | `bool` | `false` |
| `speed` | `string \| null` | `MED` |

Response: `{"status": "moved", "location": <int>, "approach_height": <float>, "only_move_z": <bool>, "speed": "<SPEED>"}`

### POST `/api/move_safe_z`

⚠️ **Moves hardware.** Retracts the liquid-handling Z axis to the profile's configured safe
position. Z only.

Request body — `SpeedRequest`: `{"speed": "<string|null>"}`

Response: `{"status": "moved_safe_z", "speed": "<SPEED>"}`

### POST `/api/aspirate`

⚠️ **Moves hardware.** Runs an aspirate task at a taught location: retract, travel, lower to the
working depth, aspirate, retract. Tips and head configuration must already be correct.

**Query parameters** (not a JSON body):

| Parameter | Type |
|---|---|
| `location` | `int` |
| `volume` | `float` (µL) |

Response: `{"status": "completed"}`

For the full parameter set (pre/post-aspirate, distance from bottom, liquid class, technique) use
`POST /api/execute_command` with `command: "aspirate"`.

### POST `/api/dispense`

⚠️ **Moves hardware.** Runs a dispense task at a taught location.

**Query parameters:** `location` (`int`), `volume` (`float`, µL).

Response: `{"status": "completed"}`

### POST `/api/tips_on`

⚠️ **Moves hardware.** Picks up disposable tips from a tip-box location, using the active head mode
and tip-selection anchor to decide which tips are acquired.

**Query parameter:** `location` (`int`).

Response: `{"status": "completed"}`

### POST `/api/tips_off`

⚠️ **Moves hardware.** Returns or discards the tips currently on the head at the given location.

**Query parameter:** `location` (`int`).

Response: `{"status": "completed"}`

### POST `/api/gripper/open`

⚠️ **Moves hardware.** Opens the gripper. No request body.

Response: `{"status": "opened"}`

### POST `/api/gripper/close`

⚠️ **Moves hardware.** Closes the gripper. No request body.

Response: `{"status": "closed"}`

### POST `/api/gripper/dock`

⚠️ **Moves hardware.** Opens the gripper and drives it into its docked/recessed position. No request
body.

Response: `{"status": "docked", "g_target": <float>, "zg_target": <float>, "forced_plate_sensor": <bool>}`
— or `{"status": "aborted", "message": "…"}` when the operator aborts the task.

### POST `/api/pick_place`

⚠️ **Moves hardware.** Full gripper-based labware transfer: retract, open and nest the gripper,
travel to the source, grip, lift to a carry height, travel to the destination, release, and
re-nest. Intermediate poses are solved from teachpoints plus labware handling geometry.

Request body — `PickPlaceRequest`:

| Field | Type | Default |
|---|---|---|
| `from_location` | `int` | required |
| `to_location` | `int` | required |
| `speed` | `string \| null` | `MED` |

Response: `{"status": "completed", "from": <int>, "to": <int>, "diagnostics": {...}}` — `diagnostics`
carries the solved pick, carry, and place positions.

### POST `/api/execute_command`

⚠️ **Moves hardware.** Generic dispatcher: runs one named high-level task through a single request
shape. `command` is lower-cased and spaces become underscores.

Supported `command` values: `aspirate`, `dispense`, `mix`, `tips_on`, `tips_off`, `stack_plates`,
`destack_plate`, `mount_plates`, `unmount_plate`, `delid_plate`, `relid_plate`,
`scan_stack_height`, `read_barcode`.

Request body — `ExecuteCommandRequest`:

| Field | Type | Default | Used by |
|---|---|---|---|
| `command` | `string` | required | all |
| `location` | `int` | `1` | aspirate, dispense, mix, tips_on, tips_off, scan_stack_height, read_barcode |
| `source_location` | `int \| null` | `null` | stack_plates, destack_plate, mount_plates, unmount_plate |
| `base_location` | `int \| null` | `null` | stack_plates, mount_plates |
| `destination_location` | `int \| null` | `null` | destack_plate, unmount_plate |
| `lid_location` | `int \| null` | `null` | relid_plate |
| `plate_location` | `int \| null` | `null` | delid_plate, relid_plate |
| `lid_destination` | `int \| null` | `null` | delid_plate |
| `manual_count` | `int \| null` | `null` | scan_stack_height |
| `volume` | `float` | `0.0` | aspirate, dispense, mix |
| `pre_aspirate` | `float` | `0.0` | aspirate, mix |
| `post_aspirate` | `float` | `0.0` | aspirate |
| `distance_from_bottom` | `float` | `2.0` | aspirate, dispense, mix |
| `aspirate_distance` | `float \| null` | `null` | mix (falls back to `distance_from_bottom`) |
| `dispense_distance` | `float \| null` | `null` | mix (falls back to `distance_from_bottom`) |
| `dispense_at_different_distance` | `bool` | `false` | mix |
| `blowout` | `float` | `0.0` | dispense, mix |
| `empty_tips` | `bool` | `false` | dispense |
| `mix_cycles` | `int` | `3` | mix |
| `dynamic_tip_extension` | `float` | `0.0` | aspirate, mix |
| `dynamic_tip_retraction` | `float` | `0.0` | dispense |
| `tip_touch` | `bool` | `false` | aspirate, dispense, mix |
| `liquid_class` | `string \| null` | `null` | aspirate, dispense, mix |
| `pipette_technique` | `string \| null` | `null` | aspirate, dispense, mix |

Responses:

- `aspirate`, `dispense`, `mix`, `tips_on`, `tips_off` → `{"status": "completed", "command": "<command>"}`
- `stack_plates`, `destack_plate`, `mount_plates`, `unmount_plate`, `delid_plate`, `relid_plate`,
  `scan_stack_height`, `read_barcode` → the task's own result object is returned verbatim; see the
  corresponding `Bravo` method for the exact fields.
- Unknown command → HTTP `200` with `{"status": "error", "message": "Unknown command: <name>"}`

---

## Teachpoints

Teachpoints hold the reference X/Y/Z coordinates for each deck location and are the basis for every
location-driven task.

### GET `/api/teachpoint/{location}`

Reads the taught coordinates for one deck location.

| Path parameter | Type |
|---|---|
| `location` | `int` |

Response: `{"location": <int>, "teachpoint": {"x": <float>, "y": <float>, "z": <float>}}`, or
`{"location": <int>, "teachpoint": null}` when the location has never been taught.

### POST `/api/teachpoint/{location}`

Writes explicit X/Y/Z coordinates into the teachpoint store.

Path parameter: `location` (`int`). Request body — `TeachpointSetRequest`:
`{"x": <float>, "y": <float>, "z": <float>}`

Response: `{"status": "taught", "location": <int>}`

### POST `/api/teachpoint/{location}/teach_current`

Captures the robot's **current** X/Y/Z position as the teachpoint for a location. Requires a
connection (it reads live axis positions) and writes the resolved teach-tip fields back into the
active profile, saving it to disk.

Path parameter: `location` (`int`). Request body — `TeachCurrentRequest` (optional):

| Field | Type | Notes |
|---|---|---|
| `tip_capacity` | `float \| null` | Resolves a tip definition by capacity |
| `tip_id` | `string \| null` | Takes precedence over `tip_capacity` |

If neither is supplied, the profile's `teach_tip_capacity` / `default_tip_capacity` is used.

Response:

```json
{
  "status": "taught",
  "location": 4,
  "teach_tip_id": "...",
  "teach_tip_capacity": 200.0,
  "teach_tip_height_mm": 51.0,
  "teach_tip": {"tip_id": "...", "capacity_ul": 200.0, "label": "...",
                "length_mm": 51.0, "source": "...", "model_3d": "..."},
  "teachpoint": {"x": 0.0, "y": 0.0, "z": 0.0}
}
```

---

## Head & Tips

### GET `/api/head_mode`

Returns the active head-mode configuration — which subset of the head's barrels is considered
active for tip and liquid-handling operations.

Response: `{"head_type": "<HeadType name>", "head_mode": {...}}`

The `head_mode` object contains `subset_type`, `subset_config`, `row_count`, `column_count`,
`num_channels`, and `display_text`.

### PUT `/api/head_mode`

Updates the active head-mode configuration. Values are normalised against the installed head's
geometry, so out-of-range or unsupported combinations fall back to sane defaults rather than
erroring.

Request body — `HeadModeRequest`:

| Field | Type | Accepted values |
|---|---|---|
| `subset_type` | `string \| null` | `all_barrels`, `row`, `column`, `rectangle`, `single_barrel`, `quadrant` (normalised to `rectangle`) |
| `subset_config` | `string \| null` | `front_left`, `front_right`, `back_left`, `back_right` |
| `row_count` | `int \| null` | Used by `row` and `rectangle` |
| `column_count` | `int \| null` | Used by `column` and `rectangle` |

Response: `{"status": "updated", "head_type": "<HeadType name>", "head_mode": {...}}`

### GET `/api/head_mode/suggest`

Suggests a head mode for the labware currently assigned to a deck location, based on the installed
head and the labware's well count.

| Query parameter | Type |
|---|---|
| `location` | `int` |

Response: `{"location": <int>, "wells": <int|null>, "head_type": "<name>", "head_mode": {...}}`

### GET `/api/tipbox/legal_anchors`

Computes the anchor positions at which the given head mode can legally engage a tip box of the
given dimensions — i.e. placements where inactive barrels will not collide with tips. Used by the
designer's Tips On / Tips Off pickers.

| Query parameter | Type | Default | Notes |
|---|---|---|---|
| `subset_type` | `string` | `all_barrels` | |
| `subset_config` | `string` | `back_left` | |
| `row_count` | `int \| null` | `null` | |
| `column_count` | `int \| null` | `null` | |
| `tipbox_rows` | `int` | `8` | Must be positive |
| `tipbox_cols` | `int` | `12` | Must be positive |
| `purpose` | `string` | `pickup` | `pickup` or `return` |
| `occupied_cells` | `string \| null` | `null` | Comma-separated `row:col` pairs, e.g. `0:0,0:1,1:2` |

When `occupied_cells` is omitted, occupancy is inferred from `purpose`: `pickup` assumes a full box,
`return` assumes an empty one. Malformed tokens in `occupied_cells` are silently dropped.

Response:

```json
{"head_mode": {...}, "tipbox_rows": 8, "tipbox_cols": 12, "purpose": "pickup",
 "occupied_cells_count": 96, "legal_anchors": [{"row": 0, "col": 0, ...}, ...]}
```

Errors: `400` for an invalid head mode, non-positive box dimensions, or an unknown `purpose`.

### GET `/api/tip_selection`

Returns the current tip-selection anchor and the selection recorded for the tips presently on the
head.

Response: `{"tip_selection": {...} | null, "tips_on_head_selection": {...} | null}`

Each selection object carries `location`, `row`, `col`, `row_count`, `column_count`,
`mirror_corner`, `head_anchor`, `anchor_row`, `anchor_col`.

### PUT `/api/tip_selection`

Sets the anchor well used for partial tip pickup and return. The selection is validated against the
head mode, tip-box geometry, and accessibility rules.

Request body — `TipSelectionRequest`: `{"location": <int>, "row": <int>, "col": <int>}`

Response: `{"status": "updated", "tip_selection": {...}}`

### GET `/api/plate_selection`

Returns the current plate anchor for a location, the legal anchors, and the well footprint the head
would cover.

| Query parameter | Type |
|---|---|
| `location` | `int` |

Response: `{"location": <int>, "selection": {...} | null, "legal_anchors": [...], "footprint": [{"row": <int>, "col": <int>}, ...], "rows": <int>, "cols": <int>}`

### PUT `/api/plate_selection`

Sets the selected plate anchor well for a location.

Request body — `PlateSelectionRequest`: `{"location": <int>, "row": <int>, "col": <int>}`

Response: `{"status": "updated", "plate_selection": {...}, "legal_anchors": [...], "footprint": [...]}`

### GET `/api/tips`

Lists tip definitions.

| Query parameter | Type | Notes |
|---|---|---|
| `head_type` | `string \| null` | When set, returns only tips compatible with that head, in the head's option format |

Response: `{"tips": [...]}`

### POST `/tips`

Creates a tip definition.

Request body — `TipDefinitionRequest` (all fields optional; `None` values are dropped before the
store sees them):

| Field | Type |
|---|---|
| `tip_id` | `string` |
| `label` | `string` |
| `capacity_ul` | `float` |
| `length_mm` | `float` |
| `source` | `string` |
| `model_3d` | `string` |
| `compatible_heads` | `string[]` |

Response: `{"tip": {...}}`. Errors: `400` on invalid input.

### PATCH `/tips/{tip_id}`

Updates a tip definition. Path parameter `tip_id` (`string`); body — `TipDefinitionRequest`.

Response: `{"tip": {...}}`. Errors: `404` unknown tip, `400` invalid input.

### DELETE `/tips/{tip_id}`

**Destructive.** Deletes a tip definition.

Response: `{"status": "deleted", "tip_id": "<id>"}`. Errors: `404` unknown tip.

### POST `/api/change_head`

Changes the configured head type and resets the active head mode to the default full-head
configuration (`all_barrels` / `back_left`). If the controller supports it, the new head type is
pushed down to the controller as well.

Request body — `ChangeHeadRequest`: `{"head_type": "<HeadType name>"}`

Response: `{"status": "head_changed", "head_type": "<name>", "head_type_display": "<label>"}`

Errors: `400` for an unknown head type name.

---

## State & Diagnostics

### GET `/api/state`

Returns the consolidated runtime state used by the UI. This is the same payload broadcast over
`/ws/state`. Position and I/O fields are populated only while connected; during active motion the
server serves a cached snapshot rather than issuing live reads.

Key fields:

| Field | Type | Description |
|---|---|---|
| `connected` | `bool` | Transport is open |
| `initialized` | `bool` | Initialization has completed |
| `positions` | `{axis: float}` | Current engineering-unit positions |
| `head_type` | `string` | Installed head type name |
| `machine_id` | `string` | Machine identifier from the profile |
| `active_tip_id` | `string` | Resolved active tip definition |
| `active_tip_capacity_ul` | `float` | Capacity of the active tip |
| `head_mode` | `object` | Active head-mode configuration |
| `tips_on_head_mode` | `object \| null` | Head mode captured when tips were picked up |
| `tip_selection` | `object \| null` | Current tip anchor |
| `tips_on_head_selection` | `object \| null` | Anchor of the tips currently mounted |
| `plate_selection` | `{location: object}` | Per-location plate anchors |
| `controller_type` | `string` | Active controller type |
| `deck` | `{location: string[]}` | Labware names stacked at each location |
| `deck_details` | `{location: object[]}` | Full labware metadata plus `is_mounted` |
| `engine_busy` | `bool` | Task engine is running something |
| `motors_enabled` | `{axis: bool}` | Servo enable state per axis |
| `telemetry` | `object` | Controller-supplied telemetry |
| `head_attached` | `bool` | Head presence |
| `go_button_pressed` | `bool` | Front-panel button state |
| `plate_in_gripper` | `bool` | Resolved plate presence |
| `tips_on_head` | `bool` | Tips currently mounted |
| `tip_labware` | `string \| null` | Name of the tip-box labware in use |
| `tip_definition_id` | `string \| null` | Tip definition of the mounted tips |
| `attached_tip_length_mm` | `float \| null` | Length of the mounted tips |
| `tipbox_inventory` | `object` | Per-location tip occupancy |
| `robot_disabled` | `bool` | Safety/disable flag |
| `teachpoints` | `{location: {x, y, z}}` | Taught positions for locations 1–9 |
| `task_status` | `object` | Active task: `step`, `step_index`, `step_count`, `status`, optional `error` |

### GET `/api/positions`

Returns just the current axis positions.

Response: `{"X": <float>, "Y": <float>, "Z": <float>, "W": <float>, "G": <float>, "Zg": <float>}` —
axes that cannot be read are omitted.

### GET `/api/io_status`

Compact safety/I-O view derived from the full state.

Response: `{"robot_disabled": <bool>, "head_attached": <bool>, "head_type": "<string>", "go_button_pressed": <bool>, "plate_in_gripper": <bool>, "motors_enabled": {axis: bool}}`

### GET `/api/diagnostics`

Returns wire-level protocol diagnostics — command counts and the error log from the comm layer.
Only available on controllers that implement a diagnostics hook (currently `agile_7612`).

Response: the controller's diagnostics object, or
`{"error": "Diagnostics not available for this controller type"}` with HTTP `200` on other
controllers.

### GET `/api/accessories`

Lists configured accessories with their lazy-loaded driver runtime state.

Response: `{"devices": [{<device config fields>, "runtime": {"loaded": <bool>, "is_open": <bool>, "is_running": <bool>}}, ...]}`

### POST `/api/accessories/{accessory_id}/teleshake/start`

Starts a configured orbital shaker accessory. **Physically actuates the accessory** — make sure any
labware on the shaker is seated.

Path parameter: `accessory_id` (`string`). Request body — `TeleshakeActionRequest`:

| Field | Type | Notes |
|---|---|---|
| `rpm` | `int \| null` | Shake speed; driver default when omitted |
| `direction` | `string \| null` | Rotation direction; driver default when omitted |

Response: the driver's result object; see the handler. Errors: `400` when the accessory id is not
configured, `500` on driver failure.

### POST `/api/accessories/{accessory_id}/teleshake/stop`

Stops the shaker. Path parameter `accessory_id` (`string`); no body.

Response: the driver's result object. Errors: `400` unknown accessory, `500` driver failure.

---

## Vision

These endpoints proxy to a local vision service process (depth camera integration). **All of them
return `404` when `vision.enabled` is false in the active profile**, and `503` when the local
service is unreachable.

### GET `/api/vision/status`

Reports vision-service connectivity, camera SDK availability, and the saved calibration artifact.

Response: the service's `/status` payload.

### GET `/api/vision/calibration`

Returns the saved camera-to-deck calibration for the active machine.

Response: the service's `/calibration` payload.

### POST `/api/vision/calibration/run`

Starts the guided calibration workflow, or creates a calibration scaffold when hardware is not
ready. The active machine id is sent along automatically.

Request body — `VisionCalibrationRunRequest`: `{"notes": "<string|null>"}`

Response: the service's `/calibration/run` payload.

### POST `/api/vision/calibration/capture_baselines`

Captures a live depth reference for all 9 calibrated deck ROIs. Run with an **empty deck**, after
ROI calibration and before verification. No request body.

Response: the service's `/calibration/capture_baselines` payload.

### POST `/api/vision/verify`

Builds the expected deck scene from current deck assignments, teachpoints, head type, and tip-box
occupancy, sends it to the vision service, and returns a slot-by-slot comparison against what the
camera observes. No request body.

Response: the service's `/verify` payload — a report with per-slot expected labware, observed
status, and confidence.

### GET `/api/vision/preview`

Returns the latest camera preview image. Response body is raw bytes with the content type reported
by the vision service (typically `image/jpeg` or `image/png`).

### GET `/api/vision/preview/depth`

Returns the latest colorized depth preview image. Same response handling as `/api/vision/preview`.

### GET `/api/vision/detect`

*Excluded from the OpenAPI schema.* Fetches the service's live detection report and decorates each
slot with the labware OpenBravo expects there, adding `expected_labware` and a `display_label` to
every slot entry.

Response: the service's `/detect` payload with the added per-slot fields.

### GET `/api/vision/stream`

*Excluded from the OpenAPI schema.* Issues a `307` redirect to the vision service's MJPEG stream
URL.

### GET `/api/vision/stream/depth`

*Excluded from the OpenAPI schema.* `307` redirect to the depth stream URL.

### POST `/api/vision/calibration/roi/start`

Launches the interactive ROI calibration tool in a separate console window, against the reference
image recorded in the saved calibration. Windows-oriented — it shells out via `cmd /c start`.

| Query parameter | Type | Notes |
|---|---|---|
| `location` | `int \| null` | When set, must be 1–9; restricts calibration to one deck slot |

Response: `{"status": "starting", "reference_image_path": "<path>", "location": <int|null>, "command": [<argv>]}`

Errors: `400` when no reference image exists yet or `location` is out of range, `404` when the
recorded reference image is missing on disk, `503` when the service is unreachable.

### POST `/api/vision/service/start`

Starts the local vision service in a separate process using the repository launcher script, passing
the profile's vision settings through the environment. Returns immediately so the caller can poll
for readiness. No request body.

Response: `{"status": "already_running", "service": {...}}` when the service already answers, else
`{"status": "starting", "command": "<script path>", "python": "<interpreter>", "service_url": "<url>", "sdk_root": "<path>"}`

Errors: `400` on non-Windows hosts, `404` when the launcher script is missing.

---

## Labware

### GET `/api/labware`

Lists the runtime labware catalog, normalised and summarised.

Response: `{"labware": [<definition summary>, ...]}`

### GET `/labware/types`

Lists editable labware type definitions from the labware editor store.

Response: `{"labware_types": [...]}`

### POST `/labware/types`

Creates a labware type and refreshes the runtime catalog.

Request body — `LabwareTypeRequest` (all fields optional; `None` values are dropped):

| Field | Type |
|---|---|
| `kind` | `string` |
| `name` | `string` |
| `vendor` | `string` |
| `catalog_number` | `string` |
| `description` | `string` |
| `base_class` | `string` |
| `wells` | `int` |
| `plate_dimensions_mm` | `object` |
| `plate_properties` | `object` |
| `well_dimensions_mm` | `object` |
| `pf400` | `object` |
| `planar_motor` | `object` |
| `labware_class_ids` | `string[]` |
| `tip_definition_id` | `string` |
| `supported_tip_ids` | `string[]` |

Response: `{"labware_type": {...}}`. Errors: `400` on invalid input.

### PATCH `/labware/types/{labware_type_id}`

Updates a labware type, refreshes the runtime catalog, and refreshes any live instances of that
type currently on the deck.

Path parameter: `labware_type_id` (`string`). Body — `LabwareTypeRequest`.

Response: `{"labware_type": {...}}`. Errors: `404` unknown type, `400` invalid input.

### DELETE `/labware/types/{labware_type_id}`

**Destructive.** Deletes a labware type and refreshes the runtime catalog.

Response: `{"status": "deleted", "labware_type_id": "<id>"}`. Errors: `404` unknown type.

### POST `/labware/types/{labware_type_id}/assets/image`

Uploads a 2D image asset for a labware type.

Path parameter: `labware_type_id` (`string`). Body: `multipart/form-data` with a `file` field.

Response: `{"labware_type": {...}}` (the updated definition). Errors: `404` unknown type, `400` when
`file` is missing or invalid.

### POST `/labware/types/{labware_type_id}/assets/model`

Uploads a 3D model asset for a labware type and refreshes the runtime catalog.

Path parameter: `labware_type_id` (`string`). Body: `multipart/form-data` with a `file` field.

Response: `{"labware_type": {...}}`. Errors: `404` unknown type, `400` invalid upload.

### GET `/labware/classes`

Lists editable labware classes, used to group compatible labware entries.

Response: `{"labware_classes": [...]}`

### POST `/labware/classes`

Creates a labware class.

Request body — `LabwareClassRequest`: `{"name": "<string|null>", "description": "<string|null>"}`

Response: `{"labware_class": {...}}`. Errors: `400` on invalid input.

### PATCH `/labware/classes/{labware_class_id}`

Updates a labware class name or description. Path parameter `labware_class_id` (`string`); body —
`LabwareClassRequest`.

Response: `{"labware_class": {...}}`. Errors: `404` unknown class, `400` invalid input.

### DELETE `/labware/classes/{labware_class_id}`

**Destructive.** Deletes a labware class and removes its membership references from labware entries.

Response: `{"status": "deleted", "labware_class_id": "<id>"}`. Errors: `404` unknown class.

---

## Liquid Classes & Pipette Techniques

### GET `/api/liquid_context`

Returns the active liquid-handling context — the machine, head, and tip combination used to resolve
liquid classes.

Response: `{"machine_id": "<string>", "head_type": "<string>", "tip_id": "<string>", "tip_capacity_ul": <float>}`

### GET `/api/liquid_classes`

Lists liquid classes. By default only classes that strictly match the current (or explicitly
requested) machine/head/tip context are returned, so the designer cannot offer a class that would
fail at execution.

| Query parameter | Type | Default | Notes |
|---|---|---|---|
| `machine_id` | `string \| null` | active context | |
| `head_type` | `string \| null` | active context | |
| `tip_id` | `string \| null` | see below | |
| `tip_capacity_ul` | `float \| null` | see below | |
| `all` | `bool` | `false` | `true` returns the entire catalog, unfiltered |

Tip narrowing follows the physical head state: with tips on, results are restricted to the loaded
tip; with tips off, every class for the machine and head is listed. An explicit `tip_id` or
`tip_capacity_ul` overrides that regardless of head state. `machine_id` and `head_type` filters are
never dropped. If a tip-capacity-filtered query returns nothing, the query is retried once without
the capacity constraint.

Response:

```json
{"context": {"machine_id": "...", "head_type": "...", "tip_id": "...",
             "tip_capacity_ul": 200.0, "tips_on": false},
 "liquid_classes": [...]}
```

With `all=true` the `context` block is the unmodified active context (no `tips_on` field).

### POST `/liquid-classes`

Creates a liquid class.

Request body — `LiquidClassRequest` (all fields optional; `None` values are dropped):

| Field | Type |
|---|---|
| `name` | `string` |
| `description` | `string` |
| `machine_id` | `string` |
| `head_type` | `string` |
| `tip_id` | `string` |
| `tip_capacity_ul` | `float` |
| `aspirate` | `object` |
| `dispense` | `object` |
| `equation` | `object` |

Response: `{"liquid_class": {...}}`. Errors: `400` on invalid input.

### PATCH `/liquid-classes/{liquid_class_id}`

Updates a liquid class. Path parameter `liquid_class_id` (`string`); body — `LiquidClassRequest`.

Response: `{"liquid_class": {...}}`. Errors: `404` unknown class, `400` invalid input.

### DELETE `/liquid-classes/{liquid_class_id}`

**Destructive.** Deletes a liquid class.

Response: `{"status": "deleted", "liquid_class_id": "<id>"}`. Errors: `404` unknown class.

### GET `/api/pipette_techniques`

Lists saved pipette techniques — reusable motion patterns layered onto aspirate and dispense.

Response: `{"pipette_techniques": [...]}`

### POST `/pipette-techniques`

Creates a pipette technique.

Request body — `PipetteTechniqueRequest` (all fields optional):

| Field | Type |
|---|---|
| `name` | `string` |
| `description` | `string` |
| `motion_type` | `string` |
| `radius_mm` | `float` |
| `segments` | `int` |
| `clockwise` | `bool` |
| `apply_on_aspirate` | `bool` |
| `apply_on_dispense` | `bool` |
| `z_phase` | `string` |

Response: `{"pipette_technique": {...}}`. Errors: `400` on invalid input.

### PATCH `/pipette-techniques/{technique_id}`

Updates a pipette technique. Path parameter `technique_id` (`string`); body —
`PipetteTechniqueRequest`.

Response: `{"pipette_technique": {...}}`. Errors: `404` unknown technique, `400` invalid input.

### DELETE `/pipette-techniques/{technique_id}`

**Destructive.** Deletes a pipette technique.

Response: `{"status": "deleted", "technique_id": "<id>"}`. Errors: `404` unknown technique.

---

## Deck

Deck assignment tells the software what is physically present at each location. Motion planning,
visualization, and compatibility checks all depend on it being accurate.

### PUT `/api/deck/{location}/labware`

Assigns labware to a deck location.

Path parameter: `location` (`int`). Request body — `DeckLabwareRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `labware_id` | `string` | required | Catalog id of the labware |
| `is_lidded` | `bool` | `false` | |
| `is_sealed` | `bool` | `false` | |
| `tip_definition_id` | `string \| null` | `null` | Tip boxes only |
| `tipbox_fill_state` | `string \| null` | `null` | Tip boxes only |

Response: `{"status": "assigned", "location": <int>, "labware": {<metadata>}}`

Errors: `400` for an unknown labware id or an invalid assignment.

### DELETE `/api/deck/{location}/labware`

**Destructive** to the software model (nothing moves): clears the labware assignment at a location.

Path parameter: `location` (`int`).

Response: `{"status": "cleared", "location": <int>}`. Errors: `400` on an invalid location.

---

## Profiles

Profiles are YAML files in the profile directory (`PYBRAVO_PROFILE_DIR`, default `./profiles`). The
active profile name is persisted in a `.active_profile` marker so it survives restarts.

### GET `/api/profiles`

Lists available profile names and the currently active one.

Response: `{"profiles": ["<name>", ...], "current": "<name>" | null}` — empty list when no profile
directory is configured.

### GET `/api/profile`

Returns the active profile.

Response:

```json
{
  "name": "...",
  "connection": {"controller_type": "...", "use_ethernet": true, "address": "...",
                 "serial_port": "...", "machine_id": "..."},
  "head": {"head_type": "...", "check_on_init": true, "default_tip_id": "...",
           "teach_tip_id": "...", "default_tip_capacity": 200.0,
           "teach_tip_capacity": 200.0, "teach_tip_length_mm": 51.0,
           "teach_tip_options": [...]},
  "safety": {"approach_height": 0.0, "z_safe_position": 0.0,
             "always_move_to_safe_z": true, "run_medium_speed": false,
             "prompt_home_w": false, "ignore_plate_sensor": false,
             "enable_tips_off_tip_touch": false, "is_srt": false},
  "vision": {"enabled": false, "service_url": "...", "sdk_root": "..."},
  "accessories": {...}
}
```

### PATCH `/api/profile`

Updates the active profile in place and saves it to disk. Only the fields present in the body are
applied. Changing `head_type` also resets `default_tip_id` and `teach_tip_id` to the new head's
defaults; changing accessories reinitialises the accessory drivers.

Request body — `ProfileUpdateRequest` (every field optional):

| Field | Type | Maps to |
|---|---|---|
| `approach_height` | `float` | `safety.approach_height` |
| `z_safe_position` | `float` | `safety.z_safe_position` |
| `always_safe_z` | `bool` | `safety.always_move_to_safe_z` |
| `run_medium_speed` | `bool` | `safety.run_medium_speed` |
| `prompt_home_w` | `bool` | `safety.prompt_home_w` |
| `ignore_plate_sensor` | `bool` | `safety.ignore_plate_sensor` |
| `enable_tips_off_tip_touch` | `bool` | `safety.enable_tips_off_tip_touch` |
| `is_srt` | `bool` | `safety.is_srt` |
| `controller_type` | `string` | `connection.controller_type` |
| `use_ethernet` | `bool` | `connection.use_ethernet` |
| `serial_port` | `string` | `connection.serial_port` |
| `address` | `string` | `connection.address` |
| `machine_id` | `string` | `connection.machine_id` |
| `head_type` | `string` | `head.head_type` (unknown names are logged and ignored) |
| `check_on_init` | `bool` | `head.check_on_init` |
| `teach_tip_capacity` | `float` | `head.teach_tip_capacity` |
| `teach_tip_id` | `string` | `head.teach_tip_id` |
| `vision_enabled` | `bool` | `vision.enabled` |
| `vision_service_url` | `string` | `vision.service_url` |
| `vision_sdk_root` | `string` | `vision.sdk_root` |
| `accessories` | `object` | Replaces the whole accessories config |
| `barcode_reader_enabled` | `bool` | `accessories.barcode_reader.enabled` |
| `barcode_reader_device_type` | `string` | `accessories.barcode_reader.device_type` |
| `barcode_reader_port` | `string` | `accessories.barcode_reader.port` |
| `barcode_reader_side` | `string` | `accessories.barcode_reader.side` |
| `barcode_reader_location` | `int` | `accessories.barcode_reader.location` |

Response: `{"status": "updated", "saved": <bool>}` — `saved` is `false` if the write to disk failed
or no profile path is configured.

### POST `/api/profile/load`

Loads a different profile from disk, replacing the in-memory profile, teachpoints, accessories, and
resetting head mode to `all_barrels` / `back_left`. Updates the `.active_profile` marker.

Request body — `ProfileLoadRequest`: `{"name": "<profile name>"}`

Response: `{"status": "loaded", "name": "<name>"}`

Errors: `409` if the robot is still connected (disconnect first), `400` if the profile directory is
unavailable, `404` if the named profile does not exist.

### POST `/api/profile/duplicate`

Copies a profile YAML to a new name. Does not change the active profile.

Request body — `ProfileDuplicateRequest`:

| Field | Type | Notes |
|---|---|---|
| `new_name` | `string` | Required; must not contain path separators, `.`, or `..` |
| `source` | `string \| null` | Defaults to the currently active profile |

Response: `{"status": "duplicated", "source": "<name>", "name": "<new name>"}`

Errors: `400` invalid/empty name or no profile directory, `404` source not found, `409` destination
already exists.

### POST `/api/profile/rename`

**Destructive.** Renames a profile YAML on disk. If the renamed profile is the active one, the
in-memory path and the `.active_profile` marker are updated.

Request body — `ProfileRenameRequest`: `{"old_name": "<string>", "new_name": "<string>"}`

Response: `{"status": "renamed", "old_name": "...", "new_name": "...", "was_active": <bool>}`

Errors: `400` invalid names or identical names, `404` source not found, `409` destination exists or
the active profile is still connected.

### POST `/api/profile/import_reg`

Imports a Windows registry profile export (`.reg` text) and either previews the parsed result or
saves it as a new profile. The response includes warnings for fields that need manual review —
notably the registry's head-type and tip-id enumerations, which are not mapped automatically.

Request body — `ProfileImportRegRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `content` | `string` | required | Raw registry-export text, already decoded by the client |
| `save_as` | `string \| null` | `null` | Omit to preview only |
| `overwrite` | `bool` | `false` | Allow replacing an existing profile |

Response:

```json
{"parsed_name": "...", "warnings": ["..."], "axes": ["X", "Y", ...],
 "teachpoint_locations": [1, 2, ...], "status": "previewed"}
```

When `save_as` is supplied, `status` is `"saved"` and a `name` field is added.

Errors: `400` unparsable payload / invalid name / no profile directory, `409` name exists and
`overwrite` is false, `500` write failure.

### POST `/api/profile/import_dat`

Imports a legacy `.dat` profile directory export (the pre-registry format). The top-level folder
name becomes the profile name; each subdirectory's `<folder>/<folder>.dat` file holds that sub-key's
key/value lines. The payload is converted to the registry document form and run through the same
importer, so mapping and warnings behave identically to `/api/profile/import_reg`.

Request body — `ProfileImportDatRequest`:

| Field | Type | Notes |
|---|---|---|
| `profile_name` | `string` | Top-level folder name |
| `files` | `ProfileImportDatFile[]` | Each `{"relative_path": "96LT/Axes/X/X.dat", "content": "<text>"}`, forward-slash separated |
| `save_as` | `string \| null` | Omit to preview only |
| `overwrite` | `bool` | Default `false` |

Response and errors: same shape as `/api/profile/import_reg`; additionally `400` when `files` is
empty.

---

## Device Discovery

### POST `/api/discover_devices`

Scans the local network for Bravo devices. Three probes run concurrently and their results are
merged by IP: a UDP broadcast discovery handshake, a TCP subnet sweep (Darwin port first, then the
Agile port with a protocol ping), and a directed probe of the address already stored in the profile.
Subnets larger than `/22` are skipped. In simulation mode a single clearly-labelled virtual device
is returned instead.

Request body — `DiscoverDevicesRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `adapter` | `string` | `"All interfaces"` | An adapter IP to scan from, or the sentinel to scan all |
| `controller_type` | `string \| null` | `null` | Falls back to the profile's controller type |

Response:

```json
{"devices": [{"device_id": "...", "device_type": "...", "ip_address": "192.168.1.50",
              "mac_address": "...", "status": "Matched" | "Found",
              "controller_type": "agile_7612"}],
 "adapters": [{"name": "...", "ip": "..."}]}
```

Devices whose IP matches the profile's configured address are marked `Matched` and sorted first.

### POST `/api/select_device`

Stores a discovered device's address (and optionally controller type) into the active profile and
saves it to disk. Setting an IP also sets `use_ethernet: true`.

Request body — `SelectDeviceRequest`:

| Field | Type | Default |
|---|---|---|
| `device_id` | `string` | `""` |
| `ip_address` | `string` | `""` |
| `controller_type` | `string \| null` | `null` |

Response: `{"status": "selected", "device_id": "...", "ip_address": "...", "controller_type": "..."}`

---

## Workflows

Workflows are node graphs persisted as JSON files. See `pybravo/workflow/models.py` for the
canonical shapes:

- **`WorkflowDefinition`** — `id`, `name`, `description`, `created`, `modified`,
  `deck: {location: WorkflowDeckEntry}`, `graph: object`
- **`WorkflowDeckEntry`** — `labware_id`, `name`, `kind`, `base_class`, `wells`, `is_lidded`,
  `is_sealed`, `tip_definition_id`
- **`WorkflowNodeConfig`** — `id`, `type`, `pos`, `properties`, `inputs`, `outputs`
- **`WorkflowExecutionRequest`** — `mode` (`simulate` \| `execute`), `speed`
- **`ExecutionMode`** — `simulate`, `execute`

Note that the CRUD endpoints below accept and return raw JSON objects rather than validating
against `WorkflowDefinition`; the models describe the shape the designer produces.

### GET `/api/workflows`

Lists summary metadata for every saved workflow.

Response: `{"workflows": [{"id": "...", "name": "...", "description": "...", "created": "...", "modified": "..."}, ...]}`

### GET `/api/workflows/{workflow_id}`

Loads one workflow by id.

Response: the full stored workflow object. Errors: `404` when not found.

### POST `/api/workflows`

Creates a workflow. The body is an arbitrary JSON object (a `WorkflowDefinition`-shaped payload);
an `id` and `created`/`modified` timestamps are assigned if absent.

Response: the stored workflow object, including its assigned `id`.

### PUT `/api/workflows/{workflow_id}`

Updates an existing workflow. The body is merged over the stored object; `id` is preserved and
`modified` is refreshed.

Response: the updated workflow object. Errors: `404` when not found.

### DELETE `/api/workflows/{workflow_id}`

**Destructive.** Deletes the workflow file.

Response: `{"status": "deleted"}`. Errors: `404` when not found.

### POST `/api/workflows/import-json`

Imports a workflow from an uploaded JSON file. A fresh `id` is assigned to avoid collisions.

Body: `multipart/form-data` with a `file` field.

Response: the stored workflow object. Errors: `400` on malformed JSON or a non-object payload.

### GET `/api/workflows/{workflow_id}/export`

Exports a workflow as a downloadable JSON file.

Response: `application/json` with `Content-Disposition: attachment; filename="<workflow_id>.json"`.
Errors: `404` when not found.

### POST `/api/workflows/{workflow_id}/simulate`

Runs a workflow in simulation mode against a fresh simulation-mode Bravo that reuses the active
profile, so calibrated teachpoints drive the 3D viewport. A snapshot of the live runtime selection
state is applied so subset geometry matches the main UI. **No hardware motion.**

Execution is asynchronous: the endpoint returns as soon as the run starts, and progress arrives as
`workflow:*` events on `/ws/state`.

Response: `{"status": "started", "workflow_id": "<id>", "mode": "simulate"}`

Errors: `404` unknown workflow; `400` with a structured detail when pre-flight validation finds
unresolvable liquid-class or pipette-technique references:

```json
{"detail": {"message": "Workflow has 2 invalid reference(s) …",
            "invalid_nodes": [{"node_id": 4, "node_type": "liquid/Aspirate",
                               "node_title": "...", "field": "liquid_class",
                               "value": "...", "reason": "..."}]}}
```

### POST `/api/workflows/{workflow_id}/execute`

⚠️ **Moves hardware — runs an entire workflow graph on the physical robot.** Every task node
dispatches against the live connection. If the controller has not been initialized yet, the server
initializes it first (which homes the machine). Same asynchronous model and `workflow:*` event
stream as `/simulate`.

Response: `{"status": "started", "workflow_id": "<id>", "mode": "execute"}`

Errors: `404` unknown workflow; `409` when no Bravo exists, the Bravo is not connected, the profile
is configured for the `simulation` controller, or initialization fails; `400` with the same
validation-detail shape as `/simulate`.

### POST `/api/workflows/stop`

Aborts the running simulation or execution. No request body.

⚠️ Aborting mid-execution leaves the robot wherever the current step stopped — inspect the deck
before resuming.

Response: `{"status": "stopped"}`, or `{"status": "no_workflow_running"}` when nothing is active.

### POST `/api/script_action`

Resolves a paused script-error prompt raised by a Script node.

Request body — `ScriptActionRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `action` | `string` | required | `retry`, `edit_retry`, or `abort` |
| `new_source` | `string` | `""` | Replacement script text for `edit_retry` |

Response: `{"accepted": <bool>, "action": "<action>"}`, or
`{"accepted": false, "reason": "no_workflow_running"}`.

⚠️ Choosing `retry` or `edit_retry` resumes execution, which resumes motion on a live run.

### GET `/api/script_snippets`

Returns the script-snippet registry backing the designer's "Insert snippet" menu.

Response: `{"snippets": [...]}`

### POST `/api/user_prompt_response`

Resolves a paused `prompt_user()` call inside a Script node, unblocking the sandboxed script thread.

Request body — `UserPromptResponse`:

| Field | Type | Default |
|---|---|---|
| `request_id` | `string` | required |
| `value` | `string` | `""` |
| `cancelled` | `bool` | `false` |

Response: `{"accepted": <bool>, "request_id": "<id>"}`, or
`{"accepted": false, "reason": "no_workflow_running"}`.

⚠️ Answering a prompt resumes execution, which resumes motion on a live run.

### POST `/api/workflow/import`

Imports a legacy XML protocol file (`.pro`) and returns the parsed workflow. The upload is written
to a temporary file, parsed, and the temp file is removed. Nothing is persisted — the caller decides
whether to save the result via `POST /api/workflows`.

Body: `multipart/form-data` with a `file` field.

Response: `{"workflow": {...}}`. Errors: `400` when the file cannot be parsed.

---

## Workflow Drafting (LLM)

These endpoints generate draft workflows from natural language or from scientific-paper PDFs. They
depend on optional services: an LLM provider (credentials via environment/`.env`) and a
document-parsing service configured through `PYBRAVO_DOCLING_URL`.

Shared error mapping:

| Status | Meaning |
|---|---|
| `501` | A required dependency or configuration is missing on this server |
| `502` | An upstream call (LLM or document parser) failed |
| `503` | No LLM credentials are available |
| `500` | Parsing failed for another reason |

None of these endpoints move hardware.

### POST `/api/workflow/draft`

Generates a draft workflow from a natural-language prompt, optionally grounded in the deck currently
loaded in the designer so the model reuses exact `labware_id` values. The draft is recorded with a
`session_id` for later diffing against the user's edits.

Request body — `WorkflowDraftRequest`:

| Field | Type | Notes |
|---|---|---|
| `prompt` | `string` | Required, must be non-empty |
| `deck` | `object \| null` | The designer tab's current deck configuration |

Response:

```json
{"workflow": {...}, "warnings": ["..."], "errors": ["..."],
 "meta": {"provider": "...", "model": "...", "attempts": 1},
 "session_id": "..."}
```

Errors: `400` empty prompt, plus the shared 501/502/503 mapping.

### POST `/api/workflow/parse_pdf`

Parses a PDF through the configured document-parsing service and returns its structured content. It
does **not** draft a workflow — use it to inspect what a later drafting pass will see.

Body: `multipart/form-data` with a `file` field (must end in `.pdf` and be non-empty).

Response:

```json
{"source_name": "paper.pdf", "page_count": 8,
 "markdown_preview": "<first 2000 chars>", "markdown_length": 45123,
 "paragraph_count": 312, "sections_detected": {"methods": 47, ...},
 "methods_paragraphs": [{"id": "p-134", "text": "...", "page": 4, "kind": "paragraph"}]}
```

Errors: `400` non-PDF or empty upload, plus the shared mapping.

### POST `/api/workflow/draft_from_pdf`

End-to-end draft from a paper: parse the PDF, extract grounded facts from the methods text, convert
those facts into a workflow where each non-structural node carries a citation back to its source
paragraph, then validate citation coverage and graph sanity. Parsed papers are content-addressed and
cached, so repeat uploads skip the parsing step.

Body: `multipart/form-data` with a `file` field.

| Query parameter | Type | Default | Notes |
|---|---|---|---|
| `include_deck` | `bool` | `true` | Accepted but currently unused — this endpoint runs without live deck context |

Response: the `/api/workflow/draft` payload, plus:

| Field | Type | Description |
|---|---|---|
| `facts` | `object[]` | Extracted grounded facts, each tagged with its source paragraph |
| `summary` | `string` | Model-written summary of the extracted protocol |
| `paragraph_excerpts` | `{paragraph_id: {text, page, section}}` | Excerpts (≤500 chars) for every cited paragraph |
| `source_file` | `string` | Original filename |
| `page_count` | `int` | |
| `session_id` | `string` | For later `.../patch` calls |
| `pdf_hash` | `string` | SHA-256 of the uploaded bytes |
| `paper_history` | `object` | Prior uploads of this same PDF |

Errors: `400` non-PDF or empty upload, plus the shared mapping.

### POST `/api/workflow/segment_paper`

First pass of the picker flow: parse a PDF and return candidate protocols found within it, sorted by
confidence, so the operator can choose which one to draft.

Body: `multipart/form-data` with a `file` field, plus a `refine_with_llm` form field
(`bool`, default `true`).

Response:

```json
{"pdf_hash": "sha256…", "source_file": "paper.pdf", "page_count": 16,
 "paper_history": {...}, "candidates": [{...}], "autoselect_idx": null, "notes": ""}
```

`autoselect_idx` is a non-null index when one candidate is confident enough to skip the picker.

Errors: `400` non-PDF or empty upload, `500` when segmentation fails, plus the shared mapping.

### POST `/api/workflow/draft_from_analyzed`

Drafts a workflow from a user-selected subset of a previously segmented paper's paragraphs. Requires
a prior `POST /api/workflow/segment_paper` call so the parsed paper is cached.

Request body — `DraftFromAnalyzedRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `pdf_hash` | `string` | required | From `segment_paper` |
| `selected_paragraph_ids` | `string[]` | required | Paragraphs to draft from |
| `selected_candidate_title` | `string` | `""` | Bookkeeping only |
| `candidates_presented` | `object[] \| null` | `null` | Picker telemetry |
| `deck` | `object \| null` | `null` | Current deck context |
| `time_on_picker_s` | `float \| null` | `null` | Picker telemetry |

Response: the same shape as `/api/workflow/draft_from_pdf` (minus `paper_history`), including
`facts`, `summary`, `paragraph_excerpts`, `source_file`, `page_count`, `pdf_hash`, and a new
`session_id`.

Errors: `404` when the PDF has not been analyzed yet, plus the shared mapping.

### POST `/api/workflow/draft/{session_id}/patch`

Records a user-edited workflow against the frozen as-drafted snapshot, computing a structural diff.
Called by the designer on save, save-as, simulate, and execute of a drafted tab. Primarily a
side-effecting call.

Path parameter: `session_id` (`string`). Request body — `DraftPatchRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `workflow` | `object` | required | The current workflow JSON |
| `trigger` | `string` | `"save"` | `save`, `save_as`, `execute`, or `simulate` |
| `workflow_id_saved_as` | `string \| null` | `null` | Set on save-as |

Response: `{"recorded": true, "diff_summary": {...}}`, or `{"recorded": false, "reason": "unknown_session"}`
(HTTP `200`) when the session id is not recognised — for example on a workflow that predates the
drafter.

### GET `/api/drafter/status`

Diagnostic: whether the drafter's persistence layer is configured and reachable.

Response:

```json
{"mongo_configured": true, "mongo_reachable": true, "mongo_db": "...",
 "pdf_cache_dir": "...", "pdf_count": 12, "local_store_dir": "...",
 "parsed_papers_in_memory": 3, "node_property_defaults": {...}}
```

### GET `/api/drafter/debug`

Per-collection counts, sampled last rows, and index lists. Large fields (markdown, raw document,
drafted/final workflows) are replaced with `<type len=N>` placeholders so the response stays small.

Returns an object; see `pybravo/workflow/drafter/store.py::debug_snapshot`.

### GET `/api/drafter/dashboard`

JSON aggregates backing the `/drafter-dashboard` page.

| Query parameter | Type | Default | Notes |
|---|---|---|---|
| `days` | `int` | `30` | Clamped to 1–365; controls the drafts-per-day window only |

Returns an object; see `pybravo/workflow/drafter/store.py::dashboard_aggregates`.

### GET `/api/drafter/paper/{pdf_hash}/page/{page_no}.png`

Renders one page of a cached PDF as a PNG. Rendered pages are cached next to the PDF, so only the
first request pays the render cost.

| Parameter | In | Type | Default |
|---|---|---|---|
| `pdf_hash` | path | `string` (SHA-256) | — |
| `page_no` | path | `int` (1-indexed) | — |
| `scale` | query | `float` | `1.5` |

Response: `image/png`. Errors: `404` when the PDF is not cached or the page is out of range.

### GET `/api/drafter/paper/{pdf_hash}/paragraphs`

Returns the full paragraph list for a previously analyzed PDF, so the picker can show body text and
page numbers without re-parsing.

Path parameter: `pdf_hash` (`string`).

Response: `{"pdf_hash": "...", "source_file": "...", "page_count": <int>, "paragraphs": [...]}`

Errors: `404` when the PDF is not cached.

---

## HTML pages and static mounts

These routes serve the browser UI. All are `GET`, return `text/html`, and are excluded from the
OpenAPI schema (`include_in_schema=False`).

| Path | Serves |
|---|---|
| `/labware-editor` | Labware editor — the React dashboard source is inlined into a standalone HTML page |
| `/liquid-class-editor` | `frontend/liquid_class_editor.html` |
| `/tip-editor` | `frontend/tip_editor.html` |
| `/workflow` | `frontend/workflow_editor.html` |
| `/designer` | `frontend/designer.html`, served with `no-store` cache headers |
| `/drafter-dashboard` | `frontend/drafter_dashboard.html` |
| `/vision-calibration` | `frontend/vision_calibration.html` |
| `/` | `index.html` from the configured static directory — registered only when `run_server(static_dir=…)` is given |

Each file-backed page returns `404` when the source file is missing.

### Static mounts

Mounted at startup inside `run_server()`:

| Mount | Directory | Condition |
|---|---|---|
| `/labware-assets` | The labware editor's asset directory | Always |
| `/model` | `pybravo/model` | Only with `static_dir`, and only if the directory exists |
| `/labware` | `<repo>/labware` | Only with `static_dir`, and only if the directory exists |
| `/static` | The configured static directory (HTML mode enabled) | Only with `static_dir` |

The `/labware` mount is registered after the `/labware/types` and `/labware/classes` API routes, so
those routes continue to take precedence.

---

## WebSocket: `/ws/state`

`ws://localhost:8000/ws/state`

On connect, the socket is accepted and registered with the server's connection manager. The server
then pushes state frames in a loop; the client is not required to send anything.

### State frames

While the robot is connected, each tick sends the **exact same JSON object returned by
`GET /api/state`** — see [that section](#get-apistate) for the full field list. When the robot is
not connected, the loop still runs but sends nothing, so an idle disconnected client sees no
traffic.

### Poll and throttle rates

| Controller type | Interval | Approximate rate |
|---|---|---|
| `darwin`, `darwin_native`, `agile`, `agile_7612` | `0.2 s` | 5 Hz |
| Everything else (including `simulation`) | `1/30 s` | 30 Hz |

Real controllers are throttled to 5 Hz because each state frame costs real protocol reads.

### Workflow event frames

The same socket is used as a broadcast channel by the workflow executor. While a workflow is
simulating or executing, additional JSON messages are pushed to every connected client. These are
distinguished from state frames by a `type` field beginning with `workflow:`. Observed event types
include:

`workflow:start`, `workflow:complete`, `workflow:error`, `workflow:node_start`,
`workflow:node_step`, `workflow:node_complete`, `workflow:branch`, `workflow:positions`,
`workflow:runtime_state`, `workflow:vars_update`, `workflow:script_result`,
`workflow:script_error`, `workflow:user_prompt`, `workflow:task_warning`,
`workflow:task_aborted`, `workflow:barcode_read`, `workflow:plate_pick`.

Clients should branch on the presence of `type`: messages with a `workflow:` type are execution
events, and messages without one are full state snapshots.

Broadcast is best-effort — a send failure on one client is swallowed and does not affect the others.

---

## Route inventory

127 HTTP routes and 1 WebSocket endpoint are declared in `pybravo/web/server.py`:

| Method | Count |
|---|---|
| `GET` | 44 |
| `POST` | 65 |
| `PUT` | 5 |
| `PATCH` | 6 |
| `DELETE` | 7 |
| `WEBSOCKET` | 1 |
| **Total** | **128** |

The `GET /` route is included in the `GET` count but is only registered when the server is started
with a static directory.
