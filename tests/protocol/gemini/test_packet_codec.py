"""Round-trip tests for the Packet and InstructionAddress codecs."""

from __future__ import annotations

import pytest

from pybravo.protocol.gemini.enums import CommandTypes, CommonSubCommands
from pybravo.protocol.gemini.packet import (
    BROADCAST_ADDRESS,
    HOST_ADDRESS,
    MASTER_ADDRESS,
    InstructionAddress,
    Packet,
)


# --- InstructionAddress unit tests --------------------------------------------


@pytest.mark.parametrize(
    "node_id, dev_id, expected_byte",
    [
        (0, 0, 0x00),      # host
        (1, 0, 0x01),      # master
        (63, 0, 0x3F),     # broadcast
        (4, 0, 0x04),      # YX node, device 0
        (4, 1, 0x44),      # YX node, device 1
        (5, 1, 0x45),
        (6, 0, 0x06),
        (6, 1, 0x46),
    ],
)
def test_instruction_address_byte_encoding(node_id, dev_id, expected_byte):
    addr = InstructionAddress(node_id=node_id, dev_id=dev_id)
    assert addr.byte == expected_byte
    roundtrip = InstructionAddress.from_byte(expected_byte)
    assert roundtrip.node_id == node_id
    assert roundtrip.dev_id == dev_id


def test_well_known_addresses():
    assert HOST_ADDRESS.byte == 0x00
    assert MASTER_ADDRESS.byte == 0x01
    assert BROADCAST_ADDRESS.byte == 0x3F


@pytest.mark.parametrize("node_id", [-1, 64, 100])
def test_node_id_out_of_range_raises(node_id):
    with pytest.raises(ValueError):
        InstructionAddress(node_id=node_id, dev_id=0)


@pytest.mark.parametrize("dev_id", [-1, 4, 10])
def test_dev_id_out_of_range_raises(dev_id):
    with pytest.raises(ValueError):
        InstructionAddress(node_id=0, dev_id=dev_id)


# --- Packet construction and round-trip ---------------------------------------


def test_packet_get_request_encoding():
    """The first frame of a session is a GETCMD for FW_VERSION on node 4."""
    p = Packet.get_request(
        dest=InstructionAddress(node_id=4, dev_id=0),
        sub_command=CommonSubCommands.FW_VERSION,
        msg_id=0,
    )
    assert p.to_bytes() == bytes.fromhex("0004030400000000")


def test_packet_msg_id_encoded_in_high_nibble():
    p = Packet(
        src=HOST_ADDRESS,
        dest=InstructionAddress(4),
        cmd_type=CommandTypes.GETCMD,
        sub_command=4,
        msg_id=2,
    )
    # byte 2 = (msg_id=2 << 4) | (cmd_type=3) = 0x23
    assert p.to_bytes()[2] == 0x23


def test_packet_roundtrip_simple():
    original = Packet(
        src=InstructionAddress(4, 1),
        dest=HOST_ADDRESS,
        cmd_type=CommandTypes.GETCMD_RESP,
        sub_command=30,
        cmd_val=0x12345678,
        msg_id=1,
    )
    recovered = Packet.from_bytes(original.to_bytes())
    assert recovered == original


def test_packet_cmd_val_is_big_endian():
    p = Packet(
        src=HOST_ADDRESS,
        dest=InstructionAddress(1),
        cmd_type=CommandTypes.SETCMD,
        sub_command=0,
        cmd_val=0x01020304,
    )
    b = p.to_bytes()
    assert b[4:8] == b"\x01\x02\x03\x04"


def test_packet_response_predicates():
    resp = Packet(HOST_ADDRESS, HOST_ADDRESS, CommandTypes.GETCMD_RESP, 4, 57)
    assert resp.is_response()
    assert not resp.is_error()

    err = Packet(HOST_ADDRESS, HOST_ADDRESS, CommandTypes.SETCMD_ERR_RESP, 4, 3)
    assert err.is_response()
    assert err.is_error()

    req = Packet(HOST_ADDRESS, HOST_ADDRESS, CommandTypes.SETCMD, 4, 0)
    assert not req.is_response()


def test_packet_rejects_wrong_length():
    with pytest.raises(ValueError):
        Packet.from_bytes(b"\x00" * 7)
    with pytest.raises(ValueError):
        Packet.from_bytes(b"\x00" * 9)


# --- Round-trip over the canonical value corpus -------------------------------


def test_packet_corpus_roundtrips_bit_exact(canonical_packet_payloads):
    """Every encodable packet must decode and re-encode byte-for-byte."""
    checked = 0
    for payload in canonical_packet_payloads:
        assert len(payload) == 8, f"non-8-byte packet payload: {payload.hex()}"
        pkt = Packet.from_bytes(payload)
        assert pkt.to_bytes() == payload, (
            f"round-trip mismatch: input={payload.hex()}, "
            f"output={pkt.to_bytes().hex()}"
        )
        checked += 1
    assert checked > 1000, f"expected thousands of packets, saw {checked}"


def test_packet_reserved_bits_are_not_preserved():
    """Bits 6-7 of byte 2 are reserved and are dropped on decode.

    This is why the round-trip corpus is built from encoder output rather than
    arbitrary bytes: a packet carrying those bits cannot re-serialize to its
    input, and asserting otherwise would be testing a property the wire format
    does not have.
    """
    with_reserved = bytes([0x01, 0x02, 0b1100_0011, 0x04, 0, 0, 0, 5])
    decoded = Packet.from_bytes(with_reserved)
    assert decoded.to_bytes() == bytes([0x01, 0x02, 0b0000_0011, 0x04, 0, 0, 0, 5])
    assert decoded.cmd_type == 0x03
    assert decoded.msg_id == 0
