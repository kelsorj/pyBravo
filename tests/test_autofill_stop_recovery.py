"""Autofill: a stop that is not acknowledged, and cleanup after the transport changes.

A pump runs until it hears a stop, so the driver must never forget a run whose
stop it could not deliver, and cleanup must reach the transport the run is on.
"""
from __future__ import annotations

import time

import pytest

from pybravo.accessories import autofill
from pybravo.accessories.autofill import AutofillError
from pybravo.bravo import Bravo
from pybravo.profile.profile import AccessoryDeviceConfig, BravoProfile
from pybravo.web import server
from tests.test_autofill import STOP, RecordingBus, _station


@pytest.fixture()
def short_quiet(monkeypatch):
    monkeypatch.setattr(autofill, "FAULT_QUIET_S", 0.2)


def _wait(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def test_unacknowledged_manual_stop_keeps_the_run_for_cleanup(short_quiet):
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(0.15)

    bus.fail_on = STOP
    with pytest.raises(AutofillError):
        station.stop_pumps()
    assert not station.is_running
    assert station.stop_uncertain and station.needs_stop

    # Cleanup while the bus is quiet sends nothing (the module's own timeout
    # needs silence) but does not drop the run: once the quiet time ends the
    # stop goes out again, even though the driver has been closed.
    bus.fail_on = None
    stops_before = bus.stops
    station.close()
    assert bus.stops == stops_before
    assert _wait(lambda: not station.stop_uncertain)
    assert bus.stops == stops_before + 2
    assert not station.needs_stop


def test_cleanup_after_the_quiet_time_sends_the_stop_at_once(short_quiet):
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(5)
    bus.fail_on = STOP
    with pytest.raises(AutofillError):
        station.stop_pumps()
    station._cancel_stop_retry()

    time.sleep(0.25)
    bus.fail_on = None
    station.close()
    assert bus.stops == 4
    assert not station.needs_stop


def test_no_new_run_while_a_stop_is_unacknowledged(short_quiet):
    bus = RecordingBus(fail_on=STOP)
    station = _station(bus)
    station.run_pumps(5)
    with pytest.raises(AutofillError):
        station.stop_pumps()
    station._cancel_stop_retry()

    time.sleep(0.25)  # past the quiet time: only the missing stop blocks the start
    with pytest.raises(AutofillError, match="not acknowledged"):
        station.run_pumps(5)

    bus.fail_on = None
    station.stop_pumps()  # a manual stop is the way out
    station.run_pumps(0.1)
    assert station.is_running
    station.stop_pumps()


def test_one_acknowledged_stop_of_the_pair_is_enough(short_quiet):
    class FirstStopFails(RecordingBus):
        def __call__(self, payload):
            if payload == STOP and self.stops == 0:
                self.sent.append(payload)
                raise TimeoutError("no reply")
            return super().__call__(payload)

    bus = FirstStopFails()
    station = _station(bus)
    station.run_pumps(5)
    with pytest.raises(AutofillError):
        station.stop_pumps()
    assert bus.stops == 2
    assert not station.stop_uncertain and not station.needs_stop


def test_unacknowledged_stop_is_retried_after_the_quiet_time(short_quiet):
    bus = RecordingBus(fail_on=STOP)
    station = _station(bus)
    station.run_pumps(5)
    with pytest.raises(AutofillError):
        station.stop_pumps()
    assert bus.stops == 2

    bus.fail_on = None
    assert _wait(lambda: not station.stop_uncertain)
    assert bus.stops == 4


def test_watchdog_stop_failure_is_retried_once(short_quiet):
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(0.1)
    bus.fail_on = STOP
    assert _wait(lambda: station.stop_uncertain)
    assert _wait(lambda: bus.stops >= 4)  # the watchdog's pair, then one retry pair
    time.sleep(0.5)
    assert bus.stops == 4  # no endless retries against a module that keeps failing
    assert station.stop_uncertain


def test_module_error_without_acknowledged_stop_is_uncertain(short_quiet):
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(5)

    def broken(payload):
        bus.sent.append(payload)
        raise TimeoutError("no reply")

    station._sender_provider = lambda: broken
    with pytest.raises(AutofillError):
        station.read_weight()
    assert not station.is_running
    assert station.stop_uncertain


def _sim_bravo() -> Bravo:
    profile = BravoProfile.default()
    profile.connection.controller_type = "simulation"
    profile.accessories.devices = [
        AccessoryDeviceConfig(id="autofill", type="autofill", name="Autofill", location=3,
                              settings={"tare": 21300, "range": 24300}),
    ]
    return Bravo(profile=profile)


def test_stop_all_pumps_stops_a_run_with_an_unacknowledged_stop(short_quiet):
    bravo = _sim_bravo()
    bravo.run_autofill_pumps("autofill", duration_s=30)
    driver = bravo._accessories._drivers["autofill"]
    driver._stop_uncertain = True
    driver._running = False

    bravo.stop_all_pumps()
    assert not driver.stop_uncertain


class _HardwareController:
    """Stands in for a connected instrument; the accessory bus is a recording fake."""

    def __init__(self, bus: RecordingBus) -> None:
        self.send_serial = bus
        self.is_connected = True

    def close(self) -> None:
        self.is_connected = False


def _hardware_bravo(bus: RecordingBus) -> Bravo:
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin_native"
    profile.accessories.devices = [
        AccessoryDeviceConfig(id="autofill", type="autofill", name="Autofill", location=3,
                              settings={"tare": 21300, "range": 24300}),
    ]
    bravo = Bravo(profile=profile)
    bravo._controller = _HardwareController(bus)
    return bravo


def test_switching_the_profile_to_simulation_stops_the_hardware_run():
    bus = RecordingBus()
    bravo = _hardware_bravo(bus)
    bravo.run_autofill_pumps("autofill", duration_s=30)

    bravo.profile.connection.controller_type = "simulation"
    bravo.reinit_accessories()

    assert bus.stops == 2


@pytest.mark.asyncio
async def test_profile_update_to_simulation_stops_the_hardware_run(monkeypatch):
    bus = RecordingBus()
    bravo = _hardware_bravo(bus)
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_profile_path", None)
    bravo.run_autofill_pumps("autofill", duration_s=30)

    accessories = bravo.profile.accessories.to_dict()
    await server.update_profile(
        server.ProfileUpdateRequest(controller_type="simulation", accessories=accessories)
    )

    assert bus.stops == 2
    # Later runs go to the simulator, not the instrument.
    sent_before = len(bus.sent)
    bravo.run_autofill_pumps("autofill", duration_s=0.1)
    bravo.stop_all_pumps()
    assert len(bus.sent) == sent_before


def test_connecting_rebuilds_a_driver_made_before_the_connection(monkeypatch):
    import pybravo.darwin.controller as darwin_controller

    bus = RecordingBus()

    class FakeDarwin(_HardwareController):
        def __init__(self, **_kwargs):
            super().__init__(bus)

        def open_tcp(self, _address):
            pass

    monkeypatch.setattr(darwin_controller, "DarwinController", FakeDarwin)
    bravo = _sim_bravo()
    bravo.read_autofill_level("autofill")  # not connected yet: builds the driver on the simulator
    bravo.profile.connection.controller_type = "darwin_native"
    bravo.profile.connection.address = "192.0.2.1"
    bravo.connect()

    bravo.read_autofill_level("autofill")
    assert bus.sent, "the level read went to the simulator, not the instrument"


def test_switching_to_hardware_keeps_a_simulated_run_on_the_simulator():
    bravo = _sim_bravo()
    bravo.run_autofill_pumps("autofill", duration_s=30)
    sim = bravo._simulated_accessory_bus
    bus = RecordingBus()
    bravo._controller = _HardwareController(bus)

    bravo.profile.connection.controller_type = "darwin_native"
    bravo.reinit_accessories()

    assert bus.stops == 0
    assert sim is bravo._simulated_accessory_bus
