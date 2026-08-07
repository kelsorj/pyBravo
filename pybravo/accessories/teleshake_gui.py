"""Standalone Teleshake control panel (tkinter).

Split out of :mod:`pybravo.accessories.teleshake` so that importing the serial
driver never pulls in tkinter. Some Python builds ship without it, and the
driver is what the server needs — the GUI is an operator convenience.

Run it with::

    python -m pybravo.accessories.teleshake_gui
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from pybravo.accessories.teleshake import COMMANDS, DIRECTION_VALUES

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None


class TeleshakeSimpleControl:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Orbital Shaking Station")
        self.root.geometry("310x430")
        self.root.resizable(False, False)

        self.ser = None
        self.is_running = False
        self.reader_stop = threading.Event()
        self.reader_thread = None
        self.rx_queue: queue.Queue[str] = queue.Queue()

        self.port_var = tk.StringVar(value="COM4")
        self.rpm_var = tk.StringVar(value="100")
        self.direction_var = tk.StringVar(value="NWSE")
        self.status_var = tk.StringVar(value="Disconnected")
        self.reply_lines: list[str] = []

        self._build_ui()
        self._refresh_ports()
        self.root.after(100, self._drain_rx_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        conn = ttk.LabelFrame(main, text="Connection", padding=8)
        conn.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(conn, text="Port").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, width=10)
        self.port_combo.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Button(conn, text="Refresh", command=self._refresh_ports).grid(
            row=0, column=2, sticky="w", padx=4, pady=4
        )

        self.connect_button = ttk.Button(conn, text="Connect", command=self._toggle_connection)
        self.connect_button.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 4))

        ttk.Label(main, textvariable=self.status_var).pack(anchor="w", pady=(0, 8))

        station = ttk.Frame(main)
        station.pack(fill=tk.X)

        rpm_box = ttk.LabelFrame(station, text="RPM", padding=10)
        rpm_box.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(rpm_box, textvariable=self.rpm_var, width=8).pack(side=tk.LEFT)
        ttk.Label(rpm_box, text="(100 - 2000 RPM)").pack(side=tk.LEFT, padx=(10, 0))

        direction_box = ttk.LabelFrame(station, text="Stir direction", padding=10)
        direction_box.pack(fill=tk.X, pady=(0, 10))
        ttk.Combobox(
            direction_box,
            textvariable=self.direction_var,
            values=list(DIRECTION_VALUES.keys()),
            width=10,
            state="readonly",
        ).pack(anchor="w")
        ttk.Label(
            direction_box,
            text="*Note: Diagonal movements generate the\nmost motion.",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(10, 0))

        self.start_stop_button = ttk.Button(
            station,
            text="Start",
            command=self._toggle_shaker,
        )
        self.start_stop_button.pack(fill=tk.X, ipady=8, pady=(0, 12))

        ttk.Button(station, text="Emergency Stop", command=self._stop_shaker).pack(
            fill=tk.X, ipady=4, pady=(0, 12)
        )

        log_box = ttk.LabelFrame(main, text="Command log", padding=8)
        log_box.pack(fill=tk.BOTH, expand=True)
        self.reply_log = tk.Text(log_box, height=5, width=34, wrap=tk.WORD)
        self.reply_log.pack(fill=tk.BOTH, expand=True)
        self.reply_log.configure(state=tk.DISABLED)

    def _refresh_ports(self) -> None:
        if serial is None:
            self.port_combo["values"] = ["COM4"]
            self.status_var.set("Install pyserial first: pip install pyserial")
            return

        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports or ["COM4"]
        if self.port_var.get() not in ports and ports:
            self.port_var.set(ports[0])

    def _toggle_connection(self) -> None:
        if self.ser and self.ser.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        if serial is None:
            messagebox.showerror("Missing dependency", "pyserial is required. Run: pip install pyserial")
            return

        try:
            self.ser = serial.Serial(
                port=self.port_var.get().strip(),
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=False,
                dsrdtr=False,
                timeout=0.1,
            )
            self.ser.setRTS(True)
            self.ser.setDTR(True)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception as exc:
            self.status_var.set("Connection failed")
            messagebox.showerror("Connection failed", str(exc))
            return

        self.status_var.set(f"Connected to {self.ser.port} at 9600 8N1")
        self.connect_button.configure(text="Disconnect")

    def _disconnect(self) -> None:
        if self.is_running:
            self._stop_shaker()
        self.reader_stop.set()
        if self.ser:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.is_running = False
        self.start_stop_button.configure(text="Start")
        self.connect_button.configure(text="Connect")
        self.status_var.set("Disconnected")

    def _toggle_shaker(self) -> None:
        if self.is_running:
            self._stop_shaker()
        else:
            self._start_shaker()

    def _start_shaker(self) -> None:
        if not self._ensure_connected():
            return

        try:
            rpm = int(self.rpm_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid RPM", "RPM must be a whole number.")
            return

        if not 100 <= rpm <= 2000:
            messagebox.showwarning("Invalid RPM", "RPM must be from 100 to 2000.")
            return

        direction = self.direction_var.get()
        direction_value = DIRECTION_VALUES[direction]
        speed_bytes = self._speed_to_cycle_time_bytes(rpm)

        self._clear_log()
        self.status_var.set("Starting...")
        sequence = [
            ("speed", self._build_packet(COMMANDS["SET_SPEED"], list(speed_bytes))),
            ("direction", self._build_packet(COMMANDS["SET_DIRECTION"], [0x00, 0x00, direction_value])),
            ("apply", self._build_packet(COMMANDS["APPLY_SETTINGS"], [0x00, 0xFF, 0xFF])),
            ("start", self._build_packet(COMMANDS["START"], [0x00, 0x00, 0x00])),
        ]

        for label, packet in sequence:
            if not self._write_packet(label, packet):
                self.status_var.set("Start failed")
                return
            time.sleep(0.08)

        self.is_running = True
        self.start_stop_button.configure(text="Stop")
        self.status_var.set(f"Running at {rpm} RPM, {direction}")

    def _stop_shaker(self) -> None:
        if not self._ensure_connected():
            return

        packet = self._build_packet(COMMANDS["STOP"], [0x00, 0x00, 0x00])
        if self._write_packet("stop", packet):
            self.is_running = False
            self.start_stop_button.configure(text="Start")
            self.status_var.set("Stopped")
        else:
            self.status_var.set("Stop failed")

    def _ensure_connected(self) -> bool:
        if self.ser and self.ser.is_open:
            return True
        messagebox.showwarning("Not connected", "Connect to COM4 first.")
        return False

    def _write_packet(self, label: str, packet: bytes) -> bool:
        if not self.ser or not self.ser.is_open:
            return False

        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        try:
            self.ser.write(packet)
            self.ser.flush()
            reply = self._read_exact(6, timeout_s=0.6)
            if reply:
                self._append_command_log(label, packet, reply)
            else:
                self._append_command_log(label, packet, None)
            return True
        except Exception as exc:
            messagebox.showerror("Serial write failed", str(exc))
            return False

    def _read_exact(self, size: int, timeout_s: float) -> bytes:
        if not self.ser or not self.ser.is_open:
            return b""

        deadline = time.time() + timeout_s
        response = bytearray()
        while len(response) < size and time.time() < deadline:
            chunk = self.ser.read(size - len(response))
            if chunk:
                response.extend(chunk)
            else:
                time.sleep(0.01)
        return bytes(response)

    @staticmethod
    def _build_packet(command: int, data: list[int]) -> bytes:
        payload = [0x61, command & 0xFF] + [byte & 0xFF for byte in data[:3]]
        while len(payload) < 5:
            payload.append(0)
        payload.append(sum(payload[:5]) % 256)
        return bytes(payload)

    @staticmethod
    def _speed_to_cycle_time_bytes(rpm: int) -> tuple[int, int, int]:
        cycle_time_us = int(60_000_000 / rpm)
        return (
            (cycle_time_us >> 16) & 0xFF,
            (cycle_time_us >> 8) & 0xFF,
            cycle_time_us & 0xFF,
        )

    def _drain_rx_queue(self) -> None:
        while not self.rx_queue.empty():
            self._append_text(self.rx_queue.get())
        self.root.after(100, self._drain_rx_queue)

    def _clear_log(self) -> None:
        self.reply_lines.clear()
        self.reply_log.configure(state=tk.NORMAL)
        self.reply_log.delete("1.0", tk.END)
        self.reply_log.configure(state=tk.DISABLED)

    def _append_command_log(self, label: str, packet: bytes, reply: bytes | None) -> None:
        rx = self._format_hex(reply) if reply else "timeout"
        self._append_text(f"{label}: TX {self._format_hex(packet)} | RX {rx}")

    def _append_text(self, text: str) -> None:
        self.reply_lines.append(text)
        self.reply_lines = self.reply_lines[-8:]
        self.reply_log.configure(state=tk.NORMAL)
        self.reply_log.delete("1.0", tk.END)
        self.reply_log.insert(tk.END, "\n".join(self.reply_lines))
        self.reply_log.see(tk.END)
        self.reply_log.configure(state=tk.DISABLED)

    @staticmethod
    def _format_hex(data: bytes) -> str:
        return " ".join(f"{byte:02X}" for byte in data)

    def _on_close(self) -> None:
        self._disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    TeleshakeSimpleControl(root)
    root.mainloop()


if __name__ == "__main__":
    main()
