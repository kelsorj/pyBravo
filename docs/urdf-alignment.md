# URDF head alignment (3D view only)

> **Status: all three axes are fixed.**
>
> - **Z — done.** The head was drawn one teach-tip too low. Corrected via the
>   live `head.teach_tip_length_mm`; see "The Z axis" below.
> - **X and Y — done.** Fixed as measured gantry datums in `JOINT_AXIS_MAP`
>   (`xaxis: 182.64`, `yaxis: 2.3`), not by moving the drawn tips; see "The X/Y
>   datum" below. Two earlier attempts moved the tips instead of the gantry and
>   were reverted — "Two alignments, not one" below explains why that can never
>   work, and remains the most useful thing on this page.
>
> All of this is cosmetic. Real motion is unaffected.
>
> **Confirmation recording:**
> [docs/media/tips-on-off-simulation.mov](media/tips-on-off-simulation.mov) — a
> simulate run captured after the fixes: the head descends aligned with the
> box, the tips leave the box and attach at the bottom of the press, ride up
> with the head, and are released into the destination box at the eject.
> (GitHub plays committed videos from the file's blob page; open the link.)

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

## The problem this existed to solve

In the 3D view the head's barrels were drawn a couple of columns away from the
wells they were actually over — worst at the edge of an empty tip box, where
the tips visibly hung off the end.

## The X/Y datum — diagnosed and fixed

The error was in the gantry's visual datum, not in where the tips sit on the
head. Measured with the head-tip array (placed from the head mesh) against the
rendered wells, at four commanded poses spanning 99 mm of X travel and both
deck rows:

| | error, every pose |
|---|---|
| X | **−10.40 mm** (≈2.3 columns), constant |
| row (robot Y) | **+2.40 / +2.19 mm** per location (mean ≈ half a pitch) |

Constant across the whole span in both axes — a datum error, not scale. Both
derivatives measured exactly 1 mm world per commanded mm, so the correction is
a pure `homeOffset` shift in `JOINT_AXIS_MAP` (`frontend/src/robot-scene.js`):
`xaxis` 193.04 → **182.64**, `yaxis` 0 → **2.3**. After the change the residual
is 0.00 mm in X and ±0.11 mm in row at all four poses — the ±0.11 is the two
locations' real taught difference, which a flat model deck cannot represent.

Why this works where the two reverted attempts could not: `homeOffset` moves
the **whole gantry chain** — head mesh, attached tips, and gripper together, as
on the real machine (they share one carriage; confirmed against the URDF joint
tree, where both hang under `ygantry` ← `xgantry`). The tips↔mesh relationship
is preserved by construction (barrel-to-tip gap 17.9 mm before and after), and
deck pads and labware are static children of the base and do not move. The
earlier attempts moved only the drawn tips, which can satisfy tips↔labware or
tips↔mesh but never both.

The remaining per-location ±0.1 mm class of residual would only be removable by
modelling the deck's taught Z/XY variation, which is not worth it for a
visualization.

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

## How this was defined in the end

The original plan on this page was a per-head-link constant (`A1_OFFSET`,
placing the barrel array within the head link). The fix that actually shipped
is simpler and sits one level up: the error was the same for the whole gantry,
so it lives in the per-joint `homeOffset` datums in `JOINT_AXIS_MAP`, and the
tips stay placed from the head mesh exactly as before. A per-head constant
would only become necessary if a head's *mesh* were misplaced within its own
link — no evidence of that once the gantry datum was corrected.

If a future head model does need a per-link constant, the measurement
procedure is unchanged: command the A1 anchor over a known box, read where the
mesh's barrels render versus the wells, at two distant locations (the value
must be constant), and for the 96/384 pair check their difference comes out at
(2.25, 2.25) — the two grids are concentric at different pitches.

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

## The Z axis — diagnosed and fixed

The head rendered one **teach tip** too low, at every Z.

Robot Z is referenced to the tip of the teach tip, not to the barrel face: the
backend computes `deck_surface_z = teach_z + teach_tip_length`. The renderer
placed the head as though Z meant the barrel, so it sat low by exactly that
length.

Measured against the deck surface, head mesh only, with the synthetic tips
excluded — they are children of the head link and contaminate the measurement
if included, which is how an earlier pass produced a spurious 44.1 mm:

| | before | after |
|---|---|---|
| loc 1 (50 mm box), Z = 0, 50, 100, 115.1, 140, 165.1 | −26.14 mm at every Z | **−0.04 mm** |
| loc 4 (25 mm box), Z = 0, 50, 100, 115.1, 140, 164.9 | −25.94 mm at every Z | **+0.16 mm** |

Constant across Z, independent of labware height, and −26.04 mm on average
against a nominal `teach_tip_length_mm` of 26.1. The residual ±0.1 mm is the
0.20 mm difference between the two locations' taught Z, which a flat model deck
cannot represent — it is real machine variation, not modelling error.

The fix uses the `homeOffset` mechanism already present in `JOINT_AXIS_MAP`,
which at the time was calibrated for X (193.04 — since re-measured to 182.64,
see "The X/Y datum" above) and Zg (−20) but left at 0 for Z. The value
is taken live from `head.teach_tip_length_mm` on `/api/profile` rather than
hardcoded, so a different head or teach tip follows automatically, with the
common 26.1 seeded as a fallback.

Two things were checked because they could have been broken silently:

- **Tips still sit on the head.** The gap between the barrels and the tips'
  bottoms is 17.9 mm before and after. The whole head link moves, so this
  relationship is preserved by construction — unlike the X/Y attempts, which
  moved only the drawn tips.
- **The gripper did not move.** `zaxis-gripper` is coupled to Z, so it could
  have been dragged along; measured at 87.8 mm before and after, unchanged.

### `tip_offsets.yaml` was never the problem

Worth stating plainly, because it is the intuitive place to look. The backend
already resolves `config/tip_offsets.yaml` and folds the result into the
commanded Z: `deck_surface_z - labware_height`, then the eject offset. Moving
between the Tips On and Tips Off poses raised the head by exactly 14.2 mm in the
view, which is `tips_off_z_offset: 14.0` arriving intact.

**Those offsets are therefore already in the Z the renderer receives, and must
not be applied again there — that would double-count them.** What the view was
missing was the datum, not the offsets.

### A more durable version of this fix

The backend computes `deck_surface_z` in the tips motion path (`executor.py`)
but does not send it to the frontend. Sending it would let the renderer derive
the Z datum from the deck directly, rather than reconstructing it from the teach
tip length. Worth doing if this ever drifts again.

## Open questions

- The X/Y/Z datums were measured with the 384 head model. If a 96-head mesh is
  ever added to the URDF, re-run the four-pose sweep with it — the gantry
  datums should hold (they are properties of the gantry, not the head), and any
  residual would indicate the new head mesh is misplaced within its link.
- The datums are tied to this revision of the URDF. If the URDF is re-exported,
  re-measure — the sweep takes minutes and the procedure is above.

## Notes for whoever picks this up

The renderer currently falls back to `_getHeadTipMountFrame` whenever it lacks
better information, which is the right shape — keep a fallback so the tips are
always drawn somewhere sane, and prefer the defined constant when available.

Be aware that the designer pauses the `/ws/state` socket while a simulation runs
(`setSimulationMode`), so anything the renderer needs from server state must not
depend on that socket being live. A previous attempt at this fix was correct but
never executed for exactly that reason.
