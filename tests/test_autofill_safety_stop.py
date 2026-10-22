"""A safety stop of the instrument stops the autofill pumps.

Measured 2026-10-22: a light-curtain trip disables the axes (STOP_DISABLE) but
the pump module keeps running and answering, so without this a fill could keep
going, and overflow the tray, while the operator recovers.
"""
from __future__ import annotations

import time

from pybravo.darwin.controller import DarwinController
from pybravo.protocol.gemini.enums import ReservedEvent
from tests.test_autofill import _sim_bravo
from tests.test_autofill_stop_recovery import _wait


def _driver(bravo):
    return bravo._accessories._drivers["autofill"]


def test_a_safety_stop_stops_running_pumps_and_records_a_fault():
    bravo = _sim_bravo()
    bravo.run_autofill_pumps("autofill", duration_s=30)
    driver = _driver(bravo)
    faults = bravo.autofill_fault_count()

    bravo._on_safety_stop("STOP_DISABLE")

    assert _wait(lambda: not driver.is_running)
    assert bravo.autofill_fault_count() == faults + 1
    assert "safety stop" in driver.last_error


def test_a_safety_stop_without_running_pumps_records_nothing():
    bravo = _sim_bravo()
    bravo.read_autofill_level("autofill")
    bravo._on_safety_stop("STOP_DISABLE")
    time.sleep(0.2)
    assert bravo.autofill_fault_count() == 0


class _FakeEngine:
    def __init__(self) -> None:
        self.callbacks = []

    def on_reserved_event(self, cb) -> None:
        self.callbacks.append(cb)

    def fire(self, reserved) -> None:
        for cb in self.callbacks:
            cb(reserved, None)


def test_darwin_forwards_only_safety_events():
    ctrl = DarwinController.__new__(DarwinController)
    ctrl._engine = _FakeEngine()
    seen = []
    ctrl.on_safety_stop(seen.append)

    for event in (ReservedEvent.STOP, ReservedEvent.ERROR, ReservedEvent.STOP_DISABLE,
                  ReservedEvent.SAFETY_NOTICE, ReservedEvent.CONTINUE):
        ctrl._engine.fire(event)

    assert seen == ["STOP_DISABLE", "SAFETY_NOTICE"]
