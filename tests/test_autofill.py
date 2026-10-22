"""Autofill station: accessory-bus pump module and weigh pad.

The safety-critical property is that a pump always gets a stop: the run
command has no duration, so the module keeps pumping until it hears ``AE``.
"""
from __future__ import annotations

import time

import pytest

from pybravo.accessories.autofill import (
    AutofillConfig,
    AutofillError,
    AutofillStation,
    PumpSettings,
    build_read_weigh_pad,
    build_run_pump,
    build_stop_pumps,
    level_percent,
)
from pybravo.bravo import Bravo
from pybravo.profile.profile import AccessoryDeviceConfig, BravoProfile

STOP = bytes.fromhex("ae 00 00 00 00 00 00 00 00")


class RecordingBus:
    """Fake accessory bus: records payloads and answers like the module does."""

    def __init__(self, weight: int = 21284, fail_on: bytes | None = None) -> None:
        self.sent: list[bytes] = []
        self.weight = weight
        self.fail_on = fail_on

    def __call__(self, payload: bytes) -> bytes:
        self.sent.append(payload)
        if self.fail_on is not None and payload == self.fail_on:
            raise TimeoutError("no reply")
        return bytes([payload[0], 0, self.weight & 0xFF, self.weight >> 8, 0, 0, 0, 0])

    @property
    def stops(self) -> int:
        return sum(1 for p in self.sent if p == STOP)


def _station(bus: RecordingBus, **kwargs) -> AutofillStation:
    config = AutofillConfig(
        fill=PumpSettings(module=1, pump=1, direction="forward", speed_pct=100),
        empty=PumpSettings(module=1, pump=2, direction="reverse", speed_pct=25),
        tare=21544,
        range=24231,
        **kwargs,
    )
    return AutofillStation(config, lambda: bus)


def test_payloads_match_the_module_protocol():
    assert build_run_pump(1, 1, "forward", 100) == bytes.fromhex("ac 01 01 01 10 27 00 00 00")
    assert build_run_pump(1, 2, "reverse", 25) == bytes.fromhex("ac 01 02 00 c4 09 00 00 00")
    assert build_run_pump(1, 1, "forward", 50) == bytes.fromhex("ac 01 01 01 88 13 00 00 00")
    assert build_stop_pumps() == STOP
    assert build_read_weigh_pad(1) == bytes.fromhex("b3 01 00 00 00 00 00 00 00")


@pytest.mark.parametrize("speed", [-1, 100.5])
def test_out_of_range_speed_is_rejected(speed):
    with pytest.raises(AutofillError):
        build_run_pump(1, 1, "forward", speed)


def test_weight_reading_and_level():
    bus = RecordingBus(weight=21290)
    level = _station(bus).read_level()
    assert level["reading"] == 21290
    assert level["level_pct"] == pytest.approx(-9.45, abs=0.01)
    assert level_percent(100, 100, 100) is None


def test_run_starts_both_pumps_and_watchdog_stops_them():
    bus = RecordingBus()
    station = _station(bus)

    station.run_pumps(0.15)

    assert bus.sent[:2] == [
        bytes.fromhex("ac 01 01 01 10 27 00 00 00"),
        bytes.fromhex("ac 01 02 00 c4 09 00 00 00"),
    ]
    assert station.is_running
    deadline = time.monotonic() + 2
    while station.is_running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not station.is_running
    assert bus.stops == 2


def test_running_pumps_are_kept_alive_with_status_polls():
    """The module stops pumps on its own unless the host keeps polling it."""
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(1.0)
    time.sleep(1.4)
    polls = [p for p in bus.sent if p == bytes.fromhex("af 01 00 00 00 00 00 00 00")]
    assert len(polls) >= 2
    assert not station.is_running


def test_manual_stop_cancels_the_watchdog():
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(0.2, empty=False)
    station.stop_pumps()
    time.sleep(0.4)
    assert bus.stops == 2  # only the manual stop, no second one from the watchdog


def test_failed_start_stops_the_pump_that_did_start():
    empty_cmd = bytes.fromhex("ac 01 02 00 c4 09 00 00 00")
    bus = RecordingBus(fail_on=empty_cmd)
    station = _station(bus)

    with pytest.raises(AutofillError):
        station.run_pumps(5)

    assert bus.stops == 2
    assert not station.is_running


def test_close_stops_running_pumps():
    bus = RecordingBus()
    station = _station(bus)
    station.run_pumps(30)
    station.close()
    assert bus.stops == 2
    assert not station.is_running


@pytest.mark.parametrize("duration", [0, -1, 601])
def test_run_time_must_be_bounded(duration):
    with pytest.raises(AutofillError):
        _station(RecordingBus()).run_pumps(duration)


def test_module_error_status_is_reported():
    station = _station(RecordingBus())
    station._sender_provider = lambda: (lambda payload: bytes([payload[0], 0x02, 0, 0, 0, 0, 0, 0]))
    with pytest.raises(AutofillError, match="status 0x02"):
        station.read_weight()


def _sim_bravo() -> Bravo:
    profile = BravoProfile.default()
    profile.connection.controller_type = "simulation"
    profile.accessories.devices = [
        AccessoryDeviceConfig(
            id="autofill", type="autofill", name="Autofill", location=3,
            settings={"tare": 21300, "range": 24300, "fill_speed_pct": 100},
        )
    ]
    return Bravo(profile=profile)


def test_simulated_fill_raises_the_weight_and_abort_stops_it():
    bravo = _sim_bravo()
    before = bravo.read_autofill_level("autofill")["reading"]

    bravo.run_autofill_pumps("autofill", duration_s=30, empty=False)
    time.sleep(0.3)
    bravo.abort()

    after = bravo.read_autofill_level("autofill")["reading"]
    assert after > before
    time.sleep(0.2)
    assert bravo.read_autofill_level("autofill")["reading"] == pytest.approx(after, abs=2)


def test_disconnect_stops_pumps():
    bravo = _sim_bravo()
    bravo.run_autofill_pumps("autofill", duration_s=30)
    driver = bravo._accessories._drivers["autofill"]
    bravo.disconnect()
    assert not driver.is_running
