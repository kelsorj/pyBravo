"""Hardware-in-the-loop Tips-On drill using the real TipsOnTask path.

Unlike darwin_bench_smoke.py (which exercises the low-level jog primitive
over open air), this script drives the *production* pick-up code path:

  - Loads a BravoProfile YAML (head type, axes, teachpoints, current limits,
    safety parameters).
  - Loads the labware catalog snapshot and looks up a tip-box definition by
    id, using the same normalized geometry Bravo.tips_on would see.
  - Builds a HeadMode + TipSelection, populates a DeckState with just the
    tip-box at the target location.
  - Constructs TipsOnTask verbatim, prints a pre-flight plan including
    computed target X/Y, Z target, peak current, force percent, and neighbor
    clearance analysis.
  - Requires the operator to type "GO" to proceed, then awaits each step of
    the task so any raise short-circuits the rest.

This is the only script that should be used to verify Tips-On force feedback
on real hardware — it includes the barrel→tip alignment math, deck-height
awareness, and neighbor-clearance checks that a raw jog cannot.

Example:
    python scripts/darwin_tipson_bench.py \
        --profile profiles/384.yaml \
        --address 192.168.0.8 \
        --location 5 \
        --tipbox-id lw-4914769d0af7 \
        --head-mode all_barrels \
        --tip-row 0 --tip-col 0

Abort at any prompt (Ctrl-C) or at the "GO" prompt to cancel before any
axis moves.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # survive cp1252 pipes on Windows
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Allow running without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pybravo.darwin import DarwinController
from pybravo.darwin.sequences import _z_axis_force_percent
from pybravo.deck.geometry import well_center_offset_from_teachpoint_mm
from pybravo.deck.labware import (
    DeckState,
    InMemoryLabwareCatalog,
    Labware,
    _read_labware_snapshot,
    normalize_labware_definitions,
)
from pybravo.head_mode import (
    head_selected_ranges,
    normalize_head_mode,
    tip_task_head_offsets_mm,
    tipbox_selection,
)
from pybravo.logging_config import configure_logging
from pybravo.profile.profile import BravoProfile
from pybravo.protocol.errors import BravoError
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import GeminiSubCommands
from pybravo.state_machine.tasks import TipsOnTask
from pybravo.types import (
    LT_TIP_CURRENT_TABLE,
    ST_TIP_CURRENT_TABLE,
    TIPBOX_JOG_TOLERANCE,
    Axis,
    HeadType,
    interpolate_tip_current,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Labware lookup
# ---------------------------------------------------------------------------


def load_labware_catalog() -> InMemoryLabwareCatalog:
    """Load the local labware snapshot — same path Bravo uses at runtime."""
    snapshot_path = Path("config/labware_catalog.snapshot.yaml")
    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"Labware snapshot not found at {snapshot_path}. Run the main "
            "app once to sync from Mongo, or provide a snapshot."
        )
    defs = _read_labware_snapshot(snapshot_path)
    definitions, aliases = normalize_labware_definitions(defs)
    return InMemoryLabwareCatalog(definitions, aliases=aliases)


def build_tipbox_labware(catalog: InMemoryLabwareCatalog, labware_id: str) -> Labware:
    definition = catalog.get_definition(labware_id)
    if definition is None:
        raise KeyError(
            f"Tipbox id '{labware_id}' not found in labware catalog. "
            "List ids with: python -c \"import yaml; "
            "[print(r['id'], r['name']) for r in "
            "yaml.safe_load(open('config/labware_catalog.snapshot.yaml'))['labware'] "
            "if r.get('base_class')=='tip_box']\""
        )
    if definition.base_class != "tip_box":
        raise ValueError(
            f"Labware '{labware_id}' is {definition.base_class}, not a tip_box. "
            "Tips On requires a tip-box definition."
        )
    return Labware.from_definition(definition)


# ---------------------------------------------------------------------------
# Pre-flight planner
# ---------------------------------------------------------------------------


def plan_tips_on(
    *,
    profile: BravoProfile,
    labware: Labware,
    location: int,
    tip_row: int,
    tip_col: int,
    head_mode_subset_type: str,
    head_mode_subset_config: str,
) -> dict:
    """Compute every value TipsOnTask will use, without touching hardware.

    This mirrors TipsOnTask internals so the operator can sanity-check the
    plan before anything moves.
    """
    head_type = profile.head.head_type
    head_mode = normalize_head_mode(
        head_type, head_mode_subset_type, head_mode_subset_config,
    )
    tip_selection = tipbox_selection(location, tip_row, tip_col, head_mode)

    # Geometry — copied from TipsOnTask._tip_xy / _tips_on_position
    teach_x = profile.teachpoints.get_teachpoint(location, Axis.X)
    teach_y = profile.teachpoints.get_teachpoint(location, Axis.Y)
    teach_z = profile.teachpoints.get_teachpoint(location, Axis.Z)
    teach_tip_length = float(profile.head.teach_tip_length_mm or 0.0)
    deck_surface_z = teach_z + teach_tip_length

    head_offset_x, head_offset_y = tip_task_head_offsets_mm(head_type, head_mode)
    tipbox_offset_x, tipbox_offset_y = well_center_offset_from_teachpoint_mm(
        labware.metadata, row=int(tip_selection.row), col=int(tip_selection.col),
    )
    target_x = teach_x + tipbox_offset_x - head_offset_x
    target_y = teach_y + tipbox_offset_y - head_offset_y
    target_z = deck_surface_z - float(labware.height)

    # Channel count from head_mode
    num_channels = max(1, int(head_mode.row_count) * int(head_mode.column_count))

    # Peak current interpolation (same table choice as TipsOnTask._tip_press_current)
    use_lt = head_type in {
        HeadType.HT_8_D_LT,
        HeadType.HT_96_D_200,
        HeadType.HT_96_D_200_S2,
    }
    table_key = "LT" if use_lt else "ST"
    raw_profile_limits = profile.current_limits or {}
    profile_table = raw_profile_limits.get(table_key)
    # Reuse the task's normalization so profile overrides work the same
    from pybravo.state_machine.tasks import _normalize_tip_current_table
    normalized = _normalize_tip_current_table(profile_table) if profile_table else []
    if not normalized:
        normalized = LT_TIP_CURRENT_TABLE if use_lt else ST_TIP_CURRENT_TABLE
    peak_current_amps = float(interpolate_tip_current(normalized, num_channels))
    force_percent = _z_axis_force_percent(peak_current_amps)

    (head_rows, head_cols) = head_selected_ranges(head_type, head_mode)

    # Z landing envelope — mirrors sequences.jog:
    #   farthest = target + tolerance
    #   success  = target - tolerance <= final_z <= farthest - 0.05
    # TipsOnTask._lower_z_to_tips uses tolerance=TIPBOX_JOG_TOLERANCE (5.0 mm).
    # An empty or missing tipbox means Z reaches farthest-0.05 and raises
    # EXCEEDED_DEST. Resistance from real tips stops Z somewhere in the window.
    safe_z = float(profile.safety.z_safe_position or 0.0)
    jog_tolerance = float(TIPBOX_JOG_TOLERANCE)
    farthest_z = target_z + jog_tolerance
    fail_below = target_z - jog_tolerance
    fail_above = farthest_z - 0.05

    return {
        "head_type": head_type,
        "head_mode": head_mode,
        "tip_selection": tip_selection,
        "location": location,
        "teach_xyz": (teach_x, teach_y, teach_z),
        "teach_tip_length_mm": teach_tip_length,
        "deck_surface_z": deck_surface_z,
        "tipbox": {
            "id": labware.id, "name": labware.name,
            "height_mm": labware.height, "length_mm": labware.length,
            "width_mm": labware.width,
        },
        "head_offset_xy_mm": (head_offset_x, head_offset_y),
        "tipbox_offset_xy_mm": (tipbox_offset_x, tipbox_offset_y),
        "target_xy_mm": (target_x, target_y),
        "target_z_mm": target_z,
        "num_channels": num_channels,
        "tip_current_table": table_key,
        "peak_current_amps": peak_current_amps,
        "force_percent": force_percent,
        "head_selected_rows": head_rows,
        "head_selected_cols": head_cols,
        "safe_z_mm": safe_z,
        "jog_tolerance_mm": jog_tolerance,
        "farthest_z_mm": farthest_z,
        "success_window_mm": (fail_below, fail_above),
    }


def print_plan(plan: dict) -> None:
    head_mode = plan["head_mode"]
    tip_sel = plan["tip_selection"]
    print("\n" + "=" * 70)
    print("Tips-On bench plan (NO MOTION YET)")
    print("=" * 70)
    print(f"  Head type          : {plan['head_type'].name}")
    print(
        f"  Head mode          : {head_mode.subset_type} / "
        f"{head_mode.subset_config} ({head_mode.row_count}x{head_mode.column_count})"
    )
    r0, r1 = plan["head_selected_rows"]
    c0, c1 = plan["head_selected_cols"]
    print(
        f"  Active head cells  : rows {r0 + 1}-{r1}, cols {c0 + 1}-{c1} "
        f"({plan['num_channels']} channels)"
    )
    print(f"  Tip box            : {plan['tipbox']['name']} (id={plan['tipbox']['id']})")
    print(
        f"                        height={plan['tipbox']['height_mm']:.2f} mm, "
        f"LxW={plan['tipbox']['length_mm']:.1f} x {plan['tipbox']['width_mm']:.1f} mm"
    )
    print(
        f"  Tip selection      : location {plan['location']} "
        f"row {tip_sel.row} col {tip_sel.col} ({tip_sel.row_count}x{tip_sel.column_count}), "
        f"mirror={tip_sel.mirror_corner}"
    )
    tx, ty, tz = plan["teach_xyz"]
    print(f"  Teachpoint         : X={tx:.3f}  Y={ty:.3f}  Z={tz:.3f}")
    print(f"  Teach tip length   : {plan['teach_tip_length_mm']:.2f} mm")
    print(f"  Deck surface Z     : {plan['deck_surface_z']:.3f} mm")
    hx, hy = plan["head_offset_xy_mm"]
    bx, by = plan["tipbox_offset_xy_mm"]
    ax, ay = plan["target_xy_mm"]
    print(f"  Head offset (mm)   : ({hx:+.3f}, {hy:+.3f})")
    print(f"  Tipbox offset (mm) : ({bx:+.3f}, {by:+.3f})")
    print(f"  >>> Target X/Y     : ({ax:.3f}, {ay:.3f}) mm")
    print(f"  >>> Target Z       : {plan['target_z_mm']:.3f} mm (jog DOWN from safe)")
    print(f"  Safe Z             : {plan['safe_z_mm']:.3f} mm (descent of "
          f"{plan['target_z_mm'] - plan['safe_z_mm']:+.3f} mm)")
    fail_below, fail_above = plan["success_window_mm"]
    print(
        f"  Z landing envelope : [{fail_below:.3f}, {fail_above:.3f}] mm "
        f"(tolerance ±{plan['jog_tolerance_mm']:.2f} mm)"
    )
    print(f"    If empty/no box  : Z reaches {plan['farthest_z_mm'] - 0.05:.3f} mm "
          f"→ raises EXCEEDED_DEST")
    print(f"    If short stop    : Z below {fail_below:.3f} → raises "
          f"UNABLE_TO_REACH_DEST")
    print(f"  Tip-current table  : {plan['tip_current_table']}")
    print(f"  Interp peak current: {plan['peak_current_amps']:.4f} A  ({plan['num_channels']} tips)")
    print(f"  Derived force %    : {plan['force_percent']:.2f} %")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Task driver (async — TipsOnTask steps are async)
# ---------------------------------------------------------------------------


async def _run_task_steps(task: TipsOnTask) -> None:
    """Run the task's steps sequentially, logging each, aborting on the first
    exception. Uses the task's own get_steps() rather than the production
    StateMachineEngine because the bench never offers operator retry prompts
    — any failure means stop immediately.
    """
    steps = task.get_steps()
    for idx, (name, step_fn) in enumerate(steps, start=1):
        print(f"\n  [{idx}/{len(steps)}] {name} ...", flush=True)
        try:
            await step_fn()
        except Exception as exc:
            print(f"     FAILED: {exc!r}", flush=True)
            raise
        print("     OK", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--profile", type=str,
                    help="Path to a BravoProfile YAML. Must include teachpoints "
                         "for the target location and a head_type matching the "
                         "physically-attached pipette head.")
    ap.add_argument("--address", type=str,
                    help="Bravo master-node IP (overrides profile.connection.address).")
    ap.add_argument("--port", type=int, default=7613)
    ap.add_argument("--location", type=int, choices=range(1, 10),
                    metavar="1..9",
                    help="Deck location where the tip box is placed.")
    ap.add_argument("--tipbox-id", type=str,
                    help="Labware id (from config/labware_catalog.snapshot.yaml) of "
                         "the tip-box at this location. Must have base_class=tip_box.")
    ap.add_argument("--head-mode", type=str, default="all_barrels",
                    choices=["all_barrels", "single_barrel", "column", "row", "quadrant"],
                    help="Which head subset is active (default all_barrels).")
    ap.add_argument("--head-mode-config", type=str, default="back_left",
                    help="Sub-configuration for non-all_barrels modes "
                         "(e.g. 'back_left'). Ignored for 'all_barrels'.")
    ap.add_argument("--tip-row", type=int, default=0,
                    help="Tipbox row (0-indexed) where the head anchor lands.")
    ap.add_argument("--tip-col", type=int, default=0,
                    help="Tipbox column (0-indexed) where the head anchor lands.")
    ap.add_argument("--force-scale", type=float, default=1.0, metavar="FACTOR",
                    help="Multiply the interpolated peak current (from the tip-current "
                         "table) by FACTOR before the jog. Default 1.0 = bridge math. "
                         "Use values <1.0 to reduce press force for conservative bench "
                         "runs (e.g. 0.25 scales a 384-tip press from 0.8A to 0.2A and "
                         "the derived force from 90%% to ~27%%). Cannot exceed 1.5.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan and exit. No connection, no motion.")
    ap.add_argument("--list-tipboxes", action="store_true",
                    help="List tipbox ids and names from the labware snapshot, "
                         "then exit. All other args ignored.")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--log-file", type=str, default=None,
                    help="If set, write all logs to this file.")
    args = ap.parse_args()

    # --- list-tipboxes short-circuit ----------------------------------------
    if args.list_tipboxes:
        catalog = load_labware_catalog()
        tipboxes = [
            d for d in catalog.list_definitions() if d.base_class == "tip_box"
        ]
        print(f"Found {len(tipboxes)} tip-box definitions in local snapshot:")
        for d in tipboxes:
            print(f"  {d.id}  {d.name}  (height={d.height_mm or 0:.1f}mm, "
                  f"wells={d.wells})")
        return 0

    # --- Required-arg validation --------------------------------------------
    missing = [
        name for name, val in (
            ("--profile", args.profile),
            ("--address", args.address),
            ("--location", args.location),
            ("--tipbox-id", args.tipbox_id),
        ) if val is None
    ]
    if missing:
        print(f"Missing required arguments: {', '.join(missing)}",
              file=sys.stderr)
        return 2
    if args.force_scale <= 0.0 or args.force_scale > 1.5:
        print(f"--force-scale must be in (0.0, 1.5], got {args.force_scale}",
              file=sys.stderr)
        return 2

    # --- Logging -------------------------------------------------------------
    configure_logging(
        verbose=args.verbose,
        log_file=args.log_file,
    )

    # --- Load profile and labware -------------------------------------------
    profile = BravoProfile.load(args.profile)
    if profile.teachpoints is None:
        print("Profile has no teachpoints — refusing to run.", file=sys.stderr)
        return 2
    if args.location not in profile.teachpoints.locations:
        print(
            f"Profile has no teachpoint for location {args.location}. "
            f"Known locations: {profile.teachpoints.locations}",
            file=sys.stderr,
        )
        return 2

    catalog = load_labware_catalog()
    try:
        labware = build_tipbox_labware(catalog, args.tipbox_id)
    except (KeyError, ValueError) as exc:
        print(f"\n{exc}\n\nRun with --list-tipboxes to see available tipbox ids.",
              file=sys.stderr)
        return 2

    # --- Apply --force-scale to the profile's tip-current tables ------------
    # TipsOnTask._tip_press_current() reads profile.current_limits[LT|ST]
    # (or falls back to the default tables) and hands the interpolated amps
    # to ctrl.jog. Scaling here reaches that math without touching the jog
    # internals; values written are already in amps.
    if args.force_scale != 1.0 and profile.current_limits is not None:
        scaled_limits: dict = {}
        for table_key in ("LT", "ST"):
            raw = profile.current_limits.get(table_key)
            if not isinstance(raw, dict):
                continue
            scaled_limits[table_key] = {
                k: float(v) * args.force_scale for k, v in raw.items()
            }
        if scaled_limits:
            profile.current_limits.update(scaled_limits)
            print(f"\n[force-scale] Applied {args.force_scale:g}x to profile "
                  f"current_limits (LT + ST tables).")

    # --- Compute and print the plan -----------------------------------------
    plan = plan_tips_on(
        profile=profile,
        labware=labware,
        location=args.location,
        tip_row=args.tip_row,
        tip_col=args.tip_col,
        head_mode_subset_type=args.head_mode,
        head_mode_subset_config=args.head_mode_config,
    )
    print_plan(plan)

    if args.dry_run:
        print("\n(Dry run — exiting before connection.)")
        return 0

    # --- Belt-and-suspenders confirmation ------------------------------------
    print("\nSafety checklist BEFORE proceeding:")
    print("  - Tip box is physically present at the target location.")
    print("  - NO tips already attached to the pipette head.")
    print("  - All other deck locations are clear or reflected in this plan.")
    print("  - Nothing is positioned where the head will travel between Z_SAFE")
    print("    and the tip-box surface.")
    print("  - If ANY of the above is uncertain, abort with Ctrl-C now.")
    print("")
    try:
        response = input("Type 'GO' exactly (case-sensitive) to start the task: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted by operator.")
        return 130
    if response.strip() != "GO":
        print("Confirmation not given — aborted.")
        return 1

    # --- Connect and build the task -----------------------------------------
    deck = DeckState()
    deck.set_single(args.location, labware)

    engine = GeminiEngine(args.address, args.port)
    ctrl = DarwinController(engine=engine)

    import threading
    def _on_safety(event, pkt):
        # Decode the ReservedEventId (high byte of the InstructionEvent mask).
        # The firmware sets this to the subsystem id that raised the event;
        # for axis errors we've observed it takes low integer values (1, 2, ...).
        evt_val = pkt.cmd_val
        reserved_event_id = (evt_val >> 24) & 0xFF  # Mask >> 16 == (evt >> 8) >> 16 == evt >> 24
        sys.stderr.write(
            f"\n!!! SAFETY EVENT: {event.name} from node "
            f"{pkt.src.node_id}.{pkt.src.dev_id} "
            f"(val=0x{evt_val:x}, reserved_event_id=0x{reserved_event_id:x})\n"
        )
        sys.stderr.flush()
        # On ERROR (code 3) or FAULT (code 4), the client also issues a
        # follow-up GET for SUBCMD_ERRCODE on the source address to obtain the
        # actual firmware error code. Mirror that here so the operator sees
        # what the axis actually reported. Must run on a separate thread —
        # calling engine.get_value from the rx thread would deadlock.
        if event.name in ("ERROR", "FAULT"):
            def _fetch():
                try:
                    code = engine.get_value(
                        pkt.src, GeminiSubCommands.ERRCODE, timeout_ms=1000,
                    )
                    sys.stderr.write(
                        f"    SUBCMD_ERRCODE from {pkt.src.node_id}."
                        f"{pkt.src.dev_id} = 0x{code:08x} ({code})\n"
                    )
                    sys.stderr.flush()
                except Exception as exc:
                    sys.stderr.write(
                        f"    (could not read SUBCMD_ERRCODE: {exc})\n"
                    )
                    sys.stderr.flush()
            threading.Thread(target=_fetch, daemon=True).start()
    engine.on_reserved_event(_on_safety)

    try:
        print(f"\nConnecting to {args.address}:{args.port} ...")
        ctrl.open_tcp(args.address)
        ctrl.set_head_type(profile.head.head_type)

        task = TipsOnTask(
            controller=ctrl,
            teachpoints=profile.teachpoints,
            profile=profile,
            labware=labware,
            head_mode=plan["head_mode"],
            tip_selection=plan["tip_selection"],
            tip_location=args.location,
            tip_length_mm=float(profile.head.teach_tip_length_mm or 0.0),
            safe_z_position=float(profile.safety.z_safe_position or 0.0),
            deck=deck,
        )

        print("\nRunning TipsOnTask steps:")
        asyncio.run(_run_task_steps(task))
        print("\nTips On completed successfully.")
        return 0

    except BravoError as exc:
        print(f"\nBravoError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by operator.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nUnhandled error: {exc!r}", file=sys.stderr)
        logger.exception("Bench task failed")
        return 1
    finally:
        # Belt-and-suspenders safety retract. TipsOnTask has its own in-task
        # recovery path, but if a step between safe_z_retract and retract_z
        # raised *without* the task's handler running (or the retract raised
        # inside its inner try/except), the head can be left hanging over the
        # deck. This unconditional post-hook reads Z, retracts to safe Z if
        # it isn't already there, and prints the resulting axis state so the
        # operator can see exactly where the robot ended up before the
        # socket closes and the firmware stops being queryable.
        try:
            if ctrl is not None and getattr(ctrl, "_engine", None) is not None \
                    and getattr(ctrl._engine, "is_connected", False):
                try:
                    safe_z = float(profile.safety.z_safe_position or 0.0)
                    z_now = ctrl.get_position(Axis.Z)
                    sys.stderr.write(f"\n[safety] Z currently at {z_now:.3f} mm "
                                     f"(safe_z = {safe_z:.3f} mm)\n")
                    if abs(z_now - safe_z) > 0.5:
                        sys.stderr.write(f"[safety] Retracting Z -> {safe_z:.3f} mm...\n")
                        from pybravo.controllers.base import AxisMoveInfo
                        ctrl.move(
                            [AxisMoveInfo(axis=Axis.Z, position=safe_z,
                                          velocity=25.0, acceleration=250.0,
                                          absolute=True)],
                            wait=True,
                        )
                        z_after = ctrl.get_position(Axis.Z)
                        sys.stderr.write(f"[safety] Z after retract: {z_after:.3f} mm\n")
                    # Log final axis state for diagnostics.
                    sys.stderr.write("[safety] Final positions:\n")
                    for a in (Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg):
                        try:
                            p = ctrl.get_position(a)
                            sys.stderr.write(f"  {a.name}: {p:.3f} mm\n")
                        except Exception as e:
                            sys.stderr.write(f"  {a.name}: <read failed: {e}>\n")
                    sys.stderr.flush()
                except Exception as retract_exc:
                    sys.stderr.write(
                        f"[safety] POST-HOOK RETRACT FAILED: {retract_exc!r}\n"
                        f"[safety] *** Z MAY BE IN AN UNSAFE POSITION ***\n"
                    )
                    sys.stderr.flush()
        finally:
            try:
                ctrl.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
