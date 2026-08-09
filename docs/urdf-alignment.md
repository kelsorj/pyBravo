# URDF head alignment (3D view only)

> **Status: unfinished. The misalignment described here is still present.**
>
> This page is a specification and an investigation record, not a report of
> work completed. `A1_OFFSET` has not been measured, the renderer has not been
> changed, and the head's barrels are still drawn a couple of columns from the
> wells they are over.
>
> Two attempts at a fix were made and both reverted — see "Two alignments, not
> one" below for why they failed, which is the most useful thing on this page
> for whoever picks it up. Nothing in the 3D view is currently worse than it
> was; it is simply not yet better.
>
> This is cosmetic. Real motion is unaffected, so there is no urgency beyond
> the picture being misleading to look at.

> **Scope: the 3D view, and nothing else.**
>
> Real motion is correct. Tips on and tips off work on hardware in full, row,
> column and partial modes, on both 96 and 384 heads. The commanded positions,
> the teachpoints, and the geometry in `pybravo/deck/geometry.py` and
> `pybravo/head_mode.py` are therefore **authoritative and out of scope**.
> Nothing described here may change a commanded position, and no change made
> for the reasons in this document belongs anywhere except
> `frontend/src/robot-scene.js`.
>
> If a proposed change to the 3D view would require editing anything under
> `pybravo/`, the proposal is wrong.

## The problem this exists to solve

In the 3D view the head's barrels are drawn a couple of columns away from the
wells they are actually over. It shows up worst at the edge of an empty tip box,
where the tips visibly hang off the end.

The cause is that the renderer does not know where the head's A1 barrel is. It
guesses, in `_getHeadTipMountFrame`, by taking the bounding box of the bottom
slice of the head mesh and treating its centre as the centre of the barrel
array. That picks up shroud and mounting structure, so the guess is wrong by a
constant — measured at about 10.4 mm in X and 4.5 mm in Y for `HT_384_D_70`.

This document is the definition the renderer should use instead.

## The invariant

> When the robot is commanded to a position, the **rendered** head's A1 barrel
> must appear at the deck position that command implies — for every head type,
> every labware format, and every head mode.

Equivalently, and more usefully as a test: when a tip operation is commanded
against the A1 anchor of a piece of labware, the rendered A1 barrel sits on the
rendered A1 well.

## Two alignments, not one

This is the trap that has already cost a round of work. There are two
relationships, and a change to the drawn tip positions trades one against the
other:

| | what it means | how it is currently satisfied |
|---|---|---|
| tips ↔ head mesh | the drawn tips sit under the head's visible barrels | correct today, because the tips are placed from the mesh bounding box |
| tips ↔ labware | the drawn tips sit over the wells they are commanded to | wrong today, by the constant above |

Moving the drawn tips can only ever satisfy one of these. Fixing this properly
means establishing where A1 is **in the model**, so both hold at once.

**Any verification must check both.** A measurement that only checks tips against
labware will report success while the tips float away from the head.

## What the backend already fixes for us

Read-only facts, established from the code. These are inputs to the render, not
things to change.

**Barrel grids** — `head_geometry_for_type`, `pybravo/head_mode.py`:

| head type | grid (rows × cols) | pitch |
|---|---|---|
| `HT_384_D_70`, `_S2`, `HT_384_F_50`, `HT_384_PINTOOL` | 16 × 24 | 4.5 mm |
| 96-format (the fallback) | 8 × 12 | 9.0 mm |
| `HT_1536_PINTOOL` | 32 × 48 | 2.25 mm |
| `HT_16_D_ST` | 16 × 1 | 4.5 mm |
| `HT_8_D_LT` | 8 × 1 | 9.0 mm |

**Corner and axis conventions** — pinned by the Head Mode picker, which draws
BACK as −Y, LEFT as −X, and marks the anchor corner at back-left:

- A1 is the **back-left** barrel: minimum X, minimum Y.
- Column index increases toward **+X** (right).
- Row index increases toward **+Y** (front).

**Head A1 offsets between formats** — from the hardware, not from code: the
384 head's A1 barrel sits **2.25 mm left and 2.25 mm back** of the 96 head's A1
barrel. The two grids are concentric at different pitches, so their A1 corners
do not coincide.

## The definition to establish

One constant per head type, expressed in the head link's own frame:

> **`A1_OFFSET[head_type]`** — the position of the centre of barrel A1
> (row 0, column 0) relative to the origin of the URDF link the head model is
> attached to, in millimetres, as (x, y).

Everything else follows without further constants:

```
barrel(row, col) = A1_OFFSET[head_type] + (col * pitch_x, row * pitch_y)
```

with pitch from the table above, and the axis directions as stated.

| head type | `A1_OFFSET` (x, y) mm | status |
|---|---|---|
| `HT_384_D_70` | _to be measured_ | see procedure below |
| 96-format | _to be measured_ | expected to differ from the 384 value by (+2.25, +2.25) |
| others | _not yet needed_ | add when a model exists for them |

The 2.25 mm relationship above is a useful cross-check: once both are measured,
their difference should come out at (2.25, 2.25). If it does not, one of the two
measurements is wrong.

## Procedure for measuring `A1_OFFSET`

To be done in simulation, by jogging — no hardware, no teachpoints touched.

1. Load a tip box of the matching format at a known deck location, so the
   rendered wells give a reference grid.
2. Command the robot to the position that puts A1 on that box's A1 well — the
   A1 anchor of a tip operation at that location.
3. Read where the head **mesh's** own barrels are drawn, not the synthetic
   tips. The mesh is the ground truth for where the physical barrels are; the
   tips are what we are trying to place correctly.
4. `A1_OFFSET` is the position of the mesh's back-left barrel expressed in the
   head link's local frame.
5. Repeat at a second, distant location. The value must come out the same — it
   is a property of the model, not of position. If it drifts, the deck mapping
   and the URDF kinematics disagree about scale, which is a different problem.
6. Repeat for the 96 head and check the (2.25, 2.25) difference.

## Verification

A fix is only complete when all of these hold:

1. **tips ↔ labware**: the rendered A1 barrel sits on the rendered A1 well when
   commanded to the A1 anchor, for both head formats.
2. **tips ↔ head mesh**: the rendered tips sit under the head mesh's own
   barrels — checked visually and by comparing tip positions against mesh
   barrel positions.
3. **Pose independence**: both hold at several positions across the deck, not
   just the one used to derive the constant.
4. **Mode independence**: both hold for full, row, column and partial modes.
5. **`git diff --name-only -- pybravo/` is empty.** No Python changed, so no
   commanded position can have changed.

## The Z axis has the same problem, and the mechanism to fix it already exists

The head also renders at the wrong height. Measured with a 384 tip box (50 mm)
on the deck, `HT_384_D_70`, tips on the head:

| commanded Z | head mesh bottom | box top | error |
|---|---|---|---|
| 115.1 (Tips On, seated on the box) | 36.0 mm | 80.1 mm | **44.1 mm too low** |
| 100.9 (Tips Off, 14 mm clear) | 50.2 mm | 80.1 mm | **44.1 mm too low** |

Two things follow.

**The arithmetic is right.** Moving between those two poses raised the head by
exactly 14.2 mm, which is `tips_off_z_offset: 14.0` from
`config/tip_offsets.yaml`. The backend already resolves the offsets and folds
them into the commanded Z — `deck_surface_z - labware_height`, then the eject
offset — and the view reproduces that motion faithfully. **The tip offsets are
therefore already accounted for and must not be applied again in the renderer;
doing so would double-count them.**

**The error is a constant**, identical at both poses: the model's Z datum does
not correspond to the machine's Z reference. This is the Z analogue of
`A1_OFFSET`.

Unlike X and Y, the mechanism for correcting it is already present.
`JOINT_AXIS_MAP` in `frontend/src/robot-scene.js` carries a `homeOffset` per
joint, and it is calibrated for the axes somebody has already looked at:

```js
'xaxis':         { bravoAxis: 'X',  homeOffset: 193.04, scale:  1 },
'zaxis':         { bravoAxis: 'Z',  homeOffset: 0,      scale: -1 },   // never calibrated
'zaxis-gripper': { bravoAxis: 'Zg', homeOffset: -20,    scale:  1, ... },
```

`zaxis` is the odd one out at zero. With `scale: -1`, raising `homeOffset` by
44.1 raises the rendered head by 44.1 mm, so the measured value is the
candidate. It should be confirmed at several Z positions and against the deck
surface as well as a box top before being adopted, and treated as the same kind
of measured datum as `A1_OFFSET` rather than a magic number.

Worth noting: the backend computes `deck_surface_z` (`executor.py`, in the tips
motion path) but does not send it to the frontend. Sending it would let the
renderer derive the Z datum from the deck rather than carry a constant, which is
the more durable version of this fix.

## Open questions

- `A1_OFFSET` for both head types — the measurement above.
- Whether the head link's origin is a stable reference across URDF revisions, or
  whether the constant should be expressed relative to a named mesh feature.
- Whether the 1536 and single-column heads need entries, or whether they are
  never rendered.

## Notes for whoever picks this up

The renderer currently falls back to `_getHeadTipMountFrame` whenever it lacks
better information, which is the right shape — keep a fallback so the tips are
always drawn somewhere sane, and prefer the defined constant when available.

Be aware that the designer pauses the `/ws/state` socket while a simulation runs
(`setSimulationMode`), so anything the renderer needs from server state must not
depend on that socket being live. A previous attempt at this fix was correct but
never executed for exactly that reason.
