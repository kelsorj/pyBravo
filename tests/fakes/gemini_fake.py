"""A test-only fake Gemini controller.

Binds to a localhost port and speaks the same framed protocol the real Bravo
does. Intended for engine-level integration tests — it's not a full motion
simulator.

Handler model:
- Default: echo GET with a per-address/subcommand value from ``.storage``; echo
  SET with SETCMD_RESP.
- Custom: register a handler via ``on_get(addr, sub, handler)`` or
  ``on_set(...)`` for specific cases.
- Error injection: ``seed_nak(...)`` queues a NAK for the next matching request.

Usage::

    fake = FakeGeminiServer()
    fake.start()
    try:
        fake.storage[(4, 4)] = 0x04000039  # node 4, SUBCMD_FW_VERSION
        engine = GeminiEngine("127.0.0.1", port=fake.port)
        engine.connect()
        fw = engine.get_value(InstructionAddress(4), CommonSubCommands.FW_VERSION)
        assert fw == 0x04000039
    finally:
        engine.close()
        fake.stop()
"""

from __future__ import annotations

import logging
import socket
import threading
from collections import defaultdict, deque
from typing import Callable

from pybravo.protocol.gemini.enums import (
    FRAME_HEADER_SIZE,
    MSG_SYNC,
    PROTOCOL_VERSION,
    CommandTypes,
    TCPMessageType,
)
from pybravo.protocol.gemini.framing import (
    FrameHeader,
    MultipacketResponse,
    pack_packet_frame,
    unpack_multipacket_batch,
)
from pybravo.protocol.gemini.packet import InstructionAddress, Packet

logger = logging.getLogger(__name__)

# (node_id, dev_id, sub_command)
StorageKey = tuple[int, int, int]
Handler = Callable[[Packet], Packet | None]


class FakeGeminiServer:
    """Threaded localhost TCP server that serves Gemini requests."""

    def __init__(self, host: str = "127.0.0.1"):
        self._host = host
        self._port = 0
        self._server_sock: socket.socket | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # (node, dev, sub) -> stored uint32 value (returned for GETs)
        self.storage: dict[StorageKey, int] = {}
        # (node, dev, sub) -> list of values returned on successive SETs-as-GETs
        # (unused for now; reserved for streaming)
        self._nak_queue: dict[StorageKey, deque[int]] = defaultdict(deque)
        self._get_handlers: dict[StorageKey, Handler] = {}
        self._set_handlers: dict[StorageKey, Handler] = {}

        # Log of requests received (useful for test assertions)
        self.received_packets: list[Packet] = []
        self.received_multipackets: list[list[Packet]] = []

        # Listeners invoked for broadcast SETs (node_id=63). No response is sent.
        self._broadcast_listeners: list[Callable[[Packet], None]] = []

        # Reference to the currently-connected client so broadcast listeners
        # can push packets to it (used to emulate move-complete echoes).
        self._client_conn: socket.socket | None = None
        self._client_send_lock = threading.Lock()

    # --- Lifecycle ----------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, 0))
        self._port = self._server_sock.getsockname()[1]
        self._server_sock.listen(1)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="gemini-fake-accept", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._client_thread is not None:
            self._client_thread.join(timeout=2.0)
            self._client_thread = None

    def __enter__(self) -> "FakeGeminiServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # --- Handler registration ----------------------------------------------

    def on_get(
        self, addr: InstructionAddress, sub_command: int, handler: Handler
    ) -> None:
        """Register a callable that handles GETs for (addr, sub). Return a Packet
        to reply with, or None to fall back to storage lookup."""
        self._get_handlers[(addr.node_id, addr.dev_id, sub_command)] = handler

    def on_set(
        self, addr: InstructionAddress, sub_command: int, handler: Handler
    ) -> None:
        """Register a callable that handles SETs. Return a Packet to reply with,
        or None to fall back to default SETCMD_RESP."""
        self._set_handlers[(addr.node_id, addr.dev_id, sub_command)] = handler

    def seed_nak(
        self, addr: InstructionAddress, sub_command: int, nak_code: int
    ) -> None:
        """Queue a NAK response for the next request to (addr, sub)."""
        key = (addr.node_id, addr.dev_id, sub_command)
        self._nak_queue[key].append(nak_code)

    def on_broadcast(self, listener: Callable[[Packet], None]) -> None:
        """Register a callback for every broadcast SET packet (node_id=63).

        Used by tests to simulate axis responses to SUBCMD_TRIGGER.
        """
        self._broadcast_listeners.append(listener)

    # --- Server loop --------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            sock = self._server_sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            self._client_thread = threading.Thread(
                target=self._serve_client,
                args=(conn,),
                name="gemini-fake-client",
                daemon=True,
            )
            self._client_thread.start()

    def send_to_client(self, frame: bytes) -> None:
        """Write a fully-framed Gemini packet directly to the connected client.

        Used by broadcast listeners (e.g. the axis motion sim) to push
        move-complete trigger echoes.
        """
        with self._client_send_lock:
            conn = self._client_conn
            if conn is None:
                return
            try:
                conn.sendall(frame)
            except OSError:
                pass

    def _serve_client(self, conn: socket.socket) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(0.1)
        self._client_conn = conn
        try:
            while not self._stop_event.is_set():
                try:
                    header_bytes = _recv_exact(conn, FRAME_HEADER_SIZE)
                except socket.timeout:
                    continue
                except (ConnectionError, OSError):
                    return
                if header_bytes is None:
                    return
                header = FrameHeader.from_bytes(header_bytes)
                if not header.is_valid_sync:
                    logger.warning("fake: bad sync 0x%04x", header.msg_sync)
                    continue
                payload = b""
                if header.payload_size > 0:
                    payload_bytes = _recv_exact(conn, header.payload_size)
                    if payload_bytes is None:
                        return
                    payload = payload_bytes
                self._handle_frame(conn, header, payload)
        finally:
            with self._client_send_lock:
                self._client_conn = None
            try:
                conn.close()
            except OSError:
                pass

    def _handle_frame(
        self, conn: socket.socket, header: FrameHeader, payload: bytes
    ) -> None:
        if header.payload_type == TCPMessageType.PACKET:
            pkt = Packet.from_bytes(payload)
            with self._lock:
                self.received_packets.append(pkt)
            response = self._handle_packet(pkt)
            if response is not None:
                conn.sendall(pack_packet_frame(response))
        elif header.payload_type == TCPMessageType.MULTIPACKET:
            packets = unpack_multipacket_batch(payload)
            with self._lock:
                self.received_multipackets.append(packets)
                self.received_packets.extend(packets)
            # Apply each sub-packet: invoke registered handlers for their side
            # effects, then fall back to storage. Multipacket responses are
            # aggregated — no per-packet response, but handlers still fire.
            for p in packets:
                key = (p.dest.node_id, p.dest.dev_id, p.sub_command)
                if p.cmd_type == CommandTypes.SETCMD:
                    handler = self._set_handlers.get(key)
                    if handler is not None:
                        handler(p)
                    else:
                        self.storage[key] = p.cmd_val
                elif p.cmd_type == CommandTypes.GETCMD:
                    handler = self._get_handlers.get(key)
                    if handler is not None:
                        handler(p)
            resp = MultipacketResponse(
                num_exchanges=len(packets),
                error_code=0,
                error_device_addr=0,
                device_error_nak=0,
            )
            frame_hdr = FrameHeader(
                msg_sync=MSG_SYNC,
                protocol_version=PROTOCOL_VERSION,
                payload_type=TCPMessageType.MULTIPACKET,
                payload_size=len(resp.to_bytes()),
            )
            conn.sendall(frame_hdr.to_bytes() + resp.to_bytes())
        # SERIAL_DATA is not handled in v1

    def _handle_packet(self, pkt: Packet) -> Packet | None:
        key = (pkt.dest.node_id, pkt.dest.dev_id, pkt.sub_command)

        # Broadcast SETs don't expect a response. Notify listeners so tests can
        # simulate downstream effects (axis state transitions, event echoes).
        if pkt.dest.node_id == 63:
            for listener in self._broadcast_listeners:
                try:
                    listener(pkt)
                except Exception:  # pragma: no cover - diagnostic
                    import logging
                    logging.getLogger(__name__).exception(
                        "broadcast listener raised"
                    )
            return None

        # NAK queue takes priority.
        if self._nak_queue[key]:
            nak = self._nak_queue[key].popleft()
            err_cmd = (
                CommandTypes.GETCMD_ERR_RESP
                if pkt.cmd_type == CommandTypes.GETCMD
                else CommandTypes.SETCMD_ERR_RESP
            )
            return Packet(
                src=pkt.dest,
                dest=pkt.src,
                cmd_type=err_cmd,
                sub_command=pkt.sub_command,
                cmd_val=nak,
                msg_id=pkt.msg_id,
            )

        if pkt.cmd_type == CommandTypes.GETCMD:
            handler = self._get_handlers.get(key)
            if handler is not None:
                custom = handler(pkt)
                if custom is not None:
                    return custom
            value = self.storage.get(key, 0)
            return Packet(
                src=pkt.dest,
                dest=pkt.src,
                cmd_type=CommandTypes.GETCMD_RESP,
                sub_command=pkt.sub_command,
                cmd_val=value,
                msg_id=pkt.msg_id,
            )

        if pkt.cmd_type == CommandTypes.SETCMD:
            handler = self._set_handlers.get(key)
            if handler is not None:
                custom = handler(pkt)
                if custom is not None:
                    return custom
            self.storage[key] = pkt.cmd_val
            return Packet(
                src=pkt.dest,
                dest=pkt.src,
                cmd_type=CommandTypes.SETCMD_RESP,
                sub_command=pkt.sub_command,
                cmd_val=0,
                msg_id=pkt.msg_id,
            )

        return None


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except socket.timeout:
            raise
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
