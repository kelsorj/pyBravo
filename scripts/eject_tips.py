"""Eject tips: drive the W axis to the eject position, then retract.

Does not initialize, home, or move X/Y/Z. Just actuates the ejection plate.

Runs against a simulated instrument unless you pass --hardware.

Usage:
    python -B scripts/eject_tips.py                      # simulation
    python -B scripts/eject_tips.py --hardware --profile profiles/Opportunity.yaml

W must already be homed. This script will refuse to move an unhomed W axis
rather than guess where the ejector is.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pybravo.bravo import Bravo
from pybravo.controllers.base import AxisMoveInfo
from pybravo.logging_config import configure_logging
from pybravo.types import Axis

configure_logging()

logger = logging.getLogger(__name__)


async def run(profile_path: str) -> int:
    bravo = Bravo(profile=profile_path)
    bravo.connect()
    try:
        ctrl = bravo.controller

        # Do not forge homed state here. An unhomed W has no meaningful zero,
        # so a commanded position is a guess, and the guess drives the ejector
        # plate. Fail closed and make the operator home it deliberately.
        if not ctrl._homed.get(Axis.W.value):
            logger.error(
                "W axis is not homed. Home it first (POST /api/home_axis with "
                "{\"axes\": [\"W\"]}, or the UI), then re-run."
            )
            return 2

        w_eject = float(bravo._profile.safety.tips_off_w_position)
        logger.info("Ejecting tips (W -> %s)", w_eject)
        ctrl.move([AxisMoveInfo(axis=Axis.W, position=w_eject)], wait=True)
        logger.info("Retracting (W -> 0)")
        ctrl.move([AxisMoveInfo(axis=Axis.W, position=0.0)], wait=True)
        logger.info("Done")
        return 0
    except Exception:
        logger.exception("Eject failed")
        return 1
    finally:
        bravo.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="Run against the real instrument in --profile. Without this, runs in simulation.",
    )
    ap.add_argument(
        "--profile",
        default=None,
        help="Profile to load. Defaults to profiles/simulation.yaml unless --hardware is set.",
    )
    args = ap.parse_args()

    profile = args.profile
    if not args.hardware:
        profile = profile or "profiles/simulation.yaml"
        logger.info("Simulation run. Pass --hardware to drive a real instrument.")
    elif not profile:
        ap.error("--hardware requires --profile naming the instrument to drive")

    return asyncio.run(run(profile))


if __name__ == "__main__":
    sys.exit(main())
