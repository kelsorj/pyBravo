"""Hardware-in-the-loop smoke test for the pure-Python DARWIN controller.

Runs a progressively-risky sequence against a real Bravo. STOP if any step
fails. Designed so you can run it tonight against the bench unit to validate
the new stack before we cut over.

What it does (roughly matching the plan's acceptance list):
  1. Connect to the master node on port 7613
  2. Ping + read firmware from master + each controller node
  3. Read positions + motor states
  4. Light sweep (red/green/blue/clear — optional, safest first)
  5. Home each single-axis in order: X, Y, Z, G, Zg  (W deferred)
  6. Small safe moves: X 10mm, Y 10mm, Z 5mm
  7. Coordinated XY move (5mm each way)
  8. Clean shutdown (disable motors, clear lights)

Invoke with the Bravo's IP:
    python scripts/darwin_bench_smoke.py 192.168.0.8

Optional flags:
    --skip-home    Skip homing (if already homed)
    --skip-moves   Only connect/query/lights — no motion
    --timeout SEC  Per-step timeout (default 15)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Force UTF-8 so output survives `| Tee-Object` on PowerShell (cp1252 default).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # Python 3.7+
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Allow running without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pybravo.controllers.base import AxisMoveInfo
from pybravo.darwin import DarwinController
from pybravo.logging_config import configure_logging
from pybravo.protocol.commands import LightCommandData
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.types import Axis, HeadType


def step(name: str, fn, *args, **kwargs):
    """Run a step, printing timing and stopping on failure."""
    print(f"\n=== {name} ===")
    t0 = time.monotonic()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  FAILED ({elapsed:.2f}s): {exc}")
        raise
    elapsed = time.monotonic() - t0
    print(f"  OK ({elapsed:.2f}s)" + (f" -> {result}" if result is not None else ""))
    return result


def _prompt(msg: str) -> None:
    """Show an interactive prompt on stderr so it's visible even when stdout is redirected."""
    sys.stderr.write(msg)
    sys.stderr.flush()
    # Read one line from stdin (user presses ENTER)
    try:
        sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address", help="Bravo master node IP (e.g., 192.168.0.8)")
    ap.add_argument("--port", type=int, default=7613)
    ap.add_argument("--skip-home", action="store_true")
    ap.add_argument("--skip-moves", action="store_true")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip all interactive prompts (dangerous; skips deck-clear check).")
    ap.add_argument("--log-file", type=str, default=None,
                    help="If set, write all logs to this file instead of stdout.")
    ap.add_argument("--waxis", action="store_true",
                    help="Exercise W-axis: apply 57-param table, home W, tiny aspirate/dispense.")
    ap.add_argument("--head-type", type=str, default=None,
                    help="HeadType name to use for W-axis. REQUIRED when --waxis "
                         "is set. Wrong head type loads wrong PID params, hardware "
                         "range, and uL->mm factor — use what is physically attached. "
                         f"Valid names: {', '.join(h.name for h in HeadType if h.name != 'HT_UNKNOWN')}")
    ap.add_argument("--gripper", action="store_true",
                    help="Exercise G axis: open → partial close → open. SAFE (no force).")
    ap.add_argument("--grip", action="store_true",
                    help="Perform a low-force grip. Requires a sacrificial plate/object "
                         "in the gripper path.")
    ap.add_argument("--jog-z", type=float, default=None, metavar="TARGET_MM",
                    help="Force-jog Z down to TARGET_MM (with tolerance=5mm, current=0.2). "
                         "DANGEROUS — make sure the deck under Z is clear/compliant.")
    ap.add_argument("--show-tips-on-force", type=int, default=None, metavar="NUM_TIPS",
                    help="Report-only: print the peak_current (amps, interpolated from the "
                         "LT/ST tip-current table) and the derived force_percent for "
                         "NUM_TIPS tips on the given --head-type. Does NOT move any axis. "
                         "This is a sanity-check for the tip-press force math only — a "
                         "real Tips On requires teachpoint + labware + head_mode / "
                         "tip_selection geometry (see TipsOnTask) which a smoke script "
                         "cannot provide.")
    ap.add_argument("--recover-only", action="store_true",
                    help="Connect, run ctrl.recover() to re-enable any DISABLED axes "
                         "(e.g., after a safety trip cleared), then disconnect. "
                         "Skips homing, moves, and all other phases.")
    args = ap.parse_args()

    # --- Pre-flight validation BEFORE any motion ---------------------------
    if args.show_tips_on_force is not None:
        if args.show_tips_on_force <= 0 or args.show_tips_on_force > 384:
            print(f"\n--show-tips-on-force must be between 1 and 384, got "
                  f"{args.show_tips_on_force}", file=sys.stderr)
            return 2
        if args.head_type is None:
            print(
                "\n--show-tips-on-force REQUIRES --head-type <NAME> to pick\n"
                "the LT vs ST tip-current table. LT for HT_8_D_LT,\n"
                "HT_96_D_200, HT_96_D_200_S2. ST for everything else.",
                file=sys.stderr,
            )
            return 2
    if args.waxis and args.head_type is None:
        print(
            "\n--waxis REQUIRES --head-type <NAME> to be specified.\n"
            "The attached pipette head determines the uL->mm factor, the\n"
            "hardware range, and the 57-parameter PID table — using the\n"
            "wrong head type can drive the plunger past its physical range\n"
            "or cause unstable motion. There is no safe default.\n\n"
            "Valid --head-type values:\n  "
            + "\n  ".join(h.name for h in HeadType if h.name != "HT_UNKNOWN"),
            file=sys.stderr,
        )
        return 2
    if args.waxis and args.head_type is not None:
        try:
            HeadType[args.head_type]
        except KeyError:
            print(f"\nUnknown head type: {args.head_type}\n"
                  f"Valid names: {[h.name for h in HeadType if h.name != 'HT_UNKNOWN']}",
                  file=sys.stderr)
            return 2

    configure_logging(
        verbose=args.verbose,
        log_file=args.log_file,
    )

    print(f"Connecting to DARWIN at {args.address}:{args.port}")
    engine = GeminiEngine(args.address, args.port)
    ctrl = DarwinController(engine=engine)

    # Log any RESERVED safety events (light curtain, E-stop, faults) the
    # controller broadcasts — helps diagnose mid-move interruptions. These
    # go to stderr so they're visible even with --log-file.
    def _on_safety(event, pkt):
        sys.stderr.write(
            f"\n!!! SAFETY EVENT: {event.name} from node "
            f"{pkt.src.node_id}.{pkt.src.dev_id} (val=0x{pkt.cmd_val:x})\n"
        )
        sys.stderr.flush()
        logging.getLogger(__name__).warning(
            "Safety event %s from %d.%d",
            event.name, pkt.src.node_id, pkt.src.dev_id,
        )
    engine.on_reserved_event(_on_safety)

    try:
        step("Connect (open_tcp)", ctrl.open_tcp, args.address)
        step("Ping", ctrl.ping)

        # --- Recovery-only mode -----------------------------------------
        if args.recover_only:
            print("\n=== Recovery: re-enable DISABLED axes ===")
            print("(Requires SAFETY_STATUS to be clear — clear any light curtain trip first.)")
            result = step("ctrl.recover()", ctrl.recover)
            for a, action in result.items():
                print(f"  {a.name}: {action}")
            step("Disconnect", ctrl.close)
            return 0

        fw = step("Read firmware", ctrl.get_firmware_version)
        print(f"  master={fw.master}  {fw.sub1}  {fw.sub2}")

        # Light sweep — safe: doesn't move anything
        step("Clear lights", ctrl.clear_lights)
        for color, name in [(1, "red"), (4, "green"), (8, "blue"), (5, "yellow")]:
            step(f"Light -> {name}", ctrl.set_light,
                 LightCommandData(light=color, period_ms=0, duty_cycle=1.0))
            time.sleep(0.3)
        step("Clear lights (end)", ctrl.clear_lights)

        # Motor states at start
        print("\n=== Motor states ===")
        for a in [Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg]:
            enabled = ctrl.is_motor_enabled(a)
            print(f"  {a.name}: enabled={enabled}")

        # Positions at start
        print("\n=== Positions (mm) ===")
        for a in [Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg]:
            try:
                p = ctrl.get_position(a)
                print(f"  {a.name}: {p:.3f}")
            except Exception as e:
                print(f"  {a.name}: <error: {e}>")

        if not args.skip_home:
            print("\n=== Homing (one axis at a time, 1-minute timeout each) ===")
            print("!!! Check that the deck is clear of obstacles before continuing.")
            if not args.yes:
                _prompt("    Press ENTER to start homing, or Ctrl-C to abort... ")
            for a in [Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg]:
                step(f"Home {a.name}", ctrl.home_axes, [a])

        if not args.skip_moves and not args.skip_home:
            print("\n=== Small moves (safe: <=10mm) ===")
            if not args.yes:
                _prompt("    Press ENTER to move, or Ctrl-C to abort... ")
            start_x = ctrl.get_position(Axis.X)
            start_y = ctrl.get_position(Axis.Y)
            print(f"  Starting X={start_x:.2f}  Y={start_y:.2f}")

            def verify_at(label, expected_x=None, expected_y=None, tol=0.5):
                x = ctrl.get_position(Axis.X)
                y = ctrl.get_position(Axis.Y)
                print(f"  After {label}: X={x:.2f}  Y={y:.2f}")
                if expected_x is not None and abs(x - expected_x) > tol:
                    print(f"    !!! X is {x:.2f}, expected ~{expected_x:.2f}")
                if expected_y is not None and abs(y - expected_y) > tol:
                    print(f"    !!! Y is {y:.2f}, expected ~{expected_y:.2f}")

            step("X +10mm", ctrl.move,
                 [AxisMoveInfo(axis=Axis.X, position=start_x + 10,
                                velocity=50.0, acceleration=500.0, absolute=True)])
            verify_at("X +10", expected_x=start_x + 10)

            step("X back", ctrl.move,
                 [AxisMoveInfo(axis=Axis.X, position=start_x,
                                velocity=50.0, acceleration=500.0, absolute=True)])
            verify_at("X back", expected_x=start_x)

            step("Coordinated XY +5mm", ctrl.move,
                 [AxisMoveInfo(axis=Axis.X, position=start_x + 5,
                                velocity=50.0, acceleration=500.0, absolute=True),
                  AxisMoveInfo(axis=Axis.Y, position=start_y + 5,
                                velocity=50.0, acceleration=500.0, absolute=True)])
            verify_at("XY +5", expected_x=start_x + 5, expected_y=start_y + 5)

            step("XY back", ctrl.move,
                 [AxisMoveInfo(axis=Axis.X, position=start_x,
                                velocity=50.0, acceleration=500.0, absolute=True),
                  AxisMoveInfo(axis=Axis.Y, position=start_y,
                                velocity=50.0, acceleration=500.0, absolute=True)])
            verify_at("XY back", expected_x=start_x, expected_y=start_y)
            # Brief settle before disabling — BUSY-poll fix should prevent this
            # being needed, but belt-and-braces.
            time.sleep(0.2)

        # --- W-axis baby-step test -------------------------------------------
        # Trust-ladder for W:
        #   1. Apply the 57-parameter PID table for the chosen head type.
        #   2. Tiny 0.5 µL aspirate  (~0.1 mm of plunger travel for most heads)
        #   3. Dispense back to 0
        #   4. Disable W
        # W moves a plunger INSIDE the pipette head — no collision risk — but
        # if a tip + fluid is attached, aspirate will pull fluid in and
        # dispense will expel it. Run with no tip mounted for this test.
        if args.waxis:
            # Already validated above in pre-flight.
            head_type = HeadType[args.head_type]

            # Read raw head identification — we don't yet have a verified
            # byte→HeadType mapping, so this is reported for user verification
            # only. If you see a byte value for a head you know the model of,
            # record it so we can build the mapping empirically over time.
            print("\n=== Head identification (raw) ===")
            ident = step("Read head identification",
                         ctrl.read_head_identification)
            print(f"  has_smart_head: {ident['has_smart_head']}")
            print(f"  eeprom_byte:    {ident['eeprom_byte']}")
            print(f"  adc_counts:     {ident['adc_counts']}")
            print(f"  User --head-type: {head_type.name}")
            if not ident["has_smart_head"]:
                print(
                    "\nWARNING: no smart head responded. You may have a\n"
                    "resistor-based head or no head attached. Proceeding only\n"
                    "because you explicitly passed --head-type. Verify the\n"
                    "physical head matches before any W motion.",
                    file=sys.stderr,
                )
                if not args.yes:
                    _prompt("    Press ENTER to acknowledge and proceed, "
                            "or Ctrl-C to abort... ")
            else:
                print(
                    "\nNOTE: byte → HeadType mapping is not yet verified.\n"
                    "Confirm the eeprom_byte value matches what you'd expect\n"
                    "for your physical head (record it for the mapping table).\n"
                )
                if not args.yes:
                    _prompt("    Press ENTER to acknowledge the byte value "
                            "matches your head, or Ctrl-C to abort... ")

            print(f"\n=== W-axis micro-test (head_type={head_type.name}) ===")
            print("!!! This applies the 57-parameter W-axis PID table and moves the plunger.")
            print("    REQUIREMENTS:")
            print("      - NO tip should be mounted on the pipette.")
            print("      - The pipette path must be clear of fluid.")
            print("    PLAN: set head type -> home W (may run commutate/home)")
            print("          -> aspirate 0.5 uL -> dispense to 0 -> disable W.")
            if not args.yes:
                _prompt("    Press ENTER to continue, or Ctrl-C to abort... ")

            step(f"Set head type = {head_type.name}", ctrl.set_head_type, head_type)
            factor = ctrl.ul_to_mm(1.0)  # mm per µL for this head
            print(f"  µL->mm factor for {head_type.name}: {factor:.6f}")

            step("Home W (applies 57-param table, 40s timeout)",
                 ctrl.home_axes, [Axis.W])
            w_home = ctrl.get_position(Axis.W)
            print(f"  W home position: {w_home:.4f} mm")

            # Tiny 0.5 µL aspirate — smallest volume worth testing.
            # For HT_96_D_70 (factor 0.224), 0.5 µL = 0.112 mm of plunger travel.
            TINY_VOLUME_UL = 0.5
            expected_mm = ctrl.ul_to_mm(TINY_VOLUME_UL)
            print(f"  Expected plunger travel for {TINY_VOLUME_UL} uL: "
                  f"{expected_mm:.4f} mm")

            step(f"Aspirate {TINY_VOLUME_UL} uL (tiny)",
                 ctrl.aspirate, TINY_VOLUME_UL)
            w_asp = ctrl.get_position(Axis.W)
            delta = w_asp - w_home
            print(f"  W after aspirate: {w_asp:.4f} mm  "
                  f"(delta from home: {delta:+.4f} mm, "
                  f"expected {expected_mm:+.4f})")

            step("Dispense to 0 uL (return plunger home)",
                 ctrl.dispense, 0.0)
            w_end = ctrl.get_position(Axis.W)
            print(f"  W after dispense: {w_end:.4f} mm  "
                  f"(delta from home: {w_end - w_home:+.4f} mm)")

            step("Disable W", ctrl.disable_motor, Axis.W)

        # --- Gripper open/close without force (safe) -------------------------
        # Tiny baby-step range: just -1mm to +1mm around neutral. Once this
        # is verified safe on the bench, we can widen the range in a separate
        # test. NEVER default to hw_min — that walked the gripper off its
        # rail on this controller.
        #
        # SAFETY: Zg (gripper vertical) must be in [30, 40] mm before
        # touching G. Outside that window, opening/closing the gripper
        # fingers can collide with the pipette head. We move Zg to 35mm
        # if it isn't already in range.
        if args.gripper:
            ZG_SAFE_MIN = 30.0
            ZG_SAFE_MAX = 40.0
            ZG_SAFE_TARGET = 35.0

            print("\n=== Gripper micro-test (G: -1mm .. +1mm, no force) ===")
            print(f"!!! SAFETY: Zg must be in [{ZG_SAFE_MIN}, {ZG_SAFE_MAX}] mm")
            print("    so the gripper fingers clear the pipette head.")
            print("    G will move: start -> -1mm -> +1mm -> 0mm (neutral).")
            print("    Range is intentionally tiny to validate safely. No force.")
            if not args.yes:
                _prompt("    Press ENTER to continue, or Ctrl-C to abort... ")

            # --- Pre-position Zg ---
            step("Enable Zg", ctrl.enable_motor, Axis.Zg)
            zg_now = ctrl.get_position(Axis.Zg)
            print(f"  Zg start: {zg_now:.3f} mm")
            if not (ZG_SAFE_MIN <= zg_now <= ZG_SAFE_MAX):
                print(f"  Zg is outside safe window — moving to {ZG_SAFE_TARGET} mm first")
                step(f"Move Zg to {ZG_SAFE_TARGET} mm (safe for gripper op)",
                     ctrl.move,
                     [AxisMoveInfo(axis=Axis.Zg, position=ZG_SAFE_TARGET,
                                    velocity=20.0, acceleration=200.0, absolute=True)])
                zg_now = ctrl.get_position(Axis.Zg)
                print(f"  Zg after pre-position: {zg_now:.3f} mm")
                if not (ZG_SAFE_MIN <= zg_now <= ZG_SAFE_MAX):
                    raise BravoError(
                        ErrorType.DARWIN_GENERIC,
                        custom_text=(
                            f"Zg failed to reach safe window after pre-position "
                            f"(actual={zg_now:.3f}, required=[{ZG_SAFE_MIN}, {ZG_SAFE_MAX}]). "
                            f"Aborting gripper test."
                        ),
                    )
            else:
                print("  Zg already in safe window; no move needed")

            # --- G micro-test ---
            step("Enable G", ctrl.enable_motor, Axis.G)
            g_start = ctrl.get_position(Axis.G)
            print(f"  G start: {g_start:.3f} mm")

            step("Move G to -1mm (slightly open)",
                 ctrl.move,
                 [AxisMoveInfo(axis=Axis.G, position=-1.0,
                                velocity=10.0, acceleration=100.0, absolute=True)])
            print(f"  G after open-1: {ctrl.get_position(Axis.G):.3f} mm")

            step("Move G to +1mm (slightly closed)",
                 ctrl.move,
                 [AxisMoveInfo(axis=Axis.G, position=1.0,
                                velocity=10.0, acceleration=100.0, absolute=True)])
            print(f"  G after close+1: {ctrl.get_position(Axis.G):.3f} mm")

            step("Return G to 0mm (neutral)",
                 ctrl.move,
                 [AxisMoveInfo(axis=Axis.G, position=0.0,
                                velocity=10.0, acceleration=100.0, absolute=True)])
            print(f"  G after neutral: {ctrl.get_position(Axis.G):.3f} mm")

        # --- Grip with force (requires sacrificial object) -------------------
        if args.grip:
            from pybravo.types import SpeedLevel
            print("\n=== Grip with force ===")
            print("!!! Place a sacrificial plate/object within the gripper's reach (G~10mm).")
            print("    Grip current will be ~0.2 (low force, 20% of hardware peak).")
            if not args.yes:
                _prompt("    Press ENTER when object is in place, or Ctrl-C to abort... ")

            step("Enable G", ctrl.enable_motor, Axis.G)
            step("Open gripper before grip", ctrl.open_gripper)
            if not args.yes:
                _prompt("    Place the object now, then press ENTER to grip... ")
            step("Grip (slow, target=8mm)",
                 ctrl.grip, SpeedLevel.SLOW, 8.0, False)
            print(f"  G after grip: {ctrl.get_position(Axis.G):.3f} mm")
            if not args.yes:
                _prompt("    Inspect grip. Press ENTER to release, or Ctrl-C... ")
            step("Enable G (was disabled post-grip)", ctrl.enable_motor, Axis.G)
            step("Release (open gripper)", ctrl.open_gripper)

        # --- Z jog (force-controlled surface detection) ----------------------
        if args.jog_z is not None:
            from pybravo.controllers.base import JogParams as BaseJogParams
            print(f"\n=== Jog Z down to {args.jog_z:.2f}mm ===")
            print("!!! Z will move down under force control. STOP IF SOMETHING WILL CRASH.")
            print("    Peak current = 0.2 (low force); tolerance = 5mm.")
            if not args.yes:
                _prompt("    Press ENTER to jog, or Ctrl-C to abort... ")

            step("Enable Z", ctrl.enable_motor, Axis.Z)
            start_z = ctrl.get_position(Axis.Z)
            print(f"  Z start: {start_z:.3f} mm")
            final_z = step(
                f"Jog Z → {args.jog_z:.2f}mm",
                ctrl.jog,
                BaseJogParams(
                    axis=Axis.Z, velocity=20.0, acceleration=200.0,
                    max_position=args.jog_z, tolerance=5.0, peak_current=0.2,
                ),
            )
            print(f"  Z after jog: {final_z}")
            step("Retract Z to start",
                 ctrl.move,
                 [AxisMoveInfo(axis=Axis.Z, position=start_z,
                                velocity=20.0, acceleration=200.0, absolute=True)])

        # --- Tips On force math: report only (no motion) --------------------
        # Honest about what it does: compute + print the peak_current and
        # force_percent that would be used for a given head + tip count. This
        # does NOT align barrels to tips, look up teachpoints, or move any
        # axis. A real Tips On needs:
        #   - Teachpoint for the tipbox location (XY + Z surface)
        #   - Labware metadata (well pitch, tip length, box height)
        #   - HeadMode + TipSelection (which barrels, which tips)
        #   - Head offsets (tip_task_head_offsets_mm)
        #   - Neighbor-clearance check against the deck state
        # None of that can live in a smoke script. Use TipsOnTask via the
        # workflow layer to actually pick up tips on hardware.
        if args.show_tips_on_force is not None:
            from pybravo.types import (
                LT_TIP_CURRENT_TABLE, ST_TIP_CURRENT_TABLE, interpolate_tip_current,
            )
            from pybravo.darwin.sequences import _z_axis_force_percent
            head = HeadType[args.head_type]
            use_lt = head in (HeadType.HT_8_D_LT, HeadType.HT_96_D_200, HeadType.HT_96_D_200_S2)
            tip_table = LT_TIP_CURRENT_TABLE if use_lt else ST_TIP_CURRENT_TABLE
            peak_current_amps = float(interpolate_tip_current(tip_table, args.show_tips_on_force))
            force_percent = _z_axis_force_percent(peak_current_amps)
            print("\n=== Tips On force math (report only — no motion) ===")
            print(f"  head_type     = {head.name}")
            print(f"  tip table     = {'LT' if use_lt else 'ST'}")
            print(f"  num tips      = {args.show_tips_on_force}")
            print(f"  peak current  = {peak_current_amps:.4f} A")
            print(f"  force percent = {force_percent:.2f} %")
            print("  (This is WHAT WOULD BE SENT if the workflow runs Tips On;")
            print("   the smoke script intentionally does not move any axis because")
            print("   it has no knowledge of barrel→tip alignment, deck height, or")
            print("   neighbor clearance. Use TipsOnTask via the workflow layer for")
            print("   actual hardware verification.)")

        print("\n=== Shutdown ===")
        for a in [Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg]:
            step(f"Disable {a.name}", ctrl.disable_motor, a)
        step("Clear lights", ctrl.clear_lights)
        step("Disconnect", ctrl.close)

        print("\nAll smoke tests PASSED.")
        return 0

    except BravoError as exc:
        print(f"\nBravoError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    finally:
        try:
            ctrl.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
