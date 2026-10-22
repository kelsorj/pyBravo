"""A step that did not complete must stop the workflow, and the pumps with it.

Hardware run 2026-10-07 (Darwin-generation instrument): the operator crossed
the light curtain during Tips On. The press failed with STOP_DISABLE, every
Retry waited out a 30 s Z retract against disabled axes, the task was aborted
-- and the workflow reported the node as ok and started the water. The pump
module then answered every command with status 0x0B while our keepalive polls
kept its pumps running, and the hold loop, unable to read the level, never
reached its overflow guard.

Everything here runs against the simulation controller and fakes.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from pybravo.accessories.autofill import FAULT_QUIET_S, AutofillError, build_pump_status
from pybravo.bravo import RecoverRefused
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.state_machine.engine import (
    SAFETY_STOP_MAX_RETRIES,
    ErrorAction,
    StateMachineEngine,
    StateMachineTask,
    is_safety_stop,
    watch_aborted_tasks,
)
from pybravo.types import Axis, HeadType
from pybravo.workflow.executor import WorkflowExecutor
from tests.test_autofill import STOP, RecordingBus, _sim_bravo, _station
from tests.test_tips_on_press_gating import _build
from tests.test_workflow_autofill import STATION, _chain, _driver

SAFETY_STOP = BravoError(
    ErrorType.ROBOT_DISABLE,
    custom_text="Move aborted [Z]: controller broadcast RESERVED event STOP_DISABLE from node 1.0",
)
STATUS_POLL = build_pump_status(1)


# -- Helpers ------------------------------------------------------------------


class _FailingTipsOn(StateMachineTask):
    """Stands in for TipsOn: its press step raises ``error`` every time."""

    def __init__(self, error: BaseException) -> None:
        super().__init__("TipsOn_9")
        self.attempts = 0
        self._error = error

    def get_steps(self):
        async def lower_z_to_tips() -> None:
            self.attempts += 1
            raise RuntimeError(f"press failed: {self._error}") from self._error
        return [("lower_z_to_tips", lower_z_to_tips)]


def _install_failing_tips_on(bravo, error: BaseException = SAFETY_STOP) -> _FailingTipsOn:
    task = _FailingTipsOn(error)

    async def tips_on(location):  # like Bravo.tips_on: returns None when the task is aborted
        await bravo._engine.execute(task)

    bravo.tips_on = tips_on
    return task


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _wait_for_prompt(engine: StateMachineEngine) -> None:
    await _wait_for(lambda: engine.awaiting_error_action)


def _hold(**overrides) -> tuple[str, dict]:
    props = {"location": STATION, "reservoir_mode": "hold", "pump_speed_pct": 50,
             "target_level_pct": 60, "pump_on_time_s": 60, "allow_concurrent": True}
    props.update(overrides)
    return ("accessory/PumpReagent", props)


def _fill(**overrides) -> tuple[str, dict]:
    props = {"location": STATION, "reservoir_mode": "fill", "pump_speed_pct": 50,
             "pump_on_time_s": 60, "allow_concurrent": True}
    props.update(overrides)
    return ("accessory/PumpReagent", props)


def _sleep_script(seconds: float) -> tuple[str, dict]:
    return ("logic/Script", {"script": f"import time\ntime.sleep({seconds})", "timeout": 30})


def _start(bravo, graph):
    events: list[dict] = []

    async def on_event(event):
        events.append(event)

    executor = WorkflowExecutor(bravo, graph, on_event=on_event, preview_animation=False)
    run = asyncio.ensure_future(executor.execute())
    return executor, run, events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


def _started(events) -> list[int]:
    return [e["node_id"] for e in events if e.get("type") == "workflow:node_start"]


def _completed(events) -> list[int]:
    return [e["node_id"] for e in events if e.get("type") == "workflow:node_complete"]


def _assert_failed(events) -> dict:
    types = _types(events)
    assert "workflow:complete" not in types
    errors = [e for e in events if e["type"] == "workflow:error"]
    assert len(errors) == 1
    return errors[0]


def _assert_pumps_and_holds_stopped(bravo) -> None:
    assert not _driver(bravo).is_running
    assert bravo.accessory_status()["devices"][0]["runtime"]["hold"] is None
    assert all(task.done() for task in bravo._pump_supervisors)


class _RecoverableController:
    """Wraps the simulation controller with a recover() like the Darwin one."""

    def __init__(self, inner, result=None, error: Exception | None = None) -> None:
        self._inner = inner
        self.recover_calls = 0
        self._result = result if result is not None else {Axis.X: "ok", Axis.Z: "enabled"}
        self._error = error

    def recover(self):
        self.recover_calls += 1
        if self._error is not None:
            raise self._error
        return self._result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _with_recover(bravo, **kwargs) -> _RecoverableController:
    bravo.connect()
    ctrl = _RecoverableController(bravo._controller, **kwargs)
    bravo._controller = ctrl
    return ctrl


# -- A. A failed or aborted task fails the workflow node -----------------------


@pytest.mark.asyncio
async def test_an_aborted_tips_on_never_starts_the_next_pump_step_and_stops_the_hold():
    """The incident: Hold level (concurrent) -> Tips On (aborted) -> Pump Reagent."""
    bravo = _sim_bravo()
    _install_failing_tips_on(bravo, RuntimeError("Exceeded destination on Z."))
    executor, run, events = _start(bravo, _chain(_hold(), ("tips/TipsOn", {"location": 9}), _fill()))
    await _wait_for_prompt(bravo._engine)
    assert _driver(bravo).is_running, "the hold runs alongside Tips On"

    assert bravo.abort() is True
    await asyncio.wait_for(run, timeout=5)

    error = _assert_failed(events)
    assert "TipsOn_9 was aborted" in error["error"]
    assert 4 not in _started(events), "the pump step after the aborted Tips On must never start"
    assert 3 not in _completed(events), "the aborted node must not be reported ok"
    assert any(e["type"] == "workflow:task_aborted" and e["node_id"] == 3 for e in events)
    _assert_pumps_and_holds_stopped(bravo)


@pytest.mark.asyncio
async def test_a_retry_that_fails_again_and_is_then_aborted_fails_the_workflow():
    bravo = _sim_bravo()
    task = _install_failing_tips_on(bravo, RuntimeError("Exceeded destination on Z."))
    executor, run, events = _start(bravo, _chain(("tips/TipsOn", {"location": 9}), _fill()))
    await _wait_for_prompt(bravo._engine)
    assert bravo.retry() is True
    await _wait_for(lambda: task.attempts == 2 and bravo._engine.awaiting_error_action)
    assert bravo.abort() is True
    await asyncio.wait_for(run, timeout=5)

    _assert_failed(events)
    assert 3 not in _started(events)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_stopping_the_workflow_while_a_task_waits_on_its_prompt_ends_it_with_an_error():
    bravo = _sim_bravo()
    _install_failing_tips_on(bravo, RuntimeError("Exceeded destination on Z."))
    executor, run, events = _start(bravo, _chain(_fill(), ("tips/TipsOn", {"location": 9}), _fill()))
    await _wait_for_prompt(bravo._engine)
    executor.abort()  # the designer's Stop button
    await asyncio.wait_for(run, timeout=5)

    _assert_failed(events)
    assert 4 not in _started(events)
    _assert_pumps_and_holds_stopped(bravo)


@pytest.mark.asyncio
async def test_the_operator_abort_endpoint_also_ends_the_running_workflow(monkeypatch):
    from pybravo.web import server

    bravo = _sim_bravo()
    _install_failing_tips_on(bravo, RuntimeError("Exceeded destination on Z."))
    executor, run, events = _start(bravo, _chain(_fill(), ("tips/TipsOn", {"location": 9}), _fill()))
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_active_workflow_executor", executor)
    await _wait_for_prompt(bravo._engine)

    response = await server.abort()
    await asyncio.wait_for(run, timeout=5)

    assert response["accepted"] is True and response["workflow_stopped"] is True
    _assert_failed(events)
    assert 4 not in _started(events)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_stopping_the_workflow_between_nodes_is_an_error_not_a_completion():
    bravo = _sim_bravo()
    executor, run, events = _start(bravo, _chain(_fill(), _sleep_script(0.6), _fill()))
    await _wait_for(lambda: 3 in _started(events))
    executor.abort()
    await asyncio.wait_for(run, timeout=5)

    error = _assert_failed(events)
    assert "stopped by the operator" in error["error"]
    assert 4 not in _started(events)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_a_task_returning_aborted_status_ends_the_workflow_with_an_error():
    bravo = _sim_bravo()

    async def tips_on(location):
        return {"status": "aborted", "message": "Tips On aborted by operator."}

    bravo.tips_on = tips_on
    executor, run, events = _start(bravo, _chain(_fill(), ("tips/TipsOn", {"location": 9}), _fill()))
    await asyncio.wait_for(run, timeout=5)

    _assert_failed(events)
    assert 4 not in _started(events)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_the_engine_handlers_are_restored_after_a_workflow():
    bravo = _sim_bravo()
    before = bravo._engine.get_handlers()
    executor, run, events = _start(bravo, _chain(_fill(pump_on_time_s=0.2, allow_concurrent=False)))
    await asyncio.wait_for(run, timeout=5)
    assert "workflow:complete" in _types(events)
    assert bravo._engine.get_handlers() == before


@pytest.mark.asyncio
async def test_aborted_tasks_are_collected_per_call_chain():
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task = _FailingTipsOn(RuntimeError("boom"))
    with watch_aborted_tasks() as aborted:
        run = asyncio.ensure_future(engine.execute(task))
        await _wait_for_prompt(engine)
        engine.abort()
        await run
    assert aborted == [task]
    with watch_aborted_tasks() as unrelated:
        pass
    assert unrelated == []


# -- B. Safety stop -----------------------------------------------------------


def test_safety_stop_is_found_through_the_cause_chain():
    try:
        try:
            raise SAFETY_STOP
        except BravoError as exc:
            raise RuntimeError("Tips On failed") from exc
    except RuntimeError as wrapped:
        assert is_safety_stop(wrapped)
    assert is_safety_stop(BravoError(ErrorType.ROBOT_DISABLE_BUTTON))
    assert not is_safety_stop(RuntimeError("Exceeded destination on Z."))
    assert not is_safety_stop(BravoError(ErrorType.EXCEEDED_DEST))
    assert not is_safety_stop(None)


@pytest.mark.asyncio
async def test_a_safety_stop_needs_recover_before_retry_and_cannot_be_ignored():
    bravo = _sim_bravo()
    ctrl = _with_recover(bravo)
    task = _install_failing_tips_on(bravo)
    run = asyncio.ensure_future(bravo.tips_on(9))
    await _wait_for_prompt(bravo._engine)

    prompt = task.status_payload()["operator_prompt"]
    assert prompt["kind"] == "safety_stop"
    assert prompt["choices"] == ["recover", "retry", "abort"]
    assert "light curtain" in prompt["message"] and "Recover" in prompt["message"]

    assert bravo.ignore() is False
    assert "cannot be ignored" in bravo._engine.last_refusal
    assert bravo.retry() is False, "no Retry into a latched interlock"
    assert "Recover first" in bravo._engine.last_refusal

    assert bravo.recover()["status"] == "recovered"
    assert ctrl.recover_calls == 1
    assert bravo.retry() is True
    await _wait_for(lambda: task.attempts == 2 and bravo._engine.awaiting_error_action)
    assert bravo.retry() is False, "each safety stop needs its own Recover"
    assert bravo.abort() is True
    await asyncio.wait_for(run, timeout=2)


@pytest.mark.asyncio
async def test_safety_stop_retries_are_limited():
    bravo = _sim_bravo()
    _with_recover(bravo)
    task = _install_failing_tips_on(bravo)
    run = asyncio.ensure_future(bravo.tips_on(9))
    for attempt in range(1, SAFETY_STOP_MAX_RETRIES + 1):
        await _wait_for(lambda a=attempt: task.attempts == a and bravo._engine.awaiting_error_action)
        bravo.recover()
        assert bravo.retry() is True
    await _wait_for(lambda: task.attempts == SAFETY_STOP_MAX_RETRIES + 1
                    and bravo._engine.awaiting_error_action)
    prompt = task.status_payload()["operator_prompt"]
    assert prompt["choices"] == ["recover", "abort"]
    bravo.recover()
    assert bravo.retry() is False
    assert "limit" in bravo._engine.last_refusal
    assert bravo.abort() is True
    await asyncio.wait_for(run, timeout=2)
    assert task.attempts == SAFETY_STOP_MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_a_safety_stop_in_a_workflow_explains_the_recovery():
    bravo = _sim_bravo()
    _install_failing_tips_on(bravo)
    executor, run, events = _start(bravo, _chain(("tips/TipsOn", {"location": 9}), _fill()))
    await _wait_for_prompt(bravo._engine)
    assert bravo._engine.awaiting_safety_stop
    bravo.abort()
    await asyncio.wait_for(run, timeout=5)

    error = _assert_failed(events)
    assert "Safety stop" in error["error"] and "Recover" in error["error"] and "Home All" in error["error"]
    assert 3 not in _started(events)


def test_tips_on_does_not_command_a_retract_after_a_safety_stop():
    task, ctrl = _build(HeadType.HT_96_D_200, "lt_200ul")

    def stopped_jog(params):
        raise SAFETY_STOP

    ctrl.jog = stopped_jog
    with pytest.raises(BravoError) as raised:
        asyncio.run(task._lower_z_to_tips())
    assert is_safety_stop(raised.value)
    assert [m for m in ctrl.moves if m[0] == "Z"] == [], "no Z move while the interlock is latched"


def test_tips_on_still_retracts_after_an_ordinary_press_failure():
    task, ctrl = _build(HeadType.HT_96_D_200, "lt_200ul")

    def missing_tips(params):
        raise BravoError(ErrorType.EXCEEDED_DEST, axis=Axis.Z)

    ctrl.jog = missing_tips
    with pytest.raises(RuntimeError):
        asyncio.run(task._lower_z_to_tips())
    assert [m for m in ctrl.moves if m[0] == "Z"], "the head is retracted to safe Z"
    assert "ignore" in task._operator_prompt["choices"]


def test_tips_on_does_not_repeat_a_retract_that_already_failed():
    task, ctrl = _build(HeadType.HT_96_D_200, "lt_200ul")
    retracts = []

    def missing_tips(params):
        raise BravoError(ErrorType.EXCEEDED_DEST, axis=Axis.Z)

    def failing_move(moves, wait=True):
        retracts.append(moves)
        raise BravoError(ErrorType.MOVE_TIMEOUT, custom_text="Move timeout [Z]")

    ctrl.jog = missing_tips
    ctrl.move = failing_move
    for _ in range(3):  # the press and two Retries
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(task._lower_z_to_tips())
    assert len(retracts) == 1, "a failed retract must not be repeated on every Retry"
    assert "could NOT be retracted" in str(raised.value)


@pytest.mark.asyncio
async def test_recover_is_refused_while_a_step_is_still_executing():
    bravo = _sim_bravo()
    ctrl = _with_recover(bravo)
    release = asyncio.Event()

    class _Moving(StateMachineTask):
        def get_steps(self):
            async def move() -> None:
                await release.wait()
            return [("move", move)]

    run = asyncio.ensure_future(bravo._engine.execute(_Moving("Move")))
    await _wait_for(lambda: bravo._engine.is_busy)
    with pytest.raises(RecoverRefused) as refused:
        bravo.recover()
    assert refused.value.status_code == 409
    assert ctrl.recover_calls == 0, "axes must not be re-enabled under a running step"
    release.set()
    await run
    assert bravo.recover()["status"] == "recovered"


def test_recover_reports_an_incomplete_recovery_and_a_missing_one():
    bravo = _sim_bravo()
    _with_recover(bravo, result={Axis.X: "enabled", Axis.Z: "failed: NAK"})
    assert bravo.recover() == {"status": "incomplete", "axes": {"X": "enabled", "Z": "failed: NAK"}}

    plain = _sim_bravo()
    plain.connect()  # the simulation controller has no recover()
    with pytest.raises(RecoverRefused) as refused:
        plain.recover()
    assert refused.value.status_code == 400


@pytest.mark.asyncio
async def test_recover_endpoint_maps_a_latched_interlock_to_409(monkeypatch):
    from fastapi import HTTPException

    from pybravo.web import server

    bravo = _sim_bravo()
    _with_recover(bravo, error=BravoError(ErrorType.ROBOT_DISABLE, custom_text="interlock still active"))
    monkeypatch.setattr(server, "_bravo", bravo)
    with pytest.raises(HTTPException) as raised:
        await server.recover_after_safety_stop()
    assert raised.value.status_code == 409
    assert "interlock still active" in raised.value.detail

    _with_recover(bravo)
    assert (await server.recover_after_safety_stop())["status"] == "recovered"


class _SafetyEngine:
    """Gemini engine stand-in: answers SAFETY_STATUS, records everything else."""

    def __init__(self, safety_status=None) -> None:
        self._safety_status = safety_status
        self.calls: list[str] = []

    def master_get_uint(self, subcommand, timeout_ms=0):
        if self._safety_status is None:
            raise TimeoutError("no reply")
        return self._safety_status

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"unexpected engine call {name}")
        return record


@pytest.mark.parametrize("status,error_type", [
    (None, ErrorType.COULD_NOT_QUERY_STATE),  # unreadable: fail closed
    (0x01, ErrorType.ROBOT_DISABLE),           # interlock still latched
])
def test_darwin_recover_enables_nothing_unless_the_interlock_reads_clear(status, error_type):
    from pybravo.darwin.controller import DarwinController

    engine = _SafetyEngine(status)
    ctrl = DarwinController(engine=engine)
    with pytest.raises(BravoError) as raised:
        ctrl.recover()
    assert raised.value.error_type == error_type
    assert engine.calls == []


# -- C. Autofill module errors fail closed -------------------------------------


class FaultingBus(RecordingBus):
    """Answers normally until ``fault()``; then every reply carries status 0x0B."""

    def __init__(self) -> None:
        super().__init__()
        self.faulted_at: int | None = None

    def fault(self) -> None:
        self.faulted_at = len(self.sent)

    def __call__(self, payload: bytes) -> bytes:
        self.sent.append(payload)
        status = 0x0B if self.faulted_at is not None else 0
        return bytes([payload[0], status, self.weight & 0xFF, self.weight >> 8, 0, 0, 0, 0])

    def after_fault(self) -> list[bytes]:
        return self.sent[self.faulted_at:]


def test_a_keepalive_error_ends_the_run_and_stops_polling():
    bus = FaultingBus()
    station = _station(bus)
    station.run_pumps(30)
    time.sleep(0.4)
    bus.fault()
    time.sleep(1.5)  # ~5 keepalive intervals

    assert not station.is_running
    sent = bus.after_fault()
    assert [p for p in sent if p == STATUS_POLL] == [STATUS_POLL], "kept polling a module that reports errors"
    assert sent[1:] == [STOP, STOP], "the stop is sent at most twice"
    assert "0x0B" in station.last_error
    assert station.fault_count == 1


def test_a_weigh_pad_error_during_a_run_ends_the_run_and_quiets_the_bus():
    bus = FaultingBus()
    station = _station(bus)
    station.run_pumps(30)
    bus.fault()
    with pytest.raises(AutofillError, match="0x0B"):
        station.read_level()
    assert not station.is_running
    count = len(bus.sent)
    time.sleep(0.8)
    assert len(bus.sent) == count, "no more traffic once the run ended"
    with pytest.raises(AutofillError, match="quiet"):
        station.read_level()
    with pytest.raises(AutofillError, match="quiet"):
        station.run_pumps(5)
    assert len(bus.sent) == count, "nothing but a manual stop goes out while the bus is quiet"
    with pytest.raises(AutofillError):
        station.stop_pumps()  # a manual stop is still sent (and still fails here)
    assert bus.sent[count:] == [STOP, STOP]


def test_a_timeout_during_a_run_fails_closed_too():
    bus = RecordingBus(fail_on=bytes.fromhex("b3 01 00 00 00 00 00 00 00"))
    station = _station(bus)
    station.run_pumps(30)
    with pytest.raises(AutofillError, match="no reply"):
        station.read_level()
    assert not station.is_running
    assert bus.stops == 2
    assert station.fault_count == 1


def test_an_error_outside_a_run_sends_nothing_else():
    bus = FaultingBus()
    station = _station(bus)
    bus.fault()
    with pytest.raises(AutofillError):
        station.read_level()
    assert bus.after_fault() == [bytes.fromhex("b3 01 00 00 00 00 00 00 00")]
    assert "0x0B" in station.last_error
    assert station.fault_count == 0


def test_the_bus_quiet_time_ends():
    bus = FaultingBus()
    station = _station(bus)
    station.run_pumps(30)
    bus.fault()
    with pytest.raises(AutofillError):
        station.read_level()
    station._quiet_until = time.monotonic()  # pretend FAULT_QUIET_S has passed
    bus.faulted_at = None
    assert station.read_level()["reading"] == bus.weight
    # Neither stop was acknowledged, so no run starts until the retry (sent when
    # the quiet time ends) gets one through.
    assert station.stop_uncertain
    with pytest.raises(AutofillError, match="not acknowledged"):
        station.run_pumps(0.1)
    station._cancel_stop_retry()
    station._retry_stop()
    assert not station.stop_uncertain
    station.run_pumps(0.1)
    assert station.last_error is None, "a successful start clears the error"
    assert FAULT_QUIET_S >= 2.0


def _faulting_sim_bus(bravo):
    """Wrap the simulated module so it can be switched to answer 0x0B."""
    sim = bravo._accessory_serial_sender()
    state = {"faulted": False, "after": []}

    def bus(payload: bytes) -> bytes:
        if state["faulted"]:
            state["after"].append(payload)
            return bytes([payload[0], 0x0B, 0, 0, 0, 0, 0, 0])
        return sim(payload)

    _driver(bravo)._sender_provider = lambda: bus
    return state


@pytest.mark.asyncio
async def test_a_module_error_mid_hold_ends_the_hold_its_keepalive_and_the_workflow():
    bravo = _sim_bravo()
    state = _faulting_sim_bus(bravo)
    executor, run, events = _start(bravo, _chain(_hold(), _sleep_script(1.5), _fill()))
    await _wait_for(lambda: 3 in _started(events))
    assert _driver(bravo).is_running
    state["faulted"] = True
    await asyncio.wait_for(run, timeout=10)

    error = _assert_failed(events)
    assert "0x0B" in error["error"]
    assert 4 not in _started(events), "no pump step after the module error"
    _assert_pumps_and_holds_stopped(bravo)
    runtime = bravo.accessory_status()["devices"][0]["runtime"]
    assert "0x0B" in runtime["last_error"]

    polls = [p for p in state["after"] if p[0] == 0xAF]
    assert len(polls) <= 1, "kept polling a module that reports errors"
    assert len([p for p in state["after"] if p[0] == 0xAE]) <= 2
    count = len(state["after"])
    await asyncio.sleep(0.8)
    assert len(state["after"]) == count, "still talking to a module that reports errors"


@pytest.mark.asyncio
async def test_a_level_read_failure_stops_the_hold_instead_of_continuing_blind():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    real_read_level = driver.read_level
    reads = {"n": 0}

    def flaky_read_level():
        reads["n"] += 1
        if reads["n"] > 2:
            raise ValueError("weigh pad read garbled")
        return real_read_level()

    driver.read_level = flaky_read_level
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                             target_level_pct=60, pump_on_time_s=60, allow_concurrent=True)
    await _wait_for(lambda: not driver.is_running, timeout=5)
    await asyncio.sleep(0.1)
    assert bravo.accessory_status()["devices"][0]["runtime"]["hold"] is None
    assert "weigh pad read garbled" in driver.last_error
    assert bravo.autofill_fault_count() == 1


@pytest.mark.asyncio
async def test_a_missing_level_stops_a_blocking_hold_and_fails_its_step():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    real_read_level = driver.read_level
    reads = {"n": 0}

    def uncalibrated_after_start():
        reads["n"] += 1
        level = real_read_level()
        return level if reads["n"] <= 1 else {**level, "level_pct": None}

    driver.read_level = uncalibrated_after_start
    with pytest.raises(RuntimeError, match="no tare/range"):
        await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                                 target_level_pct=60, pump_on_time_s=30)
    assert not driver.is_running


@pytest.mark.asyncio
async def test_a_module_error_fails_a_blocking_hold_step():
    bravo = _sim_bravo()
    state = _faulting_sim_bus(bravo)
    run = asyncio.ensure_future(bravo.pump_reagent(
        STATION, reservoir_mode="hold", pump_speed_pct=50, target_level_pct=60, pump_on_time_s=30,
    ))
    await asyncio.sleep(0.8)
    state["faulted"] = True
    with pytest.raises(RuntimeError, match="0x0B"):
        await asyncio.wait_for(run, timeout=5)
    assert not _driver(bravo).is_running


@pytest.mark.asyncio
async def test_a_level_read_failure_stops_a_concurrent_weigh_station_fill():
    bravo = _sim_bravo()
    driver = _driver(bravo)
    real_read_level = driver.read_level
    reads = {"n": 0}

    def flaky_read_level():
        reads["n"] += 1
        if reads["n"] > 2:
            raise ValueError("weigh pad read garbled")
        return real_read_level()

    driver.read_level = flaky_read_level
    result = await bravo.pump_reagent(
        STATION, reservoir_mode="fill", pump_speed_pct=50, pump_on_time_s=60, allow_concurrent=True,
        use_weigh_station=True, action_threshold_pct=50, stop_threshold_pct=90,
    )
    assert result["status"] == "running"
    await _wait_for(lambda: not driver.is_running, timeout=5)
    assert "weigh pad read garbled" in driver.last_error


@pytest.mark.asyncio
async def test_stopping_all_pumps_ends_every_hold_loop():
    bravo = _sim_bravo()
    await bravo.pump_reagent(STATION, reservoir_mode="hold", pump_speed_pct=50,
                             target_level_pct=60, pump_on_time_s=60, allow_concurrent=True)
    assert bravo._pump_supervisors
    bravo.stop_all_pumps()
    await asyncio.sleep(0.05)
    _assert_pumps_and_holds_stopped(bravo)


@pytest.mark.asyncio
async def test_a_fault_from_before_the_run_does_not_fail_it():
    bravo = _sim_bravo()
    _driver(bravo).record_fault("old fault from a manual run")
    executor, run, events = _start(bravo, _chain(_fill(pump_on_time_s=0.2, allow_concurrent=False)))
    await asyncio.wait_for(run, timeout=5)
    assert "workflow:complete" in _types(events)


@pytest.mark.asyncio
async def test_retry_and_ignore_endpoints_say_why_they_were_refused(monkeypatch):
    from pybravo.web import server

    bravo = _sim_bravo()
    _install_failing_tips_on(bravo)
    monkeypatch.setattr(server, "_bravo", bravo)
    run = asyncio.ensure_future(bravo.tips_on(9))
    await _wait_for_prompt(bravo._engine)

    retry = await server.retry()
    ignore = await server.ignore_error()
    assert retry["accepted"] is False and "Recover" in retry["reason"]
    assert ignore["accepted"] is False and "cannot be ignored" in ignore["reason"]
    bravo._engine.resolve_error(ErrorAction.ABORT)
    await asyncio.wait_for(run, timeout=2)
