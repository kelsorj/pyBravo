"""Autofill supervisors and hold loops own only the run they started.

A supervisor that outlives its run (Stop Pumps, then a new Fill/Empty) must not
act on the next run, and a cancelled one must not leave its pumps running
without level protection.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.test_autofill import _sim_bravo
from tests.test_workflow_autofill import STATION, _driver


def _scripted_level(driver, start: float) -> dict:
    """Make the weigh pad report a level the test sets."""
    level = {"pct": start}
    driver.read_level = lambda: {"reading": 0, "tare": 0, "range": 1, "level_pct": level["pct"]}
    return level


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return predicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_s", [0.0, 0.3])
async def test_an_old_fill_supervisor_does_not_stop_a_later_empty(pause_s):
    bravo = _sim_bravo()
    driver = _driver(bravo)
    level = _scripted_level(driver, 0.0)

    await bravo.pump_reagent(STATION, reservoir_mode="fill", use_weigh_station=True,
                             action_threshold_pct=50, stop_threshold_pct=5,
                             pump_on_time_s=30, allow_concurrent=True)
    await asyncio.sleep(0.05)  # the supervisor is watching
    await bravo.autofill_stop_pumps(STATION)
    level["pct"] = 10.0  # above the old Fill's stop threshold
    await asyncio.sleep(pause_s)
    await bravo.pump_reagent(STATION, reservoir_mode="empty", pump_on_time_s=30, allow_concurrent=True)
    empty_run = driver.run_token

    await asyncio.sleep(0.8)  # several supervisor intervals
    assert driver.is_running and driver.run_token == empty_run, "the old Fill supervisor stopped the Empty"
    bravo.stop_all_pumps()


@pytest.mark.asyncio
async def test_a_fill_supervisor_still_stops_its_own_run():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    level = _scripted_level(driver, 0.0)
    await bravo.pump_reagent(STATION, reservoir_mode="fill", use_weigh_station=True,
                             action_threshold_pct=50, stop_threshold_pct=5,
                             pump_on_time_s=30, allow_concurrent=True)
    level["pct"] = 6.0
    assert await _wait_for(lambda: not driver.is_running)


@pytest.mark.asyncio
async def test_a_failed_concurrent_hold_stops_its_pumps_and_records_the_fault():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    real_read_level = driver.read_level
    reads = {"n": 0}

    def read_then_time_out():
        reads["n"] += 1
        if reads["n"] > 1:
            raise TimeoutError("weigh pad did not answer")
        return {**real_read_level(), "level_pct": 0.0}

    driver.read_level = read_then_time_out
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                             target_level_pct=75, pump_on_time_s=30, allow_concurrent=True)
    assert driver.is_running
    assert await _wait_for(lambda: not driver.is_running)
    runtime = bravo.accessory_status()["devices"][0]["runtime"]
    assert runtime["hold"] is None
    assert "did not answer" in runtime["last_error"]
    assert bravo.autofill_fault_count() == 1


@pytest.mark.asyncio
async def test_cancelling_a_hold_stops_its_pumps():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    _scripted_level(driver, 40.0)
    task = asyncio.ensure_future(
        bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                           target_level_pct=60, pump_on_time_s=30)
    )
    assert await _wait_for(lambda: driver.is_running)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not driver.is_running
    assert bravo.accessory_status()["devices"][0]["runtime"]["hold"] is None


@pytest.mark.asyncio
async def test_cancelling_a_pump_supervisor_stops_its_pumps():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    _scripted_level(driver, 0.0)
    task = asyncio.ensure_future(
        bravo.pump_reagent(STATION, reservoir_mode="fill", use_weigh_station=True,
                           action_threshold_pct=50, stop_threshold_pct=90, pump_on_time_s=30)
    )
    assert await _wait_for(lambda: driver.is_running)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not driver.is_running


@pytest.mark.asyncio
async def test_a_cancelled_hold_does_not_stop_a_newer_run():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    _scripted_level(driver, 40.0)
    task = asyncio.ensure_future(
        bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                           target_level_pct=60, pump_on_time_s=30)
    )
    assert await _wait_for(lambda: driver.is_running)
    await bravo.autofill_stop_pumps(STATION)
    await bravo.pump_reagent(STATION, reservoir_mode="empty", pump_on_time_s=30, allow_concurrent=True)
    empty_run = driver.run_token

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.is_running and driver.run_token == empty_run
    bravo.stop_all_pumps()
