"""Eject tips: connect and drive W axis to eject position, then retract.

Does not initialize, home, or move X/Y/Z. Just actuates the ejection plate.

Usage:
    python -B scripts/eject_tips.py
    python -B scripts/eject_tips.py --profile profiles/Opportunity.yaml
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


async def run(profile_path: str) -> int:
    bravo = Bravo(profile=profile_path)
    bravo.connect()
    try:
        ctrl = bravo.controller
        ctrl._homed[Axis.W.value] = True
        w_eject = float(bravo._profile.safety.tips_off_w_position)
        print(f"Ejecting tips (W -> {w_eject})...")
        ctrl.move([AxisMoveInfo(axis=Axis.W, position=w_eject)], wait=True)
        print("Retracting (W -> 0)...")
        ctrl.move([AxisMoveInfo(axis=Axis.W, position=0.0)], wait=True)
        print("Done")
        return 0
    except Exception:
        logging.getLogger(__name__).exception("Eject failed")
        return 1
    finally:
        bravo.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="profiles/Opportunity.yaml")
    args = ap.parse_args()
    return asyncio.run(run(args.profile))


if __name__ == "__main__":
    sys.exit(main())
