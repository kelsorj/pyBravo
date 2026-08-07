"""Probe a Bravo liquid handler — check TCP connectivity and firmware version.

Tries both NGS (port 7612, V11-NGS framing) and Darwin (port 7613, Gemini)
protocols to identify the device type and firmware.

Usage:
    python scripts/probe_bravo.py 192.168.0.28
    python scripts/probe_bravo.py 192.168.0.28 --port 7612
    python scripts/probe_bravo.py 192.168.0.28 --timeout 5
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time


def _tcp_connect(host: str, port: int, timeout: float) -> socket.socket | None:
    """Try to open a TCP connection. Returns socket or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return sock
    except (OSError, ConnectionRefusedError, TimeoutError) as exc:
        print(f"  [{port}] Connection failed: {exc}")
        sock.close()
        return None


def _recv_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    """Receive exactly n bytes."""
    sock.settimeout(timeout)
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {n} bytes (got {len(buf)})")
        sock.settimeout(min(remaining, timeout))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"Connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


def probe_ngs(host: str, port: int = 7612, timeout: float = 3.0) -> dict | None:
    """Probe using V11-NGS framing: [cmd][length_u16_LE][data]."""
    print(f"\n  Probing NGS protocol at {host}:{port}...")
    sock = _tcp_connect(host, port, timeout)
    if sock is None:
        return None

    try:
        # QUERY_VERSION = 0x00, no payload
        cmd = 0x00
        frame = struct.pack("<BH", cmd, 0)  # cmd=0x00, length=0
        sock.sendall(frame)

        # Response: [cmd(1)][length_u16_LE(2)][error(1)][data...]
        header = _recv_exact(sock, 3, timeout)
        resp_len = struct.unpack("<H", header[1:3])[0]

        if resp_len == 0:
            print(f"  [{port}] Got empty response (length=0)")
            return None

        payload = _recv_exact(sock, resp_len, timeout)
        error_code = payload[0]
        data = payload[1:]

        if error_code != 0:
            print(f"  [{port}] Firmware error code: 0x{error_code:02X}")
            return None

        # Parse null-terminated version strings
        parts = data.split(b"\x00")
        strings = [p.decode("ascii", errors="replace") for p in parts if p]

        result = {
            "protocol": "V11-NGS",
            "port": port,
            "master": strings[0] if len(strings) > 0 else "",
            "sub1": strings[1] if len(strings) > 1 else "",
            "sub2": strings[2] if len(strings) > 2 else "",
            "raw_hex": data.hex(),
        }

        # Try QUERY_STATE (0xA9) to check device status
        try:
            state_cmd = 0xA9
            state_frame = struct.pack("<BH", state_cmd, 0)
            sock.sendall(state_frame)
            state_header = _recv_exact(sock, 3, timeout)
            state_len = struct.unpack("<H", state_header[1:3])[0]
            if state_len > 0:
                state_payload = _recv_exact(sock, state_len, timeout)
                state_err = state_payload[0]
                state_data = state_payload[1:]
                result["state_error"] = state_err
                result["state_hex"] = state_data.hex()
                if len(state_data) >= 1:
                    flags = state_data[0]
                    result["motor_power"] = bool(flags & 0x02)
                    result["robot_disable"] = bool(flags & 0x01)
                    result["go_button"] = bool(flags & 0x04)
        except Exception as exc:
            result["state_note"] = f"QUERY_STATE failed: {exc}"

        return result

    except (TimeoutError, ConnectionError, OSError, struct.error) as exc:
        print(f"  [{port}] NGS probe failed: {exc}")
        return None
    finally:
        sock.close()


def probe_v11_standard(host: str, port: int = 7612, timeout: float = 3.0) -> dict | None:
    """Probe using standard V11 framing: [length_u16_LE][cmd][data]."""
    print(f"\n  Probing standard V11 protocol at {host}:{port}...")
    sock = _tcp_connect(host, port, timeout)
    if sock is None:
        return None

    try:
        # QUERY_VERSION = 0x00, payload_length=0
        cmd = 0x00
        frame = struct.pack("<HB", 0, cmd)  # length=0, cmd=0x00
        sock.sendall(frame)

        # Response: [length_u16_LE(2)][error(1)][data...]
        header = _recv_exact(sock, 2, timeout)
        resp_len = struct.unpack("<H", header)[0]

        if resp_len == 0:
            print(f"  [{port}] Got empty response (length=0)")
            return None

        payload = _recv_exact(sock, resp_len, timeout)
        error_code = payload[0]
        data = payload[1:]

        if error_code != 0:
            print(f"  [{port}] Firmware error code: 0x{error_code:02X}")
            return None

        parts = data.split(b"\x00")
        strings = [p.decode("ascii", errors="replace") for p in parts if p]

        return {
            "protocol": "V11-Standard",
            "port": port,
            "master": strings[0] if len(strings) > 0 else "",
            "sub1": strings[1] if len(strings) > 1 else "",
            "sub2": strings[2] if len(strings) > 2 else "",
            "raw_hex": data.hex(),
        }

    except (TimeoutError, ConnectionError, OSError, struct.error) as exc:
        print(f"  [{port}] Standard V11 probe failed: {exc}")
        return None
    finally:
        sock.close()


def probe_darwin(host: str, port: int = 7613, timeout: float = 3.0) -> dict | None:
    """Probe Darwin/Gemini protocol — just check if TCP port is open."""
    print(f"\n  Probing Darwin/Gemini at {host}:{port}...")
    sock = _tcp_connect(host, port, timeout)
    if sock is None:
        return None

    try:
        # Gemini uses a different protocol; just confirm the port is open
        # and try to read any welcome/banner data
        sock.settimeout(1.0)
        try:
            banner = sock.recv(256)
            banner_hex = banner.hex() if banner else "(empty)"
        except (TimeoutError, OSError):
            banner_hex = "(no banner)"

        return {
            "protocol": "Darwin/Gemini (port open)",
            "port": port,
            "banner_hex": banner_hex,
        }
    finally:
        sock.close()


def print_result(label: str, result: dict | None):
    if result is None:
        print(f"\n  {label}: No response")
        return

    print(f"\n  {label}: SUCCESS")
    print(f"    Protocol: {result.get('protocol', '?')}")
    print(f"    Port:     {result.get('port', '?')}")

    if "master" in result:
        print(f"    Firmware:  master={result['master']}")
        if result.get("sub1"):
            print(f"               sub1={result['sub1']}")
        if result.get("sub2"):
            print(f"               sub2={result['sub2']}")

    if "motor_power" in result:
        print(f"    Motor power:    {'ON' if result['motor_power'] else 'OFF'}")
        print(f"    Robot disable:  {'YES' if result['robot_disable'] else 'NO'}")
        print(f"    Go button:      {'PRESSED' if result['go_button'] else 'released'}")

    if "state_hex" in result:
        print(f"    State raw:      {result['state_hex']}")
    if "state_note" in result:
        print(f"    State note:     {result['state_note']}")
    if "banner_hex" in result:
        print(f"    Banner:         {result['banner_hex']}")
    if "raw_hex" in result:
        print(f"    Version raw:    {result['raw_hex']}")


def main():
    parser = argparse.ArgumentParser(description="Probe a Bravo liquid handler for connectivity and firmware version.")
    parser.add_argument("host", help="IP address of the Bravo (e.g. 192.168.0.28)")
    parser.add_argument("--port", type=int, default=None, help="Specific port to probe (default: try 7612 and 7613)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Connection timeout in seconds (default: 3)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Bravo Probe: {args.host}")
    print(f"  Timeout: {args.timeout}s")
    print("=" * 60)

    # Quick ping check
    print("\n  Checking TCP reachability...")

    results = []

    if args.port:
        # Probe specific port with both framings
        ngs = probe_ngs(args.host, args.port, args.timeout)
        if ngs:
            results.append(("NGS (V11-NGS)", ngs))
        else:
            std = probe_v11_standard(args.host, args.port, args.timeout)
            if std:
                results.append(("Standard V11", std))
            elif args.port == 7613:
                darwin = probe_darwin(args.host, args.port, args.timeout)
                if darwin:
                    results.append(("Darwin", darwin))
    else:
        # Try NGS port first (7612)
        ngs = probe_ngs(args.host, 7612, args.timeout)
        if ngs:
            results.append(("NGS (V11-NGS)", ngs))
        else:
            std = probe_v11_standard(args.host, 7612, args.timeout)
            if std:
                results.append(("Standard V11", std))

        # Try Darwin port (7613)
        darwin = probe_darwin(args.host, 7613, args.timeout)
        if darwin:
            results.append(("Darwin", darwin))

    # Summary
    print("\n" + "=" * 60)
    print("  Results")
    print("=" * 60)

    if not results:
        print(f"\n  No response from {args.host} on any protocol.")
        print("  Check:")
        print("    - Is the device powered on?")
        print("    - Is the network cable connected?")
        print(f"    - Can you ping {args.host}?")
        print("    - Is another application already connected to the instrument?")
        sys.exit(1)

    for label, result in results:
        print_result(label, result)

    # Recommend controller type
    print("\n" + "-" * 60)
    for label, result in results:
        proto = result.get("protocol", "")
        master = result.get("master", "")

        if "NGS" in proto:
            print("  Recommendation: controller_type = agile_ngs")
            print("  Profile settings:")
            print("    connection:")
            print("      controller_type: agile_ngs")
            print(f"      ip_address: {args.host}")
            if master:
                v = master.split(".")
                major = int(v[0]) if v else 0
                print(f"  Firmware major version: {major}")
        elif "V11-Standard" in proto:
            print("  Recommendation: controller_type = agile")
            print("  Profile settings:")
            print("    connection:")
            print("      controller_type: agile")
            print(f"      ip_address: {args.host}")
        elif "Darwin" in proto:
            print("  Recommendation: controller_type = darwin_native")
            print("  Profile settings:")
            print("    connection:")
            print("      controller_type: darwin_native")
            print(f"      ip_address: {args.host}")

    print("=" * 60)


if __name__ == "__main__":
    main()
