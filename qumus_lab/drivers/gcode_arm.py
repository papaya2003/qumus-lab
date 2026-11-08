"""G-code robot arm (left = exfoliation, right = stacking).

Wire format taken from v1 common/hardware/robot_arm_controller.py (commands end with CR LF, the
controller answers ``ok``; ``M888`` lines get no ``ok``). Only generic operations live here:
move_to / get_pose / set_vacuum / release_item / rotate_nozzle / dwell / finish_moves.
Named places (stage, slots, park) are in config/points.yaml, not in code.

Changed on purpose versus v1 (hazards):
- connect() sends only ``M114`` (v1 sent G21, G90 and M17 on connect).
- the unit/absolute mode (G21, G90) is sent once before the first move of a connection.
- every move needs an explicit feed; there is no default F5000.
- a move or switch with no reply raises OutcomeUnknown (v1 printed and carried on).
"""

from __future__ import annotations

import re
import time

from ..transport.serial_port import PortSpec
from .base import Device, FirmwareError, NoReply, OutcomeUnknown, Refused, fmt_num

OK_RE = re.compile(r"\bok\b", re.IGNORECASE)
ERR_RE = re.compile(r"(?:^|[\r\n])(?:error|alarm|unknown command|!!)", re.IGNORECASE)
POSE_RE = re.compile(r"X:([\-\d\.]+)\s+Y:([\-\d\.]+)\s+Z:([\-\d\.]+)")
BOOT_RE = re.compile(r"start|marlin|grbl", re.IGNORECASE)
NO_OK_PREFIXES = ("M888",)  # v1 robot_arm_controller.py:15


class GcodeArm(Device):
    kind = "arm"
    fields = {"x_mm": "mm", "y_mm": "mm", "z_mm": "mm", "nozzle_deg": "deg", "vacuum_on": "bool",
              "pose_trusted": "bool", "moving": "bool"}

    def __init__(self, device_id: str, cfg: dict, serial_factory):
        super().__init__(device_id, cfg)
        self.spec = PortSpec.from_config(cfg["port"])
        self.factory = serial_factory
        self.ser = None
        self.reply_timeout_s = float(cfg.get("reply_timeout_s", 2.0))
        self.open_settle_s = float(cfg.get("open_settle_s", 2.0))
        self.stop_gcode: list[str] = list(cfg.get("stop_gcode") or [])
        self.pose: dict[str, float | None] = {"x": None, "y": None, "z": None}
        self.nozzle_deg: float | None = None
        self.vacuum_on: bool | None = None
        self.pose_trusted = False
        self.boot_banner = ""
        self._modes_sent = False
        self.moving = False

    # --- link ----------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            self.ser = self.factory.open(self.spec)
            if self.open_settle_s > 0:
                time.sleep(self.open_settle_s)  # opening may reset the board (DTR); v1 waited 2 s
            try:
                self.boot_banner = self.ser.read_all().decode(errors="ignore") if hasattr(self.ser, "read_all") else ""
            except Exception:
                self.boot_banner = ""
            self.ser.reset_input_buffer()
            self.connected = True
            self._modes_sent = False
            self.read()
            # A reset board reports X0 Y0 Z0 or prints a banner: its position is not trustworthy.
            p = self.pose
            reset_like = bool(BOOT_RE.search(self.boot_banner)) or (p["x"] == 0 and p["y"] == 0 and p["z"] == 0)
            self.pose_trusted = not reset_like and p["x"] is not None

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                self.ser.close()
            self.connected = False

    def _write_line(self, cmd: str) -> None:
        self.ser.reset_input_buffer()
        self.ser.write((cmd.strip() + "\r\n").encode())

    def _wait_ok(self, timeout_s: float) -> str:
        """Wait for ``ok``. ``timeout_s`` is silence: any byte from the controller renews it (v1 rule)."""
        t0 = time.monotonic()
        buf = ""
        while True:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n).decode(errors="ignore")
                t0 = time.monotonic()
                if ERR_RE.search(buf):
                    raise FirmwareError(f"{self.id}: {buf.strip()}")
                if OK_RE.search(buf):
                    return buf
            else:
                if time.monotonic() - t0 > timeout_s:
                    raise NoReply(f"{self.id}: no 'ok' within {timeout_s:.1f} s (got {buf.strip()!r})")
                time.sleep(0.003)

    def send(self, cmd: str, *, actuating: bool = True, timeout_s: float | None = None) -> str:
        """Send one G-code line and wait for ``ok`` (unless it is an M888 line)."""
        self.require_connected()
        with self._lock:
            if cmd.split(" ", 1)[0].upper() in ("G0", "G1", "G2", "G3", "G28"):
                self.check_not_stopped()
            self._write_line(cmd)
            if cmd.strip().upper().startswith(NO_OK_PREFIXES):
                return ""
            try:
                return self._wait_ok(timeout_s or self.reply_timeout_s)
            except NoReply as exc:
                if actuating:
                    raise OutcomeUnknown(f"{exc} after {cmd!r}") from exc
                raise

    # --- reads ---------------------------------------------------------
    def read(self) -> dict:
        self.require_connected()
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write(b"M114\r\n")
            deadline = time.monotonic() + self.reply_timeout_s
            got = None
            while time.monotonic() < deadline:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    m = POSE_RE.search(line)
                    if m:
                        got = tuple(float(g) for g in m.groups())
                    if OK_RE.search(line):
                        break
                else:
                    time.sleep(0.005)
            if got is None:
                raise NoReply(f"{self.id}: M114 gave no position")
            self.pose = {"x": got[0], "y": got[1], "z": got[2]}
            self.stamp_read()
            return self.snapshot()

    def snapshot(self) -> dict:
        return {"x_mm": self.pose["x"], "y_mm": self.pose["y"], "z_mm": self.pose["z"],
                "nozzle_deg": self.nozzle_deg, "vacuum_on": self.vacuum_on, "pose_trusted": self.pose_trusted,
                "moving": self.moving}

    # --- generic operations ---------------------------------------------
    def _ensure_modes(self) -> None:
        if not self._modes_sent:
            self.send("G21")  # millimetres
            self.send("G90")  # absolute
            self._modes_sent = True

    def move_to(self, x: float | None, y: float | None, z: float | None, feed: float, *,
                linear: bool = False, wait: bool = True) -> dict:
        """Move to an absolute pose in the arm's own frame. Omitted axes stay where they are."""
        if feed is None or float(feed) <= 0:
            raise Refused("feed (mm/min) is required")
        parts = ["G1" if linear else "G0"]
        for name, v in (("X", x), ("Y", y), ("Z", z)):
            if v is not None:
                parts.append(f"{name}{fmt_num(v)}")
        parts.append(f"F{fmt_num(feed)}")
        with self._lock:
            self._ensure_modes()
            dist = self._distance(x, y, z)
            self.moving = True
            try:
                self.send(" ".join(parts))
                if wait:
                    self.finish_moves(expected_s=60.0 * dist / float(feed) if dist is not None else None)
            finally:
                self.moving = False
        return self.snapshot()

    def _distance(self, x, y, z) -> float | None:
        cur = self.pose
        if any(cur[k] is None for k in "xyz"):
            return None
        tgt = {"x": x if x is not None else cur["x"], "y": y if y is not None else cur["y"],
               "z": z if z is not None else cur["z"]}
        return sum((tgt[k] - cur[k]) ** 2 for k in "xyz") ** 0.5

    def finish_moves(self, expected_s: float | None = None) -> dict:
        """M400: wait until queued moves finish, then read the pose (v1 did the same after each move)."""
        budget = self.reply_timeout_s + (1.5 * expected_s if expected_s else float(self.cfg.get("unknown_move_s", 60)))
        self.send("M400", timeout_s=budget)
        try:
            self.read()
        except NoReply:
            pass
        return self.snapshot()

    def dwell(self, ms: int) -> None:
        self.send(f"G4 P{int(ms)}")

    def set_vacuum(self, on: bool) -> None:
        """Nozzle suction. v1 vacuum_on: M888 P2, M1000. vacuum_off: M888 P2, M1003."""
        self.send("M888 P2")
        self.send("M1000" if on else "M1003")
        self.vacuum_on = bool(on)

    def release_item(self) -> None:
        """v1 place_item: M888 P2, M1002, M1001, G4 P300, M1003, M400 (release valve then pump off)."""
        self.send("M888 P2")
        self.send("M1002")
        self.send("M1001")
        self.send("G4 P300")
        self.send("M1003")
        self.vacuum_on = False
        self.finish_moves(expected_s=1.0)

    def rotate_nozzle(self, deg: float) -> None:
        """v1 rotate_nose: M888 P6, M2100, M2101 R<int deg>."""
        self.send("M888 P6")
        self.send("M2100")
        self.send(f"M2101 R{int(deg)}")
        self.nozzle_deg = float(int(deg))

    def stop(self) -> bool | None:
        if not self.connected:
            return None
        self._stop_evt.set()  # no further queued move is sent (e.g. approach -> final)
        if not self.moving:
            return None
        if not self.stop_gcode:
            return False  # v1 had no stop command: cannot confirm (docs/open-questions.md)
        try:
            for line in self.stop_gcode:
                self.ser.write((line + "\r\n").encode())
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
