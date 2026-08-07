"""Integration tests for GeminiEngine against the FakeGeminiServer."""

from __future__ import annotations

import threading
import time

import pytest

from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    CommandNAKTypes,
    CommandTypes,
    CommonSubCommands,
    DarwinMasterNodeSubCommands,
    GeminiSubCommands,
)
from pybravo.protocol.gemini.errors import (
    GeminiTimeoutError,
    NAKError,
)
from pybravo.protocol.gemini.instruction import pack_float32
from pybravo.protocol.gemini.packet import (
    BROADCAST_ADDRESS,
    HOST_ADDRESS,
    InstructionAddress,
    Packet,
)
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


# --- Basic GET / SET --------------------------------------------------------


def test_get_value_returns_stored_uint(engine, fake):
    node4 = InstructionAddress(4)
    fake.storage[(4, 0, CommonSubCommands.FW_VERSION)] = 0x04000039
    assert engine.get_value(node4, CommonSubCommands.FW_VERSION) == 0x04000039


def test_get_value_on_unseeded_address_returns_zero(engine):
    assert engine.get_value(InstructionAddress(5), CommonSubCommands.FW_VERSION) == 0


def test_get_float_unpacks_as_ieee754(engine, fake):
    node5 = InstructionAddress(5)
    fake.storage[(5, 0, GeminiSubCommands.POSITION)] = pack_float32(123.456)
    assert abs(engine.get_float(node5, GeminiSubCommands.POSITION) - 123.456) < 1e-4


def test_set_uint_stores_value_and_returns(engine, fake):
    node4 = InstructionAddress(4, 1)
    engine.set_uint(node4, GeminiSubCommands.MOTOR_STATE, 18)  # READY
    assert fake.storage[(4, 1, GeminiSubCommands.MOTOR_STATE)] == 18


def test_set_float_packs_as_ieee754(engine, fake):
    node5 = InstructionAddress(5, 1)
    engine.set_float(node5, GeminiSubCommands.POSITION, 0.2)
    stored = fake.storage[(5, 1, GeminiSubCommands.POSITION)]
    assert stored == 0x3E4CCCCD


def test_master_convenience_helpers(engine, fake):
    fake.storage[(1, 0, CommonSubCommands.FW_VERSION)] = 0x01020304
    assert engine.master_get_uint(CommonSubCommands.FW_VERSION) == 0x01020304

    engine.master_set_uint(DarwinMasterNodeSubCommands.STATUS_LIGHTS, 0x64000000)
    assert fake.storage[(1, 0, DarwinMasterNodeSubCommands.STATUS_LIGHTS)] == 0x64000000


# --- Broadcast --------------------------------------------------------------


def test_broadcast_set_does_not_wait_for_response(engine, fake):
    """Broadcast set_uint returns quickly — no response is expected from the
    controller, and the engine must not wait for the full timeout."""
    start = time.monotonic()
    engine.set_uint(
        BROADCAST_ADDRESS, CommonSubCommands.TRIGGER, 127, timeout_ms=10_000
    )
    elapsed = time.monotonic() - start
    # Broadcast is send + sleep(BROADCAST_WAIT_MS). Should be well under 100ms
    # regardless of Windows timer granularity.
    assert elapsed < 0.1


def test_broadcast_trigger_reaches_callback(engine):
    received: list[Packet] = []
    engine.on_trigger(lambda p: received.append(p))

    engine.set_uint(BROADCAST_ADDRESS, CommonSubCommands.TRIGGER, 42)

    # Give the rx loop a moment to drain the local queue.
    for _ in range(50):
        if received:
            break
        time.sleep(0.01)

    assert len(received) == 1
    assert received[0].cmd_val == 42
    assert received[0].sub_command == CommonSubCommands.TRIGGER


# --- NAK handling -----------------------------------------------------------


def test_get_on_nak_raises(engine, fake):
    node4 = InstructionAddress(4)
    fake.seed_nak(node4, CommonSubCommands.FW_VERSION, CommandNAKTypes.INVALID_SUBCMD)
    with pytest.raises(NAKError) as exc_info:
        engine.get_value(node4, CommonSubCommands.FW_VERSION)
    assert exc_info.value.nak == CommandNAKTypes.INVALID_SUBCMD
    assert exc_info.value.dest_node == 4


def test_set_on_nak_raises(engine, fake):
    node4 = InstructionAddress(4, 1)
    fake.seed_nak(
        node4, GeminiSubCommands.MOTOR_STATE, CommandNAKTypes.MOVE_IN_PROGRESS
    )
    with pytest.raises(NAKError):
        engine.set_uint(node4, GeminiSubCommands.MOTOR_STATE, 18)


def test_engine_recovers_after_nak(engine, fake):
    """A NAK shouldn't corrupt engine state — the next request succeeds."""
    node4 = InstructionAddress(4)
    fake.seed_nak(node4, GeminiSubCommands.POSITION, CommandNAKTypes.READ_ONLY)
    with pytest.raises(NAKError):
        engine.get_value(node4, GeminiSubCommands.POSITION)

    fake.storage[(4, 0, GeminiSubCommands.POSITION)] = pack_float32(10.0)
    assert abs(engine.get_float(node4, GeminiSubCommands.POSITION) - 10.0) < 1e-4


# --- Timeout ----------------------------------------------------------------


def test_get_times_out_if_no_response(engine, fake):
    """Register a GET handler that returns None → fake falls through to storage
    lookup which would normally reply. So we make a handler that deliberately
    drops the response."""
    node4 = InstructionAddress(4)

    def swallow(pkt):
        # Return a sentinel unexpected cmd_type so the engine doesn't set
        # command_complete — simulates a dropped response.
        return Packet(
            src=pkt.dest, dest=pkt.src, cmd_type=CommandTypes.STREAM,
            sub_command=pkt.sub_command, cmd_val=0,
        )
    fake.on_get(node4, CommonSubCommands.FW_VERSION, swallow)

    with pytest.raises(GeminiTimeoutError):
        engine.get_value(node4, CommonSubCommands.FW_VERSION, timeout_ms=200)


# --- Multipacket ------------------------------------------------------------


def test_multipacket_applies_each_packet(engine, fake):
    node5 = InstructionAddress(5)
    packets = [
        Packet(
            src=HOST_ADDRESS, dest=node5, cmd_type=CommandTypes.SETCMD,
            sub_command=GeminiSubCommands.INSTR_NEW_INSTR, cmd_val=1,
        ),
        Packet(
            src=HOST_ADDRESS, dest=node5, cmd_type=CommandTypes.SETCMD,
            sub_command=GeminiSubCommands.INSTR_TBL_VAL, cmd_val=0xDEADBEEF,
        ),
    ]
    engine.send_multipacket(packets)
    assert fake.storage[(5, 0, GeminiSubCommands.INSTR_NEW_INSTR)] == 1
    assert fake.storage[(5, 0, GeminiSubCommands.INSTR_TBL_VAL)] == 0xDEADBEEF


def test_multipacket_chunks_oversize_batches(engine, fake):
    node4 = InstructionAddress(4)
    # 65 packets — one more than MAX_PACKETS_PER_MULTIPACKET. Engine must chunk.
    packets = [
        Packet(
            src=HOST_ADDRESS, dest=node4, cmd_type=CommandTypes.SETCMD,
            sub_command=GeminiSubCommands.INSTR_TBL_VAL, cmd_val=i,
        )
        for i in range(65)
    ]
    engine.send_multipacket(packets)
    # Fake should have seen two multipackets: 64 + 1
    assert len(fake.received_multipackets) == 2
    assert len(fake.received_multipackets[0]) == 64
    assert len(fake.received_multipackets[1]) == 1


def test_multipacket_empty_list_is_noop(engine, fake):
    engine.send_multipacket([])
    assert len(fake.received_multipackets) == 0


# --- Serialization (command lock) -------------------------------------------


def test_concurrent_requests_serialize_correctly(engine, fake):
    """Two threads hitting the engine at the same time must not interleave —
    each should get its own correct response."""
    node4 = InstructionAddress(4)
    node5 = InstructionAddress(5)
    fake.storage[(4, 0, CommonSubCommands.FW_VERSION)] = 0xAAAA0000
    fake.storage[(5, 0, CommonSubCommands.FW_VERSION)] = 0xBBBB0000

    results: dict[str, int] = {}

    def worker(name, addr, expected):
        for _ in range(20):
            results[name] = engine.get_value(addr, CommonSubCommands.FW_VERSION)
            assert results[name] == expected, f"{name}: got {results[name]:#010x}"

    t1 = threading.Thread(target=worker, args=("a", node4, 0xAAAA0000))
    t2 = threading.Thread(target=worker, args=("b", node5, 0xBBBB0000))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()


# --- End-to-end: replay a captured frame's request pattern -----------------


def test_engine_emits_bytes_matching_captured_fw_version_request(fake):
    """The first TX frame of a session is a GETCMD for SUBCMD_FW_VERSION
    on node 4. The engine should emit the exact 16-byte frame."""
    # Hand-craft the expected wire bytes: header + packet.
    expected_bytes = bytes.fromhex("aaaa010001000800" + "0004030400000000")

    engine = GeminiEngine("127.0.0.1", port=fake.port)
    engine.connect()
    try:
        # Seed so GET returns quickly.
        fake.storage[(4, 0, CommonSubCommands.FW_VERSION)] = 0x04000039
        engine.get_value(InstructionAddress(4), CommonSubCommands.FW_VERSION)

        # The fake records every packet it sees; we compare its packet's wire
        # representation to the expected bytes.
        assert len(fake.received_packets) == 1
        recvd = fake.received_packets[0]
        wire = bytes.fromhex("aaaa010001000800") + recvd.to_bytes()
        assert wire == expected_bytes
    finally:
        engine.close()
