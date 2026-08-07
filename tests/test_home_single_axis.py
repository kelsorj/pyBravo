"""An explicit per-axis Home must actually home the axis.

The Darwin start-up path deliberately skips an axis that already reports itself
initialized — that is what makes reconnecting to a running instrument cheap. But
the same call backs the operator's per-axis Home button, so pressing Home on a
healthy axis silently did nothing: no fault clear, no flag search, no motion.
It only appeared to work right after a power cycle, when nothing was homed yet.

W additionally has to end at 0, matching what a cold initialize leaves behind
(InitializeTask._home_w).
"""

from __future__ import annotations

import pytest

from pybravo.controllers.base import AxisMoveInfo
from pybravo.types import Axis


class _RecordingController:
    """Captures what an operator Home actually asks the hardware to do."""

    def __init__(self, w_position: float = 5.0) -> None:
        self.home_calls: list[tuple[list[Axis], bool]] = []
        self.moves: list[AxisMoveInfo] = []
        self._w = w_position

    def home_axes(self, axes, *, force: bool = False) -> None:
        self.home_calls.append((list(axes), force))

    def get_position(self, axis: Axis) -> float:
        return self._w if axis is Axis.W else 0.0

    def move(self, moves, wait: bool = True) -> None:
        self.moves.extend(moves)
        for m in moves:
            if m.axis is Axis.W:
                self._w = m.position


@pytest.fixture()
def bravo(monkeypatch):
    from pybravo.bravo import Bravo

    b = Bravo(mode="simulation")
    return b


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg])
async def test_operator_home_forces_the_routine(bravo, monkeypatch, axis):
    """Home must not be skipped just because the axis reports itself homed."""
    ctrl = _RecordingController()
    monkeypatch.setattr(type(bravo), "controller", property(lambda self: ctrl))

    await bravo.home_single_axis(axis)

    assert ctrl.home_calls == [([axis], True)], (
        "operator Home must pass force=True, otherwise an already-initialized "
        "axis is silently skipped and the button does nothing"
    )


@pytest.mark.asyncio
async def test_homing_w_returns_it_to_zero(bravo, monkeypatch):
    """A cold initialize leaves W at 0; an explicit W home must match."""
    ctrl = _RecordingController(w_position=12.5)
    monkeypatch.setattr(type(bravo), "controller", property(lambda self: ctrl))

    await bravo.home_single_axis(Axis.W)

    w_moves = [m for m in ctrl.moves if m.axis is Axis.W]
    assert len(w_moves) == 1, "W should be parked exactly once after homing"
    assert w_moves[0].position == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_w_already_at_zero_is_not_moved_again(bravo, monkeypatch):
    """No redundant plunger motion when W already homed to zero."""
    ctrl = _RecordingController(w_position=0.0)
    monkeypatch.setattr(type(bravo), "controller", property(lambda self: ctrl))

    await bravo.home_single_axis(Axis.W)

    assert [m for m in ctrl.moves if m.axis is Axis.W] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", [Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg])
async def test_non_w_axes_are_not_repositioned(bravo, monkeypatch, axis):
    """Homing X or Y must not trigger an unrequested lateral move."""
    ctrl = _RecordingController()
    monkeypatch.setattr(type(bravo), "controller", property(lambda self: ctrl))

    await bravo.home_single_axis(axis)

    assert ctrl.moves == []


def test_every_backend_accepts_force():
    """The kwarg has to exist on all backends or operator Home breaks on one."""
    import inspect

    from pybravo.controllers.agile import AgileController
    from pybravo.controllers.agile_7612 import Agile7612Controller
    from pybravo.controllers.agile_srt import AgileSrtController
    from pybravo.controllers.base import BravoController
    from pybravo.controllers.simulation import SimulationController
    from pybravo.darwin.controller import DarwinController

    for cls in (BravoController, SimulationController, AgileController,
                Agile7612Controller, AgileSrtController, DarwinController):
        sig = inspect.signature(cls.home_axes)
        assert "force" in sig.parameters, f"{cls.__name__}.home_axes lacks force"
        assert sig.parameters["force"].default is False, f"{cls.__name__} must default to False"
