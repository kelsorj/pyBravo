"""``get_all_positions`` must key its result by axis NAME on every backend.

``Bravo.get_all_positions`` delegates to the controller whenever the controller
implements the method, and that value is broadcast on ``/ws/state`` for the UI's
axis readouts and the URDF viewport.

``Axis`` is an ``IntEnum``, so an enum-keyed dict survives Python untouched but
serialises to JSON as ``{"0": ..., "1": ...}``. Nothing raises. The UI simply
looks up "X"/"Y"/"Z", finds nothing, and every axis reads 0.000 forever while
the jog log happily reports the moves. The simulation backend shipped that way,
so positions froze in simulation and only in simulation.
"""

from __future__ import annotations

import json

import pytest

from pybravo.types import Axis

AXIS_NAMES = {axis.name for axis in Axis}


def _simulation_controller():
    from pybravo.controllers.simulation import SimulationController
    from pybravo.profile.profile import BravoProfile

    controller = SimulationController(BravoProfile.default().head.head_type)
    controller.open_tcp("simulation")
    return controller


def test_simulation_positions_are_keyed_by_axis_name():
    positions = _simulation_controller().get_all_positions()

    assert positions, "expected a position for every axis"
    assert set(positions) <= AXIS_NAMES, (
        f"positions are keyed by {sorted(positions)!r} rather than axis names. "
        "Axis is an IntEnum, so enum keys reach the browser as \"0\"..\"5\" and "
        "every axis readout silently stays at zero"
    )
    assert all(isinstance(key, str) for key in positions)


def test_simulation_positions_survive_json_as_axis_names():
    """The failure only shows up after serialisation, so assert on that."""
    controller = _simulation_controller()
    controller.get_all_positions()  # ensure no lazy init changes the shape

    round_tripped = json.loads(json.dumps(controller.get_all_positions()))

    assert set(round_tripped) <= AXIS_NAMES, (
        f"after JSON the keys are {sorted(round_tripped)!r} — this is exactly "
        "what the browser receives"
    )
    assert "X" in round_tripped


def test_simulation_positions_track_moves():
    """A move has to be visible in the reported positions, by name."""
    from pybravo.controllers.base import AxisMoveInfo

    controller = _simulation_controller()
    controller.move([AxisMoveInfo(axis=Axis.X, position=263.04, absolute=True)], wait=True)

    positions = controller.get_all_positions()
    assert positions.get("X") == pytest.approx(263.04)


@pytest.mark.parametrize(
    "module_path, class_name",
    [
        ("pybravo.controllers.simulation", "SimulationController"),
        ("pybravo.controllers.agile_7612", "Agile7612Controller"),
        ("pybravo.darwin.controller", "DarwinController"),
    ],
)
def test_every_backend_declares_name_keyed_positions(module_path, class_name):
    """Static guard across backends: the annotation states the contract, and a
    backend that drifts from it breaks the UI without raising anywhere."""
    import importlib
    import typing

    cls = getattr(importlib.import_module(module_path), class_name)
    method = cls.__dict__.get("get_all_positions")
    if method is None:
        pytest.skip(f"{class_name} inherits get_all_positions")

    hints = typing.get_type_hints(method)
    assert hints.get("return") == dict[str, float], (
        f"{class_name}.get_all_positions is annotated {hints.get('return')!r}; "
        "it must return dict[str, float] keyed by Axis.name"
    )
