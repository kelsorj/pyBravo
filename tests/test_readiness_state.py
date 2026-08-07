"""The three readiness gates the UI reports: connected, initialized, homed.

`homed` is tracked in software rather than polled. `is_axis_homed()` costs a
wire read per axis on Darwin, and the state feed runs several times a second —
polling six axes there would put traffic on the instrument link during motion,
which the state poller is deliberately throttled to avoid.

The trade-off is the same one `initialized` already makes: if the instrument is
power-cycled behind our back, the flag goes stale until the next connect.
"""

from __future__ import annotations

import pytest

from pybravo.bravo import Bravo
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis


def _bravo() -> Bravo:
    return Bravo(profile=BravoProfile.default(), mode="simulation")


def test_gates_start_false():
    b = _bravo()
    s = b.get_state()
    assert (s["connected"], s["initialized"], s["homed"]) == (False, False, False)


def test_connecting_does_not_set_initialized_or_homed():
    b = _bravo()
    b.connect()
    try:
        s = b.get_state()
        assert s["connected"] is True
        assert s["initialized"] is False
        assert s["homed"] is False
    finally:
        b.disconnect()


@pytest.mark.asyncio
async def test_initialize_satisfies_all_three_gates():
    b = _bravo()
    b.connect()
    try:
        await b.initialize()
        s = b.get_state()
        assert (s["connected"], s["initialized"], s["homed"]) == (True, True, True)
    finally:
        b.disconnect()


@pytest.mark.asyncio
async def test_homing_every_expected_axis_sets_homed():
    b = _bravo()
    b.connect()
    try:
        assert b.get_state()["homed"] is False
        await b.home()
        assert b.get_state()["homed"] is True
    finally:
        b.disconnect()


@pytest.mark.asyncio
async def test_a_partial_home_does_not_report_homed():
    """Homing X alone must not make the UI claim the machine is ready."""
    b = _bravo()
    b.connect()
    try:
        await b.home([Axis.X])
        s = b.get_state()
        assert s["homed"] is False
        assert "X" in s["homed_axes"]
    finally:
        b.disconnect()


@pytest.mark.asyncio
async def test_single_axis_home_is_recorded():
    b = _bravo()
    b.connect()
    try:
        await b.home_single_axis(Axis.Z)
        assert "Z" in b.get_state()["homed_axes"]
    finally:
        b.disconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_readiness():
    """A reconnected instrument may have been power-cycled — assume nothing."""
    b = _bravo()
    b.connect()
    await b.initialize()
    assert b.get_state()["homed"] is True

    b.disconnect()
    s = b.get_state()
    assert (s["connected"], s["initialized"], s["homed"]) == (False, False, False)
    assert s["homed_axes"] == []


def test_expected_axes_follow_the_machine():
    """A gripperless machine must not wait forever for G/Zg to be homed."""
    profile = BravoProfile.default()
    b = Bravo(profile=profile, mode="simulation")
    b.connect()
    try:
        expected = set(b._axes_expected_home())
        assert {Axis.X, Axis.Y, Axis.Z} <= expected

        profile.safety.ignore_w_axis = True
        assert Axis.W not in set(b._axes_expected_home())

        profile.axes.pop("G", None)
        profile.axes.pop("Zg", None)
        assert {Axis.G, Axis.Zg}.isdisjoint(set(b._axes_expected_home()))
    finally:
        b.disconnect()
