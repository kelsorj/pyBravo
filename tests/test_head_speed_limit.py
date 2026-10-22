"""Head speed limit: X, Y and Z never move faster than the profile allows.

Requested 2026-10-22 for the first tip-wash runs on hardware ("slow head motion
everywhere"). Most task moves leave the velocity at 0, which the controller
runs at 100 % of the axis maximum, so the limit is applied where every move
passes: DarwinController.move.
"""
from __future__ import annotations

import pytest

from pybravo.controllers.base import AxisMoveInfo
from pybravo.darwin import controller as darwin
from pybravo.darwin.controller import DarwinController
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis


class _Limits:
    velocity = 500.0
    acceleration = 2000.0


@pytest.fixture()
def captured(monkeypatch):
    ctrl = DarwinController.__new__(DarwinController)
    ctrl._head_speed_limit_pct = 100.0
    ctrl._axes = {a: darwin._AxisState(calibration=darwin.AxisCalibration(hardware_min=-1000, hardware_max=1000))
                  for a in Axis}
    monkeypatch.setattr(ctrl, "_ensure_axis_enabled", lambda axis: None, raising=False)
    monkeypatch.setattr(ctrl, "_ensure_waxis_params", lambda: None, raising=False)
    monkeypatch.setattr(ctrl, "_limits", lambda axis: _Limits())
    monkeypatch.setattr(ctrl, "get_position", lambda axis: 0.0)
    from pybravo.protocol.gemini.enums import MotorState
    monkeypatch.setattr(darwin.axis_module, "read_motor_state", lambda *a, **k: MotorState.READY)
    calls = []
    monkeypatch.setattr(darwin.motion, "move_multi",
                        lambda engine, requests, wait, timeout_ms: calls.append((requests, timeout_ms)))
    ctrl._engine = None
    return ctrl, calls


def _by_axis(requests):
    return {r.axis_name: r for r in requests}


def test_no_limit_by_default(captured):
    ctrl, calls = captured
    ctrl.move([AxisMoveInfo(axis=Axis.X, position=100.0)])
    (requests, timeout), = calls
    assert requests[0].velocity_percent == 100.0
    assert timeout == 30000


def test_limit_caps_head_axes_and_stretches_the_timeout(captured):
    ctrl, calls = captured
    ctrl.set_head_speed_limit(25)
    ctrl.move([AxisMoveInfo(axis=Axis.X, position=100.0),
               AxisMoveInfo(axis=Axis.Z, position=10.0),
               AxisMoveInfo(axis=Axis.W, position=5.0)])
    (requests, timeout), = calls
    moves = _by_axis(requests)
    assert moves["X"].velocity_percent == 25.0 and moves["X"].acceleration_percent == 25.0
    assert moves["Z"].velocity_percent == 25.0
    assert moves["W"].velocity_percent == 100.0  # liquid handling keeps its rates
    assert timeout == 30000 * 4


def test_limit_never_speeds_a_slow_move_up(captured):
    ctrl, calls = captured
    ctrl.set_head_speed_limit(25)
    ctrl.move([AxisMoveInfo(axis=Axis.Y, position=10.0, velocity=50.0, acceleration=200.0)],
              timeout_ms=8000)
    (requests, timeout), = calls
    assert requests[0].velocity_percent == pytest.approx(10.0)  # 50 of 500 mm/s
    assert requests[0].acceleration_percent == pytest.approx(10.0)
    assert timeout == 8000  # not slowed by the limit, so not stretched


def test_limit_is_clamped(captured):
    ctrl, _ = captured
    ctrl.set_head_speed_limit(0)
    assert ctrl._head_speed_limit_pct == 1.0
    ctrl.set_head_speed_limit(250)
    assert ctrl._head_speed_limit_pct == 100.0


def test_profile_round_trips_the_limit(tmp_path):
    profile = BravoProfile.default()
    profile.safety.head_speed_limit_pct = 30.0
    path = tmp_path / "p.yaml"
    profile.save(path)
    assert BravoProfile.load(path).safety.head_speed_limit_pct == 30.0
