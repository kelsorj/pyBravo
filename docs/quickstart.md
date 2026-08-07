# Quickstart

Your first fifteen minutes with OpenBravo: start the server, tour the browser
UI, and drive a simulated Bravo through a home, a jog, and a complete
aspirate/dispense cycle — with no hardware attached.

Everything on this page runs against the built-in simulation. Nothing moves in
the physical world. When you are ready for a real instrument, jump to
[Moving to real hardware](#moving-to-real-hardware) at the end.

**Before you start:** you need a working install. See
[installation](installation.md).

Each step below says what *should* happen, so you can tell whether it worked
before moving on. Where a step is easier in the browser, the equivalent HTTP
call is given too — the UI and the API do exactly the same thing.

---

## 1. Start the server

From the repository root:

```bash
./scripts/start_pybravo.sh
```

On Windows, use `scripts\start_pybravo.bat`.

**What should happen.** The launcher prints the interpreter it chose, then
uvicorn starts. You should see log lines similar to:

```
Loaded profile from /path/to/OpenBravo/profiles/default.yaml
```

You will probably also see a `WARNING` about falling back to a local labware
snapshot after a Mongo failure. **This is expected and harmless** when you have
no MongoDB configured — the labware catalog simply uses its local copy. See
[troubleshooting](troubleshooting.md#the-server-logs-a-mongodb-connection-warning-on-startup)
if you want the full story.

Leave this terminal running. Every step below assumes the server is up.

---

## 2. Open the UI

Open <http://localhost:8000> in a browser.

**What should happen.** You land on the main control page. Take thirty seconds
to get oriented — there are three regions:

**The header**, across the top:

- The title, then a **connection indicator**: a dot and the word
  `Disconnected`. It turns green and reads `Connected (<controller_type>)` once
  you connect. This indicator is driven by a WebSocket, not by polling; if it
  never changes, see
  [the UI loads but shows no live state](troubleshooting.md#the-ui-loads-but-positions-and-status-never-update).
- **Designer** — opens the visual workflow designer on a separate page.
- **Theme** — toggles light/dark.
- **Connect**, **Initialize**, **Home All** — the three steps you are about to
  perform, in order.
- **Abort** — stops the running task. On real hardware this is your software
  stop; it is not a substitute for the physical emergency stop.
- **Quit** — shuts down the backend process.

**The 3D viewport**, on the left: a URDF model of the robot that mirrors the
live axis positions. It shows `URDF loading…` until the model is ready.

**The side panel**, on the right, with six tabs:

| Tab | What it holds |
|---|---|
| **Jog/Teach** | Live X/Y/Z/W positions, the jog pad, jog step sizes and speed, teachpoints for the nine deck locations, and per-axis and multi-axis homing. |
| **Gripper** | G and Zg positions, gripper jogging, open/close/dock actions, pick-and-place, and gripper teaching. |
| **Config** | Assign labware to deck locations, a 3×3 deck layout view, deck verification (if the camera is enabled), and accessories. |
| **I/O** | Robot status, head detection, go-button state, plate detection, and per-axis motor enable state. |
| **Processes** | Run a single operation — aspirate, dispense, mix, tips on/off, plate and lid handling, barcode read — with a 2D well/tip selector. |
| **Profiles** | Load, duplicate, rename, and import profiles; the connection settings; safety and head settings. |

**The log panel**, at the bottom: a running record of what the UI did and what
the server said back. Watch it as you work through the rest of this page.

A few other pages are served by the same process, and are worth knowing about:
`/designer`, `/labware-editor`, `/liquid-class-editor`, `/tip-editor`, and
`/docs` (the interactive API reference).

---

## 3. Switch the active profile to simulation

The shipped `default.yaml` profile is configured for a real instrument. Point
it at the simulation controller so that connecting does not try to reach a
robot over the network.

In the UI: **Profiles** tab → **Connection** → set **Controller** to
`Simulation` → click **Save Settings** in the Profile Management section.

Or over the API:

```bash
curl -X PATCH http://localhost:8000/api/profile -H 'Content-Type: application/json' -d '{"controller_type":"simulation"}'
```

**What should happen.** The response is
`{"status":"updated","saved":true}`. The change is written back to
`profiles/default.yaml`, so it survives a restart. The IP address and serial
port fields disappear from the UI, because the simulation needs neither.

> If you would rather not modify the shipped profile, duplicate it first
> (**Duplicate…** in the Profiles tab) and edit the copy.

---

## 4. Connect

Click **Connect** in the header, or:

```bash
curl -X POST http://localhost:8000/api/connect -H 'Content-Type: application/json' -d '{}'
```

**What should happen.** The response is
`{"status":"connected","controller":"simulation"}`. In the UI, the header dot
turns green and the label changes to `Connected (simulation)`. The axis
positions in the Jog/Teach tab start updating, and the 3D model begins to track
them.

If the dot stays grey but the API call returned success, the WebSocket is being
blocked — the state stream only flows over `ws://<host>:8000/ws/state`. See
[troubleshooting](troubleshooting.md#the-ui-loads-but-positions-and-status-never-update).

---

## 5. Initialize

Initializing runs the startup state machine: it checks the device responds,
verifies the safety interlock, detects the gripper, configures the head, and
brings the axes into a known state.

Click **Initialize**, or:

```bash
curl -X POST http://localhost:8000/api/initialize
```

**What should happen.** The response is
`{"status":"initialized","controller":"simulation"}`, and the log panel shows
the initialization steps going by.

On **real** hardware this step can pause and raise an *Operator Action
Required* dialog — for example asking you to confirm it is safe to home the
W axis, or telling you a plate appears to be sitting in the gripper. Each
prompt offers **Retry**, **Ignore**, and **Abort**. Read them; they exist
because the alternative is a crash. In simulation you will not see them,
because the simulated axes report themselves as already homed and the simulated
sensors report a clear deck.

---

## 6. Home

Homing drives each axis to its reference position so that every subsequent
coordinate means something. Nothing else is trustworthy until this has happened.

Click **Home All**, or:

```bash
curl -X POST http://localhost:8000/api/home
```

**What should happen.** The response lists the axes that were homed, for
example `{"status":"homed","axes":["X","Y","Z","W","G","Zg"]}`. The positions
in the Jog/Teach tab settle at their homed values, and the 3D model moves to
match.

> **On real hardware, homing is the single most dangerous routine you will
> run.** It moves axes at speed toward hard stops, and it does it before the
> software knows where anything is. Clear the deck, make sure nothing is in the
> gripper, and keep hands out. Read [safety](safety.md) first.

---

## 7. Jog an axis

Jogging moves one axis by a relative step. It is how you position the head by
hand — for teaching deck locations, for inspecting a plate, for recovering from
an awkward position.

In the UI: **Jog/Teach** tab. The jog pad has one button per direction —
`Left −X` / `Right +X`, `Back −Y` / `Fwd +Y`, `Up −Z` / `Down +Z`, and
`Asp +W` / `Dsp −W`. Underneath, pick a step size per axis group (W, XY, Z) and
a speed (Slow / Medium / Fast). Click `Right +X` a couple of times.

Or over the API — this jogs X by +5 mm:

```bash
curl -X POST http://localhost:8000/api/jog -H 'Content-Type: application/json' -d '{"axis":"X","step":5,"direction":1}'
```

**What should happen.** The response reports the axis, the signed step, and the
resulting position, for example
`{"status":"jogged","axis":"X","step":5.0,"position":5.0}`. The X readout in
the Jog/Teach tab increases by the step size, and the 3D model slides along the
X axis.

Confirm the position independently:

```bash
curl http://localhost:8000/api/positions
```

Note the sign conventions, because they are not what everyone expects: **−Z is
up** and **+Z is down**, and **−Y is back** while **+Y is forward**. On W, which
is the aspirate/dispense axis and is measured in microlitres rather than
millimetres, **+W aspirates** and **−W dispenses**. Getting a Z sign wrong on
real hardware drives the head into the deck, so it is worth internalising here.

---

## 8. Put labware on the deck

The Bravo deck has nine locations, numbered 1–9. Liquid-handling operations
refer to a location number, and the software needs to know what is sitting
there to compute heights and well positions.

First see what is in your labware catalog:

```bash
curl http://localhost:8000/api/labware
```

**What should happen.** A JSON object with a `labware` array. Each entry has an
`id` and a `name`; the `id` is what you assign to a location. Note down the id
of a **tip box** and the id of a **microplate**.

> If the list is very short — two entries and no tip box — your local labware
> snapshot has not been generated yet. See
> [troubleshooting](troubleshooting.md#the-labware-list-is-nearly-empty-or-has-no-tip-boxes).

Now assign them. In the UI: **Config** tab → **Location Configuration** →
choose a **Location**, choose a **Labware** from the dropdown, and click
**Assign Labware**. Do this three times:

- location **1** → your tip box
- location **2** → a microplate (this will be the source)
- location **3** → another microplate (this will be the destination)

Or over the API, substituting real ids:

```bash
curl -X PUT http://localhost:8000/api/deck/1/labware -H 'Content-Type: application/json' -d '{"labware_id":"<tip-box-id>"}'
```

**What should happen.** Each call returns `{"status":"assigned", ...}` with the
labware metadata. In the **Config** tab, the 3×3 **Deck Layout** grid now shows
the labware name in cells 1, 2, and 3.

---

## 9. Pick up tips

The default head (`HT_96_D_70`) is a disposable-tip head, so liquid handling
requires tips before it will run at all.

In the UI: **Processes** tab → set **Location** to `1` → set **Command** to
`Tips On` → click **Execute Command**.

Or:

```bash
curl -X POST "http://localhost:8000/api/tips_on?location=1"
```

**What should happen.** The response is `{"status":"completed"}`. The head
travels to location 1, presses onto the tips, and retracts. The log panel shows
the steps; in the 3D viewport you can watch the head move over location 1 and
back up.

On real hardware this is a force-controlled press, and the most common failure
is the head not meeting the resistance it expects — a missing tip box, an empty
column, or a teachpoint that is off. You get a dialog explaining exactly that.
See [troubleshooting](troubleshooting.md#tips-will-not-pick-up).

---

## 10. Aspirate and dispense

Now move liquid from location 2 to location 3.

In the UI: **Processes** tab → **Location** `2` → **Command** `Aspirate` → set
**Volume** to `10` in the Aspirate Properties panel → **Execute Command**. Then
switch **Location** to `3`, **Command** to `Dispense`, volume `10`, and execute
again.

Or over the API — aspirate 10 µL from location 2:

```bash
curl -X POST "http://localhost:8000/api/aspirate?location=2&volume=10"
```

Then dispense 10 µL at location 3:

```bash
curl -X POST "http://localhost:8000/api/dispense?location=3&volume=10"
```

**What should happen.** Each returns `{"status":"completed"}`. Watch the **W**
readout in the Jog/Teach tab: it rises as the plunger draws liquid in and falls
as it pushes liquid out. X, Y, and Z move to the target location and back to a
safe height between operations.

The Processes tab exposes considerably more than volume — pre- and
post-aspirate air gaps, distance from the well bottom, blowout, tip touch,
liquid class, and pipette technique. Those are covered in the
[user guide](user-guide.md).

---

## 11. Eject the tips

In the UI: **Processes** tab → **Location** `1` → **Command** `Tips Off` →
**Execute Command**.

Or:

```bash
curl -X POST "http://localhost:8000/api/tips_off?location=1"
```

**What should happen.** `{"status":"completed"}`. The head returns the tips to
location 1 and the software stops tracking tips on the head.

That is a full liquid-handling cycle. You have connected, initialized, homed,
jogged, configured a deck, picked up tips, moved liquid, and put the tips back.

---

## 12. Look at the rest of the system

With the simulation still connected, a few things are worth a minute each:

**The full runtime state** — everything the UI renders, in one document:

```bash
curl http://localhost:8000/api/state
```

**The API reference** — every endpoint, with schemas and a try-it button, at
<http://localhost:8000/docs>. It is generated from the running server, so it is
always accurate for the version you have. The prose version is in
[api-reference.md](api-reference.md).

**The workflow designer** at <http://localhost:8000/designer>, or the
**Designer** button in the header. Build a protocol as a node graph, simulate
it, then run it. Because you are in simulation, you can run anything you build
without consequence. This is the right place to develop a protocol before it
ever touches an instrument.

---

## Moving to real hardware

> **Stop and read [safety](safety.md) before this section.** A Bravo moves
> heavy assemblies quickly and does not know your hand is in the way. It can
> destroy labware, bend a pipette head, and cause injury. Everything up to this
> point was simulated; from here it is not.

The transition has four parts, and [hardware setup](hardware-setup.md) covers
all of them in detail:

1. **Networking.** Put the instrument and the computer on the same subnet. Each
   generation listens on a different TCP port, so the `controller_type` in your
   profile has to match the machine you actually own:

   | Instrument generation | `controller_type` | Connection |
   |---|---|---|
   | Darwin-generation Bravo | `darwin_native` | TCP, port 7613 |
   | Agile 7612 Bravo | `agile_7612` | TCP, port 7612 |
   | Bravo SRT | `agile_srt` | TCP, port 7612 |
   | Legacy Agile Bravo | `agile` | Serial, or TCP port 10000 |

2. **Finding the instrument.** In the Profiles tab, **Find Available Device**
   scans the network and lists what it finds; selecting a device writes its
   address into the profile. The same thing is available as
   `POST /api/discover_devices` and `POST /api/select_device`.

3. **A correct profile.** Axis travel limits, encoder scaling, head type,
   gripper currents, and safety behavior all live in the profile. Getting these
   wrong is how a machine crashes into its own deck. If you have configuration
   from a previous system, the Profiles tab can import Windows registry profile
   exports (`.reg`) and legacy `.dat` profile directories. See
   [configuration](configuration.md).

4. **Teachpoints.** Every deck location needs a taught XYZ position for the
   installed labware. Teach them at low speed with the jog controls, one at a
   time, checking clearance as you go.

Then repeat this quickstart against the real machine — connect, initialize,
home, jog, and only then run liquid handling. Dry-run every new protocol in
simulation before you run it for real.

---

## Where to go next

- [Safety](safety.md) — required reading before the robot moves.
- [Hardware setup](hardware-setup.md) — networking, discovery, profiles,
  teachpoints.
- [User guide](user-guide.md) — the whole UI, end to end.
- [Configuration](configuration.md) — profiles, environment variables, labware
  and liquid-class catalogs.
- [API reference](api-reference.md) — every HTTP and WebSocket endpoint.
- [Troubleshooting](troubleshooting.md) — when a step above does not behave as
  described.
- [FAQ](faq.md) — common questions.
