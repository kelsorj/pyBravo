# User guide

A tour of the pyBravo web interface: the operator dashboard, the editors for
labware, tips and liquid classes, and the normal order of operations for
bringing an instrument from cold to working.

This guide assumes the server is already running and reachable. If it is not,
start with [installation](installation.md) and [quickstart](quickstart.md).

> **This software moves a physical robot.** Most of the controls described
> below command motion the moment they are clicked — there is no separate
> confirmation step. Read [safety](safety.md) before you operate real hardware,
> and keep hands clear of the deck whenever the instrument is connected.

---

## The pages

The server publishes several browser pages from one process:

| Path | Page | Purpose |
|---|---|---|
| `/` | Control panel | Day-to-day operation: connect, home, jog, pipette, move plates |
| `/designer` | Workflow designer | Build, simulate and run node-graph protocols |
| `/labware-editor` | Labware dashboard | Edit labware entries and labware classes |
| `/liquid-class-editor` | Liquid class editor | Edit liquid classes and pipette techniques |
| `/tip-editor` | Tip editor | Edit tip definitions |
| `/vision-calibration` | Vision calibration | Calibrate and verify the optional deck camera |
| `/workflow` | Workflow editor (earlier) | Superseded by `/designer` |

`/workflow` is an earlier, simpler workflow editor that is still served for
backwards compatibility. `/designer` is the actively developed one; use it for
anything new. Everything about it is covered in [workflows](workflows.md).

---

## Order of operations

The instrument has to be brought up in a fixed order. Each step depends on the
one before it.

1. **Pick a profile.** The *Profiles* tab selects which YAML profile is active.
   The profile carries the controller type, network address, head type,
   teachpoints and safety preferences. See [configuration](configuration.md).
2. **Connect.** The `Connect` button in the header opens the transport to the
   instrument. That is all it does — it does **not** initialize the instrument,
   and the robot is not ready for motion until you initialize it separately.
3. **Initialize.** Initialization verifies communication and runs the standard
   startup task sequence for the configured controller. **This causes motion.**
4. **Home.** `Home All` retracts to a safe Z, places the gripper in a safe
   docked state where one is fitted, homes the machine axes, and moves the
   homed axes to their park positions. **This causes motion.** Individual axes
   can be homed from the per-axis `Home` buttons on the *Jog/Teach* and
   *Gripper* tabs.
5. **Describe the deck.** Assign labware to each occupied deck location on the
   *Config* tab. Motion planning, collision checks and the 3D view all depend
   on the software knowing what is physically present.
6. **Work.** Jog, teach, pipette, or run a workflow.

`Abort` in the header signals the task engine to stop the operation currently
running. It is the control to reach for when something looks wrong; for a true
emergency, use the instrument's own emergency stop. `Quit` terminates the
backend server process.

The connection indicator beside the title shows the live transport state, and
the log panel along the bottom of the page records every command and its
result.

---

## The 3D viewport

The left side of the control panel renders a URDF model of the robot — the
digital twin. It mirrors the axis positions streamed from the instrument, and
draws the labware currently assigned to each deck location, tips on the head,
and plates held in the gripper. In simulation mode it is driven by the
simulated controller instead, which is what makes it useful for rehearsing a
layout before any hardware is involved.

The viewport is a display, not a control surface: nothing you do in it moves
the robot.

---

## Control panel tabs

### Jog / Teach

**Axis positions** shows the four machine axes with their live values and a
per-axis `Home` button:

| Axis | Direction | Unit |
|---|---|---|
| X | left / right | mm |
| Y | back / front | mm |
| Z | up / down | mm |
| W | aspirate / dispense (plunger) | µL |

**Jog axes** is a directional pad: `Left −X` / `Right +X`, `Back −Y` /
`Fwd +Y`, `Up −Z` / `Down +Z`, and `Asp +W` / `Dsp −W`. Each group has its own
step-size selector — W in µL, XY in mm, Z in mm — plus a `Speed` selector with
Slow, Medium and Fast. **Every jog button commands immediate motion.** Start
with a small step size, especially near labware.

A `Diagnostics` checkbox reveals a `Z current limit` selector. With it enabled,
Z jogs run in force-limited mode and stop when the selected current is reached.

**Teachpoints** define the reference X, Y and Z coordinates for each of the
nine deck locations. Pick a `Location`, then:

- `Move` drives to the stored teachpoint. **Causes motion.**
- `Approach` drives to the stored teachpoint offset upward by the
  `Approach height` value. **Causes motion.**
- `Teach` writes the robot's current position into that location's teachpoint.
- `Move to Safe Z` retracts Z to the profile's configured safe position.
  **Causes motion.**

The `Tip` selector tells the teaching maths which tip is fitted, since tip
length changes where the head has to sit to reach a given well. Teaching
procedure and tolerances are covered in [hardware setup](hardware-setup.md).

**Multiple axes** offers `Home XYZ` (**causes motion**), `Enable All Motors`
and `Disable All Motors`. Disabling motors removes servo power so axes can be
repositioned by hand — which also means an unsupported axis can fall under its
own weight. Read [safety](safety.md) before disabling motors.

### Gripper

**Gripper axes** shows the two gripper axes with live values and `Home`
buttons: `G` (open/close, mm) and `Zg` (up/down, mm).

**Jog gripper axes** provides `Up −Zg` / `Down +Zg` and `Open −G` / `Close +G`
with their own step selectors. **Causes motion.**

**Gripper actions** are `Open Gripper`, `Close Gripper` and `Dock Gripper`
(opens and stows the gripper). A status lamp reports whether a plate is
detected in the gripper. **All three cause motion.**

**Pick and place** moves labware between two deck locations. Choose
`Location A` and `Location B`, then run `Pick A → Place B` or
`Pick B → Place A`. **These run a full transport sequence** — the gripper
descends, grips, lifts, traverses and sets down. Confirm both locations hold
what you think they hold before running it.

**Gripper teaching** calibrates the gripper against a specific labware type:
choose the `Labware`, set the `Y offset` and `Approach` height, then use
`Teach Y Offset`, `Approach` and `Move`. The latter two **cause motion**.

### Config

**Location configuration** is where the software is told what is on the deck.
Select a `Location` (1–9), a location `Type` (standard platepad or accessory),
and a `Labware` definition, tick `Lidded` and/or `Sealed` as appropriate, then
`Assign Labware`. `Clear Location` removes the assignment.

Accurate deck assignment is not cosmetic. Motion planning, the collision check
that blocks unsafe head moves, tip-box inventory tracking and the 3D view all
read from it. A location that is physically occupied but empty in software is
a collision waiting to happen.

**Deck layout** shows the 3×3 grid with the labware currently assigned to each
position.

**Deck verification** appears when the optional camera is enabled in the
profile. `Verify Deck Setup` compares what the camera sees against what the
software believes is on the deck; `Vision Calibration` opens the calibration
page.

**Accessories** manages hardware attached to a deck location — a barcode
reader or an orbital shaker. Each accessory has a name, ID, type, deck
location, serial port, and a flag for whether it can hold labware. Barcode
readers add a scanner model and a side (east or west); shakers add a default
RPM, a shake direction, an optional temperature-control flag, and `Start` /
`Stop` buttons. **Starting a shaker begins physical agitation.** An optional 3D
model path and Z hint control how the accessory is drawn in the viewport.

### I/O

A read-only diagnostics view of the instrument's discrete signals, refreshed
from the live state stream:

- **Robot status** — robot disable (emergency stop) and motor power fault.
- **Head detection** — whether a head is attached, plus the configured head
  type and a `Refresh` button.
- **Go button** — whether the instrument's go button is pressed.
- **Plate detection** — whether a plate is sensed in the gripper.
- **Motor enable** — per-axis servo power for X, Y, Z and W.

This is the first place to look when an operation refuses to start. See
[troubleshooting](troubleshooting.md).

### Processes

The *Processes* tab runs one high-level operation at a time — the same
operations the workflow designer chains together, executed individually.

Choose a `Location` and confirm the `Labware` assigned there. The **2D
selector** draws the labware's well or tip grid, and is where tip-box pickup
regions are picked directly.

Under **Command parameters**, the `Command` dropdown selects the operation:
Aspirate, Dispense, Mix, Tips On, Tips Off, Stack Plates, Destack Plate, Mount
Plates, Unmount Plate, Delid Plate, Relid Plate, Scan Stack Height, or Read
Barcode. The properties panel below changes to match. `Execute Command` runs
it. **Every command in this list except Read Barcode causes motion.**

Property panels, by command:

- **Aspirate** — volume (µL), pre-aspirate volume, post-aspirate volume,
  liquid class, distance from well bottom (mm), dynamic tip extension (mm/µL),
  tip touch, pipette technique.
- **Dispense** — empty tips, volume (µL), blowout volume, liquid class,
  distance from well bottom (mm), dynamic tip retraction (mm/µL), tip touch,
  pipette technique.
- **Mix** — volume, pre-aspirate volume, blowout volume, liquid class, mix
  cycles, dynamic tip extension, aspirate distance, an optional separate
  dispense distance, tip touch, pipette technique.
- **Stack Plates** / **Mount Plates** — base location and source location,
  each showing the plate currently resolved there.
- **Destack Plate** / **Unmount Plate** — source stack location and
  destination pad.
- **Delid Plate** — plate location and lid destination.
- **Relid Plate** — lid location and plate location.
- **Scan Stack Height** — reports the configured plate, its stacking
  thickness, the measured height, the inferred plate count and the rounded
  stacking height.
- **Read Barcode** — reports the scan result.

Mount and Unmount are the stacking pair that also *locks* the two plates
together, so that a later pick-and-place transports both as a unit. Stack and
Destack move a single plate and leave the pair independent.

At the bottom, the **Head mode** panel shows the current head and its active
mode, with a `Set Head Mode` button that opens the head mode selector.

### Profiles

**Profile management** lists the profiles on disk. `Load` activates one,
`Save Settings` writes the current form back to the active profile, and
`Duplicate…` / `Rename…` manage the files. `Reinitialize` re-runs
initialization against the loaded profile — **this causes motion.** There are
also importers that read settings out of legacy instrument profile exports
(`.reg` files and `.dat` directory trees).

**Connection** selects the `Controller` type and its transport settings:

| Controller | Transport |
|---|---|
| Simulation | none |
| Agile (BioNet Ethernet) | Ethernet |
| Agile 7612 (Ethernet) | Ethernet |
| Bravo SRT (Ethernet) | Ethernet |
| Darwin (Ethernet) | Ethernet |
| Darwin (Serial) | serial port |

Ethernet controllers take an IP address, a machine ID and a device ID, and
offer `Find Available Device` — a discovery scan that lists the devices it can
see with their device ID, type, IP address, MAC address and status, optionally
restricted to one network adapter. Serial controllers take a port name.

**Miscellaneous** holds the safety and behaviour preferences: approach height,
Z safe position, whether to prompt for a W home on first initialization,
medium-speed operation, always moving to safe Z, ignoring the plate sensor
during pick and place, tips-off tip touch, and the deck-verification camera
settings.

**Head information** sets the installed `Head type` and whether to check the
head type on initialize. `Change Head Wizard` walks through a physical head
swap: it warns that the instrument will move to the highest point of location
5, then prompts you to power off, clear all labware, remove the old head,
select the newly installed head, power back on and finish. **The first step
causes motion.**

---

## Head modes and tip selection

A head with many barrels does not have to use all of them. The *head mode* is
the subset of barrels treated as active for tip pickup, tip return and liquid
handling. It is set from the head mode selector (*Processes* tab →
`Set Head Mode`) and read back through the live state.

The selector has three inputs:

- **Subset** — `All barrels`, `Single barrel`, `Full row`, `Full column`, or
  `Rectangle`.
- **Orientation** — which corner the subset is anchored to: front left, front
  right, back left, or back right. `All barrels` always anchors back left.
- **Count** — how many rows or columns (or, for a rectangle, both), shown only
  for the modes that need it.

A preview grid draws the active barrels and marks the anchor corner. Two
shortcuts sit beside it: `Suggest From Labware` proposes a mode based on the
installed head and the labware assigned at a location, and `Reset To All`
returns to the full head.

Head geometry follows the installed head type. The 384-barrel heads and the
384 pin tool are 16 × 24 on a 4.5 mm pitch; the 1536 pin tool is 32 × 48 on a
2.25 mm pitch; the 8- and 16-channel single-column heads are 8 × 1 and 16 × 1;
everything else is 8 × 12 on a 9 mm pitch. Modes that cannot apply to a given
geometry — a row subset on a single-column head, for instance — fall back to
all barrels.

The suggestion logic is straightforward: if the labware's well count matches
the head's channel count, use the full head; a 96-channel head on a 384-well
plate is suggested an 8 × 12 rectangle; an 8-channel head on a 96- or 384-well
plate is suggested a full column; a 16-channel head on a 384-well plate is
suggested a full row.

**Tip selection** is the companion setting: which block of a tip box lines up
with the active subset of the head. It is chosen from the 2D selector on the
*Processes* tab, and is validated against head mode, tip-box geometry and the
box's current inventory. The rules are:

- The selected block must match the active subset's row and column counts.
- For pickup, every well in the block must still hold a tip.
- For return, every well in the block must be empty.
- No tips may remain beyond the block on the side the head approaches from, so
  that partial boxes are consumed from one corner inward rather than leaving
  the head straddling occupied wells.

The selector only offers anchors that satisfy all of these, so an illegal
selection cannot be made by clicking. Full-head modes always anchor at the
back-left corner and need no selection.

Head mode and tip selection persist as instrument state: whatever is set here
applies to subsequent tip and liquid-handling operations until it is changed.

---

## Deck model and labware

The deck is nine locations in a 3 × 3 grid, numbered 1–9. Each location holds
a stack of labware — usually one plate, sometimes a plate with a lid, a plate
mounted on another plate, or a taller stack built by repeated stacking
operations.

Labware definitions come from the catalog managed at `/labware-editor`. The
dashboard has two views, **Labware Entries** and **Labware Classes**. An entry
is edited across several tabs:

- **Plate Properties** — physical dimensions and stacking geometry.
- **Pipette/Well Definition** — the well grid and per-well geometry used to
  compute pipetting positions.
- **Image** — a 2D image asset for the UI.
- **PF400** and **Planar Motor** — gripper and transport parameters for those
  integrations.

Labware classes group compatible entries, which is how lid and plate
compatibility is expressed.

Assigning labware to a location, in either the control panel's *Config* tab or
the designer's deck panel, records the definition plus whether the item is
lidded or sealed, and for tip boxes which tip definition it holds.

---

## Tips, liquid classes and pipette techniques

**Tip definitions** are edited at `/tip-editor`. Each definition has a tip ID,
a display label, a nominal volume in µL, a tip length in mm, an optional 3D
model path, a source field, and the list of heads it is compatible with. Tip
length feeds directly into teaching and into every Z position the software
computes, so it has to be right.

**Liquid classes** are edited at `/liquid-class-editor`. A liquid class is
resolved against a context — machine ID, head type, tip definition and tip
capacity — shown at the top of the editor, so the same class name can carry
different tuning on different instruments or with different tips. Each class
has a name and notes plus three tabs:

- **Aspirate** — plunger velocity, acceleration and post-delay, and the Z-axis
  velocities and accelerations for moving into and out of the liquid.
- **Dispense** — the same set of parameters for the dispense stroke. A
  `Copy values to dispense` / `Copy values to aspirate` button moves settings
  between the two.
- **Equation** — the volume-correction relationship, with a
  `Reset to identity` button.

Changes are committed with `Save changes` or `Save changes as…`.

**Pipette techniques** are managed in the same page. A technique is a reusable
motion pattern layered on top of an aspirate or dispense: a radius in mm, a
number of segments, the Z phase it applies to (entering the liquid, exiting,
or both), a direction, and flags for whether it applies on aspirate, on
dispense, or both. Techniques are referenced by name from any operation that
accepts one, which keeps motion tuning out of individual commands.

Both liquid classes and pipette techniques are validated before a workflow
runs: a reference to a name that does not exist in the current tip and head
context stops the run before any motion, and names the offending nodes.

---

## Live state readout

The control panel subscribes to a WebSocket state stream at `/ws/state`, and
the same snapshot is available as a single request from `GET /api/state`. It
updates roughly 30 times a second in simulation and about five times a second
on hardware, where reads are throttled to avoid competing with motion.

The snapshot carries:

- **Connection** — `connected`, `initialized`, `controller_type`,
  `machine_id`.
- **Motion** — `positions` for every available axis, `motors_enabled` per
  axis, `engine_busy`, and `teachpoints` for all nine locations.
- **Head and tips** — `head_type`, `head_attached`, `head_mode`,
  `tip_selection`, `plate_selection`, plus `tips_on_head`,
  `tips_on_head_mode` and `tips_on_head_selection` describing what is
  physically on the head right now, and `active_tip_id`,
  `active_tip_capacity_ul`, `tip_labware`, `tip_definition_id` and
  `attached_tip_length_mm` describing which tip definition is in play.
- **Deck** — `deck` (labware names per location), `deck_details` (full
  metadata, including whether each item is mounted to the one below it), and
  `tipbox_inventory`.
- **Discretes** — `go_button_pressed`, `plate_in_gripper`, `robot_disabled`.
- **Task** — `task_status` with the running task's current step name, step
  index, step count, status, and error details when a step has failed.
- **Telemetry** — controller-specific diagnostic values.

Full field-by-field documentation is in the [API reference](api-reference.md).

---

## Operator prompts

Some failures pause the run and wait for a decision rather than aborting:

- **Operator action required** — a task step failed. The choices are `Retry`
  (run the step again), `Ignore` (skip it and continue), and `Abort`. Ignoring
  a failed step continues with an instrument in a state the software may no
  longer be tracking correctly; prefer retry or abort unless you understand
  exactly what was skipped.
- **Pickup verification failed** — the gripper may not have acquired a plate.
  The choices are `Retry Pickup`, `Ignore And Continue`, and `Abort`.
- **Process blocked** — the requested move would overlap occupied neighbouring
  deck positions. The dialog reports the command, the head mode, the allowed
  top plane, and a grid of the conflicting positions. This is a pre-motion
  check: nothing has moved. Fix the deck assignment or the head mode and try
  again.

The workflow designer surfaces the same prompts during a run, along with two of
its own for Script nodes — see [workflows](workflows.md).

---

## Where to go next

- [Workflows](workflows.md) — building and running multi-step protocols.
- [Configuration](configuration.md) — profiles, environment variables, files.
- [Hardware setup](hardware-setup.md) — networking, discovery, teaching.
- [API reference](api-reference.md) — driving all of this from code.
- [Architecture](architecture.md) — how the pieces fit together.
- [Troubleshooting](troubleshooting.md) and [FAQ](faq.md).
