"""Motorised objective turret on COM10 (9600 baud).

Wire format from v1 common/hardware/turret_controller.py: ``cRDC<n>\\r`` rotates to position n (1-based);
the controller may answer ``oRDC``. v1 flows never waited for the answer, so by default the reply is
recorded but not required (``expect_reply: false``; confirm at L2). There is no position query in v1:
the position reported is the last commanded one.
"""

from __future__ import annotations

import time

from ..transport.serial_port import PortSpec
from .base import Device, OutcomeUnknown, Refused


class Turret(Device):
    kind = "turret"
    fields = {"position": "index", "objective": "x", "last_reply": "text"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.terminator = str(cfg.get("terminator", "\r"))
        self.positions = {int(k): int(v) for k, v in (cfg.get("objectives") or {5: 1, 10: 2, 20: 3, 100: 4}).items()}
        self.expect_reply = bool(cfg.get("expect_reply", False))
        self.settle_s = float(cfg.get("settle_s", 1.0))
        self.position: int | None = None
        self.last_reply: str | None = None

    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            self.connected = True

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    def rotate_to(self, position: int) -> dict:
        self.require_connected()
        position = int(position)
        if position not in self.positions.values():
            raise Refused(f"{self.id}: position {position} not in {sorted(self.positions.values())}")
        with self._lock:
            self.ser.write(f"cRDC{position}{self.terminator}".encode())
            reply = self.ser.readline().decode(errors="ignore").strip() if self.expect_reply else None
            self.last_reply = reply
            if self.expect_reply and reply != "oRDC":
                self.position = None
                raise OutcomeUnknown(f"{self.id}: expected 'oRDC', got {reply!r}")
        if self.settle_s:
            time.sleep(self.settle_s)
        self.position = position
        return self.snapshot()

    def objective_for(self, position: int | None) -> int | None:
        for mag, pos in self.positions.items():
            if pos == position:
                return mag
        return None

    def read(self) -> dict:
        self.require_connected()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"position": self.position, "objective": self.objective_for(self.position), "last_reply": self.last_reply}
