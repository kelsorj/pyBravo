"""Topology tests — validates node addresses against observed hardware."""

from __future__ import annotations

import pytest

from pybravo.darwin.topology import (
    CONTROLLER_NODES,
    DARWIN_GZG,
    DARWIN_YX,
    DARWIN_ZW,
    all_axes,
    axis_address,
    axis_node,
)
from pybravo.types import Axis


def test_node_ids_match_bridge_build_system():
    assert DARWIN_YX.node_id == 4
    assert DARWIN_ZW.node_id == 5
    assert DARWIN_GZG.node_id == 6


@pytest.mark.parametrize(
    "axis, expected_byte",
    [
        # node 4 (DarwinYX): dev 0 = Y, dev 1 = X → Y=0x04, X=0x44
        (Axis.Y, 0x04),
        (Axis.X, 0x44),
        # node 5 (DarwinZW): dev 0 = Z, dev 1 = W → Z=0x05, W=0x45
        (Axis.Z, 0x05),
        (Axis.W, 0x45),
        # node 6 (DarwinGZg): dev 0 = G, dev 1 = Zg → G=0x06, Zg=0x46
        (Axis.G, 0x06),
        (Axis.Zg, 0x46),
    ],
)
def test_axis_address_wire_bytes(axis, expected_byte):
    """These addresses match destinations observed on hardware
    (frames 3-8 are position queries on all six axis devices)."""
    addr = axis_address(axis)
    assert addr.byte == expected_byte


def test_axis_node_lookup():
    assert axis_node(Axis.X) is DARWIN_YX
    assert axis_node(Axis.Y) is DARWIN_YX
    assert axis_node(Axis.Z) is DARWIN_ZW
    assert axis_node(Axis.W) is DARWIN_ZW
    assert axis_node(Axis.G) is DARWIN_GZG
    assert axis_node(Axis.Zg) is DARWIN_GZG


def test_all_axes_returns_all_six():
    assert set(all_axes()) == {Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg}
    assert len(all_axes()) == 6


def test_controller_nodes_covers_all_axes():
    covered: set[Axis] = set()
    for node in CONTROLLER_NODES:
        for axis in node.axes:
            covered.add(axis)
    assert covered == {Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg}


def test_node_address_is_device_zero():
    assert DARWIN_YX.address.byte == 0x04
    assert DARWIN_ZW.address.byte == 0x05
    assert DARWIN_GZG.address.byte == 0x06
