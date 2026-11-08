"""Two tape reel steppers on an Arduino (A4988), exfoliation module.

Wire format from v1 common/hardware/motor_controller.py: ASCII line + ``\\n`` at 115200 baud:
``MOTOR1_ON``, ``MOTOR1_OFF``, ``MOTOR2_ON``, ``MOTOR2_OFF``, ``MOTOR<n>_<CW|CCW>_<deg>``.
v1 read whatever came back within 50 ms and did not require a reply.

Changed on purpose: v1 powered both motors on at connect; here connect sends nothing.
"""

from __future__ import annotations

import time

from ..transport.serial_port import PortSpec
from .base import Device, Refused


class TapeReels(Device):
    kind = "tape_reels"
    fields = {"motor1_on": "bool", "motor2_on": "bool", "last_command": "text", "last_reply": "text"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.max_deg = float(cfg.get("max_deg_per_command", 3600))
        self.powered = {1: None, 2: None}
        self.last_command: str | None = None
        self.last_reply: str | None = None

    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            time.sleep(float(self.cfg.get("open_settle_s", 2.0)))  # Arduino resets on open
            self.connected = True

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    def _send(self, cmd: str) -> None:
        self.require_connected()
        with self._lock:
            self.ser.write((cmd + "\n").encode())
            self.ser.flush()
            time.sleep(0.05)
            replies = []
            while self.ser.in_waiting:
                replies.append(self.ser.readline().decode(errors="ignore").strip())
            self.last_command = cmd
            self.last_reply = " | ".join(r for r in replies if r) or None

    def power(self, motor: int, on: bool) -> None:
        if motor not in (1, 2):
            raise Refused("motor must be 1 or 2")
        self._send(f"MOTOR{motor}_{'ON' if on else 'OFF'}")
        self.powered[motor] = bool(on)

    def rotate(self, motor: int, clockwise: bool, deg: float) -> None:
        deg = float(deg)
        if motor not in (1, 2):
            raise Refused("motor must be 1 or 2")
        if not 0 < deg <= self.max_deg:
            raise Refused(f"deg must be in (0, {self.max_deg}]")
        self._send(f"MOTOR{motor}_{'CW' if clockwise else 'CCW'}_{deg}")

    def read(self) -> dict:
        self.require_connected()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"motor1_on": self.powered[1], "motor2_on": self.powered[2], "last_command": self.last_command,
                "last_reply": self.last_reply}

    def safe_state(self) -> bool | None:
        return None  # a command rotates a fixed angle and ends by itself; nothing to stop
