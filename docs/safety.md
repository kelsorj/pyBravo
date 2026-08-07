# Safety

OpenBravo drives a physical robot with motors strong enough to break labware,
destroy a pipette head, and injure a hand. This page is the one document you
should read before connecting to real hardware.

> [!WARNING]
> This software is provided under the Apache License 2.0, **without warranty of
> any kind and without any fitness-for-purpose guarantee**. It is not a
> validated instrument control system, it is not certified for clinical,
> diagnostic, or GxP use, and it has not been safety-certified by any body. You
> are responsible for the safe operation of your instrument.

## The short version

1. Simulate first. Every new or edited protocol runs in `simulation` before it
   runs on metal.
2. Keep a hand near the emergency stop the first time any new motion executes.
3. Verify teachpoints after any mechanical change, head swap, or deck change.
4. Never run a workflow you have not read, including one generated for you.
5. Watch the first full run of any protocol end to end. Do not walk away.

## What can actually go wrong

**Head-into-deck crash.** The Z and W axes carry the pipette head down toward
the deck. A wrong teachpoint, a wrong `ticks_per_eng_unit`, a labware
definition with the wrong height, or a plate that is present when the software
thinks the location is empty will drive the head into a hard surface. This is
the most common way to cause expensive damage, and it happens fast enough that
you cannot react in time. It is prevented by correct configuration, not by
reflexes.

**Crash during homing.** Homing moves axes toward their limits, sometimes at
speed, and it runs before the software knows where anything is. If the deck is
not clear, homing is when you find out. Clear the deck before homing.

The order matters and is enforced in code (`SAFE_HOME_ORDER` in
`pybravo/types.py`): **Z, then Zg, then G, then X and Y, then W.** The head and
gripper must lift before the gantry moves laterally, or an unhomed head sitting
low is dragged sideways through whatever is on the deck. `HomeTask` also tries
to retract Z and dock the gripper first, but both of those steps are skipped
when the axes are not yet homed — which is exactly the cold-start case — so the
ordering is the real guarantee. Do not reorder it without understanding what is
physically above the deck at each step.

Sequence axes on the server, never in a client. `POST /api/home_axis` accepts
`{"axes": ["X", "Y", "Z"]}` and orders the group; issuing one request per axis
bypasses the invariant entirely, because a single-axis request has no order to
correct.

**Gripper and pick-and-place collisions.** Moving labware between locations
assumes the destination is empty and the path is clear. If the deck state in
software disagrees with the deck in front of you, the robot will move as though
its model is correct.

**Tip pickup force.** Tip pickup presses the head down onto a tip box with
deliberate force. A misaligned tip box, the wrong tip definition, or a wrong
head mode turns that force into damage.

**Pinch and crush points.** The moving head and gripper have no light curtain
in most configurations. Do not reach into the work envelope while the
instrument is powered and enabled, even when it appears idle — a queued command
or a network message can start motion with no warning.

**Liquid hazards.** The software does not know what is in your plates. Aspirate
and dispense operations with the wrong volumes or the wrong liquid class can
spray, aerosolize, or cross-contaminate. Apply your own lab's handling rules for
whatever is on the deck.

## Before you connect to hardware

- Read [Hardware setup](hardware-setup.md) and build a profile for your
  specific instrument. Do not reuse another lab's profile: teachpoints, axis
  ranges, and calibration are machine-specific.
- Confirm `controller_type` matches your instrument generation. Speaking the
  wrong protocol to a robot produces undefined behavior.
- Confirm axis ranges and `ticks_per_eng_unit` are right. If ticks-per-unit is
  wrong, a commanded 10 mm move can travel 100 mm.
- Know where the emergency stop is, and confirm it works, before the first move.
- Clear the deck.

## Before each run

- Confirm the deck in software matches the deck in reality: right labware, right
  locations, lids where the protocol expects them.
- Confirm the head type in the profile matches the head physically installed.
  On Agile 7612 instruments the firmware cannot report the head type, so nothing
  will catch this mistake for you.
- Simulate the run and read the motion sequence.
- For a protocol's first run on hardware, consider running it with empty
  labware.

## Working on the code

If you are changing anything in `pybravo/controllers/`, `pybravo/motion/`,
`pybravo/state_machine/`, or `pybravo/darwin/`, you are working on the parts
that can break a machine.

- Never widen an axis range, raise a current limit, or remove a sensor check
  without saying so explicitly in your pull request.
- Homing and tip-pickup sequences carry the most risk. `tests/test_agile_srt_homing.py`
  pins the SRT homing frame sequence precisely so that an accidental change is
  caught; if it fails, treat that as a real signal.
- Prefer failing closed. When state is unknown, stop rather than assume.
- Test in simulation, then on hardware with a clear deck, then in normal use.

## Known limitations that affect safety

- **Head detection is unavailable on Agile 7612 instruments.** The firmware
  supports neither ADC read nor smart head detect, so the head type must be set
  manually in the profile and cannot be verified by software.
- **The W position register is noisy**, with a spread of roughly 5–10 µL between
  reads. Do not build logic that depends on precise in-motion W readings.
- **Controller 2 position reads (G and Zg) are unreliable during motion.**
  At-rest values are correct; in-motion values jump.
- **The deck model is software state.** Nothing physically senses whether a
  plate is where the software believes it is, unless you have the optional
  camera-based deck verification configured.

## Emergency stop

Know your instrument's physical emergency stop before you begin. In software,
`POST /api/abort` stops the running task and `POST /api/workflows/stop` aborts a
running workflow, but **software abort is not a substitute for the physical
emergency stop.** A hung process, a dropped network connection, or a crashed
browser tab cannot stop a moving axis. The hardware stop can.

## Reporting a safety problem

If you find a defect that could cause injury or damage, please report it
privately first — see [SECURITY.md](../SECURITY.md). Do not open a public issue
containing a working reproduction that would crash someone else's instrument.
