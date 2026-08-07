"""Functional test: full 96-tip aspirate/dispense cycle.

Uses pybravo as a library (no web server required).

Deck layout:
    Location 1: 96 V11 LT250 Tip Box (19477.002)
    Location 5: Axygen Reservoir (filled with water)
    Location 8: 96 Eppendorf TwinTec plate (destination)

Sequence:
    1. Connect and initialize the robot
    2. Set up the deck
    3. Load all 96 tips from location 1
    4. Aspirate 100 uL from reservoir at location 5
    5. Dispense (empty tips) into TwinTec plate at location 8
    6. Unload tips back to location 1
    7. Disconnect

Runs in simulation unless you pass --hardware. This moves liquid and tips, so
verify the deck matches the layout above before running it on an instrument.

Usage:
    python -B scripts/functional_test_aspirate_dispense.py               # simulation
    python -B scripts/functional_test_aspirate_dispense.py --hardware --profile profiles/Opportunity.yaml
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

TIPBOX_LOCATION = 1
RESERVOIR_LOCATION = 5
PLATE_LOCATION = 8

TIPBOX_ID = "legacy-200f9adcb745"       # 96 V11 LT250 Tip Box 19477.002
RESERVOIR_ID = "legacy-e4a06376fb50"    # Axygen Reservoir
PLATE_ID = "legacy-cc3c1c191276"        # 96 Eppendorf TwinTec

ASPIRATE_VOLUME = 100.0  # uL

ALL_AXES = [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg]


def step(name: str):
    """Print a step banner and return start time."""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    return time.monotonic()


def done(t0: float):
    elapsed = time.monotonic() - t0
    print(f"  OK ({elapsed:.1f}s)")


async def run(profile_path: str, simulation: bool) -> int:
    bravo = Bravo(
        profile=profile_path,
        mode="simulation" if simulation else None,
    )

    # --- Connect ---
    t0 = step("1. Connect")
    bravo.connect()
    done(t0)

    try:
        # --- Initialize (only if needed) ---
        t0 = step("2. Initialize")
        all_homed = all(bravo.controller.is_axis_homed(ax) for ax in ALL_AXES)
        if all_homed:
            print("     All axes already homed — skipping initialization")
            bravo._initialized = True
        else:
            unhomed = [ax.name for ax in ALL_AXES if not bravo.controller.is_axis_homed(ax)]
            print(f"     Unhomed axes: {', '.join(unhomed)} — initializing (auto_confirm=True)")
            await bravo.initialize(auto_confirm=True)
        done(t0)

        # --- Deck setup ---
        t0 = step("3. Set up deck")
        bravo.set_labware(TIPBOX_LOCATION, TIPBOX_ID)
        print(f"     Location {TIPBOX_LOCATION}: 96 V11 LT250 Tip Box")
        bravo.set_labware(RESERVOIR_LOCATION, RESERVOIR_ID)
        print(f"     Location {RESERVOIR_LOCATION}: Axygen Reservoir")
        bravo.set_labware(PLATE_LOCATION, PLATE_ID)
        print(f"     Location {PLATE_LOCATION}: 96 Eppendorf TwinTec")
        done(t0)

        # --- Tips On ---
        t0 = step("4. Tips On (all 96)")
        await bravo.tips_on(TIPBOX_LOCATION)
        done(t0)

        # --- Aspirate ---
        t0 = step(f"5. Aspirate {ASPIRATE_VOLUME} uL from reservoir")
        await bravo.aspirate(
            RESERVOIR_LOCATION,
            volume=ASPIRATE_VOLUME,
            distance_from_bottom=4.0,
        )
        done(t0)

        # --- Dispense ---
        t0 = step("6. Dispense (empty tips) to TwinTec plate")
        await bravo.dispense(
            PLATE_LOCATION,
            volume=ASPIRATE_VOLUME,
            empty_tips=True,
            distance_from_bottom=4.0,
        )
        done(t0)

        # --- Tips Off ---
        t0 = step("7. Tips Off (return to tip box)")
        await bravo.tips_off(TIPBOX_LOCATION)
        done(t0)

        print(f"\n{'='*60}")
        print("  ALL STEPS COMPLETED SUCCESSFULLY")
        print(f"{'='*60}\n")
        return 0

    except Exception:
        logger.exception("Functional test failed")
        return 1

    finally:
        t0 = step("8. Disconnect")
        bravo.disconnect()
        done(t0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--profile", default=None,
        help="Path to Bravo profile YAML (default: profiles/Opportunity.yaml)",
    )
    ap.add_argument(
        "--hardware", action="store_true",
        help="Drive the real instrument named by --profile. Without this, runs in simulation.",
    )
    args = ap.parse_args()
    profile = args.profile
    if args.hardware:
        if not profile:
            ap.error("--hardware requires --profile naming the instrument to drive")
    else:
        profile = profile or "profiles/simulation.yaml"
    return asyncio.run(run(profile, simulation=not args.hardware))


if __name__ == "__main__":
    sys.exit(main())
