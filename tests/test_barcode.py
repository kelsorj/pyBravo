"""Standalone barcode scanner diagnostic script.

Usage:
    python test_barcode.py [COM_PORT]

Talks to an MS-3 on 7-E-1, sets trigger char to '1', triggers a read,
and reports the result.
"""

import sys
import time

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
    print(f"Barcode scanner test — {port} 9600-7-E-1")
    print("=" * 50)

    ser = serial.Serial(
        port=port, baudrate=9600, bytesize=7,
        parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_ONE,
        timeout=2.0,
    )
    ser.reset_input_buffer()

    def cmd(data, label, delay=0.3):
        ser.reset_input_buffer()
        ser.write(data)
        ser.flush()
        time.sleep(delay)
        raw = ser.read(ser.in_waiting or 256)
        text = raw.decode("ascii", errors="replace").strip() if raw else "(no response)"
        print(f"  {label}: {text}")
        return text

    # Verify communication
    resp = cmd(b"<K100?>", "Host port config")
    if "K100" not in resp:
        print("FAILED: Scanner not responding. Check port/cabling/power.")
        ser.close()
        return

    # Set trigger char to '1' (printable, avoids GS control char issues)
    cmd(b"<K201,1>", "Set trigger char to '1'")
    # Set Serial Data trigger mode
    cmd(b"<K200,4>", "Set Serial Data trigger mode")
    # Activate without saving to power-on
    cmd(b"<A>", "Activate settings")

    # Confirm
    cmd(b"<K200?>", "Trigger mode")
    cmd(b"<K201?>", "Trigger char")

    # Trigger reads in a loop
    print("\n" + "=" * 50)
    print("Scanner ready. Place a barcode in view and press Enter.")
    print("Type 'q' to quit.\n")

    ser.timeout = 5.0
    while True:
        user = input("Press Enter to trigger scan (q to quit): ").strip()
        if user.lower() == "q":
            break

        ser.reset_input_buffer()
        ser.write(b"<1>")
        ser.flush()

        raw = ser.read_until(b"\r\n")
        if raw:
            decoded = raw.decode("ascii", errors="replace").strip()
            if decoded in ("EROR", "NR", ""):
                print(f"  No read: {decoded!r}")
            else:
                print(f"  BARCODE: {decoded}")
        else:
            print("  Timeout — no response from scanner")

    # Restore GS as trigger char
    print("\nRestoring default trigger char (GS)...")
    cmd(b"<K201,\x1d>", "Restore K201")
    cmd(b"<A>", "Activate")

    ser.close()
    print("Done.")


if __name__ == "__main__":
    main()
