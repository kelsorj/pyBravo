"""Hardware-in-the-loop pick-and-place drill using the real PickPlaceTask path.

Drives the *production* pick-and-place code path end-to-end on a real DARWIN:

  - Loads a BravoProfile YAML (head type, axes, teachpoints, gripper, safety).
  - Loads the labware catalog snapshot and looks up a microplate definition
    by id (e.g. the 1536 Labcyte LP-0400 LDV at id lw-3918306f45b8).
  - Populates a DeckState with just the source plate at --from-location; the
    destination is left empty (get_stacking_height returns 0 for empty stacks,
    which is what PickPlaceTask expects).
  - Prints a two-phase pre-flight plan: first purely geometric (dry-run stops
    here); then, after connecting, the solved plan via task.debug_plan() which
    factors in live Z. Operator confirms with "GO" before any axis moves.
  - Runs all 10 steps of PickPlaceTask sequentially, raising on the first
    error.
  - `finally` post-hook: unconditionally retracts Z to safe_z and opens the
    gripper before closing the socket, even on failure. Logs all axis
    positions so the operator can see exactly where the robot ended up.

Example:
    python scripts/darwin_pickplace_bench.py --profile profiles/384.yaml --address 192.168.0.8 --from-location 5 --to-location 6 --labware-id lw-3918306f45b8 --speed SLOW --dry-run

Abort at any prompt (Ctrl-C) or at the "GO" prompt to cancel before any motion.
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

from pybravo.controllers.base import AxisMoveInfo
from pybravo.darwin import DarwinController
from pybravo.deck.labware import (
    DeckState,
    InMemoryLabwareCatalog,
    Labware,
    _read_labware_snapshot,
    normalize_labware_definitions,
)
from pybravo.logging_config import configure_logging
from pybravo.profile.profile import BravoProfile
from pybravo.protocol.errors import BravoError
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import GeminiSubCommands
from pybravo.state_machine.tasks import (
    _GRIPPER_RECESS_DEPTH,
    _PICK_PLACE_GRIP_TARGET,
    _PICKUP_FAILURE_G_THRESHOLD_MM,
    PickPlaceTask,
    _gripper_head_offsets,
)
from pybravo.types import OPEN_GRIPPER_POSITION, Axis, SpeedLevel

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


def build_plate_labware(catalog: InMemoryLabwareCatalog, labware_id: str) -> Labware:
    """Resolve a microplate definition and build a Labware instance."""
    definition = catalog.get_definition(labware_id)
    if definition is None:
        raise KeyError(
            f"Labware id '{labware_id}' not found in labware catalog. "
            "Run with --list-plates to see available microplate ids."
        )
    if definition.base_class != "microplate":
        raise ValueError(
            f"Labware '{labware_id}' is {definition.base_class}, not a microplate. "
            "Pick-place requires a microplate definition (tip boxes, lids, "
            "and accessories are not supported by this script)."
        )
    return Labware.from_definition(definition)


# ---------------------------------------------------------------------------
# Phase 1 — geometric pre-flight plan (no hardware)
# ---------------------------------------------------------------------------


def plan_pick_place_geometry(
    *,
    profile: BravoProfile,
    labware: Labware,
    from_location: int,
    to_location: int,
) -> dict:
    """Compute the geometric values PickPlaceTask will use, WITHOUT hardware.

    The full Z/Zg solution needs live get_position(Axis.Z) — that happens in
    phase 2 via task.debug_plan() post-connect. Phase 1 covers everything the
    operator should see before even deciding to connect.
    """
    head_type = profile.head.head_type
    gripper_y = float(profile.gripper.y_offset or 0.0)
    _, head_y = _gripper_head_offsets(head_type)
    y_correction = gripper_y + head_y

    src_tx = profile.teachpoints.get_teachpoint(from_location, Axis.X)
    src_ty = profile.teachpoints.get_teachpoint(from_location, Axis.Y)
    src_tz = profile.teachpoints.get_teachpoint(from_location, Axis.Z)
    dst_tx = profile.teachpoints.get_teachpoint(to_location, Axis.X)
    dst_ty = profile.teachpoints.get_teachpoint(to_location, Axis.Y)
    dst_tz = profile.teachpoints.get_teachpoint(to_location, Axis.Z)

    return {
        "head_type": head_type,
        "gripper_y_offset": gripper_y,
        "head_y_offset": head_y,
        "y_correction": y_correction,
        "grip_current_amps": float(profile.gripper.grip_current or 0.0),
        "ignore_plate_sensor": bool(
            getattr(profile.safety, "ignore_plate_sensor", False)
        ),
        "safe_z_mm": float(profile.safety.z_safe_position or 0.0),
        "plate": {
            "id": labware.id,
            "name": labware.name,
            "height_mm": labware.height,
            "stack_height_mm": labware.stack_height,
            "gripper_offset_mm": labware.gripper_offset,
            "length_mm": labware.length,
            "width_mm": labware.width,
        },
        "from_location": from_location,
        "source_teach_xyz": (src_tx, src_ty, src_tz),
        "source_target_xy": (src_tx, src_ty + y_correction),
        "to_location": to_location,
        "dest_teach_xyz": (dst_tx, dst_ty, dst_tz),
        "dest_target_xy": (dst_tx, dst_ty + y_correction),
    }


def print_geometric_plan(plan: dict) -> None:
    print("\n" + "=" * 70)
    print("Pick-Place bench plan (NO MOTION YET) — Phase 1: geometry")
    print("=" * 70)
    print(f"  Head type           : {plan['head_type'].name}")
    print(f"  Gripper Y offset    : {plan['gripper_y_offset']:+.3f} mm (profile)")
    print(f"  Head Y offset       : {plan['head_y_offset']:+.3f} mm (from head type)")
    print(f"  Combined Y correct. : {plan['y_correction']:+.3f} mm (applied to pick/place Y)")
    print(f"  Grip current        : {plan['grip_current_amps']:.3f} A  (profile.gripper)")
    print(f"  ignore_plate_sensor : {plan['ignore_plate_sensor']}")
    print(f"  safe_z              : {plan['safe_z_mm']:.3f} mm")
    p = plan["plate"]
    print(f"  Plate               : {p['name']}  (id={p['id']})")
    print(
        f"                        height={p['height_mm']:.2f} mm, "
        f"stack_height={p['stack_height_mm']:.2f} mm, "
        f"gripper_offset={p['gripper_offset_mm']:.2f} mm"
    )
    print(f"                        footprint = {p['length_mm']:.1f} x {p['width_mm']:.1f} mm")
    sx, sy, sz = plan["source_teach_xyz"]
    stx, sty = plan["source_target_xy"]
    print(
        f"  Source (loc {plan['from_location']:>2})     : "
        f"teach=({sx:.3f}, {sy:.3f}, {sz:.3f})  "
        f"target XY=({stx:.3f}, {sty:.3f})"
    )
    dx, dy, dz = plan["dest_teach_xyz"]
    dtx, dty = plan["dest_target_xy"]
    print(
        f"  Dest   (loc {plan['to_location']:>2})     : "
        f"teach=({dx:.3f}, {dy:.3f}, {dz:.3f})  "
        f"target XY=({dtx:.3f}, {dty:.3f})"
    )
    print("  Safety constants:")
    print(f"    Zg nesting depth         = {_GRIPPER_RECESS_DEPTH:.2f} mm")
    print(f"    Grip target (G)          = {_PICK_PLACE_GRIP_TARGET:.2f} mm")
    print(f"    Pickup-fail G threshold  = {_PICKUP_FAILURE_G_THRESHOLD_MM:.2f} mm")
    print(f"    Open gripper position    = {OPEN_GRIPPER_POSITION:.2f} mm")
    print(
        "  Pickup verification : If ctrl.is_plate_in_gripper raises "
        "NotImplementedError\n"
        "                        (current DarwinController state), the task "
        "catches it and\n"
        "                        falls back to the G-rule "
        f"(G >= {_PICKUP_FAILURE_G_THRESHOLD_MM:.1f} mm after close => "
        "pickup failed)."
    )
    print("=" * 70)


# ---------------------------------------------------------------------------
# Phase 2 — solved plan (post-connect)
# ---------------------------------------------------------------------------


def print_solved_plan(plan: dict) -> None:
    """Pretty-print the dict returned by PickPlaceTask.debug_plan().

    Requires the task to be constructed AFTER ctrl.open_tcp — its __init__
    calls _calculate_positions which reads ctrl.get_position(Axis.Z).
    """
    print("\n" + "=" * 70)
    print("Phase 2: solved plan (post-connect, live Z)")
    print("=" * 70)
    print(f"  Head type           : {plan['head_type']}")
    print(
        f"  Teach tip id/len    : {plan['teach_tip_id']} "
        f"/ {plan['teach_tip_length_mm']:.3f} mm"
    )
    print(
        f"  Plate               : {plan['labware_name']}  "
        f"height={plan['plate_height_mm']:.2f} mm  "
        f"stack={plan['stack_height_mm']:.2f} mm  "
        f"gripper_off={plan['gripper_offset_mm']:.2f} mm"
    )
    print(f"  Source (loc {plan['from_location']:>2}):")
    print(f"    teach Z           = {plan['source_teach_z']:.3f} mm")
    print(f"    top of stack      = {plan['source_top_z']:.3f} mm")
    print(f"    grip plane Z      = {plan['source_grip_plane_z']:.3f} mm")
    print(f"    pick height       = {plan['source_pick_height_mm']:.3f} mm")
    print(f"    support height    = {plan['source_support_height_mm']:.3f} mm")
    print(f"  Dest   (loc {plan['to_location']:>2}):")
    print(f"    teach Z           = {plan['dest_teach_z']:.3f} mm")
    print(f"    top of stack      = {plan['dest_top_z']:.3f} mm")
    print(f"    stack height      = {plan['dest_stack_height_mm']:.3f} mm")
    print(f"    support height    = {plan['dest_support_height_mm']:.3f} mm")
    print("  Solved Z/Zg targets:")
    print(f"    pick  Z={plan['pick_z']:+.3f}  Zg={plan['pick_zg']:+.3f}")
    print(f"    carry Z={plan['carry_z']:+.3f}  Zg={plan['carry_zg']:+.3f}")
    print(f"    place Z={plan['place_z']:+.3f}  Zg={plan['place_zg']:+.3f}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Task driver (async — PickPlaceTask steps are async)
# ---------------------------------------------------------------------------


async def _run_task_steps(task: PickPlaceTask) -> None:
    """Run the task's steps sequentially, logging each, aborting on the first
    exception. Bench never offers operator retry prompts — any failure means
    stop immediately.
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
                    help="Path to a BravoProfile YAML. Must include teachpoints for "
                         "both locations and a head_type matching the physically-"
                         "attached pipette head.")
    ap.add_argument("--address", type=str,
                    help="Bravo master-node IP.")
    ap.add_argument("--port", type=int, default=7613)
    ap.add_argument("--from-location", type=int, choices=range(1, 10),
                    metavar="1..9",
                    help="Deck location where the plate currently sits.")
    ap.add_argument("--to-location", type=int, choices=range(1, 10),
                    metavar="1..9",
                    help="Deck location to move the plate to. Must be empty.")
    ap.add_argument("--labware-id", type=str,
                    help="Labware id of the plate to move (from "
                         "config/labware_catalog.snapshot.yaml). Must be a "
                         "microplate. Run with --list-plates to see ids.")
    ap.add_argument("--speed", type=str, default="SLOW",
                    choices=["FAST", "MED", "SLOW"],
                    help="SpeedLevel for the pick-place moves (default SLOW). "
                         "PickPlaceTask internally promotes SLOW to MED for the "
                         "grip action, so this only affects XY/Z/Zg travel speed.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan and exit. No connection, no motion.")
    ap.add_argument("--list-plates", action="store_true",
                    help="List microplate ids and names from the labware snapshot, "
                         "then exit. All other args ignored.")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--log-file", type=str, default=None,
                    help="If set, write all logs to this file.")
    args = ap.parse_args()

    # --- list-plates short-circuit -----------------------------------------
    if args.list_plates:
        catalog = load_labware_catalog()
        plates = [
            d for d in catalog.list_definitions() if d.base_class == "microplate"
        ]
        print(f"Found {len(plates)} microplate definitions in local snapshot:")
        for d in plates:
            print(f"  {d.id}  {d.name}  "
                  f"(height={d.height_mm or 0:.1f}mm, wells={d.wells})")
        return 0

    # --- Required-arg validation -------------------------------------------
    missing = [
        name for name, val in (
            ("--profile", args.profile),
            ("--address", args.address),
            ("--from-location", args.from_location),
            ("--to-location", args.to_location),
            ("--labware-id", args.labware_id),
        ) if val is None
    ]
    if missing:
        print(f"Missing required arguments: {', '.join(missing)}",
              file=sys.stderr)
        return 2
    if args.from_location == args.to_location:
        print(f"--from-location and --to-location must differ "
              f"(both are {args.from_location}).", file=sys.stderr)
        return 2

    # --- Logging -----------------------------------------------------------
    configure_logging(
        verbose=args.verbose,
        log_file=args.log_file,
    )

    # --- Load profile ------------------------------------------------------
    profile = BravoProfile.load(args.profile)
    if profile.teachpoints is None:
        print("Profile has no teachpoints — refusing to run.", file=sys.stderr)
        return 2
    for required_loc in (args.from_location, args.to_location):
        if required_loc not in profile.teachpoints.locations:
            print(
                f"Profile has no teachpoint for location {required_loc}. "
                f"Known locations: {profile.teachpoints.locations}",
                file=sys.stderr,
            )
            return 2

    # --- Resolve plate in catalog ------------------------------------------
    catalog = load_labware_catalog()
    try:
        labware = build_plate_labware(catalog, args.labware_id)
    except (KeyError, ValueError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    # --- Phase 1 plan (no hardware) ----------------------------------------
    geometric = plan_pick_place_geometry(
        profile=profile,
        labware=labware,
        from_location=args.from_location,
        to_location=args.to_location,
    )
    print_geometric_plan(geometric)

    if args.dry_run:
        print("\n(Dry run — exiting before connection.)")
        return 0

    # --- Belt-and-suspenders confirmation ----------------------------------
    print("\nSafety checklist BEFORE proceeding:")
    print(f"  - The plate IS physically present at location {args.from_location}.")
    print(f"  - Location {args.to_location} IS empty.")
    print("  - Path between source and destination is CLEAR of obstacles.")
    print("  - NO tips are attached to the pipette head.")
    print("  - Gripper + Zg are free to move through their travel range.")
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

    # --- Build deck state --------------------------------------------------
    deck = DeckState()
    deck.set_single(args.from_location, labware)  # destination intentionally empty

    # --- Connect and register safety callback ------------------------------
    engine = GeminiEngine(args.address, args.port)
    ctrl = DarwinController(engine=engine)

    import threading

    def _on_safety(event, pkt):
        # Decode ReservedEventId (high byte of the InstructionEvent mask) for
        # the operator; on ERROR/FAULT also issue a follow-up GET for
        # SUBCMD_ERRCODE on the source address to reveal the firmware error
        # code. Must run on a separate thread — calling engine.get_value
        # from the rx thread would deadlock.
        evt_val = pkt.cmd_val
        reserved_event_id = (evt_val >> 24) & 0xFF
        sys.stderr.write(
            f"\n!!! SAFETY EVENT: {event.name} from node "
            f"{pkt.src.node_id}.{pkt.src.dev_id} "
            f"(val=0x{evt_val:x}, reserved_event_id=0x{reserved_event_id:x})\n"
        )
        sys.stderr.flush()
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

        task = PickPlaceTask(
            controller=ctrl,
            teachpoints=profile.teachpoints,
            profile=profile,
            deck=deck,
            from_location=args.from_location,
            to_location=args.to_location,
            speed=SpeedLevel[args.speed],
        )
        print_solved_plan(task.debug_plan())

        print("\nRunning PickPlaceTask steps:")
        asyncio.run(_run_task_steps(task))
        print("\nPick-and-Place completed successfully.")
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
        # Report-only safety post-hook. We intentionally do NOT issue any G
        # (gripper) move or force-move here: after a pick-place step failure
        # the gripper is often in an intermediate state, and slamming it to
        # any default position can trip the firmware pos-error guard
        # (errcode 0x00050003) and walk the fingers off their rail. The
        # operator must visually inspect the gripper and decide the safe
        # recovery path (typically: re-home, then an explicit open_gripper
        # with an operator-verified target).
        #
        # We do still retract Z to safe_z, because leaving Z hovering over
        # the deck is strictly worse than pulling it out of the way — and a
        # Z-only move has no gripper-finger-walk-off failure mode.
        try:
            if ctrl is not None and getattr(ctrl, "_engine", None) is not None \
                    and getattr(ctrl._engine, "is_connected", False):
                safe_z = float(profile.safety.z_safe_position or 0.0)

                # 1) Retract Z to safe_z if it's not already there.
                try:
                    z_now = ctrl.get_position(Axis.Z)
                    sys.stderr.write(f"\n[safety] Z currently at {z_now:.3f} mm "
                                     f"(safe_z = {safe_z:.3f} mm)\n")
                    if abs(z_now - safe_z) > 0.5:
                        sys.stderr.write(
                            f"[safety] Retracting Z -> {safe_z:.3f} mm...\n"
                        )
                        ctrl.move(
                            [AxisMoveInfo(axis=Axis.Z, position=safe_z,
                                          velocity=25.0, acceleration=250.0,
                                          absolute=True)],
                            wait=True,
                        )
                        z_after = ctrl.get_position(Axis.Z)
                        sys.stderr.write(
                            f"[safety] Z after retract: {z_after:.3f} mm\n"
                        )
                except Exception as retract_exc:
                    sys.stderr.write(
                        f"[safety] POST-HOOK Z RETRACT FAILED: {retract_exc!r}\n"
                        f"[safety] *** Z MAY BE IN AN UNSAFE POSITION ***\n"
                    )

                # 2) Log all final positions for the operator. Do NOT move G.
                try:
                    sys.stderr.write("[safety] Final positions (G/Zg NOT moved by hook):\n")
                    for a in (Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg):
                        try:
                            p = ctrl.get_position(a)
                            sys.stderr.write(f"  {a.name}: {p:.3f} mm\n")
                        except Exception as e:
                            sys.stderr.write(f"  {a.name}: <read failed: {e}>\n")
                    sys.stderr.write(
                        "[safety] If the gripper is in an unsafe position, "
                        "DO NOT call open_gripper blindly — inspect first, "
                        "then re-home G before issuing any G move.\n"
                    )
                    sys.stderr.flush()
                except Exception:
                    pass
        finally:
            try:
                ctrl.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
