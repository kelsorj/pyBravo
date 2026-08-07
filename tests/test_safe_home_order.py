"""Homing must lift the head and gripper before any lateral motion.

Homing drives axes to their limits and, on a cold start, no position is
trustworthy — the head may be sitting down inside labware. Moving X or Y first
drags the head and gripper sideways through whatever is on the deck.

HomeTask does try to retract Z and dock the gripper beforehand, but both steps
bail out when those axes are not yet homed:

    if not self._ctrl.is_axis_homed(Axis.Z):
        logger.info("Z not homed — skipping safe Z retract")
        return

That is exactly the cold-start case. So the ordering below is the real
guarantee, not the pre-retract.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from pybravo.profile.profile import BravoProfile
from pybravo.state_machine.tasks import HomeTask
from pybravo.types import SAFE_HOME_ORDER, Axis, safe_home_order

VERTICAL = (Axis.Z, Axis.Zg)
LATERAL = (Axis.X, Axis.Y)


class _SpyController:
    def __init__(self) -> None:
        self.issued: list[list[Axis]] = []

    def home_axes(self, axes, *, force: bool = False) -> None:
        self.issued.append(list(axes))


def _assert_safe(order: list[Axis]) -> None:
    for vertical in VERTICAL:
        for lateral in LATERAL:
            if vertical in order and lateral in order:
                assert order.index(vertical) < order.index(lateral), (
                    f"{vertical.name} must be homed before {lateral.name}: got "
                    f"{[a.name for a in order]}"
                )
    if Axis.Z in order and Axis.Zg in order:
        assert order.index(Axis.Z) < order.index(Axis.Zg), "Z lifts before Zg"


def test_canonical_order_is_z_then_zg_then_lateral():
    assert SAFE_HOME_ORDER == (Axis.Z, Axis.Zg, Axis.G, Axis.X, Axis.Y, Axis.W)
    _assert_safe(list(SAFE_HOME_ORDER))


@pytest.mark.parametrize(
    "requested",
    [pytest.param(list(p), id="".join(a.name for a in p))
     for p in itertools.permutations([Axis.X, Axis.Y, Axis.Z, Axis.Zg])],
)
def test_every_input_order_produces_a_safe_order(requested):
    """However the caller lists them, vertical clearance comes first."""
    _assert_safe(safe_home_order(requested))


def test_the_order_that_shipped_is_corrected():
    """The list Bravo.home() used to build, verbatim."""
    dangerous = [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg]
    assert [a.name for a in safe_home_order(dangerous)] == [
        "Z", "Zg", "G", "X", "Y", "W"
    ]


def test_home_task_issues_the_safe_order():
    """The task must reorder, not just trust its caller."""
    ctrl = _SpyController()
    task = HomeTask(ctrl, BravoProfile.default(),
                    [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg])
    asyncio.run(task._home_requested_axes())

    assert len(ctrl.issued) == 1
    _assert_safe(ctrl.issued[0])
    assert [a.name for a in ctrl.issued[0]] == ["Z", "Zg", "G", "X", "Y", "W"]


def test_subsets_are_handled():
    """A partial home must still respect the invariant."""
    assert [a.name for a in safe_home_order([Axis.Y, Axis.X])] == ["X", "Y"]
    assert [a.name for a in safe_home_order([Axis.X, Axis.Z])] == ["Z", "X"]
    # A machine with no gripper.
    assert [a.name for a in safe_home_order([Axis.X, Axis.Y, Axis.Z, Axis.W])] == [
        "Z", "X", "Y", "W"
    ]


def test_duplicates_are_dropped_and_nothing_is_lost():
    result = safe_home_order([Axis.X, Axis.X, Axis.Z, Axis.Z, Axis.Y])
    assert result == [Axis.Z, Axis.X, Axis.Y]
    # Every requested axis survives exactly once.
    assert set(result) == {Axis.X, Axis.Y, Axis.Z}


def test_empty_input_is_safe():
    assert safe_home_order([]) == []


def test_darwin_controller_orders_defensively(monkeypatch):
    """A direct call to the controller must not bypass the invariant."""
    from pybravo.darwin import axis as axis_module
    from pybravo.darwin.controller import DarwinController

    seen: list[str] = []
    monkeypatch.setattr(
        axis_module, "initialize",
        lambda engine, address, axis_name, **kw: seen.append(axis_name),
    )

    ctrl = DarwinController.__new__(DarwinController)
    ctrl._engine = None
    ctrl._ensure_waxis_params = lambda: None
    ctrl._is_estop_engaged = lambda: False

    DarwinController.home_axes(ctrl, [Axis.X, Axis.Y, Axis.Z, Axis.Zg])

    assert seen == ["Z", "Zg", "X", "Y"]


# --- Multi-axis home endpoint ------------------------------------------------


@pytest.mark.asyncio
async def test_home_axis_endpoint_orders_a_requested_group(monkeypatch):
    """The "Home XYZ" button sends a group; the server sequences it, not the UI.

    The button used to loop `['x','y','z']` client-side, one request per axis,
    which put X and Y before Z. Single-axis requests cannot be reordered by the
    server, so the sequence had to move server-side.
    """
    from pybravo.web import server

    homed: list[Axis] = []

    class _Bravo:
        async def home_single_axis(self, axis):
            homed.append(axis)

    monkeypatch.setattr(server, "get_bravo", lambda: _Bravo())

    res = await server.home_axis(server.AxisRequest(axes=["X", "Y", "Z"]))

    assert res["axes"] == ["Z", "X", "Y"]
    _assert_safe(homed)


@pytest.mark.asyncio
async def test_home_axis_endpoint_still_accepts_one_axis(monkeypatch):
    from pybravo.web import server

    homed: list[Axis] = []

    class _Bravo:
        async def home_single_axis(self, axis):
            homed.append(axis)

    monkeypatch.setattr(server, "get_bravo", lambda: _Bravo())

    res = await server.home_axis(server.AxisRequest(axis="w"))

    assert res == {"status": "homed", "axis": "w"}
    assert homed == [Axis.W]


@pytest.mark.asyncio
async def test_home_axis_endpoint_rejects_an_empty_request():
    from fastapi import HTTPException

    from pybravo.web import server

    with pytest.raises(HTTPException) as excinfo:
        await server.home_axis(server.AxisRequest())
    assert excinfo.value.status_code == 400


def test_home_task_forces_when_asked():
    """An operator home must not skip axes that merely look homed."""
    from pybravo.profile.profile import BravoProfile

    class _Spy:
        def __init__(self):
            self.forced = None

        def home_axes(self, axes, *, force=False):
            self.forced = force

    for requested_force in (True, False):
        ctrl = _Spy()
        task = HomeTask(ctrl, BravoProfile.default(), [Axis.Z], force=requested_force)
        asyncio.run(task._home_requested_axes())
        assert ctrl.forced is requested_force
