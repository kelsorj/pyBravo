"""Fixtures for Gemini protocol tests.

Builds a deterministic corpus of canonical wire values so the codec round-trip
tests cover the protocol's whole value space rather than whatever happened to
appear in one recorded session.

"Canonical" matters. Not every byte string is a valid packet: ``Packet.to_bytes``
always writes zero into bits 6-7 of byte 2 (they are reserved), so a packet
parsed from a byte string with those bits set cannot re-serialize to its input.
Feeding arbitrary bytes to a round-trip test therefore produces false failures.
The generators below emit only values the encoder itself can produce, which is
the same property real captured traffic had.
"""

from __future__ import annotations

import pytest

from pybravo.protocol.gemini.packet import InstructionAddress, Packet

# A wide but deterministic spread of field values, including boundaries.
_NODE_IDS = (0, 1, 2, 4, 6, 7, 31, 32, 62, 63)
_DEV_IDS = (0, 1, 2, 3)
_CMD_TYPES = tuple(range(16))
_MSG_IDS = (0, 1, 2, 3)
_SUB_COMMANDS = (0, 1, 2, 63, 64, 76, 127, 128, 200, 254, 255)
_CMD_VALS = (
    0x00000000,
    0x00000001,
    0x000000FF,
    0x00007FFF,
    0x0000FFFF,
    0x3E4CCCCD,  # 0.2f as IEEE-754, a value the motion path really sends
    0x7FFFFFFF,
    0x80000000,
    0xDEADBEEF,
    0xFFFFFFFE,
    0xFFFFFFFF,
)


def _canonical_packets() -> list[Packet]:
    """Every combination the encoder can emit, sampled across each field.

    Cycling the less-structural fields against the fully-enumerated ones keeps
    the corpus in the low thousands while still touching every cmd_type,
    msg_id, node_id and dev_id at least once.
    """
    packets: list[Packet] = []
    i = 0
    for node_id in _NODE_IDS:
        for dev_id in _DEV_IDS:
            for cmd_type in _CMD_TYPES:
                for msg_id in _MSG_IDS:
                    packets.append(
                        Packet(
                            src=InstructionAddress(node_id=node_id, dev_id=dev_id),
                            dest=InstructionAddress(
                                node_id=_NODE_IDS[i % len(_NODE_IDS)],
                                dev_id=(i >> 2) & 0x03,
                            ),
                            cmd_type=cmd_type,
                            sub_command=_SUB_COMMANDS[i % len(_SUB_COMMANDS)],
                            cmd_val=_CMD_VALS[i % len(_CMD_VALS)],
                            msg_id=msg_id,
                        )
                    )
                    i += 1
    return packets


@pytest.fixture(scope="session")
def canonical_packets() -> list[Packet]:
    """Packets spanning the encodable value space."""
    return _canonical_packets()


@pytest.fixture(scope="session")
def canonical_packet_payloads(canonical_packets) -> list[bytes]:
    """The same corpus as 8-byte wire payloads."""
    return [p.to_bytes() for p in canonical_packets]
