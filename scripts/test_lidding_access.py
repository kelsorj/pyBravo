"""Test: lidding/delidding access guards for aspirate, dispense, and mix.

Verifies that the backend blocks aspirate/dispense/mix when a plate is lidded,
allows them after delidding, and blocks them again after relidding.

Always runs in simulation — no hardware required, and it cannot be pointed at a
real instrument. It forges tip state to reach the guard conditions, which is
only safe against a simulated deck.

Deck layout:
    Location 5: 96 Greiner 655101 plate (starts LIDDED)
    Location 8: empty (lid destination)
    Tips are attached via simulation (no tip box needed).

Test sequence:
    1. Connect + initialize
    2. Set up deck with lidded plate at location 5, attach simulated tips
    3. Aspirate from lidded plate → expect RuntimeError
    4. Dispense to lidded plate → expect RuntimeError
    5. Mix on lidded plate → expect RuntimeError
    6. Delid plate (lid → location 8)
    7. Aspirate from de-lidded plate → expect success
    8. Dispense to de-lidded plate → expect success
    9. Relid plate (lid from 8 → plate at 5)
   10. Aspirate from re-lidded plate → expect RuntimeError
   11. Dispense to re-lidded plate → expect RuntimeError

Usage:
    python -B scripts/test_lidding_access.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pybravo.bravo import Bravo
from pybravo.logging_config import configure_logging
from pybravo.types import Axis

configure_logging()
logger = logging.getLogger(__name__)

PLATE_LOCATION = 5
LID_DESTINATION = 8

PLATE_ID = "builtin-96-greiner-655101"      # 96 Greiner 655101 (supports lids)

ALL_AXES = [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg]

passed = 0
failed = 0


def step(name: str):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    return time.monotonic()


def done(t0: float):
    elapsed = time.monotonic() - t0
    print(f"  OK ({elapsed:.1f}s)")


async def expect_error(test_name: str, coro):
    global passed, failed
    try:
        await coro
        print(f"  FAIL: {test_name} — no error raised (expected RuntimeError)")
        failed += 1
    except RuntimeError as e:
        if "lid" in str(e).lower() or "sealed" in str(e).lower():
            print(f"  PASS: {test_name} — correctly blocked: {e}")
            passed += 1
        else:
            print(f"  FAIL: {test_name} — RuntimeError but wrong reason: {e}")
            failed += 1
    except Exception as e:
        print(f"  FAIL: {test_name} — unexpected error: {type(e).__name__}: {e}")
        failed += 1


async def expect_success(test_name: str, coro):
    global passed, failed
    try:
        await coro
        print(f"  PASS: {test_name} — succeeded as expected")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {test_name} — unexpected error: {type(e).__name__}: {e}")
        failed += 1


async def run(profile_path: str, simulation: bool) -> int:
    global passed, failed
    bravo = Bravo(
        profile=profile_path,
        mode="simulation" if simulation else None,
    )

    t0 = step("1. Connect")
    bravo.connect()
    done(t0)

    try:
        t0 = step("2. Initialize")
        all_homed = all(bravo.controller.is_axis_homed(ax) for ax in ALL_AXES)
        if all_homed:
            print("     All axes already homed — skipping initialization")
            bravo._initialized = True
        else:
            await bravo.initialize(auto_confirm=True)
        done(t0)

        t0 = step("3. Set up deck + attach tips")
        bravo.set_labware(PLATE_LOCATION, PLATE_ID, is_lidded=True)
        plate = bravo._deck.get_stack(PLATE_LOCATION).top
        print(f"     Location {PLATE_LOCATION}: {plate.name} (lidded={plate.is_lidded})")
        print(f"     Location {LID_DESTINATION}: empty (lid destination)")
        bravo._tips_on_head = True
        bravo._tip_labware_name = "Simulated Tips"
        bravo._tip_definition_id = "st_200ul"
        bravo._attached_tip_length_mm = float(bravo.profile.head.teach_tip_length_mm or 0.0)
        bravo._tips_on_head_mode = bravo._head_mode
        bravo._tips_on_head_selection = None
        print("     Tips attached (simulated)")
        done(t0)

        # --- Phase 1: Lidded plate — should BLOCK ---
        step("4. Test access on LIDDED plate (expect errors)")

        await expect_error(
            "Aspirate from lidded plate",
            bravo.aspirate(PLATE_LOCATION, volume=50.0),
        )
        await expect_error(
            "Dispense to lidded plate",
            bravo.dispense(PLATE_LOCATION, volume=50.0),
        )
        await expect_error(
            "Mix on lidded plate",
            bravo.mix(PLATE_LOCATION, volume=50.0),
        )

        # --- Delid ---
        t0 = step("5. Delid plate (lid → location 8)")
        await bravo.delid_plate(
            plate_location=PLATE_LOCATION,
            lid_destination=LID_DESTINATION,
        )
        plate = bravo._deck.get_stack(PLATE_LOCATION).top
        lid = bravo._deck.get_stack(LID_DESTINATION).top
        print(f"     Plate at {PLATE_LOCATION}: lidded={plate.is_lidded}")
        print(f"     Lid at {LID_DESTINATION}: {lid.name if lid else 'None'}")
        done(t0)

        # --- Phase 2: De-lidded plate — should ALLOW ---
        step("6. Test access on DE-LIDDED plate (expect success)")

        await expect_success(
            "Aspirate from de-lidded plate",
            bravo.aspirate(PLATE_LOCATION, volume=50.0, distance_from_bottom=2.0),
        )
        await expect_success(
            "Dispense to de-lidded plate",
            bravo.dispense(PLATE_LOCATION, volume=50.0, distance_from_bottom=2.0),
        )

        # --- Relid ---
        t0 = step("7. Relid plate (lid from 8 → plate at 5)")
        await bravo.relid_plate(
            lid_location=LID_DESTINATION,
            plate_location=PLATE_LOCATION,
        )
        plate = bravo._deck.get_stack(PLATE_LOCATION).top
        print(f"     Plate at {PLATE_LOCATION}: lidded={plate.is_lidded}")
        lid_after = bravo._deck.get_stack(LID_DESTINATION).top
        print(f"     Location {LID_DESTINATION}: {lid_after.name if lid_after else 'empty'}")
        done(t0)

        # --- Phase 3: Re-lidded plate — should BLOCK again ---
        step("8. Test access on RE-LIDDED plate (expect errors)")

        await expect_error(
            "Aspirate from re-lidded plate",
            bravo.aspirate(PLATE_LOCATION, volume=50.0),
        )
        await expect_error(
            "Dispense to re-lidded plate",
            bravo.dispense(PLATE_LOCATION, volume=50.0),
        )

        # --- Summary ---
        print(f"\n{'='*60}")
        print(f"  RESULTS: {passed} passed, {failed} failed")
        print(f"{'='*60}\n")
        return 0 if failed == 0 else 1

    except Exception:
        logger.exception("Test failed")
        return 1

    finally:
        t0 = step("9. Disconnect")
        bravo.disconnect()
        done(t0)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--profile", default="profiles/simulation.yaml",
        help="Path to Bravo profile YAML (default: profiles/simulation.yaml)",
    )
    args = ap.parse_args()
    # Simulation is not optional here. This script asserts on backend guard
    # logic and gets there by forging tip state (_tips_on_head below), which is
    # a lie the software would otherwise act on — against a real instrument it
    # would delid and move plates believing it holds tips it does not.
    return asyncio.run(run(args.profile, simulation=True))


if __name__ == "__main__":
    sys.exit(main())
