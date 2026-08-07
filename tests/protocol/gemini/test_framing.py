"""Tests for frame header + multipacket payload codecs."""

from __future__ import annotations

import struct

import pytest

from pybravo.protocol.gemini.enums import (
    MAX_MULTIPACKET_SIZE,
    MAX_PACKETS_PER_MULTIPACKET,
    MSG_SYNC,
    PROTOCOL_VERSION,
    CommandTypes,
    CommonSubCommands,
    TCPMessageType,
)
from pybravo.protocol.gemini.framing import (
    FrameHeader,
    MultipacketResponse,
    pack_multipacket_batch,
    pack_multipacket_frame,
    pack_packet_frame,
    pack_serial_frame,
    unpack_multipacket_batch,
)
from pybravo.protocol.gemini.packet import (
    HOST_ADDRESS,
    InstructionAddress,
    Packet,
)


# --- Frame header ------------------------------------------------------------


def test_frame_header_packs_to_eight_little_endian_bytes():
    h = FrameHeader(
        msg_sync=MSG_SYNC,
        protocol_version=PROTOCOL_VERSION,
        payload_type=TCPMessageType.PACKET,
        payload_size=8,
    )
    assert h.to_bytes() == bytes.fromhex("aaaa010001000800")


def test_frame_header_roundtrip():
    h = FrameHeader(MSG_SYNC, PROTOCOL_VERSION, TCPMessageType.MULTIPACKET, 56)
    recovered = FrameHeader.from_bytes(h.to_bytes())
    assert recovered == h
    assert recovered.is_valid_sync


def test_frame_header_rejects_short_input():
    with pytest.raises(ValueError):
        FrameHeader.from_bytes(b"\x00" * 7)


# --- pack_*_frame shortcuts --------------------------------------------------


def test_pack_packet_frame_wraps_8_byte_packet():
    p = Packet.get_request(InstructionAddress(4), CommonSubCommands.FW_VERSION)
    frame = pack_packet_frame(p)
    assert frame == bytes.fromhex("aaaa010001000800") + p.to_bytes()
    assert len(frame) == 16


def test_pack_multipacket_frame_wraps_n_packets():
    packets = [
        Packet(HOST_ADDRESS, InstructionAddress(5), CommandTypes.SETCMD, 20, 1),
        Packet(HOST_ADDRESS, InstructionAddress(5), CommandTypes.SETCMD, 21, 0xFFFFFF00),
    ]
    frame = pack_multipacket_frame(packets)
    # header(8) + 2 packets(16) = 24
    assert len(frame) == 24
    # payload_size in header is 16 (2 packets * 8 bytes)
    header = FrameHeader.from_bytes(frame[:8])
    assert header.payload_type == TCPMessageType.MULTIPACKET
    assert header.payload_size == 16


def test_pack_serial_frame_requires_9_byte_payload():
    valid = bytes(range(9))
    frame = pack_serial_frame(valid)
    header = FrameHeader.from_bytes(frame[:8])
    assert header.payload_type == TCPMessageType.SERIAL_DATA
    assert header.payload_size == 9

    with pytest.raises(ValueError):
        pack_serial_frame(bytes(range(8)))
    with pytest.raises(ValueError):
        pack_serial_frame(bytes(range(10)))


# --- Multipacket batch encoding ---------------------------------------------


def test_multipacket_batch_roundtrip():
    packets = [
        Packet(HOST_ADDRESS, InstructionAddress(5), CommandTypes.SETCMD, sc, i)
        for i, sc in enumerate([20, 21, 21, 21, 21, 22, 23])
    ]
    payload = pack_multipacket_batch(packets)
    recovered = unpack_multipacket_batch(payload)
    assert recovered == packets


def test_multipacket_batch_rejects_oversize():
    packets = [
        Packet(HOST_ADDRESS, HOST_ADDRESS, CommandTypes.SETCMD, 0, 0)
        for _ in range(MAX_PACKETS_PER_MULTIPACKET + 1)
    ]
    with pytest.raises(ValueError):
        pack_multipacket_batch(packets)


def test_multipacket_batch_respects_byte_limit():
    packets_at_limit = [
        Packet(HOST_ADDRESS, HOST_ADDRESS, CommandTypes.SETCMD, 0, 0)
        for _ in range(MAX_PACKETS_PER_MULTIPACKET)
    ]
    payload = pack_multipacket_batch(packets_at_limit)
    assert len(payload) <= MAX_MULTIPACKET_SIZE


def test_unpack_multipacket_rejects_unaligned_input():
    with pytest.raises(ValueError):
        unpack_multipacket_batch(b"\x00" * 15)


# --- MultipacketResponse ----------------------------------------------------


def test_multipacket_response_success_roundtrip():
    r = MultipacketResponse(
        num_exchanges=7, error_code=0, error_device_addr=0, device_error_nak=0
    )
    data = r.to_bytes()
    assert data == bytes.fromhex("0700000000000000")
    assert r.is_success
    assert MultipacketResponse.from_bytes(data) == r


def test_multipacket_response_nak_encoding():
    r = MultipacketResponse(
        num_exchanges=3, error_code=1, error_device_addr=4, device_error_nak=11
    )
    # <HHBBH: num=3, err=1, addr=4, nak=11, pad=0
    expected = struct.pack("<HHBBH", 3, 1, 4, 11, 0)
    assert r.to_bytes() == expected
    assert not r.is_success


# --- Round-trip over the canonical value corpus ------------------------------


def test_multipacket_tx_batches_roundtrip(canonical_packets):
    """Every TX multipacket payload must parse into Packets and re-pack identically.

    Batches are built at a spread of sizes, including the single-packet and
    maximum-size edges, because batch length is what the framing layer gets
    wrong when it regresses.
    """
    batch_sizes = (1, 2, 3, 7, 16, 31, MAX_PACKETS_PER_MULTIPACKET)
    checked = 0
    for size in batch_sizes:
        for start in range(0, len(canonical_packets) - size, 37):
            packets = canonical_packets[start:start + size]
            payload = pack_multipacket_batch(packets)
            assert len(payload) == size * 8

            parsed = unpack_multipacket_batch(payload)
            assert len(parsed) == size
            roundtripped = pack_multipacket_batch(parsed)
            assert roundtripped == payload, (
                f"multipacket round-trip mismatch at batch size {size}, "
                f"offset {start}: {payload.hex()} vs {roundtripped.hex()}"
            )
            checked += 1
    assert checked > 100, f"expected hundreds of TX multipackets, saw {checked}"


def test_multipacket_rx_responses_roundtrip():
    """Every RX multipacket response must re-encode to its input bytes."""
    checked = 0
    for num_exchanges in (0, 1, 2, 63, 255, 4096, 0xFFFF):
        for error_code in (0, 1, 7, 0x00FF, 0xFFFF):
            for nak in (0, 1, 12, 255):
                for addr in (0, 4, 255):
                    resp = MultipacketResponse(
                        num_exchanges=num_exchanges,
                        error_code=error_code,
                        error_device_addr=addr,
                        device_error_nak=nak,
                    )
                    payload = resp.to_bytes()
                    assert len(payload) == 8
                    parsed = MultipacketResponse.from_bytes(payload)
                    assert parsed.to_bytes() == payload, (
                        "RX multipacket response round-trip mismatch for "
                        f"{resp}"
                    )
                    assert parsed.is_success == (error_code == 0)
                    checked += 1
    assert checked > 100, f"expected hundreds of RX responses, saw {checked}"
