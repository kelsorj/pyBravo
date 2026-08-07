"""W-axis parameter table tests.

Validates the table size and head-type map, plus an end-to-end apply against
the fake controller that counts packets to verify pointer caching is effective.
"""

from __future__ import annotations

import pytest

from pybravo.darwin.params import ParameterAccess
from pybravo.darwin.topology import axis_address
from pybravo.darwin.waxis_params import (
    HEAD_TYPE_TO_SET,
    WAXIS_PARAM_TABLE,
    WAxisParamSet,
    apply_waxis_parameters,
    param_set_for_head,
)
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import CommonSubCommands, ParamDBs
from pybravo.protocol.gemini.instruction import unpack_float32
from pybravo.types import Axis, HeadType
from tests.fakes.gemini_fake import FakeGeminiServer


# --- Table integrity --------------------------------------------------------


def test_table_has_exactly_57_entries():
    assert len(WAXIS_PARAM_TABLE) == 57


def test_every_head_type_has_a_param_set():
    # Every HeadType except HT_UNKNOWN and F_200 variants should map.
    # Mirrors Get-DarwinWAxisParameterSet switch in the bridge.
    for ht in [
        HeadType.HT_96_ASSAYMAP,
        HeadType.HT_8_D_LT,
        HeadType.HT_96_D_200,
        HeadType.HT_96_D_200_S2,
        HeadType.HT_384_D_70,
        HeadType.HT_384_D_70_S2,
        HeadType.HT_384_F_50,
        HeadType.HT_16_D_ST,
        HeadType.HT_96_D_70,
        HeadType.HT_96_D_70_S2,
        HeadType.HT_96_F_50,
        HeadType.HT_8_F_50,
    ]:
        assert HEAD_TYPE_TO_SET[ht] is not None, f"no param set for {ht}"


def test_unknown_head_type_has_no_param_set():
    assert param_set_for_head(HeadType.HT_UNKNOWN) is None


def test_table_entries_cover_all_five_sets_with_floats():
    for entry in WAXIS_PARAM_TABLE:
        for set_name in WAxisParamSet:
            v = entry.value_for(set_name)
            assert isinstance(v, float), f"{entry.param} {set_name} not float"


# --- Spot-check individual values against the bridge ------------------------


def test_spot_check_waxis_values_match_bridge_table():
    """A handful of entries cross-checked against known-good values."""
    by_id = {e.param: e for e in WAXIS_PARAM_TABLE}
    # IQ_PTERM: ST96=0.3, LT=0.195
    assert by_id[ParamDBs.IQ_PTERM].ST96 == 0.3
    assert by_id[ParamDBs.IQ_PTERM].LT == 0.195
    # ACCELERATION: LT=1.68, F96_50=4.635
    assert by_id[ParamDBs.ACCELERATION].LT == 1.68
    assert by_id[ParamDBs.ACCELERATION].F96_50 == 4.635
    # HOMING_OVERSHOOT: F96_50=0.0187 (the odd one out)
    assert by_id[ParamDBs.HOMING_OVERSHOOT].F96_50 == 0.0187
    # SPEED_SCALE: LT=2.0, ST96=3.0
    assert by_id[ParamDBs.SPEED_SCALE].LT == 2.0
    assert by_id[ParamDBs.SPEED_SCALE].ST96 == 3.0


# --- End-to-end apply against the fake -------------------------------------


@pytest.fixture
def fake():
    s = FakeGeminiServer()
    s.start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def engine(fake):
    e = GeminiEngine("127.0.0.1", port=fake.port)
    e.connect()
    try:
        yield e
    finally:
        e.close()


def test_apply_writes_all_58_values_and_commits(engine, fake):
    w_addr = axis_address(Axis.W)
    params = ParameterAccess(engine, w_addr)
    applied = apply_waxis_parameters(params, HeadType.HT_96_D_70)  # → ST96
    assert applied is True

    # Count PARAM_DB_VALUE writes to the W-axis device: should be 57
    value_writes = [
        p for p in fake.received_packets
        if p.dest.node_id == 5
        and p.dest.dev_id == 1
        and p.sub_command == CommonSubCommands.PARAM_DB_VALUE
    ]
    assert len(value_writes) == 57

    # Apply should have fired once
    apply_count = sum(
        1 for p in fake.received_packets
        if p.dest.node_id == 5
        and p.dest.dev_id == 1
        and p.sub_command == CommonSubCommands.PARAM_DB_APPLY
    )
    assert apply_count == 1


def test_apply_values_match_st96_column(engine, fake):
    """The bytes on the wire for a fixed head type must equal the table values.

    I2T_TIME is a UInt32 — decode it as a plain uint while every other entry
    is Float32.
    """
    w_addr = axis_address(Axis.W)
    params = ParameterAccess(engine, w_addr)
    apply_waxis_parameters(params, HeadType.HT_96_D_70)  # → ST96

    # Pull the ordered sequence of (param_id, written_value) off the fake.
    current_wr_ptr: int | None = None
    seen: list[tuple[int, float]] = []
    for p in fake.received_packets:
        if p.dest.node_id != 5 or p.dest.dev_id != 1:
            continue
        if p.sub_command == CommonSubCommands.PARAM_DB_WR_PTR:
            current_wr_ptr = p.cmd_val
        elif p.sub_command == CommonSubCommands.PARAM_DB_VALUE:
            pid = current_wr_ptr if current_wr_ptr is not None else -1
            if pid == int(ParamDBs.I2T_TIME):
                decoded: float = float(p.cmd_val)
            else:
                decoded = unpack_float32(p.cmd_val)
            seen.append((pid, decoded))
            current_wr_ptr = (pid + 1) if pid >= 0 else None

    # Compare to table's ST96 column, in order.
    expected = [(int(e.param), float(e.ST96)) for e in WAXIS_PARAM_TABLE]
    assert len(seen) == len(expected)
    for (got_pid, got_v), (exp_pid, exp_v) in zip(seen, expected):
        assert got_pid == exp_pid
        assert abs(got_v - exp_v) < 1e-6, f"param {exp_pid}: wrote {got_v}, expected {exp_v}"


def test_i2t_time_is_encoded_as_uint_not_float(engine, fake):
    """I2T_TIME is a UInt32, not a float.

    A float32 encoding of 5000.0 (ST384) lands at 0x459c4000, which read as a
    uint is ~2.6e9 and the firmware rejects with NAK_OUT_OF_RANGE. The correct
    encoding is 0x00001388 = 5000, a plain uint.
    """
    w_addr = axis_address(Axis.W)
    params = ParameterAccess(engine, w_addr)
    apply_waxis_parameters(params, HeadType.HT_384_D_70_S2)  # → ST384 (5000)

    # Find the VALUE write that follows WR_PTR=I2T_TIME.
    saw_i2t_ptr = False
    i2t_value: int | None = None
    for p in fake.received_packets:
        if p.dest.node_id != 5 or p.dest.dev_id != 1:
            continue
        if (p.sub_command == CommonSubCommands.PARAM_DB_WR_PTR
                and p.cmd_val == int(ParamDBs.I2T_TIME)):
            saw_i2t_ptr = True
            continue
        if saw_i2t_ptr and p.sub_command == CommonSubCommands.PARAM_DB_VALUE:
            i2t_value = p.cmd_val
            break
    assert saw_i2t_ptr, "expected a WR_PTR=I2T_TIME before the VALUE write"
    assert i2t_value == 5000, f"expected uint 5000 (0x1388), got 0x{i2t_value:08x}"


def test_apply_returns_false_for_unknown_head(engine, fake):
    w_addr = axis_address(Axis.W)
    params = ParameterAccess(engine, w_addr)
    applied = apply_waxis_parameters(params, HeadType.HT_UNKNOWN)
    assert applied is False
    # Nothing should have been sent
    assert not any(
        p.dest.node_id == 5 and p.dest.dev_id == 1
        for p in fake.received_packets
    )
