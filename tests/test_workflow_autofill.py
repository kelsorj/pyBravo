"""Autofill steps in workflows: Pump Reagent, Stop Pumps, Read Level.

Pump Reagent mirrors the reservoir task operators already know: fill or empty
the station at a location, optionally with the second pump, the weigh station
and concurrent operation. The safety property carries over from the driver:
no pump outlives its step unless asked to, and none outlives the workflow.
"""
from __future__ import annotations

import pytest

from pybravo.workflow.executor import WorkflowExecutor, _build_task_params
from tests.test_autofill import _sim_bravo

STATION = 3  # _sim_bravo() puts the autofill station at location 3


def _chain(*nodes: tuple[str, dict]) -> dict:
    """Start -> nodes... -> End, linked by flow links."""
    spec = [("flow/Start", {}), *nodes, ("flow/End", {})]
    graph_nodes, links = [], []
    for i, (node_type, props) in enumerate(spec, start=1):
        graph_nodes.append({
            "id": i, "type": node_type, "properties": props,
            "inputs": [{"name": "flow", "type": -1, "link": i - 1 if i > 1 else None}],
            "outputs": [{"name": "flow", "type": -1, "links": [i] if i < len(spec) else []}],
        })
        if i < len(spec):
            links.append([i, i, 0, i + 1, 0, -1])
    return {"nodes": graph_nodes, "links": links}


def _loop(count: int, node: tuple[str, dict]) -> dict:
    """Start -> Loop(count) -body-> node ; Loop -done-> End."""
    node_type, props = node
    return {
        "nodes": [
            {"id": 1, "type": "flow/Start", "properties": {},
             "outputs": [{"name": "flow", "type": -1, "links": [1]}]},
            {"id": 2, "type": "flow/Loop", "properties": {"count": count},
             "inputs": [{"name": "flow", "type": -1, "link": 1}],
             "outputs": [{"name": "body", "type": -1, "links": [2]},
                         {"name": "done", "type": -1, "links": [3]}]},
            {"id": 3, "type": node_type, "properties": props,
             "inputs": [{"name": "flow", "type": -1, "link": 2}],
             "outputs": [{"name": "flow", "type": -1, "links": []}]},
            {"id": 4, "type": "flow/End", "properties": {},
             "inputs": [{"name": "flow", "type": -1, "link": 3}], "outputs": []},
        ],
        "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1], [3, 2, 1, 4, 0, -1]],
    }


def _driver(bravo):
    return bravo._autofill_driver("autofill", require_enabled=False)


async def _run(bravo, graph) -> WorkflowExecutor:
    executor = WorkflowExecutor(bravo, graph, preview_animation=False)
    await executor.execute()
    return executor


def _fill(**overrides) -> tuple[str, dict]:
    props = {"location": STATION, "reservoir_mode": "fill", "pump_speed_pct": 50,
             "pump_on_time_s": 0.3, "allow_concurrent": False}
    props.update(overrides)
    return ("accessory/PumpReagent", props)


def test_pump_reagent_params():
    params = _build_task_params("accessory/PumpReagent", {
        "location": 3, "reservoir_mode": "empty", "pump_speed_pct": 80, "pump_on_time_s": 7,
        "how_often": 2, "allow_concurrent": True, "run_second_pump": True,
        "second_pump_speed_pct": 30, "use_weigh_station": True,
        "action_threshold_pct": 60, "stop_threshold_pct": 10,
    })
    assert params == {
        "location": 3, "reservoir_mode": "empty", "pump_speed_pct": 80.0, "pump_on_time_s": 7.0,
        "allow_concurrent": True, "run_second_pump": True, "second_pump_speed_pct": 30.0,
        "use_weigh_station": True, "action_threshold_pct": 60.0, "stop_threshold_pct": 10.0,
        "target_level_pct": 75.0, "time_from_target": False, "max_reach_time_s": 120.0,
    }


def test_mix_and_aspirate_pass_the_height_above_the_bottom():
    mix = _build_task_params("liquid/Mix", {"location": 3, "volume": 50, "cycles": 4,
                                            "distance_from_bottom": 2.5})
    assert mix["aspirate_distance"] == 2.5 and mix["mix_cycles"] == 4
    asp = _build_task_params("liquid/Aspirate", {"location": 3, "volume": 50, "distance_from_bottom": 1.5})
    assert asp["distance_from_bottom"] == 1.5


@pytest.mark.asyncio
async def test_fill_waits_for_the_pumps_then_read_level_stores_the_level():
    bravo = _sim_bravo()
    executor = await _run(bravo, _chain(
        _fill(),
        ("accessory/ReadLevel", {"location": STATION, "store_as": "level"}),
    ))
    assert not _driver(bravo).is_running
    assert executor._vars["level"] > 0


@pytest.mark.asyncio
async def test_pumping_publishes_the_level_for_the_3d_view():
    bravo = _sim_bravo()
    events = []

    async def on_event(event):
        events.append(event)

    executor = WorkflowExecutor(bravo, _chain(_fill(pump_on_time_s=1.0)), on_event=on_event,
                                preview_animation=False)
    await executor.execute()
    levels = [e["level_pct"] for e in events if e.get("type") == "workflow:autofill_level"]
    assert all(e["location"] == STATION for e in events if e.get("type") == "workflow:autofill_level")
    assert len(levels) >= 3 and levels[-1] > levels[0]


def _tank(controller, *, level=0.0, fill_gain=1.0, drain_gain=1.0, seconds=120.0, dt=0.5):
    """Simple reservoir: level change per second = gain * pump speed / 10."""
    inflows = []
    for _ in range(int(seconds / dt)):
        inflow, drain = controller.update(level, dt)
        inflows.append(inflow)
        level = max(0.0, level + (fill_gain * inflow - drain_gain * drain) / 10.0 * dt)
    return level, inflows


def test_the_measured_station_holds_without_overflowing():
    """Replays the hardware run that tripped the overflow guard.

    Measured on the instrument: inflow 50 % raised the level 6.4 %/s, and with
    the drain flat out it still rose 2.7 %/s. So the fill pump moves about
    3.5x what the drain does at the same setting, and 50 % inflow cannot be
    held. The controller must learn that, cut the inflow to what the drain can
    take, and settle without overshooting into the guard.
    """
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(65.0, 50.0)
    level, peak, inflows = 39.0, 0.0, []
    for _ in range(int(180 / 0.5)):
        inflow, drain = controller.update(level, 0.5)
        inflows.append(inflow)
        level = max(0.0, level + (0.128 * inflow - 0.037 * drain) * 0.5)
        peak = max(peak, level)
    assert peak < 80.0, f"overshot to {peak:.1f} %"
    assert abs(level - 65.0) < 3.0
    sustainable = 100 * 0.037 / 0.128
    assert inflows[-1] <= sustainable
    assert controller.inflow_limit is not None


def test_inflow_returns_to_the_request_when_it_is_sustainable():
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(50.0, 50.0)
    level, _ = _tank(controller, level=10.0, fill_gain=1.0, drain_gain=0.8, seconds=120)
    assert abs(level - 50.0) < 3.0
    assert controller.inflow == 50.0 and controller.inflow_limit is None


def test_hold_reaches_the_target_without_touching_the_inflow():
    from pybravo.accessories.autofill import LevelHoldController

    level, inflows = _tank(LevelHoldController(75.0, 50.0), level=0.0)
    assert abs(level - 75.0) < 2.0
    # The drain can hold the level, so the inflow ends where it was asked to be;
    # it only eases off briefly while the level approaches the target.
    assert inflows[-1] == 50.0
    assert min(inflows) >= 35.0
    assert sum(1 for i in inflows if i < 48.0) < len(inflows) / 10


def test_hold_compensates_for_mismatched_pumps():
    from pybravo.accessories.autofill import LevelHoldController

    # The drain pump moves 30 % less than the fill pump at the same setting.
    level, _ = _tank(LevelHoldController(60.0, 50.0), level=20.0, drain_gain=0.7)
    assert abs(level - 60.0) < 2.0


def test_hold_lowers_the_inflow_only_when_the_drain_cannot_keep_up():
    from pybravo.accessories.autofill import LevelHoldController

    # Even flat out, the drain moves less than the requested inflow.
    level, inflows = _tank(LevelHoldController(50.0, 90.0), level=50.0, drain_gain=0.5, seconds=240)
    assert inflows[-1] < 90.0
    assert abs(level - 50.0) < 5.0


def test_a_new_target_ramps_the_drain_instead_of_jumping():
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(75.0, 50.0, max_speed_change_pct_s=20.0)
    level, _ = _tank(controller, level=0.0)  # settle at 75 %
    controller.set_target(50.0)
    drains = [controller.drain]
    for _ in range(40):
        _, drain = controller.update(level, 0.5)
        drains.append(drain)
        level = max(0.0, level + (controller.inflow - drain) / 10.0 * 0.5)
    steps = [b - a for a, b in zip(drains, drains[1:])]
    assert max(abs(s) for s in steps) <= 10.0 + 1e-9  # 20 %/s over 0.5 s
    assert max(drains) > 60.0  # but it does drain harder to get there
    assert abs(level - 50.0) < 3.0


@pytest.mark.asyncio
async def test_a_hold_starting_below_target_never_opens_the_drain_first(monkeypatch):
    bravo = _sim_bravo()  # empty tray, well below the target
    calls = []
    driver = _driver(bravo)
    real = driver.run_pumps
    monkeypatch.setattr(driver, "run_pumps", lambda *a, **k: (calls.append(k), real(*a, **k))[1])
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                             target_level_pct=60, pump_on_time_s=30, allow_concurrent=True)
    assert calls[0]["empty_speed_pct"] == 0.0
    assert calls[0]["fill_speed_pct"] == 100.0  # priming the supply line
    bravo.stop_all_pumps()


def test_the_supply_is_primed_at_full_speed_until_the_level_rises():
    """Measured 2026-10-06: after idling, 50 % inflow delivered nothing in 90 s,
    while 100 % reached the tray after ~10 s."""
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(75.0, 50.0)
    inflows = [controller.update(0.0, 0.5)[0] for _ in range(10)]  # nothing arriving yet
    assert inflows == [100.0] * 10
    assert controller.update(2.0, 0.5)[0] == 50.0  # it arrives: straight to the request


def test_a_supply_that_stops_delivering_is_primed_again():
    """Measured 2026-10-07: the supply delivered for a few seconds at 50 %,
    then air reached the pump and the level stayed flat for 110 s."""
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(75.0, 50.0)
    controller.update(0.0, 0.5)
    for i in range(1, 6):
        controller.update(2.0 * i, 0.5)  # delivering: priming over, inflow 50 %
    assert controller.inflow == 50.0
    inflows = [controller.update(10.0, 0.5)[0] for _ in range(40)]  # level stuck for 20 s
    assert inflows[-1] == 100.0 and controller.fill_reprimes == 1
    assert inflows.index(100.0) <= 30  # noticed within ~15 s (the slope is smoothed)
    assert controller.update(12.0, 0.5)[0] == 50.0  # flowing again: back to the request


def test_supply_priming_gives_up_after_its_timeout():
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(75.0, 50.0, fill_prime_timeout_s=5.0)
    inflows = [controller.update(0.0, 0.5)[0] for _ in range(20)]
    # Gives up after 5 s; with nothing arriving it later tries again (the
    # hold's max_reach_time_s is what ends a supply that never delivers).
    assert inflows[8] == 100.0 and inflows[9] == 50.0


def test_a_hold_starting_above_target_ramps_the_drain_open():
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(40.0, 50.0, max_speed_change_pct_s=20.0, prime_s=0)
    _, drain = controller.update(90.0, 0.5)
    assert drain == 10.0  # opening, but no jump


def test_the_drain_is_primed_once_with_a_short_full_speed_pulse():
    """A drain line that ran dry does not pull at low speed (measured 2026-10-06:
    0.13 %/s at 35 % for 30 s, while a primed line does ~3.6 %/s), but it primes
    within about a second at 100 %."""
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(75.0, 50.0, prime_s=1.5)
    drains = [controller.update(2.0 * i, 0.5)[1] for i in range(30)]  # filling at 4 %/s up to 58 %
    assert max(drains) == 0.0  # still well below the target
    drains = [controller.update(61.0, 0.5)[1] for _ in range(6)]
    assert drains == [100.0] * 6  # full speed within 15 % of the target, level not falling yet
    drains = [controller.update(61.0 - 2.0 * i, 0.5)[1] for i in range(1, 8)]  # now it pulls
    assert drains[0] == 100.0 and min(drains) < 20.0  # then back to the smooth controller
    assert not controller.priming
    controller.update(30.0, 0.5)
    assert controller.update(70.0, 0.5)[1] < 100.0  # only once per hold


def test_drain_priming_gives_up_after_its_timeout():
    from pybravo.accessories.autofill import DRAIN_PRIME_TIMEOUT_S, LevelHoldController

    controller = LevelHoldController(75.0, 50.0, prime_fill=False)
    drains = [controller.update(70.0, 0.5)[1] for _ in range(int(DRAIN_PRIME_TIMEOUT_S / 0.5) + 2)]
    assert drains[0] == 100.0 and drains[-1] < 100.0


def test_an_almost_empty_tray_is_never_primed():
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(10.0, 50.0)
    assert controller.update(1.0, 0.5)[1] == 0.0


def test_a_new_inflow_ramps_too():
    from pybravo.accessories.autofill import LevelHoldController

    controller = LevelHoldController(50.0, 20.0, inflow_change_pct_s=10.0)
    controller.set_inflow(80.0)
    inflow, _ = controller.update(50.0, 0.5)
    assert inflow == 25.0  # 10 %/s: the inflow is the slow loop


@pytest.mark.asyncio
async def test_a_running_hold_takes_a_new_target():
    bravo = _sim_bravo()
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                             target_level_pct=60, pump_on_time_s=30, allow_concurrent=True)
    status = bravo.accessory_status()["devices"][0]["runtime"]["hold"]
    assert status["target_level_pct"] == 60
    bravo.update_autofill_hold("autofill", target_level_pct=30, inflow_pct=40)
    status = bravo.accessory_status()["devices"][0]["runtime"]["hold"]
    assert status["target_level_pct"] == 30 and status["requested_inflow_pct"] == 40
    bravo.stop_all_pumps()
    with pytest.raises(ValueError, match="target_level_pct"):
        bravo.update_autofill_hold("autofill", target_level_pct=120)


@pytest.mark.asyncio
async def test_hold_mode_in_simulation_settles_at_the_target():
    bravo = _sim_bravo()  # tare 21300 / range 24300, so 30 counts per %
    result = await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                                      target_level_pct=40, pump_on_time_s=20)
    assert result["status"] == "done"
    assert abs(result["end_level_pct"] - 40) < 5
    assert abs(result["final_inflow_pct"] - 50) < 3  # back near the request once settled
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_hold_time_can_count_from_reaching_the_target():
    import time

    bravo = _sim_bravo()  # empty tray: filling and priming come first
    t0 = time.monotonic()
    result = await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                                      target_level_pct=40, pump_on_time_s=3, time_from_target=True)
    elapsed = time.monotonic() - t0
    assert result["reached_target_after_s"] > 1.0
    assert result["held_s"] == 3
    assert elapsed >= result["reached_target_after_s"] + 3 - 0.6  # the hold itself was not cut short
    assert abs(result["end_level_pct"] - 40) < 5
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_hold_from_target_fails_when_the_target_is_out_of_reach():
    bravo = _sim_bravo()
    with pytest.raises(ValueError, match="not reached"):
        await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                                 target_level_pct=90, pump_on_time_s=30,
                                 time_from_target=True, max_reach_time_s=1.5)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_hold_from_target_explains_the_run_limit():
    bravo = _sim_bravo()
    with pytest.raises(ValueError, match="run limit"):
        await bravo.pump_reagent(STATION, reservoir_mode="hold", target_level_pct=50, pump_on_time_s=600,
                                 time_from_target=True, max_reach_time_s=120)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_a_hold_reports_its_phase():
    bravo = _sim_bravo()
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50, target_level_pct=60,
                             pump_on_time_s=30, allow_concurrent=True, time_from_target=True,
                             wait_for_target=False)  # as the manual panel does
    hold = bravo.accessory_status()["devices"][0]["runtime"]["hold"]
    assert hold["phase"] == "reaching" and hold["hold_seconds_remaining"] is None
    bravo.stop_all_pumps()


@pytest.mark.asyncio
async def test_a_concurrent_hold_from_target_returns_once_the_target_is_reached():
    """So a following Mix never starts in an empty or priming reservoir."""
    bravo = _sim_bravo()
    result = await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                                      target_level_pct=40, pump_on_time_s=30, allow_concurrent=True,
                                      time_from_target=True)
    assert result["status"] == "running"
    level = bravo.read_autofill_level("autofill")["level_pct"]
    assert abs(level - 40) < 4
    hold = bravo.accessory_status()["devices"][0]["runtime"]["hold"]
    assert hold["phase"] == "holding" and hold["hold_seconds_remaining"] > 25
    assert _driver(bravo).is_running  # still holding alongside the next steps
    bravo.stop_all_pumps()


@pytest.mark.asyncio
async def test_a_concurrent_hold_from_target_fails_the_step_when_out_of_reach():
    bravo = _sim_bravo()
    with pytest.raises(ValueError, match="not reached"):
        await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                                 target_level_pct=90, pump_on_time_s=30, allow_concurrent=True,
                                 time_from_target=True, max_reach_time_s=1.5)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_hold_stops_the_pumps_at_the_overflow_guard(monkeypatch):
    from pybravo.accessories.autofill import LevelHoldController

    bravo = _sim_bravo()
    # A regulator that never drains: the level climbs until the guard trips.
    monkeypatch.setattr(LevelHoldController, "update", lambda self, level, dt: (100.0, 0.0))
    result = await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=100,
                                      target_level_pct=50, pump_on_time_s=60)
    assert result["overflow_guard"] is True
    assert result["end_level_pct"] < 110
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_a_stopped_hold_does_not_fight_the_next_step():
    """Hold (concurrent) -> Stop Pumps -> Empty: the hold must let go at the stop.

    It used to keep regulating as soon as the drain step started the pumps
    again, switching the fill pump back on and holding the level against it.
    """
    import asyncio

    bravo = _sim_bravo()
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=100,
                             target_level_pct=60, pump_on_time_s=60, allow_concurrent=True)
    await asyncio.sleep(3.0)
    filled = bravo.read_autofill_level("autofill")["level_pct"]
    await bravo.autofill_stop_pumps(STATION)
    result = await bravo.pump_reagent(STATION, reservoir_mode="empty", pump_speed_pct=100,
                                      pump_on_time_s=20, use_weigh_station=True,
                                      action_threshold_pct=5, stop_threshold_pct=0)
    assert filled > 20
    assert result["end_level_pct"] <= 1.0, "the old hold kept refilling against the drain"
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_hold_needs_a_calibrated_weigh_station():
    bravo = _sim_bravo()
    _driver(bravo).config.range = _driver(bravo).config.tare
    with pytest.raises(ValueError, match="tare and range"):
        await bravo.pump_reagent(STATION, reservoir_mode="hold")


@pytest.mark.asyncio
async def test_weigh_station_stops_the_fill_at_the_stop_threshold():
    bravo = _sim_bravo()
    result = await bravo.pump_reagent(
        STATION, reservoir_mode="fill", pump_speed_pct=50, pump_on_time_s=30,
        use_weigh_station=True, action_threshold_pct=50, stop_threshold_pct=5,
    )
    assert result["status"] == "done"
    assert 5 <= result["end_level_pct"] < 15  # stopped long before the 30 s timeout
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_weigh_station_skips_a_fill_when_already_above_the_action_threshold():
    bravo = _sim_bravo()  # empty tray reads 0 %
    result = await bravo.pump_reagent(
        STATION, reservoir_mode="fill", pump_on_time_s=5,
        use_weigh_station=True, action_threshold_pct=0, stop_threshold_pct=50,
    )
    assert result["status"] == "skipped"
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_empty_mode_with_second_pump_runs_both_with_their_speeds(monkeypatch):
    bravo = _sim_bravo()
    calls = []
    driver = _driver(bravo)
    real = driver.run_pumps
    monkeypatch.setattr(driver, "run_pumps", lambda *a, **k: (calls.append((a, k)), real(*a, **k))[1])

    await bravo.pump_reagent(STATION, reservoir_mode="empty", pump_speed_pct=80, pump_on_time_s=0.2,
                             run_second_pump=True, second_pump_speed_pct=30)

    (args, kwargs), = calls
    assert args == (0.2,)
    assert kwargs == {"fill": True, "empty": True, "fill_speed_pct": 30, "empty_speed_pct": 80}


@pytest.mark.asyncio
async def test_how_often_acts_on_the_first_pass_and_every_nth(monkeypatch):
    bravo = _sim_bravo()
    passes = []

    async def counting_pump_reagent(**kwargs):
        passes.append(kwargs["location"])
        return {"status": "done"}

    monkeypatch.setattr(bravo, "pump_reagent", counting_pump_reagent)
    await _run(bravo, _loop(5, _fill(how_often=2)))
    assert len(passes) == 3  # passes 1, 3 and 5


@pytest.mark.asyncio
async def test_concurrent_pumping_is_stopped_when_the_workflow_ends():
    bravo = _sim_bravo()
    await _run(bravo, _chain(_fill(pump_on_time_s=60, allow_concurrent=True)))
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_stop_pumps_step():
    bravo = _sim_bravo()
    bravo.run_autofill_pumps("autofill", duration_s=60)
    await bravo.autofill_stop_pumps(STATION)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_no_station_at_the_location_is_an_error():
    bravo = _sim_bravo()
    with pytest.raises(ValueError, match="location 5"):
        await bravo.pump_reagent(5)


def test_aborting_a_workflow_stops_running_pumps():
    bravo = _sim_bravo()
    bravo.run_autofill_pumps("autofill", duration_s=60)
    executor = WorkflowExecutor(bravo, _chain(), preview_animation=False)
    executor.abort()
    assert not _driver(bravo).is_running
