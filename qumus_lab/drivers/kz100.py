"""KZ-100 multi-axis stepper controller (sample stage X/Y/L on COM9, stamp L rail on COM19).

Wire format from v1 common/hardware/drivers/kz100_pdv_api.py (57600 8N1): an ASCII command such as
``DX12800;`` is written one character at a time and the controller echoes one byte after each character.
``U<ch>;`` returns 9 ASCII characters (signed step count); ``US;``/``UM;``/``UH;`` return 1 status byte.
``D<ch><steps>;`` relative move, ``M<ch><speed>;`` max speed, ``S<ch><speed>;`` continuous speed,
``PA;`` stop all axes.

Differences from v1 (structure only, same bytes): the port stays open instead of being opened and closed
for every command; a missing echo raises instead of being ignored; the settle wait raises at its timeout
(v1 gave up silently after 5 s, the rail wait never timed out); no automatic retries of reads.
"""

from __future__ import annotations

import threading
import time

from ..transport.serial_port import PortSpec
from .base import Device, Interrupted, NoReply, OutcomeUnknown, Refused


class KZ100Link:
    """One KZ-100 controller on one serial port (shared by the axes that live on it)."""

    def __init__(self, spec: PortSpec, serial_factory):
        self.spec = spec
        self.factory = serial_factory
        self.ser = None
        self.lock = threading.Lock()

    def open(self) -> None:
        if self.ser is None:
            self.ser = self.factory.open(self.spec)

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
            self.ser = None

    def _write_command(self, command: str) -> None:
        for ch in command:
            self.ser.write(ch.encode("ascii"))
            echo = self.ser.read(1)
            if not echo:
                raise NoReply(f"KZ-100 {self.spec.port}: no echo for {ch!r} in {command!r}")

    def command(self, command: str, reply_len: int = 0) -> bytes:
        if self.ser is None:
            raise NoReply(f"KZ-100 {self.spec.port} not open")
        with self.lock:
            self._write_command(command)
            return self.ser.read(reply_len) if reply_len else b""

    def read_steps(self, channel: str) -> int:
        raw = self.command(f"U{channel};", 9)
        text = raw.decode("ascii", errors="ignore").strip()
        try:
            return int(text)
        except ValueError as exc:
            raise NoReply(f"KZ-100 {self.spec.port}: bad position reply {raw!r} for {channel}") from exc

    def read_status(self, content: str) -> str:
        return self.command(f"U{content};", 1).decode("ascii", errors="ignore")

    def move_steps(self, channel: str, steps: int, abort=None) -> None:
        """``abort()`` is checked under the port lock, so a stop (PA; also takes the lock) either comes
        after this move and stops it, or comes first and this move is never sent."""
        if self.ser is None:
            raise NoReply(f"KZ-100 {self.spec.port} not open")
        with self.lock:
            if abort is not None and abort():
                raise Interrupted(f"KZ-100 {self.spec.port}: stop requested; D{channel} not sent")
            try:
                self._write_command(f"D{channel}{int(steps)};")
            except NoReply as exc:
                raise OutcomeUnknown(str(exc)) from exc

    def set_max_speed(self, channel: str, speed: int) -> None:
        if not 0 <= int(speed) <= 16383:
            raise Refused("max speed must be 0..16383")
        self.command(f"M{channel}{int(speed)};")

    def stop_all(self) -> None:
        # Bypass the per-command lock wait as little as possible: PA; is 3 characters.
        self.command("PA;")


class KZ100Stage(Device):
    """Axes on one KZ-100 controller, in mm. ``axes`` maps axis name -> {channel, steps_per_mm, min_mm, max_mm}."""

    kind = "stage"

    def __init__(self, device_id: str, cfg: dict, serial_factory, link: KZ100Link | None = None):
        super().__init__(device_id, cfg)
        self.link = link or KZ100Link(PortSpec.from_config(cfg["port"]), serial_factory)
        self.axes: dict[str, dict] = {k: dict(v) for k, v in cfg["axes"].items()}
        self.settle_timeout_s = float(cfg.get("settle_timeout_s", 60.0))
        self.poll_s = float(cfg.get("poll_s", 0.05))
        self.deadband_steps = int(cfg.get("deadband_steps", 2))
        self.stable_reads = int(cfg.get("stable_reads", 2))
        self.pos_mm: dict[str, float | None] = {k: None for k in self.axes}
        self.moving = False
        self.fields = {f"{k}_mm": "mm" for k in self.axes}

    def connect(self) -> None:
        self.link.open()
        self.connected = True
        self.read()

    def close(self) -> None:
        self.link.close()
        self.connected = False

    def _ax(self, axis: str) -> dict:
        if axis not in self.axes:
            raise Refused(f"{self.id}: unknown axis {axis!r} (have {sorted(self.axes)})")
        return self.axes[axis]

    def read_axis_mm(self, axis: str) -> float:
        a = self._ax(axis)
        mm = self.link.read_steps(a["channel"]) / float(a["steps_per_mm"])
        self.pos_mm[axis] = mm
        return mm

    def read(self) -> dict:
        self.require_connected()
        for axis in self.axes:
            self.read_axis_mm(axis)
        self.stamp_read()
        return self.snapshot()

    def snapshot(self) -> dict:
        out = {f"{k}_mm": v for k, v in self.pos_mm.items()}
        out["moving"] = self.moving
        return out

    def move_to(self, axis: str, target_mm: float, *, wait: bool = True) -> float:
        """Absolute move of one axis: read the position, send the step difference (v1 move_axis_to)."""
        self.require_connected()
        a = self._ax(axis)
        lo, hi = a.get("min_mm"), a.get("max_mm")
        if lo is not None and hi is not None and not float(lo) <= float(target_mm) <= float(hi):
            raise Refused(f"{self.id}.{axis}: {target_mm} mm outside [{lo}, {hi}]")
        spm = float(a["steps_per_mm"])
        current = self.link.read_steps(a["channel"])
        delta = int(round(float(target_mm) * spm)) - int(round(current))
        return self._move_steps(axis, delta, wait)

    def move_by(self, axis: str, delta_mm: float, *, wait: bool = True) -> float:
        self.require_connected()
        a = self._ax(axis)
        spm = float(a["steps_per_mm"])
        current = self.link.read_steps(a["channel"]) / spm
        target = current + float(delta_mm)
        lo, hi = a.get("min_mm"), a.get("max_mm")
        if lo is not None and hi is not None and not float(lo) <= target <= float(hi):
            raise Refused(f"{self.id}.{axis}: {target:.4f} mm outside [{lo}, {hi}]")
        return self._move_steps(axis, int(round(float(delta_mm) * spm)), wait)

    def _move_steps(self, axis: str, delta: int, wait: bool) -> float:
        a = self._ax(axis)
        if delta != 0:
            self.moving = True
            try:
                self.link.move_steps(a["channel"], delta, abort=self._stop_evt.is_set)
            except Interrupted:
                self.moving = False
                raise
        if not wait:
            return float("nan")
        self.wait_settled(axis)
        return self.read_axis_mm(axis)

    def wait_settled(self, axis: str) -> None:
        a = self._ax(axis)
        t0 = time.monotonic()
        last = self.link.read_steps(a["channel"])
        same = 0
        try:
            while True:
                if self._stop_evt.is_set():
                    return
                time.sleep(self.poll_s)
                cur = self.link.read_steps(a["channel"])
                if abs(cur - last) <= self.deadband_steps:
                    same += 1
                    if same >= self.stable_reads:
                        return
                else:
                    same = 0
                    last = cur
                if time.monotonic() - t0 > self.settle_timeout_s:
                    raise OutcomeUnknown(f"{self.id}.{axis}: still moving after {self.settle_timeout_s} s")
        finally:
            self.moving = False

    def set_max_speed(self, axis: str, speed: int) -> None:
        self.link.set_max_speed(self._ax(axis)["channel"], speed)

    def stop(self) -> bool | None:
        if not self.connected:
            return None
        self._stop_evt.set()
        try:
            self.link.stop_all()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
