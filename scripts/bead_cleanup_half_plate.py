"""Bead clean-up, half plate — generated from a legacy protocol file.

Deck layout:
    Location 1: Tip Box 1 (96 Axygen LT165 Tip Box)
    Location 3: Waste (96 Deepwell Reservoir Thermo)
    Location 4: Tip Box 2 (96 Axygen LT165 Tip Box)
    Location 6: Elution Plate (96 Eppendorf TwinTec)
    Location 7: Reagents (96 Eppendorf 1000ul DeepWell blue border)
    Location 8: Tip Box 3 (96 Axygen LT165 Tip Box)
    Location 9: Sample Plate (96 Eppendorf TwinTec)

Usage:
    python -B scripts/bead_cleanup_half_plate.py
    python -B scripts/bead_cleanup_half_plate.py --profile profiles/Opportunity.yaml
    python -B scripts/bead_cleanup_half_plate.py --simulation

TODOS
- replace the fast incubation times - they're set to 10s for testing but should be 180s and 60s
- replace the mix cycles to 10 (beads) and 12 (elution) - they're set to 2 for testing but should be 10
- the plate move is grabbing the plate at the very top edge - should be lower on the plate
- a pause mechanism would be great
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

# --- Labware IDs ---
ELUTION_PLATE_ID = "legacy-cc3c1c191276"  # 96 Eppendorf TwinTec
REAGENTS_ID = "legacy-0a7dbe09303b"  # 96 Eppendorf 1000ul DeepWell blue border
SAMPLE_PLATE_ID = "legacy-cc3c1c191276"  # 96 Eppendorf TwinTec
TIP_BOX_1_ID = "legacy-64deb9515c04"  # 96 Axygen LT165 Tip Box
TIP_BOX_2_ID = "legacy-64deb9515c04"  # 96 Axygen LT165 Tip Box
TIP_BOX_3_ID = "legacy-64deb9515c04"  # 96 Axygen LT165 Tip Box
WASTE_ID = "legacy-a18cfce96b54"  # 96 Deepwell Reservoir Thermo

# --- Liquid classes ---
LC_96_DISPOSABLE_FAST_MIX = "96 disposable fast mix"  # id=liq_cb7b8c94ec
LC_96_DISPOSABLE_TIP_2___50UL = "96 disposable tip 2 - 50ul"  # id=liq_20df69c756
LC_96_DISPOSABLE_TIP_5___200UL_WATER = "96 disposable tip 5 - 200ul Water"  # id=liq_18568bed7e
LC_96_DISPOSABLE_TIP_5___200UL_WATER__SLOW = "96 disposable tip 5 - 200ul Water (slow)"  # id=liq_06e4401113
LC_ETOH = "EtOH"  # id=liq_76a9324bd6
LC_SPRI_BEADS = "SPRI Beads"  # id=liq_97ae4cc9af

# --- Protocol variables ---
BeadVolume = 40
ElutionVolume = 20
EtOHVolume = 150
InitialVolume = 40
TransferVolume = 20

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


def pause(msg: str = 'Press Enter to continue...'):
    input(f'  >>> {msg}')


async def run(profile_path: str, simulation: bool) -> int:
    bravo = Bravo(
        profile=profile_path,
        mode="simulation" if simulation else None,
    )

    # --- Connect ---
    t0 = step('1. Connect')
    bravo.connect()
    done(t0)

    try:
        # --- Initialize ---
        t0 = step('2. Initialize')
        all_homed = all(bravo.controller.is_axis_homed(ax) for ax in ALL_AXES)
        if all_homed:
            print('     All axes already homed')
            bravo._initialized = True
        else:
            await bravo.initialize(auto_confirm=True)
        done(t0)

        # --- Deck setup (named labware — pick_place auto-updates locations) ---
        t0 = step('3. Set up deck')
        bravo.set_labware(1, TIP_BOX_1_ID, name="tip_box_1")
        print('     Location 1: Tip Box 1')
        bravo.set_labware(3, WASTE_ID, name="waste")
        print('     Location 3: Waste')
        bravo.set_labware(4, TIP_BOX_2_ID, name="tip_box_2", tipbox_fill_state="empty")
        print('     Location 4: Tip Box 2 (empty)')
        bravo.set_labware(6, ELUTION_PLATE_ID, name="elution_plate")
        print('     Location 6: Elution Plate')
        bravo.set_labware(7, REAGENTS_ID, name="reagents")
        print('     Location 7: Reagents')
        bravo.set_labware(8, TIP_BOX_3_ID, name="tip_box_3")
        print('     Location 8: Tip Box 3')
        bravo.set_labware(9, SAMPLE_PLATE_ID, name="sample_plate")
        print('     Location 9: Sample Plate')
        done(t0)

        # --- Place Sample Plate at location 9 (operator) ---
        t0 = step('4. Place Sample Plate at location 9')
        pause('Place Sample Plate at location 9, then press Enter')
        done(t0)

        # --- Pick/Place Sample Plate: 9 -> 5 ---
        t0 = step('5. Pick/place Sample Plate (9 -> 5)')
        await bravo.pick_place("sample_plate", 5)
        done(t0)

        # ==================================================
        # SubProcess: Bravo SubProcess 1
        # ==================================================
        t0 = step('6. Bravo SubProcess 1')

        # Head mode: 3x8 (subset=2)
        bravo.set_head_mode('column', 'back_left', column_count=3)
        bravo.set_tip_selection("tip_box_1", row=0, col=9)
        await bravo.tips_on("tip_box_1")
        bravo.set_plate_selection("reagents", row=0, col=0)
        await bravo.aspirate("reagents", volume=BeadVolume, distance_from_bottom=1.5, liquid_class='SPRI Beads')
        bravo.set_plate_selection("sample_plate", row=0, col=0)
        await bravo.dispense("sample_plate", volume=BeadVolume, distance_from_bottom=0.5, liquid_class='SPRI Beads')
        bravo.set_tip_selection("tip_box_2", row=0, col=0)
        await bravo.tips_off("tip_box_2")
        bravo.set_tip_selection("tip_box_1", row=0, col=6)
        await bravo.tips_on("tip_box_1")
        bravo.set_plate_selection("reagents", row=0, col=0)
        await bravo.aspirate("reagents", volume=BeadVolume, distance_from_bottom=0.5, liquid_class='SPRI Beads')
        bravo.set_plate_selection("sample_plate", row=0, col=3)
        await bravo.dispense("sample_plate", volume=BeadVolume, distance_from_bottom=0.5, liquid_class='SPRI Beads')
        bravo.set_tip_selection("tip_box_2", row=0, col=3)
        await bravo.tips_off("tip_box_2")
        # Head mode: 6x8 (subset=2)
        bravo.set_head_mode('column', 'back_left', column_count=6)
        bravo.set_tip_selection("tip_box_2", row=0, col=0)
        await bravo.tips_on("tip_box_2")
        bravo.set_plate_selection("sample_plate", row=0, col=0)
        # Mix 10 cycles, aspirate_dist=2mm, dispense_dist=0.5mm
        await bravo.mix("sample_plate", volume=(BeadVolume + InitialVolume) * .8,
                        mix_cycles=2, aspirate_distance=2, dispense_distance=0.5,
                        dispense_at_different_distance=True, liquid_class='SPRI Beads')
        bravo.set_tip_selection("tip_box_2", row=0, col=0)
        await bravo.tips_off("tip_box_2")
        done(t0)

        # --- Incubate 180s ---
        t0 = step('7. Incubate 180s at location 5')
        print('     Waiting 180 seconds...')
        await asyncio.sleep(10)
        done(t0)

        # --- Pick/Place Sample Plate: 5 -> 2 ---
        t0 = step('8. Pick/place Sample Plate (5 -> 2)')
        await bravo.pick_place("sample_plate", 2)
        done(t0)

        # --- Incubate 180s ---
        t0 = step('9. Incubate 180s at location 2')
        print('     Waiting 180 seconds...')
        await asyncio.sleep(10)
        done(t0)

        # ==================================================
        # SubProcess: Aspirate Supernatant
        # ==================================================
        t0 = step('10. Aspirate Supernatant')

        # Head mode: 6x8 (subset=2)
        bravo.set_head_mode('column', 'back_left', column_count=6)
        bravo.set_tip_selection("tip_box_2", row=0, col=0)
        await bravo.tips_on("tip_box_2")
        bravo.set_plate_selection("sample_plate", row=0, col=0)
        await bravo.aspirate("sample_plate", volume=InitialVolume + BeadVolume + 5, distance_from_bottom=0.25, liquid_class='96 disposable tip 2 - 50ul')
        bravo.set_plate_selection("waste", row=0, col=0)
        await bravo.dispense("waste", volume=10, empty_tips=True, distance_from_bottom=2)
        bravo.set_tip_selection("tip_box_2", row=0, col=0)
        await bravo.tips_off("tip_box_2")
        done(t0)

        # ==================================================
        # SubProcess: 2x EtOH Wash
        # ==================================================
        t0 = step('11. 2x EtOH Wash')

        # Head mode: 6x8 (subset=0) — back_right: head cols 7-12 active, tips from tipbox cols 1-6
        bravo.set_head_mode('column', 'back_right', column_count=6)
        bravo.set_tip_selection("tip_box_3", row=0, col=5)
        await bravo.tips_on("tip_box_3")
        for LoopCounter in range(1, 3):
            print(f'     Loop iteration {LoopCounter}/2')
            bravo.set_plate_selection("reagents", row=0, col=6)
            await bravo.aspirate("reagents", volume=EtOHVolume, distance_from_bottom=2, liquid_class='EtOH')
            bravo.set_plate_selection("sample_plate", row=0, col=0)
            await bravo.dispense("sample_plate", volume=10, empty_tips=True, distance_from_bottom=2, liquid_class='EtOH')
            # Reserve location for Sample Plate (10s)
            await asyncio.sleep(10)
            bravo.set_plate_selection("sample_plate", row=0, col=0)
            await bravo.aspirate("sample_plate", volume=EtOHVolume, distance_from_bottom=0.25, liquid_class='EtOH')
            bravo.set_plate_selection("waste", row=0, col=0)
            await bravo.dispense("waste", volume=10, empty_tips=True, distance_from_bottom=10 * LoopCounter, liquid_class='EtOH')
        bravo.set_plate_selection("sample_plate", row=0, col=0)
        await bravo.aspirate("sample_plate", volume=EtOHVolume - 100, distance_from_bottom=0.25, liquid_class='EtOH')
        bravo.set_plate_selection("waste", row=0, col=0)
        await bravo.dispense("waste", volume=10, empty_tips=True, distance_from_bottom=5, liquid_class='EtOH')
        bravo.set_tip_selection("tip_box_3", row=0, col=5)
        await bravo.tips_off("tip_box_3")
        done(t0)

        # ==================================================
        # SubProcess: Add Elution Buffer
        # ==================================================
        t0 = step('12. Add Elution Buffer')

        # --- Group ---
        # Head mode: 3x8 (subset=2)
        bravo.set_head_mode('column', 'back_left', column_count=3)
        for LoopCounter in range(1, 3):
            print(f'     Loop iteration {LoopCounter}/2')
            bravo.set_tip_selection("tip_box_3", row=0, col=12 - (LoopCounter * 3))
            await bravo.tips_on("tip_box_3")
            bravo.set_plate_selection("reagents", row=0, col=3)
            await bravo.aspirate("reagents", volume=ElutionVolume, distance_from_bottom=0.5, liquid_class='96 disposable tip 5 - 200ul Water')
            bravo.set_plate_selection("sample_plate", row=0, col=((LoopCounter-1) * 3))
            await bravo.dispense("sample_plate", volume=10, empty_tips=True, distance_from_bottom=2)
            bravo.set_tip_selection("tip_box_2", row=0, col=((LoopCounter + 1) * 3))
            await bravo.tips_off("tip_box_2")
        done(t0)

        # --- Pick/Place Sample Plate: 2 -> 5 ---
        t0 = step('13. Pick/place Sample Plate (2 -> 5)')
        await bravo.pick_place("sample_plate", 5)
        done(t0)

        # ==================================================
        # SubProcess: Mix Elution Buffer
        # ==================================================
        t0 = step('14. Mix Elution Buffer')

        # Head mode: 6x8 (subset=3)
        bravo.set_head_mode('column', 'back_left', column_count=6)
        bravo.set_tip_selection("tip_box_2", row=0, col=6)
        await bravo.tips_on("tip_box_2")
        bravo.set_plate_selection("sample_plate", row=0, col=0)
        # Mix 12 cycles, aspirate_dist=0.25mm, dispense_dist=0.5mm
        await bravo.mix("sample_plate", volume=ElutionVolume,
                        mix_cycles=2, aspirate_distance=0.25, dispense_distance=0.5,
                        dispense_at_different_distance=True, liquid_class='96 disposable fast mix')
        bravo.set_tip_selection("tip_box_3", row=0, col=6)
        await bravo.tips_off("tip_box_3")
        done(t0)

        # --- Incubate 180s ---
        t0 = step('15. Incubate 180s at location 5')
        print('     Waiting 180 seconds...')
        await asyncio.sleep(10)
        done(t0)

        # --- Pick/Place Sample Plate: 5 -> 2 ---
        t0 = step('16. Pick/place Sample Plate (5 -> 2)')
        await bravo.pick_place("sample_plate", 2)
        done(t0)

        # --- Incubate 60s ---
        t0 = step('17. Incubate 60s at location 2')
        print('     Waiting 60 seconds...')
        await asyncio.sleep(10)
        done(t0)

        # ==================================================
        # SubProcess: Transfer to Elution Plate
        # ==================================================
        t0 = step('18. Transfer to Elution Plate')

        # Head mode: 6x8 (subset=2)
        bravo.set_head_mode('column', 'back_left', column_count=6)
        bravo.set_tip_selection("tip_box_1", row=0, col=0)
        await bravo.tips_on("tip_box_1")
        bravo.set_plate_selection("sample_plate", row=0, col=0)
        await bravo.aspirate("sample_plate", volume=TransferVolume, distance_from_bottom=0.5, liquid_class='96 disposable tip 5 - 200ul Water (slow)')
        bravo.set_plate_selection("elution_plate", row=0, col=0)
        await bravo.dispense("elution_plate", volume=10, empty_tips=True, distance_from_bottom=0.5, liquid_class='96 disposable tip 5 - 200ul Water (slow)')
        bravo.set_tip_selection("tip_box_2", row=0, col=6)
        await bravo.tips_off("tip_box_2")
        done(t0)

        print(f"\n{'='*60}")
        print('  ALL STEPS COMPLETED SUCCESSFULLY')
        print(f"{'='*60}\n")
        return 0

    except Exception:
        logger.exception('Protocol failed')
        return 1

    finally:
        t0 = step('19. Disconnect')
        bravo.disconnect()
        done(t0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        '--profile', default='profiles/Opportunity.yaml',
        help='Path to Bravo profile YAML',
    )
    ap.add_argument(
        '--simulation', action='store_true',
        help='Run in simulation mode',
    )
    args = ap.parse_args()
    return asyncio.run(run(args.profile, args.simulation))


if __name__ == '__main__':
    sys.exit(main())
