"""Functional test: triplicate dispensing from source to destination plate.

Uses pybravo as a library (no web server required).

Deck layout:
    Location 1: 96 V11 LT250 Tip Box (full, for pickup)
    Location 2: 96 Eppendorf TwinTec (destination, empty)
    Location 4: 96 V11 LT250 Tip Box (empty, for tip disposal)
    Location 5: Axygen Reservoir (not used in this test)
    Location 8: 96 Eppendorf TwinTec (source, columns 1-4 filled)

Workflow (1-column head mode, back_left):
    For each source column 1-4:
        - Load 1 column of tips from Location 1 (col 12, 11, 10, 9)
        - Aspirate 90 uL from source column at Location 8
        - Dispense 30 uL to each of 3 destination columns at Location 2
        - Return tips to Location 4 (col 1, 2, 3, 4)

    Source col 1 -> Dest cols 1, 2, 3
    Source col 2 -> Dest cols 4, 5, 6
    Source col 3 -> Dest cols 7, 8, 9
    Source col 4 -> Dest cols 10, 11, 12

Usage:
    python -B scripts/functional_test_triplicate.py
    python -B scripts/functional_test_triplicate.py --simulation
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

# Deck locations
TIPBOX_PICKUP = 1
DEST_PLATE = 2
TIPBOX_DISPOSAL = 4
SOURCE_PLATE = 8

# Labware IDs
TIPBOX_ID = "legacy-200f9adcb745"       # 96 V11 LT250 Tip Box 19477.002
PLATE_ID = "legacy-cc3c1c191276"        # 96 Eppendorf TwinTec

ASPIRATE_VOLUME = 90.0   # uL total
DISPENSE_VOLUME = 30.0   # uL per destination column
REPLICATES = 3

ALL_AXES = [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg]


def step(name: str):
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

    t0 = step("1. Connect")
    bravo.connect()
    done(t0)

    try:
        # --- Initialize ---
        t0 = step("2. Initialize")
        all_homed = all(bravo.controller.is_axis_homed(ax) for ax in ALL_AXES)
        if all_homed:
            print("     All axes already homed — skipping initialization")
            bravo._initialized = True
        else:
            unhomed = [ax.name for ax in ALL_AXES if not bravo.controller.is_axis_homed(ax)]
            print(f"     Unhomed axes: {', '.join(unhomed)} — initializing")
            await bravo.initialize(auto_confirm=True)
        done(t0)

        # --- Deck setup ---
        t0 = step("3. Set up deck")
        bravo.set_labware(TIPBOX_PICKUP, TIPBOX_ID)
        print(f"     Location {TIPBOX_PICKUP}: Tip box (full, pickup)")
        bravo.set_labware(DEST_PLATE, PLATE_ID)
        print(f"     Location {DEST_PLATE}: TwinTec (destination, empty)")
        bravo.set_labware(TIPBOX_DISPOSAL, TIPBOX_ID, tipbox_fill_state="empty")
        print(f"     Location {TIPBOX_DISPOSAL}: Tip box (empty, disposal)")
        bravo.set_labware(SOURCE_PLATE, PLATE_ID)
        print(f"     Location {SOURCE_PLATE}: TwinTec (source, cols 1-4)")
        done(t0)

        # --- Set head mode: 1 column, back left ---
        t0 = step("4. Set head mode: column, back_left")
        bravo.set_head_mode("column", "back_left", column_count=1)
        done(t0)

        # --- Triplicate loop ---
        for i in range(4):
            source_col = i                          # 0-indexed: 0, 1, 2, 3
            tip_pickup_col = 11 - i                 # 0-indexed: 11, 10, 9, 8
            tip_disposal_col = i                    # 0-indexed: 0, 1, 2, 3
            dest_col_start = i * REPLICATES         # 0-indexed: 0, 3, 6, 9

            print(f"\n{'*'*60}")
            print(f"  ROUND {i+1}/4: Source col {source_col+1} -> "
                  f"Dest cols {dest_col_start+1}, {dest_col_start+2}, {dest_col_start+3}")
            print(f"{'*'*60}")

            # Tips On
            t0 = step(f"  {i+1}a. Tips On (Location {TIPBOX_PICKUP}, col {tip_pickup_col+1})")
            bravo.set_tip_selection(TIPBOX_PICKUP, row=0, col=tip_pickup_col)
            await bravo.tips_on(TIPBOX_PICKUP)
            done(t0)

            # Aspirate from source
            t0 = step(f"  {i+1}b. Aspirate {ASPIRATE_VOLUME} uL (Location {SOURCE_PLATE}, col {source_col+1})")
            bravo.set_plate_selection(SOURCE_PLATE, row=0, col=source_col)
            await bravo.aspirate(
                SOURCE_PLATE,
                volume=ASPIRATE_VOLUME,
                distance_from_bottom=4.0,
            )
            done(t0)

            # Dispense to 3 destination columns
            for j in range(REPLICATES):
                dest_col = dest_col_start + j
                t0 = step(f"  {i+1}c{j+1}. Dispense {DISPENSE_VOLUME} uL (Location {DEST_PLATE}, col {dest_col+1})")
                bravo.set_plate_selection(DEST_PLATE, row=0, col=dest_col)
                await bravo.dispense(
                    DEST_PLATE,
                    volume=DISPENSE_VOLUME,
                    distance_from_bottom=4.0,
                )
                done(t0)

            # Tips Off
            t0 = step(f"  {i+1}d. Tips Off (Location {TIPBOX_DISPOSAL}, col {tip_disposal_col+1})")
            bravo.set_tip_selection(TIPBOX_DISPOSAL, row=0, col=tip_disposal_col)
            await bravo.tips_off(TIPBOX_DISPOSAL)
            done(t0)

        print(f"\n{'='*60}")
        print("  ALL 4 ROUNDS COMPLETED SUCCESSFULLY")
        print("  Source cols 1-4 -> Dest cols 1-12 (triplicates)")
        print(f"{'='*60}\n")
        return 0

    except Exception:
        logger.exception("Functional test failed")
        return 1

    finally:
        t0 = step("Disconnect")
        bravo.disconnect()
        done(t0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--profile", default="profiles/Opportunity.yaml",
        help="Path to Bravo profile YAML (default: profiles/Opportunity.yaml)",
    )
    ap.add_argument(
        "--simulation", action="store_true",
        help="Run in simulation mode (no hardware required)",
    )
    args = ap.parse_args()
    return asyncio.run(run(args.profile, args.simulation))


if __name__ == "__main__":
    sys.exit(main())
