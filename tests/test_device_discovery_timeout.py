"""Device discovery must always finish, whatever is on the network.

A subnet sweep touches every host on the LAN, including devices that are not
Bravos. Anything that accepts the port and then stops talking can stall a probe.
Two things used to make that unbounded:

* ``ThreadPoolExecutor`` used as a context manager calls ``shutdown(wait=True)``
  on exit, joining every worker. The ``as_completed`` timeout stops us
  *collecting* results but not the probes *running*, so one stuck probe held the
  request open indefinitely.
* ``_recv_exact_raw`` applied its timeout per ``recv``, so a peer sending one
  byte at a time kept resetting it.

Symptom: "Scanning 253 IPs for Bravo devices…" and then nothing, forever.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from pybravo.web import server


def test_scan_returns_within_budget_when_probes_never_finish(monkeypatch):
    """A hung probe must not hold the sweep open past its budget."""
    stall_ips = {"10.99.0.5", "10.99.0.6", "10.99.0.7"}
    release = threading.Event()

    def fake_probe(ip: str):
        if ip in stall_ips:
            release.wait(timeout=120)  # never completes within the budget
            return None
        return None

    monkeypatch.setattr(server, "_probe_bravo", fake_probe)
    monkeypatch.setattr(server, "_build_candidate_ips", lambda *a, **k: set(stall_ips))
    monkeypatch.setattr(server, "_SCAN_BUDGET_S", 2.0)

    started = time.monotonic()
    try:
        devices = server._scan_subnet([], None)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert devices == []
    assert elapsed < 6.0, (
        f"scan took {elapsed:.1f}s for a 2s budget — a stalled probe is still "
        "being joined, so discovery can hang forever"
    )


def test_scan_still_reports_devices_found_before_the_budget(monkeypatch):
    """Hitting the budget must not throw away results already collected."""
    release = threading.Event()

    def fake_probe(ip: str):
        if ip == "10.99.0.9":
            release.wait(timeout=120)
            return None
        return {
            "ip_address": ip,
            "device_type": "DARWIN",
            "raw_type": "DARWIN",
            "controller_type": "darwin_native",
            "tcp_port": 7613,
        }

    monkeypatch.setattr(server, "_probe_bravo", fake_probe)
    monkeypatch.setattr(server, "_get_mac_from_arp", lambda ip: "—")
    monkeypatch.setattr(
        server, "_build_candidate_ips", lambda *a, **k: {"10.99.0.1", "10.99.0.9"}
    )
    monkeypatch.setattr(server, "_SCAN_BUDGET_S", 2.0)

    try:
        devices = server._scan_subnet([], None)
    finally:
        release.set()

    assert [d["ip_address"] for d in devices] == ["10.99.0.1"]


def test_recv_exact_raw_honours_an_overall_deadline():
    """A peer that dribbles bytes cannot outlast the deadline."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]
    stop = threading.Event()

    def dribble():
        try:
            conn, _ = server_sock.accept()
            with conn:
                while not stop.is_set():
                    conn.sendall(b"\x00")
                    time.sleep(0.2)
        except OSError:
            pass

    t = threading.Thread(target=dribble, daemon=True)
    t.start()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(("127.0.0.1", port))
        started = time.monotonic()
        # Ask for far more bytes than the peer will ever send.
        got = server._recv_exact_raw(client, 4096, time.monotonic() + 1.0)
        elapsed = time.monotonic() - started

        assert got is None, "an incomplete read must fail, not return partial data"
        assert elapsed < 3.0, f"deadline ignored: took {elapsed:.1f}s for a 1s budget"
    finally:
        stop.set()
        client.close()
        server_sock.close()
        t.join(timeout=2)


def test_recv_exact_raw_without_deadline_is_unchanged():
    """Existing callers that pass no deadline keep the old behaviour."""
    a, b = socket.socketpair()
    try:
        a.settimeout(0.2)
        b.sendall(b"hello")
        assert server._recv_exact_raw(a, 5) == b"hello"
        # Nothing more is coming; the per-recv timeout ends it.
        assert server._recv_exact_raw(a, 5) is None
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("budget_name", ["_PROBE_BUDGET_S", "_SCAN_BUDGET_S"])
def test_budgets_are_configured(budget_name):
    value = getattr(server, budget_name)
    assert value > 0
    assert value < 120, "a discovery budget this large is indistinguishable from a hang"
