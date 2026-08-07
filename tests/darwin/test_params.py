"""Tests for pointer-cached parameter access."""

from __future__ import annotations

import pytest

from pybravo.darwin.params import ParameterAccess
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import CommonSubCommands
from pybravo.protocol.gemini.instruction import pack_float32
from pybravo.protocol.gemini.packet import InstructionAddress
from tests.fakes.gemini_fake import FakeGeminiServer


@pytest.fixture
def fake():
    server = FakeGeminiServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def engine(fake):
    eng = GeminiEngine("127.0.0.1", port=fake.port)
    eng.connect()
    try:
        yield eng
    finally:
        eng.close()


@pytest.fixture
def params(engine):
    return ParameterAccess(engine, InstructionAddress(5, 1))  # W-axis


# --- Read side --------------------------------------------------------------


def test_read_uint_sets_pointer_and_reads_value(params, fake):
    addr_key = (5, 1)
    # Fake storage: the controller returns whatever was last stored for PARAM_DB_VALUE
    fake.storage[(*addr_key, CommonSubCommands.PARAM_DB_VALUE)] = 0x12345678
    value = params.read_uint(114)  # PARAM_DB_IDX_SPEED
    assert value == 0x12345678
    # And it should have sent SUBCMD_PARAM_DB_RD_PTR = 114 first
    assert fake.storage[(*addr_key, CommonSubCommands.PARAM_DB_RD_PTR)] == 114


def test_sequential_reads_skip_redundant_pointer_set(params, fake):
    addr_key = (5, 1)
    fake.storage[(*addr_key, CommonSubCommands.PARAM_DB_VALUE)] = 42
    # First read sets the pointer
    params.read_uint(100)
    # Second read to pointer+1 should NOT re-set the pointer
    # We track this by counting RD_PTR set requests
    rd_ptr_count_before = sum(
        1 for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_RD_PTR
    )
    params.read_uint(101)
    rd_ptr_count_after = sum(
        1 for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_RD_PTR
    )
    assert rd_ptr_count_after == rd_ptr_count_before, (
        "Sequential read should skip RD_PTR set"
    )


def test_non_sequential_read_sets_pointer_again(params, fake):
    addr_key = (5, 1)
    fake.storage[(*addr_key, CommonSubCommands.PARAM_DB_VALUE)] = 0
    params.read_uint(100)  # sets pointer to 100
    params.read_uint(105)  # NOT 101 → must re-set pointer
    rd_ptr_sets = [
        p.cmd_val for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_RD_PTR
    ]
    assert rd_ptr_sets == [100, 105]


def test_read_float_unpacks_ieee754(params, fake):
    fake.storage[(5, 1, CommonSubCommands.PARAM_DB_VALUE)] = pack_float32(0.2)
    value = params.read_float(114)
    assert abs(value - 0.2) < 1e-5


# --- Write side -------------------------------------------------------------


def test_write_uint_sets_pointer_and_writes_value(params, fake):
    params.write_uint(112, 0xDEADBEEF)  # ACCELERATION
    assert fake.storage[(5, 1, CommonSubCommands.PARAM_DB_WR_PTR)] == 112
    assert fake.storage[(5, 1, CommonSubCommands.PARAM_DB_VALUE)] == 0xDEADBEEF


def test_sequential_writes_skip_redundant_pointer_set(params, fake):
    params.write_uint(100, 0)
    params.write_uint(101, 0)  # sequential
    params.write_uint(102, 0)  # sequential
    wr_ptr_count = sum(
        1 for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_WR_PTR
    )
    # Only the FIRST write should have set the pointer
    assert wr_ptr_count == 1


def test_write_float_packs_ieee754(params, fake):
    params.write_float(114, 0.2)
    assert fake.storage[(5, 1, CommonSubCommands.PARAM_DB_VALUE)] == 0x3E4CCCCD


def test_bulk_sequential_write_saves_packets(params, fake):
    """The W-axis apply writes 58 parameters. Sequential writing should cut
    the RD/WR_PTR count drastically."""
    # Write params 90..147 (58 contiguous values)
    for i, pid in enumerate(range(90, 148)):
        params.write_float(pid, float(i))
    # Only the first write should have set WR_PTR
    wr_ptr_count = sum(
        1 for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_WR_PTR
    )
    value_count = sum(
        1 for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_VALUE
    )
    assert wr_ptr_count == 1, (
        f"expected 1 WR_PTR set, got {wr_ptr_count} (pointer caching broken)"
    )
    assert value_count == 58


# --- Database-wide ops ------------------------------------------------------


def test_apply_sends_param_db_apply(params, fake):
    params.apply()
    assert fake.storage[(5, 1, CommonSubCommands.PARAM_DB_APPLY)] == 1


def test_invalidate_cache_forces_next_pointer_set(params, fake):
    params.write_uint(100, 0)
    # Next would normally skip; but after invalidate, it shouldn't
    params.invalidate_cache()
    params.write_uint(101, 0)
    wr_ptr_sets = [
        p.cmd_val for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_WR_PTR
    ]
    # Two pointer sets: one before invalidate (100), one after (101)
    assert wr_ptr_sets == [100, 101]


def test_read_and_write_pointers_are_independent(params, fake):
    """Reads and writes use different pointers — reading param N then writing
    N+1 should still require a WR_PTR set."""
    fake.storage[(5, 1, CommonSubCommands.PARAM_DB_VALUE)] = 0
    params.read_uint(100)  # sets RD_PTR to 100
    params.write_uint(101, 42)  # NOT the RD pointer — must set WR_PTR
    wr_ptr_sets = [
        p.cmd_val for p in fake.received_packets
        if p.sub_command == CommonSubCommands.PARAM_DB_WR_PTR
    ]
    assert wr_ptr_sets == [101]
