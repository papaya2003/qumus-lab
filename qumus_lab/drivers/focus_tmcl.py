"""Microscope focus motor (Thorlabs MFC1, TMCL binary protocol) on COM11, 115200 baud.

Frames from v1 common/hardware/focus_controller.py: 9 bytes
``[address, instruction, type, motor, v3, v2, v1, v0, checksum]``; instruction 4 type 1 = relative move
(MVP REL) with a 32-bit two's-complement microstep count (92.5 steps per um), instruction 3 = stop.
Checksum = sum of the first 8 bytes mod 256. The reply is 9 bytes; byte 2 is the status.

v1 kept only a software position estimate (no read-back), so the position here is also an estimate
(``position_estimated: true``). Changed on purpose: a move above ``max_chunk_um`` is refused (v1 silently
sent a zero move); v1 ``move_to_um`` had its sign inverted (it moved away from the target) and is not copied.
"""

from __future__ import annotations

from ..transport.serial_port import PortSpec
from .base import Device, FirmwareError, OutcomeUnknown, Refused

STATUS_TEXT = {1: "Checksum incorrect", 2: "Invalid command", 3: "Wrong type", 4: "Invalid value",
               5: "Configuration EEPROM locked", 6: "Command not available"}


def tmcl_frame(address: int, instruction: int, type_: int, motor: int, value: int) -> bytes:
    v = value & 0xFFFFFFFF
    body = [address & 0xFF, instruction & 0xFF, type_ & 0xFF, motor & 0xFF,
            (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]
    return bytes(body + [sum(body) & 0xFF])


def um_to_microsteps(dist_um: float, steps_per_um: float) -> int:
    """Same rounding as v1: int(abs(um) * steps_per_um), sign applied afterwards."""
    steps = int(abs(float(dist_um)) * steps_per_um)
    return steps if float(dist_um) >= 0 else -steps


class TmclFocus(Device):
    kind = "focus"
    fields = {"z_um": "um", "position_estimated": "bool"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.address = int(cfg.get("address", 1))
        self.motor = int(cfg.get("motor_index", 0))
        self.steps_per_um = float(cfg.get("steps_per_um", 92.5))
        self.max_chunk_um = float(cfg.get("max_chunk_um", 30000.0))
        self.wait_s_per_um = float(cfg.get("wait_s_per_um", 0.006))
        self.z_um = 0.0  # software estimate, zero at connect (as v1)

    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            self.connected = True

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    def _send(self, frame: bytes) -> int:
        with self._lock:
            self.ser.write(frame)
            answer = self.ser.read(9)
        if len(answer) < 3:
            raise OutcomeUnknown(f"{self.id}: no TMCL reply")
        status = answer[2]
        if status in STATUS_TEXT:
            raise FirmwareError(f"{self.id}: {STATUS_TEXT[status]}")
        return status

    def move_by_um(self, dist_um: float) -> float:
        self.require_connected()
        if abs(float(dist_um)) > self.max_chunk_um:
            raise Refused(f"{self.id}: {dist_um} um is above the {self.max_chunk_um} um single-move limit")
        steps = um_to_microsteps(dist_um, self.steps_per_um)
        with self._lock:  # the stop frame takes the same lock
            self.check_not_stopped()
            self._send(tmcl_frame(self.address, 4, 1, self.motor, steps))
        self.z_um += float(dist_um)
        return self.z_um

    def expected_move_s(self, dist_um: float) -> float:
        return self.wait_s_per_um * abs(float(dist_um))

    def read(self) -> dict:
        self.require_connected()
        self.stamp_read()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"z_um": self.z_um, "position_estimated": True}

    def stop(self) -> bool | None:
        if not self.connected:
            return None
        self._stop_evt.set()
        try:
            self._send(tmcl_frame(self.address, 3, 1, self.motor, 0))
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
