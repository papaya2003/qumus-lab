"""TP3005P bench power supply (one supply, two heaters selected by relays).

Wire format from v1 common/hardware/power_controller.py: ASCII command followed by the literal two
characters ``/n`` (not a newline; kept as v1 sent it, confirm at L1), e.g. ``VSET1:27.00/n``,
``OUTPUT1:/n``, ``OUTPUT0:/n``. v1 read reply lines for 1 s but did not require one.
``VOUT1?`` (measured voltage) comes from v1 identify_ports.py.

The supply is shared by the exfoliation and transfer heaters, so it has an owner: only one heater may
drive it at a time (v1 had no such guard).
"""

from __future__ import annotations

import threading
import time

from ..transport.serial_port import PortSpec
from .base import Device, Refused


class TP3005P(Device):
    kind = "power_supply"
    fields = {"set_v": "V", "out_v": "V", "output_on": "bool", "owner": "text"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.terminator = str(cfg.get("terminator", "/n"))
        self.max_voltage = float(cfg.get("max_voltage", 30.0))
        self.reply_window_s = float(cfg.get("reply_window_s", 1.0))
        self.set_v: float | None = None
        self.out_v: float | None = None
        self.output_on: bool | None = None
        self.owner: str | None = None
        self._owner_lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            time.sleep(float(self.cfg.get("open_settle_s", 0.5)))
            self.connected = True

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    # ownership -----------------------------------------------------------
    def acquire(self, heater_id: str) -> None:
        with self._owner_lock:
            if self.owner not in (None, heater_id):
                raise Refused(f"power supply is driving {self.owner}; turn it off first")
            self.owner = heater_id

    def release(self, heater_id: str) -> None:
        with self._owner_lock:
            if self.owner == heater_id:
                self.owner = None

    # wire ---------------------------------------------------------------
    def _command(self, cmd: str) -> list[str]:
        self.require_connected()
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + self.terminator).encode())
            self.ser.flush()
            lines: list[str] = []
            deadline = time.monotonic() + self.reply_window_s
            while time.monotonic() < deadline:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    if line:
                        lines.append(line)
                        if line.lower().startswith("ok") or line.endswith("ok") or cmd.endswith("?"):
                            break
                else:
                    time.sleep(0.05)
            return lines

    def set_voltage(self, volts: float) -> None:
        v = float(volts)
        if not 0.0 <= v <= self.max_voltage:
            raise Refused(f"voltage {v} outside 0..{self.max_voltage} V")
        self._command(f"VSET1:{v:05.2f}")
        self.set_v = v

    def output(self, on: bool) -> None:
        self._command("OUTPUT1:" if on else "OUTPUT0:")
        self.output_on = bool(on)

    def read(self) -> dict:
        lines = self._command("VOUT1?")
        self.out_v = None
        for line in lines:
            try:
                self.out_v = float(line.strip().rstrip("Vv"))
                break
            except ValueError:
                continue
        self.stamp_read()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"set_v": self.set_v, "out_v": self.out_v, "output_on": self.output_on, "owner": self.owner}

    def safe_state(self) -> bool | None:
        if not self.connected:
            return None
        try:
            self.output(False)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
