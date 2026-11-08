"""Single-channel AT relay (vacuum valves, fan, heater selection relays).

Wire format from v1 common/hardware/relay_controller.py: ``AT+CH1=1\\r\\n`` (on) and ``AT+CH1=0\\r\\n`` (off)
at 9600 baud. v1 read lines for 0.5 s and treated ``CH1=1``/``on`` as confirmation. v1 has no query, so
the state we report is the last confirmed command.
"""

from __future__ import annotations

import time

from ..transport.serial_port import PortSpec
from .base import Device, OutcomeUnknown


class RelaySwitch(Device):
    kind = "switch"
    fields = {"on": "bool", "confirmed": "bool", "raw_reply": "text"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.channel = int(cfg.get("channel", 1))
        self.reply_timeout_s = float(cfg.get("reply_timeout_s", 0.5))
        self.expect_reply = bool(cfg.get("expect_reply", True))
        self.open_settle_s = float(cfg.get("open_settle_s", 0.5))
        self.safe_on = bool(cfg.get("safe_state_on", False))  # e.g. stamp vacuum stays on at Stop
        self.on: bool | None = None
        self.confirmed = False
        self.raw_reply: str | None = None

    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            if self.open_settle_s:
                time.sleep(self.open_settle_s)
            self.connected = True  # no query exists (v1); state unknown until first command

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    def _read_reply(self) -> str | None:
        deadline = time.monotonic() + self.reply_timeout_s
        lines: list[str] = []
        while time.monotonic() < deadline:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    lines.append(line)
                    low = line.lower()
                    if low.startswith("ok") or low.endswith("ok"):
                        break
            else:
                time.sleep(0.02)
        return "\n".join(lines) if lines else None

    def set(self, on: bool) -> dict:
        self.require_connected()
        with self._lock:
            self.ser.write(f"AT+CH{self.channel}={1 if on else 0}\r\n".encode())
            time.sleep(0.1)  # v1 waited 0.1 s before reading
            reply = self._read_reply()
            self.raw_reply = reply
            want = f"CH{self.channel}={1 if on else 0}"
            word = "on" if on else "off"
            self.confirmed = reply is not None and (want in reply or word in reply.lower() or "ok" in reply.lower())
            if not self.confirmed and self.expect_reply:
                self.on = None
                raise OutcomeUnknown(f"{self.id}: no confirmation for {want} (reply {reply!r})")
            self.on = bool(on)
            self.stamp_read()
            return self.snapshot()

    def read(self) -> dict:
        self.require_connected()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"on": self.on, "confirmed": self.confirmed, "raw_reply": self.raw_reply}

    def safe_state(self) -> bool | None:
        if not self.connected or self.safe_on:
            return None
        try:
            self.set(False)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
