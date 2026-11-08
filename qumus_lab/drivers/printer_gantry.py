"""Tape exfoliator gantry (a Marlin 3D-printer board moving the upper tape roller in X/Z).

Wire format from v1 common/hardware/printer_controller.py: G-code + ``\\n`` at 115200 baud, wait for ``ok``
(5 s, renewed by ``busy``). Generic operations only: get_position (M114), move (G1, absolute or
relative), arc (the v1 draw_arc: 100 relative G1 segments), power (M17/M18 X Y Z), finish (M400).
The apply/peel geometry (arc centre, end point, approach X) is in config/points.yaml.

Changed on purpose: v1 sent ``M17 X Y Z`` on connect; here connect sends only ``M114``.
Stop sends ``M410`` (Marlin quick stop; not used by v1, confirm at L2).
"""

from __future__ import annotations

import math
import re
import time

from ..transport.serial_port import PortSpec
from .base import Device, FirmwareError, NoReply, OutcomeUnknown, Refused, fmt_num

POS_RE = re.compile(r"X:([+-]?\d+\.?\d*)\s+Y:([+-]?\d+\.?\d*)\s+Z:([+-]?\d+\.?\d*)")


def arc_segments(x0, z0, x1, z1, cx, cz, clockwise: bool, segments: int = 100) -> list[tuple[float, float]]:
    """Relative (dx, dz) steps of an arc from (x0, z0) to (x1, z1) around (cx, cz). Same maths as v1."""
    def angle(x, z):
        return math.atan2(z - cz, x - cx)

    t0, t1 = angle(x0, z0), angle(x1, z1)
    if clockwise and t1 < t0:
        t1 += 2 * math.pi
    elif not clockwise and t1 > t0:
        t1 -= 2 * math.pi
    r = math.hypot(x0 - cx, z0 - cz)
    out = []
    px, pz = x0, z0
    for i in range(1, segments + 1):
        th = t0 + (t1 - t0) * i / segments
        x, z = cx + r * math.cos(th), cz + r * math.sin(th)
        out.append((round(x - px, 4), round(z - pz, 4)))
        px, pz = x, z
    return out


class PrinterGantry(Device):
    kind = "gantry"
    fields = {"x_mm": "mm", "y_mm": "mm", "z_mm": "mm", "motors_on": "bool", "busy": "bool"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.reply_timeout_s = float(cfg.get("reply_timeout_s", 5.0))
        self.stop_gcode = list(cfg.get("stop_gcode") or ["M410"])
        self.pos: dict[str, float | None] = {"x": None, "y": None, "z": None}
        self.motors_on: bool | None = None
        self.busy = False

    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            time.sleep(float(self.cfg.get("open_settle_s", 2.0)))
            self.ser.reset_input_buffer()
            self.connected = True
            self.read()

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    def send(self, cmd: str, *, actuating: bool = True, timeout_s: float | None = None) -> None:
        self.require_connected()
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\n").encode())
            deadline = time.monotonic() + (timeout_s or self.reply_timeout_s)
            while True:
                if self.ser.in_waiting:
                    resp = self.ser.readline().decode(errors="ignore").strip()
                    if not resp:
                        continue
                    low = resp.lower()
                    if "error" in low:
                        raise FirmwareError(f"{self.id}: {resp}")
                    if "busy" in low:
                        deadline = time.monotonic() + (timeout_s or self.reply_timeout_s)
                        continue
                    if low.startswith("ok") or low.endswith(" ok") or low == "ok" or " ok" in low:
                        return
                elif time.monotonic() > deadline:
                    exc = NoReply(f"{self.id}: no 'ok' after {cmd!r}")
                    if actuating:
                        raise OutcomeUnknown(str(exc)) from exc
                    raise exc
                else:
                    time.sleep(0.005)

    def read(self) -> dict:
        self.require_connected()
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write(b"M114\n")
            deadline = time.monotonic() + 2.0
            text = ""
            while time.monotonic() < deadline:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    text += line + " "
                    if "X:" in line and "Y:" in line:
                        break
                else:
                    time.sleep(0.01)
            m = POS_RE.search(text)
            if not m:
                raise NoReply(f"{self.id}: M114 gave no position ({text.strip()!r})")
            self.pos = {"x": float(m.group(1)), "y": float(m.group(2)), "z": float(m.group(3))}
            self.stamp_read()
            return self.snapshot()

    def snapshot(self) -> dict:
        return {"x_mm": self.pos["x"], "y_mm": self.pos["y"], "z_mm": self.pos["z"],
                "motors_on": self.motors_on, "busy": self.busy}

    # generic operations --------------------------------------------------
    def power(self, on: bool) -> None:
        self.send("M17 X Y Z" if on else "M18 X Y Z")
        self.motors_on = bool(on)

    def finish(self, timeout_s: float = 120.0) -> None:
        self.send("M400", timeout_s=timeout_s)

    def set_relative(self, relative: bool) -> None:
        """G91 (relative) or G90 (absolute)."""
        self.send("G91" if relative else "G90")

    def line(self, *, x: float | None = None, z: float | None = None, feed: float) -> None:
        """One G1 line in the current mode (no mode change, no wait)."""
        if x is None and z is None:
            raise Refused("nothing to move")
        if feed is None or float(feed) <= 0:
            raise Refused("feed (mm/min) is required")
        parts = ["G1"]
        if x is not None:
            parts.append(f"X{fmt_num(x)}")
        if z is not None:
            parts.append(f"Z{fmt_num(z)}")
        parts.append(f"F{fmt_num(feed)}")
        self.send(" ".join(parts))

    def arc_relative(self, *, end_x: float, end_z: float, centre_x: float, centre_z: float, feed: float,
                     clockwise: bool, segments: int = 100) -> None:
        """Arc from the current point, numbers relative to it (v1 draw_arc with x0 = z0 = 0).
        Caller must be in relative mode (set_relative(True))."""
        if feed is None or float(feed) <= 0:
            raise Refused("feed (mm/min) is required")
        for dx, dz in arc_segments(0, 0, end_x, end_z, centre_x, centre_z, clockwise, segments):
            self.send(f"G1 X{dx} Z{dz} F{fmt_num(feed)}")

    def move(self, *, x: float | None = None, z: float | None = None, feed: float, relative: bool) -> None:
        """Convenience: mode, one line, wait, back to absolute."""
        with self._lock:
            self.busy = True
            try:
                self.set_relative(relative)
                self.line(x=x, z=z, feed=feed)
                self.finish()
                if relative:
                    self.set_relative(False)
            finally:
                self.busy = False

    def stop(self) -> bool | None:
        if not self.connected:
            return None
        try:
            for line in self.stop_gcode:
                self.ser.write((line + "\n").encode())
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
