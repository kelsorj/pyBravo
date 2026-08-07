"""Motion primitive tests: instruction loading, event triggers, settle polling."""

from __future__ import annotations

import threading
import time

import pytest

from pybravo.darwin.motion import (
    MoveRequest,
    build_load_packets,
    load_instruction,
    move_absolute,
    move_multi,
    move_relative,
    trigger_event,
    wait_for_ready,
)
from pybravo.darwin.topology import axis_address
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    AxisDirection,
    CommandTypes,
    CommonSubCommands,
    GeminiSubCommands,
    InstructionTypes,
    MotorState,
)
from pybravo.protocol.gemini.instruction import Instruction
from pybravo.protocol.gemini.packet import (
    InstructionAddress,
    Packet,
)
from pybravo.types import Axis
from tests.fakes.gemini_fake import FakeGeminiServer


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


class _AxisMotionSim:
    """Simulated axis that responds to trigger events by transitioning READY.

    Also echoes the configured SEND_EVT back as a broadcast TRIGGER packet
    after ``complete_ms`` — mirrors what the real Darwin does to signal
    move completion, which our motion primitives now wait on.
    """

    def __init__(self, address: InstructionAddress, complete_ms: int = 20):
        self.address = address
        self.state = MotorState.READY
        self.complete_ms = complete_ms
        self.start_event: int | None = None
        self.send_event: int | None = None
        self._fake: FakeGeminiServer | None = None
        self._lock = threading.Lock()

    def install(self, fake: FakeGeminiServer) -> None:
        self._fake = fake
        fake.on_get(self.address, GeminiSubCommands.MOTOR_STATE, self._get_state)
        fake.on_set(self.address, GeminiSubCommands.MOTOR_STATE, self._set_motor_state)
        fake.on_set(self.address, GeminiSubCommands.START_EVT, self._set_start)
        fake.on_set(self.address, GeminiSubCommands.SEND_EVT, self._set_send)
        fake.on_broadcast(self._broadcast)

    def _set_send(self, pkt: Packet) -> Packet:
        with self._lock:
            self.send_event = pkt.cmd_val
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=0,
        )

    def _set_motor_state(self, pkt: Packet) -> Packet:
        with self._lock:
            try:
                self.state = MotorState(pkt.cmd_val)
            except ValueError:
                pass
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=0,
        )

    def _get_state(self, pkt: Packet) -> Packet:
        with self._lock:
            return Packet(
                src=pkt.dest, dest=pkt.src,
                cmd_type=CommandTypes.GETCMD_RESP,
                sub_command=pkt.sub_command,
                cmd_val=int(self.state),
            )

    def _set_start(self, pkt: Packet) -> Packet:
        # Go BUSY when the start event is set so the test-side polling loop
        # doesn't race with the broadcast listener (see note in class docstring).
        with self._lock:
            self.start_event = pkt.cmd_val
            self.state = MotorState.BUSY
        return Packet(
            src=pkt.dest, dest=pkt.src,
            cmd_type=CommandTypes.SETCMD_RESP,
            sub_command=pkt.sub_command, cmd_val=0,
        )

    def _broadcast(self, pkt: Packet) -> None:
        if pkt.sub_command != CommonSubCommands.TRIGGER:
            return
        with self._lock:
            if self.start_event is None or pkt.cmd_val != self.start_event:
                return
            complete_at = time.monotonic() + self.complete_ms / 1000.0
            send_event = self.send_event
            fake = self._fake

        def completer():
            while time.monotonic() < complete_at:
                time.sleep(0.001)
            with self._lock:
                self.state = MotorState.READY
            # Echo back the SEND_EVT as a broadcast TRIGGER, like real hardware
            if send_event is not None and fake is not None:
                from pybravo.protocol.gemini.framing import pack_packet_frame
                from pybravo.protocol.gemini.packet import (
                    BROADCAST_ADDRESS, Packet as _Packet,
                )
                echo = _Packet(
                    src=self.address, dest=BROADCAST_ADDRESS,
                    cmd_type=CommandTypes.SETCMD,
                    sub_command=CommonSubCommands.TRIGGER, cmd_val=send_event,
                )
                fake.send_to_client(pack_packet_frame(echo))

        threading.Thread(target=completer, daemon=True).start()


# --- Packet-list builder ----------------------------------------------------


def test_build_load_packets_produces_correct_sequence():
    addr = InstructionAddress(5)
    inst = Instruction(
        instr_type=InstructionTypes.MOVE_TO, velocity_percent=100.0,
        acceleration_percent=100.0, jerk_percent=100.0, force_percent=0.0,
        direction=AxisDirection.POSITIVE,
    )
    inst.volume = 0.2
    packets = build_load_packets(addr, inst, start_event=1, send_event=0x182)

    # 7 packets: NEW_INSTR(1), 4×TBL_VAL, START_EVT, SEND_EVT.
    assert len(packets) == 7
    subs = [p.sub_command for p in packets]
    assert subs == [
        GeminiSubCommands.INSTR_NEW_INSTR,
        GeminiSubCommands.INSTR_TBL_VAL,
        GeminiSubCommands.INSTR_TBL_VAL,
        GeminiSubCommands.INSTR_TBL_VAL,
        GeminiSubCommands.INSTR_TBL_VAL,
        GeminiSubCommands.START_EVT,
        GeminiSubCommands.SEND_EVT,
    ]
    # NEW_INSTR count = 1
    assert packets[0].cmd_val == 1
    # Words match instruction
    w0, w1, w2, w3 = inst.to_words()
    assert packets[1].cmd_val == w0
    assert packets[2].cmd_val == w1
    assert packets[3].cmd_val == w2  # should be float32 bits for 0.2 = 0x3E4CCCCD
    assert packets[3].cmd_val == 0x3E4CCCCD
    assert packets[4].cmd_val == w3
    assert packets[5].cmd_val == 1          # START_EVT = start_event
    assert packets[6].cmd_val == 0x182      # SEND_EVT = composite encoding


# --- load_instruction + trigger --------------------------------------------


def test_load_instruction_sends_one_multipacket(engine, fake):
    addr = axis_address(Axis.X)
    inst = Instruction(velocity_percent=50.0, acceleration_percent=50.0)
    inst.volume = 1.0
    load_instruction(engine, addr, inst, start_event=1)
    assert len(fake.received_multipackets) == 1
    assert len(fake.received_multipackets[0]) == 7


def test_trigger_event_broadcasts_subcmd_trigger(engine, fake):
    received = []
    fake.on_broadcast(lambda p: received.append(p))
    trigger_event(engine, event_number=42)
    # Give the broadcast listener time to fire
    for _ in range(30):
        if received:
            break
        time.sleep(0.005)
    assert len(received) == 1
    assert received[0].dest.node_id == 63
    assert received[0].sub_command == CommonSubCommands.TRIGGER
    assert received[0].cmd_val == 42


# --- wait_for_ready --------------------------------------------------------


def test_wait_for_ready_returns_when_axis_ready(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisMotionSim(addr, complete_ms=50)
    sim.install(fake)
    # Simulate an in-progress move
    sim.state = MotorState.BUSY
    def finish():
        time.sleep(0.03)
        sim.state = MotorState.READY
    threading.Thread(target=finish, daemon=True).start()
    wait_for_ready(engine, addr, "X", timeout_ms=2000, poll_ms=5)
    assert sim.state == MotorState.READY


def test_wait_for_ready_times_out(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisMotionSim(addr)
    sim.install(fake)
    sim.state = MotorState.BUSY
    with pytest.raises(BravoError) as exc_info:
        wait_for_ready(engine, addr, "X", timeout_ms=50, poll_ms=5)
    assert exc_info.value.error_type == ErrorType.MOVE_TIMEOUT


# --- move_absolute (single axis) -------------------------------------------


def test_move_absolute_loads_and_triggers(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisMotionSim(addr, complete_ms=80)
    sim.install(fake)

    move_absolute(
        engine, addr, "X", target_normalized=0.5,
        velocity_percent=50.0, acceleration_percent=50.0, timeout_ms=2000,
    )

    # After the move: axis is READY
    assert sim.state == MotorState.READY
    # We sent one multipacket (load) + a broadcast trigger
    assert len(fake.received_multipackets) == 1


def test_move_relative_encodes_direction(engine, fake):
    addr = axis_address(Axis.X)
    sim = _AxisMotionSim(addr, complete_ms=80)
    sim.install(fake)

    move_relative(
        engine, addr, "X", delta_normalized=0.25,
        direction=AxisDirection.NEGATIVE, timeout_ms=2000,
    )
    # Direction bit (word1 bit 16) should be clear since NEGATIVE.
    # Packets: 0=NEW_INSTR, 1=word0, 2=word1, 3=word2, 4=word3, 5=START, 6=SEND
    multipacket = fake.received_multipackets[0]
    word1_packet = multipacket[2]
    assert (word1_packet.cmd_val & (1 << 16)) == 0


# --- move_multi (coordinated) ----------------------------------------------


def test_move_multi_coordinated_completes(engine, fake):
    x_addr = axis_address(Axis.X)
    y_addr = axis_address(Axis.Y)
    # Both sims echo the shared SEND_EVT. move_multi returns on the first echo
    # — give both sims the same completion time so we don't race on the
    # subsequent assertion checks.
    x_sim = _AxisMotionSim(x_addr, complete_ms=60)
    y_sim = _AxisMotionSim(y_addr, complete_ms=60)
    x_sim.install(fake)
    y_sim.install(fake)

    move_multi(
        engine,
        [
            MoveRequest(x_addr, "X", target_normalized=0.3),
            MoveRequest(y_addr, "Y", target_normalized=0.5),
        ],
        timeout_ms=2000,
    )
    # Give the second sim's completer a moment to settle after the first echo.
    time.sleep(0.1)

    assert x_sim.state == MotorState.READY
    assert y_sim.state == MotorState.READY
    # A single multipacket contains both loads (2 axes × 7 packets = 14)
    assert len(fake.received_multipackets) == 1
    assert len(fake.received_multipackets[0]) == 14


def test_move_multi_empty_is_noop(engine, fake):
    move_multi(engine, [])
    assert not fake.received_multipackets
    assert not fake.received_packets


def test_move_multi_times_out_if_no_echo(engine, fake):
    """When no axis emits the SEND_EVT echo, move_multi raises MOVE_TIMEOUT."""
    x_addr = axis_address(Axis.X)
    y_addr = axis_address(Axis.Y)
    # complete_ms huge on both → neither sim emits the echo in the test budget.
    x_sim = _AxisMotionSim(x_addr, complete_ms=10_000)
    y_sim = _AxisMotionSim(y_addr, complete_ms=10_000)
    x_sim.install(fake)
    y_sim.install(fake)

    with pytest.raises(BravoError) as exc_info:
        move_multi(
            engine,
            [
                MoveRequest(x_addr, "X", target_normalized=0.1),
                MoveRequest(y_addr, "Y", target_normalized=0.1),
            ],
            timeout_ms=100,
        )
    assert exc_info.value.error_type == ErrorType.MOVE_TIMEOUT
    # Error message mentions both axis names
    assert "X" in str(exc_info.value) and "Y" in str(exc_info.value)
