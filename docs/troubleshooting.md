# Troubleshooting

Common symptoms, what usually causes them, and how to fix them. Each entry
follows the same shape: what you see, why it happens, and what to do about it.

> **Some of the failures on this page involve a robot that is already moving,
> or about to.** Where a diagnostic step could cause a crash or an injury, it
> says so. Read [safety](safety.md) before working on a machine that is
> powered and connected.

**Jump to:** [Server startup](#the-server-will-not-start) ·
[The MongoDB warning](#the-server-logs-a-mongodb-connection-warning-on-startup) ·
[Connecting to the robot](#the-server-runs-but-will-not-connect-to-the-robot) ·
[Motion failures](#the-robot-connects-but-will-not-move) ·
[Wrong distances](#an-axis-moves-the-wrong-distance) ·
[Homing and crashes](#homing-fails-or-the-head-crashes-into-the-deck) ·
[Tips](#tips-will-not-pick-up) ·
[The UI](#the-ui-loads-but-positions-and-status-never-update) ·
[Labware](#the-labware-list-is-nearly-empty-or-has-no-tip-boxes) ·
[Tests](#tests-fail) ·
[Debug logging](#turning-on-debug-logging)

---

## How the API reports errors

Knowing the shape of an error response saves time on everything below.

| Cause | HTTP status | Body |
|---|---|---|
| A `RuntimeError` from the driver or task engine | `400` | `{"error": "<message>"}` |
| A `BravoError` (a firmware or protocol-level fault) | `400` | `{"error": "<message>", "error_type": "<ERROR_NAME>"}` |
| Anything unexpected | `500` | `{"error": "Internal server error"}` |

A `500` with that generic body means the real detail is only in the server log.
Go read the terminal the server is running in, or see
[turning on debug logging](#turning-on-debug-logging).

---

## The server will not start

### `SyntaxError`, `TypeError` about `X | None`, or the launcher refuses to run

**Cause.** Python older than 3.11. OpenBravo declares
`requires-python = ">=3.11"` and uses syntax that older interpreters cannot
parse.

**Fix.** Check what you actually have:

```bash
python3 --version
```

If it is below 3.11, use one of these:

- Install [uv](https://docs.astral.sh/uv/) and use `./scripts/start_pybravo.sh`.
  uv fetches a suitable interpreter for you.
- Create a virtualenv with a newer Python:
  `python3.12 -m venv .venv && .venv/bin/pip install -e .`
- Point the launcher at an interpreter you already have:
  `PYBRAVO_PYTHON=/path/to/python3.12 ./scripts/start_pybravo.sh`

The shell launcher tries `$PYBRAVO_PYTHON`, then `uv`, then `./.venv/bin/python`,
then `python3.13`/`python3.12`/`python3.11` on `PATH`, and prints installation
advice if none is usable. **The Windows `.bat` launcher does not do this
search** — it runs `%PYBRAVO_PYTHON%` or plain `python`, so on Windows you must
activate the right environment or set `PYBRAVO_PYTHON` yourself.

### `ModuleNotFoundError: No module named 'fastapi'` (or `pydantic`, `yaml`, `uvicorn`, …)

**Cause.** The interpreter that started is not the one with the dependencies
installed. This is almost always a virtualenv that is not active, or a
`PYBRAVO_PYTHON` pointing at the wrong interpreter.

**Fix.** Look at the line the launcher printed —
`Starting pybravo.web.server with: …` — and confirm it names the interpreter
you expect. Then either:

```bash
uv run --frozen python -B -m pybravo.web.server
```

or install into the environment you are actually using:

```bash
.venv/bin/pip install -e .
```

If a *feature* fails rather than startup — a missing `instructor`, `anthropic`,
or `openai` for the workflow drafter — you need the optional `llm` extra. See
[installation](installation.md#optional-extras).

### `Address already in use` / `[Errno 48]` / `[Errno 98]` / `WinError 10048`

**Cause.** Something already holds TCP port 8000. Usually it is a previous
OpenBravo server that did not shut down.

**Fix.** Find and stop it. On macOS or Linux:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

On Windows:

```bat
netstat -ano | findstr :8000
```

If the previous server is still responsive, the clean way to stop it is the
**Quit** button in the UI, or:

```bash
curl -X POST http://localhost:8000/api/shutdown
```

The port is not configurable from the command line — `run_server()` in
`pybravo/web/server.py` defaults to `8000` and the module entry point does not
take arguments. Free the port rather than trying to move the server. (The
browser UI also hardcodes port 8000 for its state WebSocket, so moving the
server would break live updates anyway.)

### The server starts but nothing is served at `/`

**Cause.** The static UI is mounted only when the `frontend/` directory is
found next to the package. Running the module from an unpacked or partial copy
of the repository can miss it.

**Fix.** Start the server from the repository root, with `frontend/` present.
The API itself still works in this state — check `http://localhost:8000/docs`
to confirm the backend is alive.

---

## The server logs a MongoDB connection warning on startup

**Symptom.** On first run — and every run, if you never configure a database —
the log contains a warning like:

```
WARNING  pybravo.deck.labware: Falling back to local labware snapshot after Mongo failure: <connection details>
```

**This is expected and harmless.** It is the single most common thing that
alarms a new user, and it is not an error.

**Cause.** OpenBravo can optionally back its labware catalog with MongoDB so
that several machines in a lab share one definition of every plate and tip box.
You see this warning when a URI *is* configured but the database cannot be
reached. The catalog then falls back to a local snapshot file and the server
carries on; nothing is lost and nothing is broken.

The shipped `config/labware_catalog.yaml` has an empty `mongo.uri`, so a fresh
install reads the local snapshot directly and never emits this warning. If you
are seeing it, something is setting a URI — check that file and the
`PYBRAVO_LABWARE_MONGO_URI` environment variable.

**Fix, if you want the warning gone.** Pick one:

- **Do nothing.** A single-machine install works with no database at all.
- **Clear the URI** in `config/labware_catalog.yaml`:

  ```yaml
  mongo:
    uri: ""
  ```

  Or set the environment variable to empty without editing the file:

  ```bash
  export PYBRAVO_LABWARE_MONGO_URI=
  ```

- **Actually connect a database** by pointing `mongo.uri`, `mongo.database`,
  and `mongo.collection` at your own MongoDB, or setting
  `PYBRAVO_LABWARE_MONGO_URI`, `PYBRAVO_LABWARE_MONGO_DB`, and
  `PYBRAVO_LABWARE_MONGO_COLLECTION`. Environment variables win over the file.

> [!WARNING]
> Do not point this at a MongoDB you use for something else. If the configured
> database is reachable but has no labware collection, the catalog loads as
> empty — successfully, with no warning.

When Mongo *is* reachable you get an informational line instead —
`Loaded N labware definitions from Mongo` — and the local snapshot is refreshed
from it. See [configuration](configuration.md) for the full set of catalog
settings.

---

## The server runs but will not connect to the robot

### `No Bravo IP address is configured. Use 'Find Available Device' and select the device first.`

**Cause.** The active profile has an empty `connection.address` and a
controller type that needs one.

**Fix.** Set the address in the **Profiles** tab → **Connection** → **IP
Address**, or use **Find Available Device** and pick the instrument from the
list. Over the API that is `POST /api/discover_devices` followed by
`POST /api/select_device`, which writes the address into the profile.

### `Could not connect to <address>: …`

Work through these in order.

**Cause 1 — wrong `controller_type` for your machine.** Each generation speaks
a different protocol on a different port, and connecting with the wrong one
fails (or, worse, half-works). Check the profile against the machine you
actually own:

| Instrument generation | `controller_type` | Connection |
|---|---|---|
| Darwin-generation Bravo | `darwin_native` | TCP, port 7613 |
| Agile 7612 Bravo | `agile_7612` | TCP, port 7612 |
| Bravo SRT | `agile_srt` | TCP, port 7612 |
| Legacy Agile Bravo | `agile` | Serial, or TCP port 10000 |
| No hardware | `simulation` | — |

The Agile 7612 and SRT controllers reject serial entirely — they log
`… does not support serial; use Ethernet`. Likewise `darwin_native` is TCP
only.

**Cause 2 — wrong IP address, or the robot is on a different subnet.** Confirm
basic reachability first:

```bash
ping <robot-ip>
```

Then confirm the *port* is open, which is the part that actually matters:

```bash
nc -vz <robot-ip> 7613
```

Substitute the port for your generation from the table above.

**Cause 3 — the discovery sweep does not find your machine.** `POST
/api/discover_devices` (the **Find Available Device** button) runs three probes
in parallel: a UDP broadcast on port 7611, a TCP subnet sweep, and a directed
probe of whatever address is already in the profile. Two limitations catch
people out:

- **The subnet sweep only probes ports 7613 and 10000.** An Agile 7612 or SRT
  machine listening on **7612** will not be found by the sweep. It *will* be
  found by the directed probe if its address is already in the profile — so
  type the IP in by hand once, and discovery will confirm it from then on.
- **The sweep skips subnets larger than `/22`**, and always includes
  `192.168.0.0/24` as a preferred range. On a large corporate network, scanning
  is deliberately not attempted; set the address manually.

### Pressing Home on a single axis does nothing

**Symptom.** The per-axis `Home` button reports success but the axis does not
move. It works right after a power cycle and stops working thereafter.

**Cause.** On Darwin-generation instruments the homing routine skips any axis
that already reports itself initialized — that is deliberate, so reconnecting to
a running instrument does not re-home everything. An explicit operator Home has
to override it.

**Fix.** This is handled as of the current version: `POST /api/home_axis` forces
the routine, so the axis clears its state, searches for the home flag, and homes
regardless of what it reported beforehand. The log confirms it:

```
Homing W axis (operator request; forced)...
Parking W at 0.0 uL after homing (current 5.230 uL)...
```

W is returned to 0 afterwards, matching the state a cold initialize leaves it
in. Other axes are homed but not repositioned, so homing X does not cause an
unrequested lateral move.

### Discovery starts and never finishes

**Symptom.** The log shows the sweep starting and then nothing further:

```
Discover: adapters=1 adapter_ip=all target=192.168.1.50
Scanning 253 IPs for Bravo devices…
```

The UI sits on "Scanning…" and the request never returns.

**Cause.** Some device on the subnet accepts one of the probed ports without
speaking the protocol, and holds the connection. A sweep touches every host on
the LAN, so this is a property of your network, not of the instrument — printers,
KVMs, and management interfaces all do it.

**Fix.** This is bounded as of the current version: a single IP gets
`_PROBE_BUDGET_S` (3 s) and the whole sweep gets `_SCAN_BUDGET_S` (15 s), after
which discovery returns whatever it found and logs how many probes were still
outstanding:

```
Subnet scan hit its 15s budget with 3 of 253 probes still outstanding; returning 1 device(s) found so far.
```

A healthy sweep of a /24 finishes in about 2–3 seconds and logs
`Subnet scan finished in 2.48s -> 1 device(s)`. If you see the budget warning
every time, something on your subnet is stalling probes; set the instrument
address manually rather than relying on the sweep.

Check the server log while discovery runs. It reports what it tried:

```
Discover: adapters=2 adapter_ip=all target=192.168.0.8
Discover: bionet=0 scan=1 directed=hit (842 ms)
```

**Cause 4 — a firewall.** The connection is an outbound TCP connection from the
computer to the instrument, plus a UDP broadcast for discovery. Host firewalls
(macOS application firewall, Windows Defender Firewall, `ufw`/`firewalld`) will
block one or both. Allow the Python interpreter running OpenBravo to make
outbound connections and to send UDP broadcasts on port 7611. Corporate
networks frequently block subnet-wide scanning outright, which shows up as
discovery finding nothing while a directed connection works fine.

**Cause 5 — the robot is on a different subnet.** A Bravo commonly ships
configured for a small static network such as `192.168.0.x`. If your computer
is on `10.x` or `172.16.x`, nothing will route. Either put a second network
interface on the robot's subnet, or reconfigure the instrument. This is covered
in [hardware setup](hardware-setup.md).

**Cause 6 — something else is holding the connection.** These controllers
accept a single client. If another copy of OpenBravo (or any other client) is
connected, the second connection fails. Make sure only one server is running.

---

## The robot connects but will not move

### `Not connected` on every motion call

**Cause.** The connection dropped between the successful `POST /api/connect`
and the motion call — a cable, a power cycle, or the instrument closing an idle
socket.

**Fix.** Reconnect. If it drops repeatedly, check cabling and look at the
server log with `PYBRAVO_LOG_LEVEL=DEBUG` for the transport error that preceded
it.

### `<axis> axis not initialized; home the axis before issuing a move.` or `Axis is not homed.`

**Cause.** You connected but did not initialize and home. Axis coordinates are
meaningless until the axis has found its reference, and the controller refuses
to move rather than move somewhere arbitrary. You will also see the jog
equivalent: `<axis> axis not initialized; home before jogging.`

**Fix.** Run the three steps in order: **Connect** → **Initialize** →
**Home All**. Over the API, `POST /api/connect`, `POST /api/initialize`,
`POST /api/home`. If only one axis is affected, `POST /api/home_axis` with
`{"axis":"Z"}` homes it alone.

### `Robot safety interlock is active (E-stop). Release the interlock and retry.`

**Cause.** The instrument reports its safety circuit as open. That is an
engaged emergency stop, a broken light curtain, an open door interlock, or a
fault in the disable circuitry (which reports separately as
`Robot disable button circuitry failure.`).

On Darwin machines a mid-motion trip produces a more specific message:

```
Cannot recover: safety interlock still active (SAFETY_STATUS bit 0 set). Clear the light curtain / release E-stop, then retry.
```

**Fix.** Physically clear the condition — release the E-stop, remove whatever
is breaking the light curtain, close the enclosure — and then retry.

> **Do not attempt to bypass a safety interlock in software.** It is the only
> thing standing between a moving head and whatever is in the work envelope.

Confirm the state before retrying:

```bash
curl http://localhost:8000/api/io_status
```

The response has six fields: `robot_disabled`, `head_attached`, `head_type`,
`go_button_pressed`, `plate_in_gripper`, and `motors_enabled` (a per-axis map).
`robot_disabled: true` means the interlock is still open. The same values are
rendered in the **I/O** tab of the UI.

### The task engine is waiting on a prompt

**Cause.** Initialization and several other tasks pause and ask the operator to
confirm something — gripper detection, a plate apparently in the gripper,
whether it is safe to home the W axis. Until you answer, nothing proceeds.

**Fix.** Answer the *Operator Action Required* dialog in the UI. The three
choices map to `POST /api/retry`, `POST /api/ignore`, and `POST /api/abort`:

- **Retry** re-attempts the step.
- **Ignore** skips it and continues. Read the prompt before choosing this —
  ignoring a plate-in-gripper warning means homing the gripper onto a plate.
- **Abort** stops the task.

### `Motor enable timeout`, or an axis is limp

**Cause.** Servo power is off for that axis, either because it was explicitly
disabled or because a fault dropped it.

**Fix.** Check `motors_enabled` in `/api/io_status`, then re-enable:

```bash
curl -X POST http://localhost:8000/api/motor/enable_all
```

> **Disabling a motor removes holding torque.** On a vertical axis that can let
> the assembly drop under its own weight. Support the head before disabling Z,
> and do not disable motors as a way of "resetting" a fault while labware is
> underneath.

---

## An axis moves the wrong distance

**Symptom.** You command 10 mm and get 3 mm, or 100 mm. Positions read back as
plausible numbers, but the physical motion does not match. Nothing errors.

**Cause.** `ticks_per_eng_unit` for that axis is wrong in the profile. This is
the encoder-counts-per-engineering-unit scaling, and every position, velocity,
and acceleration for the axis is multiplied by it. **There is no validation on
this value anywhere** — a wrong number produces silently wrong motion rather
than an error, which is what makes this failure mode dangerous.

The shipped defaults, for reference:

| Axis | Default `ticks_per_eng_unit` | Unit |
|---|---|---|
| X | 314.96 | ticks per mm |
| Y | 314.96 | ticks per mm |
| Z | 1600.0 | ticks per mm |
| G | 944.88 | ticks per mm |
| Zg | 787.4 | ticks per mm |
| W | machine-specific | ticks per **microlitre**, not per mm |

W is the exception worth remembering: it is the aspirate/dispense axis and its
engineering unit is volume, so its scaling is entirely unrelated to the linear
axes and cannot be sanity-checked against them.

**Fix.**

1. **Stop before you experiment.** A scaling error means every commanded move
   overshoots or undershoots, including moves toward the deck. Clear the deck
   and work at slow speed. See [safety](safety.md).
2. Compare the axis block in your profile (`profiles/<name>.yaml`, under
   `axes:`) against the table above and against a known-good profile for the
   same machine generation.
3. Measure. Home the axis, jog a known distance at slow speed, and measure the
   physical travel. The ratio of commanded to actual distance is the ratio your
   `ticks_per_eng_unit` is off by.
4. If you imported the profile from a previous system, re-check the import.
   Windows registry profile exports carry a per-axis "ticks per engineering
   unit" field that maps directly onto this setting, and a partial import can
   leave some axes on library defaults.

Related symptom: **the axis refuses to move at all and reports**

```
Move target 250.0000 mm on X is outside software limits [0.0000, 390.0000].
```

That is the soft-limit check doing its job. Either your target really is out of
range, or `min_range`/`max_range` for the axis are wrong in the profile. Do not
widen the limits to make a move succeed without first understanding why the
target is outside them — the limits are what stop an axis driving into its own
hard stop.

---

## Homing fails or the head crashes into the deck

> **This section describes the failure mode most likely to damage the
> instrument.** Homing moves axes at speed toward reference positions *before*
> the software knows where anything is. Clear the deck, remove plates from the
> gripper, keep hands out, and be ready on the emergency stop. Read
> [safety](safety.md).

### `Axis homing timeout [<axis>]` or `Axis homing retries exceeded [<axis>]`

**Cause.** The axis moved but never reached its home flag within the timeout —
a blocked axis, a failed home sensor, or homing parameters in the profile that
do not match the hardware.

**Fix.**

1. Power down and check the axis moves freely by hand through its full travel.
   Something jammed against a plate or an accessory will produce exactly this.
2. Check the per-axis homing settings in the profile: `home_flag_register`,
   `home_flag_bitmask`, `home_complete_register`, `home_in_positive_direction`,
   `homing_velocity`, `homing_acceleration`, and `homing_offset`. A profile
   copied from a different machine generation is a common source.
3. Home one axis at a time (`POST /api/home_axis`) to isolate which one is
   actually failing, rather than watching a six-axis home fail as a unit.

### `Axis commutation timeout` / `Axis not commutated`

**Cause.** The servo could not establish its commutation reference before
homing. Often a motor power problem, or an axis that cannot move far enough to
commutate because something is in the way.

**Fix.** Clear the obstruction, confirm motor power (`/api/io_status` →
`motors_enabled`), and retry. On Darwin machines a failed gripper commutation
can clamp harder on anything held in the gripper — remove plates from the
gripper before homing.

### `<axis> failed to home`

**Cause.** Homing completed without error but the post-home verification found
the axis not reporting itself as homed.

**Fix.** Retry the single axis. If it repeats, treat it as a hardware or
profile problem as above rather than retrying into it.

### The head drives into the deck, a plate, or a tip box

**Cause.** Almost always one of five things, in rough order of likelihood:

1. **Wrong or missing teachpoints.** Every deck location needs a taught XYZ for
   the labware you actually have there. A teachpoint taught with a short plate
   will crash a tall one.
2. **Wrong labware assigned to a location.** Heights, stack heights, and well
   depths all come from the labware definition. Assigning the wrong plate makes
   every Z calculation wrong. Check the **Config** tab's deck layout against
   the physical deck.
3. **Wrong `ticks_per_eng_unit` on Z** — see
   [an axis moves the wrong distance](#an-axis-moves-the-wrong-distance).
4. **Safe-Z is wrong or disabled.** `safety.z_safe_position` sets the clearance
   height, and `safety.always_move_to_safe_z` controls whether the head
   retracts before travelling. With that disabled, an XY move at working height
   will sweep through everything on the deck.
5. **A profile that does not match the machine.** Axis ranges, head type, and
   controller type all have to be right for the instrument in front of you.

**Fix.** After any crash: stop, power down, inspect the head and the affected
labware for damage before doing anything else. Then re-verify teachpoints one
location at a time, at slow speed, with the approach-height controls in the
**Jog/Teach** tab, before running any protocol. Never re-run the protocol that
crashed to "see if it does it again".

---

## Tips will not pick up

### `Tips On did not encounter the expected tip resistance before reaching the press limit.`

The full message continues: *"The tipbox may be missing, the selected tips may
be absent, or the teachpoint/box height is incorrect. The head was retracted to
safe Z."*

**Cause.** Tip pickup is a force-controlled press. The head descends expecting
to meet resistance from the tips; when it reaches its travel limit without
feeling anything, it concludes there are no tips where it was told to look.
That is one of: no tip box at the location, the box is empty in the selected
columns, the box is the wrong height, or the teachpoint is off.

**Fix.**

1. Confirm a tip box is physically at that location, and that the software
   agrees — **Config** tab → deck layout.
2. Confirm the box is not empty in the region the head is trying to use. The
   **Processes** tab's 2D selector shows tip occupancy.
3. Confirm the labware assigned to the location is the tip box you actually
   have. A mismatched height is enough to cause this.
4. Re-verify the teachpoint for that location.

The dialog offers **Retry**, **Ignore**, and **Abort**. **Ignore** marks the
tips as on and continues — useful when exercising the software without
hardware, and a bad idea on a real run, because everything downstream will
assume tips that are not there.

### `Tips are already on the head` or `<operation> requires a tip box at location <n>`

**Cause.** The software's tip state does not match reality, or the location you
named has no tip box assigned.

**Fix.** Assign the tip box (**Config** tab → **Assign Labware**), or eject the
tips the software thinks are on the head before picking up again.

## Tips will not eject

### `No tips are currently tracked on the head`

**Cause.** The software does not believe tips are on. This commonly happens
after a power cycle or a server restart, which clears tracked tip state while
the physical tips stay on the head.

**Fix.** The dialog's **Ignore** choice proceeds with ejection anyway, which is
the right answer when you can see tips on the head. **Retry** re-checks the
state.

### The tips come off in the wrong place, or catch on the way off

**Cause.** Teachpoint or labware height at the tips-off location, or the
tip-touch settings. `safety.enable_tips_off_tip_touch`,
`safety.tips_off_w_position`, `safety.tips_off_z_offset`, and
`safety.tips_off_tip_touch_distance` in the profile all shape this motion.

**Fix.** Re-verify the teachpoint, then adjust the tips-off settings — see
[configuration](configuration.md).

---

## The UI loads but positions and status never update

**Symptom.** The page renders, buttons work, and API calls succeed — but the
header stays on `Disconnected`, the axis readouts stay at `0.000`, and the 3D
model never moves.

**Cause 1 — you are not connected yet.** The state stream only sends data while
a controller is connected. Before **Connect**, an idle indicator is correct
behavior, not a bug.

**Cause 2 — the WebSocket is blocked.** All live state arrives over
`ws://<host>:8000/ws/state`. REST calls will keep working perfectly with the
WebSocket blocked, which is exactly why this is confusing. Culprits are proxies
that do not forward WebSocket upgrades, browser extensions, and corporate
inspection appliances.

**Cause 3 — you are not on port 8000.** The UI builds its WebSocket URL with
the port hardcoded to `8000`. If you reach the server through a reverse proxy
or a forwarded port on a different number, the page loads over your port but
the WebSocket still tries 8000 and fails.

**Fix.** Open the browser's developer console and look for a failed WebSocket
connection to `/ws/state`. The UI also logs `WebSocket connected` and
`WebSocket disconnected` into its own log panel, and retries every three
seconds — a panel that repeatedly logs disconnects is a blocked socket.

Then:

- Reach the server directly on port 8000 rather than through a proxy.
- If you must proxy, configure it to forward WebSocket upgrade requests, and
  keep the external port at 8000.
- Verify the endpoint independently with any WebSocket client against
  `ws://localhost:8000/ws/state` — you should receive a JSON state document
  several times a second once a controller is connected.

Meanwhile, `GET /api/state` returns the same document over plain HTTP, so you
can keep working while you sort the socket out.

---

## The labware list is nearly empty or has no tip boxes

**Symptom.** The **Labware** dropdown in the Config and Processes tabs offers
only a couple of plates, and no tip box, so you cannot assign one to a
location.

**Cause.** The runtime catalog is built at startup from — in order — MongoDB,
then a local snapshot file, then a small built-in fallback of two plate
definitions. On a fresh clone with no database and no snapshot yet generated,
you get the two-plate fallback.

**Fix.** Any of:

- **Create or edit a labware type** in the labware editor at
  <http://localhost:8000/labware-editor>. Saving writes the snapshot and
  refreshes the runtime catalog immediately. The same thing happens on
  `POST /labware/types` if you would rather do it over the API.
- **Point at a MongoDB** that has your lab's catalog, as described in
  [the MongoDB section](#the-server-logs-a-mongodb-connection-warning-on-startup).
- **Set `PYBRAVO_LABWARE_SNAPSHOT_PATH`** to an existing snapshot file, or the
  `cache.snapshot_path` key in `config/labware_catalog.yaml`.

Confirm what the server currently has:

```bash
curl http://localhost:8000/api/labware
```

---

## The UI renders blank or dead on a machine with no internet access

**Symptom.** On an air-gapped or heavily firewalled lab machine, the page loads
but nothing works — no 3D viewport, buttons do nothing, the log panel stays
empty. The labware editor comes up completely blank.

**Cause.** The browser pages load their JavaScript libraries from public CDNs
at page load: `three.js` from `esm.sh` for the main control page and the 3D
viewport, plus React and Babel from `esm.sh` and `unpkg.com` for the labware
editor. The main page's script is an ES module that imports `three` at the top,
so if that import fails the whole module fails and none of the UI wiring runs.

The interactive API documentation at `/docs` is affected the same way — its
browser assets also come from a CDN.

**Fix.** Either allow the browser (not the server) outbound access to those
hosts, or drive the instrument through the HTTP API instead of the UI. The
backend itself has no such dependency: every endpoint under `/api/` and the
`/ws/state` WebSocket work with no internet access at all, and the raw OpenAPI
schema is served as plain JSON from `/openapi.json`, which needs nothing
external.

For labware specifically, `GET`/`POST`/`DELETE` on `/labware/types` and
`/labware/classes` cover what the labware editor does, and each write refreshes
the runtime catalog. Alternatively, edit the catalog on a machine with internet
access and copy the resulting files across.

---

## Tests fail

Run them with the `dev` extra:

```bash
uv run --extra dev python -m pytest
```

**One failure is expected.**
`test_back_left_rectangle_uses_front_right_tipbox_anchor` in
`tests/test_bravo_init.py` is a known, pre-existing failure. A run reporting
roughly 616 passes, that one failure, and some skips is healthy.

**Skips are normal.** Several tests skip themselves when an optional dependency
or fixture is absent — the legacy protocol import tests skip without a local
protocol fixture, the vision tests skip without NumPy, and some protocol tests
skip without extracted fixture payloads. None of these indicate a problem.

**If many tests fail**, the usual causes are:

- **The wrong environment.** `python -m pytest` uses whichever interpreter is
  first on your `PATH`. Prefer `uv run --extra dev python -m pytest`, which is
  unambiguous.
- **Missing `dev` dependencies.** `pytest-asyncio` in particular is required —
  the suite sets `asyncio_mode = "auto"`, and without the plugin every async
  test errors.
- **Stale bytecode.** Run with `python -B`, or delete `__pycache__` directories,
  after switching branches.
- **A modified profile or config.** Some tests read the shipped files under
  `profiles/` and `config/`. If you have edited them, check `git status` and
  consider testing from a clean checkout.

---

## Turning on debug logging

OpenBravo uses Python's standard `logging`, configured centrally in
`pybravo/logging_config.py`. Everything below is controlled by environment
variables, which can also be set in a `.env` file in the repository root (the
server loads it at startup; shell-exported variables take precedence).

### More detail

```bash
PYBRAVO_LOG_LEVEL=DEBUG ./scripts/start_pybravo.sh
```

`DEBUG` adds controller state, position reads, and internal decisions from the
task engine — the right first step for anything involving motion or state that
does not behave as expected.

### Raw protocol frames

```bash
PYBRAVO_PROTOCOL_TRACE=1 ./scripts/start_pybravo.sh
```

This enables `TRACE` (level 5), which hex-dumps protocol frames and raw
transport bytes. Use it when a connection establishes but commands do not do
what you expect. It is verbose; it costs nothing when it is off, because the
formatting is gated behind a level check.

The two combine, and `PYBRAVO_PROTOCOL_TRACE` raises the protocol and transport
loggers independently of the root level, so you can have trace-level wire
output with ordinary `INFO` everywhere else.

### Where the logs go

**By default, to the console** — the terminal running the server. There is no
log file unless you ask for one.

**One rotated file** (10 MB, 3 backups), duplicating console output:

```bash
PYBRAVO_LOG_FILE=/tmp/pybravo.log ./scripts/start_pybravo.sh
```

**Three separate rotated files**, which is what you want when filing a bug:

```bash
PYBRAVO_LOG_DIR=/tmp/pybravo_logs ./scripts/start_pybravo.sh
```

That produces:

| File | Contents |
|---|---|
| `pybravo.log` | Everything, at the configured root level |
| `protocol.log` | `pybravo.protocol.*` and `pybravo.transport.*` only |
| `api.log` | `pybravo.web.*` only — HTTP requests and responses |

`PYBRAVO_LOG_DIR` and `PYBRAVO_LOG_FILE` are mutually exclusive;
`PYBRAVO_LOG_DIR` wins if both are set.

### HTTP request logging

Every HTTP request is logged at `INFO` with method, path, status, and duration:

```
INFO  pybravo.web.http: POST /api/jog -> 200 (12.3ms)
```

High-frequency polling endpoints (`/api/state`, `/api/deck/state`), WebSocket
upgrades, and static assets are skipped so they do not drown out everything
else. If you are trying to confirm the UI is actually reaching the server,
these lines are the fastest check.

### Wire-level counters

For Agile 7612 hardware specifically:

```bash
curl http://localhost:8000/api/diagnostics
```

This returns command counts and the error log from the communications layer. It
is only available for the `agile_7612` controller type.

---

## Still stuck

Collect these before asking for help — they answer most of the first round of
questions:

- The exact error text, including the `error_type` if the response had one.
- `curl http://localhost:8000/api/io_status` and
  `curl http://localhost:8000/api/state`.
- Your active profile (`profiles/<name>.yaml`), with anything sensitive
  removed. The `connection`, `head`, `safety`, and `axes` blocks matter most.
- A server log captured with `PYBRAVO_LOG_LEVEL=DEBUG`, and
  `PYBRAVO_PROTOCOL_TRACE=1` as well if the problem is a connection or a
  command that does not take effect.
- Your instrument generation and the `controller_type` you are using.

Then see the [FAQ](faq.md), the [architecture overview](architecture.md) for
where a given behavior lives in the code, and the
[API reference](api-reference.md) for endpoint semantics.
