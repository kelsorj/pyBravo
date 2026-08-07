"""Golden-file regression test for Bravo SRT homing.

Replays ``AgileSrtController.home_axes`` through a mock transport and asserts
that every hardware-affecting frame it emits (servo writes, PREPARE_MOVE,
move triggers, homing-complete markers, fault resets, home-register writes)
matches the known-good sequence recorded in
``tests/fixtures/srt_cold_init_homing_frames.json`` — the frames required to
home an SRT from a cold, un-homed state.

Homing is the operation most likely to damage hardware if the frame sequence
regresses, and it cannot be exercised in CI against a real instrument, so the
sequence is pinned here instead. Pure-read frames (register polls) are not
compared: they have no hardware effect. Frames are grouped and compared
per-axis rather than as one flat stream, because X and Y may be homed either
sequentially or in parallel — same frame set, different interleaving.

If this test fails, the homing frame sequence changed. Confirm the change is
intended and verified on hardware before updating the fixture. No hardware is
required to run the test itself.
"""

import json
import struct
from pathlib import Path

from pybravo.controllers.agile_srt import AgileSrtController
from pybravo.profile.profile import BravoProfile
from pybravo.protocol.v11_agile_7612_comm import V11Agile7612DeviceComm
from pybravo.types import Axis

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "srt_cold_init_homing_frames.json"
)

# byte7 values of Agile-packet frames that change hardware state.
_STATE_BYTE7 = {0x10, 0x31, 0x38, 0x52, 0x30, 0x54, 0x55}


def _expected_by_axis() -> dict[int, list[str]]:
    return {int(k): v for k, v in json.loads(_FIXTURE.read_text()).items()}


class _MockTransport:
    """Frame-valid mock: replies to each request so the controller runs."""

    is_connected = True

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._rx = b""

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def drain_pending(self) -> None: ...

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)
        self._rx += self._reply(frame)

    def receive_exact(self, n: int, timeout_ms: int = 2000) -> bytes:
        d, self._rx = self._rx[:n], self._rx[n:]
        return d + b"\x00" * (n - len(d)) if len(d) < n else d

    @staticmethod
    def _reply(frame: bytes) -> bytes:
        cmd = frame[0]
        length = struct.unpack_from("<H", frame, 1)[0]
        req = frame[3:3 + length]
        if cmd == 0xA1:
            pkt = bytearray(11)
            pkt[1] = 0x80
            if len(req) >= 8 and req[7] == 0x90:  # status read -> settled
                pkt[2] = pkt[3] = pkt[4] = pkt[5] = 0xB0
            if len(req) >= 2 and req[0] == 0x09 and req[1] == 0x10:
                # register 0x10 home-sensor read — 0x7d is the cold-start
                # value: X/Z/W on sensor (2-phase), Y off (3-phase).
                pkt[2] = 0x7D
            rd = bytes(pkt)
        elif cmd == 0xA7:
            rd = b"\x00\x00"
        else:
            rd = b"\x00"
        return bytes([cmd]) + struct.pack("<H", 1 + len(rd)) + b"\x00" + rd


def _state_changing(cmd: int, data: bytes) -> bool:
    if cmd == 0xA2 and len(data) == 17:
        return True
    return cmd == 0xA1 and len(data) == 11 and data[7] in _STATE_BYTE7


def _axis_index(cmd: int, data: bytes) -> int:
    return data[0] if cmd == 0xA2 else data[10]


def _canon(cmd: int, data: bytes) -> str:
    """Canonical form for comparison.

    The homing-complete (0x52) marker's data field is not compared (see
    agile_srt.py) — it carries scenario-specific bytes. Normalise it away so
    the comparison still verifies the marker is issued for the right axis
    with byte7=0x52.
    """
    if cmd == 0xA1 and len(data) == 11 and data[7] == 0x52:
        d = bytearray(data)
        d[2:7] = b"\x00" * 5  # data field
        d[9] = 0              # crc (depends on the data field)
        return bytes(d).hex()
    return data.hex()


def _by_axis(frames: list[tuple[int, bytes]]) -> dict[int, list[str]]:
    groups: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for cmd, data in frames:
        if _state_changing(cmd, data):
            groups[_axis_index(cmd, data)].append(_canon(cmd, data))
    return groups


def test_srt_homing_matches_golden_sequence() -> None:
    profile = BravoProfile.load(
        str(Path(__file__).resolve().parent.parent / "profiles" / "SRT_BRAVO.yaml")
    )
    controller = AgileSrtController(profile=profile)
    transport = _MockTransport()
    controller._comm = V11Agile7612DeviceComm(transport)
    controller._home_raw = {}
    controller._tracked_position = {}

    controller.home_axes([Axis.X, Axis.Y, Axis.Z, Axis.W])

    emitted = [
        (fr[0], fr[3:3 + struct.unpack_from("<H", fr, 1)[0]])
        for fr in transport.sent
    ]

    emitted_by_axis = _by_axis(emitted)
    expected_by_axis = _expected_by_axis()

    for axis_idx, name in ((2, "Z"), (3, "W"), (0, "X"), (1, "Y")):
        assert emitted_by_axis[axis_idx] == expected_by_axis[axis_idx], (
            f"{name}-axis homing frames diverge from the golden sequence"
        )
