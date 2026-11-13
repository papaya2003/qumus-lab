"""Instrument emulators that speak the same bytes as the real instruments.

They are used by the tests and by ``--demo``: the real drivers run unchanged against them, so the demo
exercises the same code path as the lab PC. Device-level simulation only (positions, switch states,
temperatures); no physics of flakes or tapes.
"""

from __future__ import annotations

import math
import re
import threading
import time

from ..transport.fake_serial import Emulator, LineEmulator


class Clock:
    """Simulation clock; ``speed`` > 1 makes moves and heating faster than real time."""

    def __init__(self, speed: float = 1.0):
        self.speed = float(speed)

    def now(self) -> float:
        return time.monotonic() * self.speed


class LinearMotion:
    """Piecewise-linear motion of a point in n dimensions at a given speed (units/s)."""

    def __init__(self, start: dict[str, float], clock: Clock):
        self.clock = clock
        self.start = dict(start)
        self.target = dict(start)
        self.t0 = clock.now()
        self.duration = 0.0
        self.lock = threading.Lock()

    def position(self) -> dict[str, float]:
        with self.lock:
            if self.duration <= 0:
                return dict(self.target)
            f = min(1.0, (self.clock.now() - self.t0) / self.duration)
            return {k: self.start[k] + (self.target[k] - self.start[k]) * f for k in self.target}

    def go(self, target: dict[str, float], speed: float) -> float:
        """Start a move; returns the real-time seconds it takes."""
        cur = self.position()
        with self.lock:
            self.start = cur
            self.target = {k: target.get(k, cur[k]) for k in cur}
            dist = math.sqrt(sum((self.target[k] - cur[k]) ** 2 for k in cur))
            self.duration = dist / speed if speed > 0 else 0.0
            self.t0 = self.clock.now()
            return self.duration / self.clock.speed

    def halt(self) -> None:
        cur = self.position()
        with self.lock:
            self.start = self.target = cur
            self.duration = 0.0

    def remaining_s(self) -> float:
        with self.lock:
            if self.duration <= 0:
                return 0.0
            return max(0.0, self.t0 + self.duration - self.clock.now()) / self.clock.speed


# --------------------------------------------------------------------------- G-code arm
class ArmEmulator(LineEmulator):
    """Robot arm G-code controller (CR LF terminated). Answers ``ok``; M888 lines get no answer."""

    terminator = b"\n"

    def __init__(self, clock: Clock, start=(0.0, 220.0, 95.0), *, boot_banner: str = ""):
        super().__init__()
        self.motion = LinearMotion({"x": start[0], "y": start[1], "z": start[2]}, clock)
        self.relative = False
        self.vacuum = False
        self.nozzle_deg = 0.0
        self.drop_replies = False
        self.boot_banner = boot_banner
        self.lines: list[str] = []

    def attach(self, port) -> None:
        super().attach(port)
        if self.boot_banner:
            self.emit(self.boot_banner + "\r\n")

    def ok(self, delay: float = 0.0) -> None:
        if not self.drop_replies:
            self.emit("ok\r\n", delay)

    def on_line(self, line: str) -> None:
        self.lines.append(line)
        if not line:
            return
        words = line.upper().split()
        cmd = words[0]
        args = {w[0]: float(w[1:]) for w in words[1:] if len(w) > 1 and re.match(r"^[A-Z][-+\d.]+$", w)}
        if cmd == "M888":
            return
        if cmd in ("G0", "G1"):
            cur = self.motion.position()
            tgt = {}
            for ax in "XYZ":
                if ax in args:
                    tgt[ax.lower()] = cur[ax.lower()] + args[ax] if self.relative else args[ax]
            feed = args.get("F", 1000.0)
            self.motion.go(tgt, feed / 60.0)
            self.ok()
        elif cmd == "M400":
            self.ok(self.motion.remaining_s())
        elif cmd == "M114":
            p = self.motion.position()
            if not self.drop_replies:
                self.emit(f"X:{p['x']:.2f} Y:{p['y']:.2f} Z:{p['z']:.2f} E:0.00 Count X:0 Y:0 Z:0\r\nok\r\n")
        elif cmd == "G90":
            self.relative = False
            self.ok()
        elif cmd == "G91":
            self.relative = True
            self.ok()
        elif cmd == "M1000":
            self.vacuum = True
            self.ok()
        elif cmd in ("M1003",):
            self.vacuum = False
            self.ok()
        elif cmd == "M2101":
            self.nozzle_deg += args.get("R", 0.0)  # relative, like the real rotary module (v1 "Rotated nose by")
            self.ok()
        elif cmd in ("M410", "M112"):
            self.motion.halt()
            self.ok()
        else:  # G21, G4, M17, M18, M1001, M1002, M2100 ...
            self.ok()


class PrinterEmulator(ArmEmulator):
    """Marlin printer board of the tape gantry (LF terminated, same ok/M114 behaviour)."""

    def __init__(self, clock: Clock):
        super().__init__(clock, start=(0.0, 0.0, 0.0))

    def ok(self, delay: float = 0.0) -> None:
        if not self.drop_replies:
            self.emit("ok\n", delay)


# --------------------------------------------------------------------------- relays, supply, reels
class RelayEmulator(LineEmulator):
    def __init__(self, reply: bool = True):
        super().__init__()
        self.on = False
        self.reply = reply
        self.history: list[bool] = []

    def on_line(self, line: str) -> None:
        m = re.match(r"AT\+CH(\d)=([01])", line)
        if m:
            self.on = m.group(2) == "1"
            self.history.append(self.on)
            if self.reply:
                self.emit(f"CH{m.group(1)}={m.group(2)}\r\nOK\r\n")


class TP3005PEmulator(Emulator):
    """Commands end with the literal two characters ``/n`` (as v1 sent them)."""

    def __init__(self):
        self.voltage = 0.0
        self.output = False
        self._buf = ""

    def on_bytes(self, data: bytes) -> None:
        self._buf += data.decode(errors="ignore")
        while "/n" in self._buf:
            cmd, self._buf = self._buf.split("/n", 1)
            if cmd.startswith("VSET1:"):
                self.voltage = float(cmd[6:])
            elif cmd == "OUTPUT1:":
                self.output = True
            elif cmd == "OUTPUT0:":
                self.output = False
            elif cmd == "VOUT1?":
                self.emit(f"{(self.voltage if self.output else 0.0):05.2f}\n")


class ReelsEmulator(LineEmulator):
    def __init__(self):
        super().__init__()
        self.power = {1: False, 2: False}
        self.angles = {1: 0.0, 2: 0.0}

    def on_line(self, line: str) -> None:
        m = re.match(r"MOTOR(\d)_(ON|OFF)$", line)
        if m:
            self.power[int(m.group(1))] = m.group(2) == "ON"
            return
        m = re.match(r"MOTOR(\d)_(CW|CCW)_([\d.]+)$", line)
        if m:
            sign = 1 if m.group(2) == "CW" else -1
            self.angles[int(m.group(1))] += sign * float(m.group(3))


# --------------------------------------------------------------------------- KZ-100, TMCL, turret
class KZ100Emulator(Emulator):
    """Echoes 0x0D after every character; ``U<ch>;`` answers 9 ASCII characters."""

    def __init__(self, clock: Clock, steps_per_s: dict[str, float] | None = None,
                 start_steps: dict[str, int] | None = None):
        self.clock = clock
        self.speed = {"X": 64000.0, "Y": 64000.0, "Z": 64000.0, "L": 65600.0}
        self.speed.update(steps_per_s or {})
        start = {"X": 0, "Y": 0, "Z": 0, "L": 0}
        start.update(start_steps or {})
        self.axes = {ch: LinearMotion({"s": float(v)}, clock) for ch, v in start.items()}
        self._cmd = ""
        self.commands: list[str] = []
        self.mute = False

    def steps(self, ch: str) -> int:
        return int(round(self.axes[ch].position()["s"]))

    def on_bytes(self, data: bytes) -> None:
        for b in data:
            ch = chr(b)
            if not self.mute:
                self.emit(b"\r")
            self._cmd += ch
            if ch == ";":
                self._handle(self._cmd)
                self._cmd = ""

    def _handle(self, cmd: str) -> None:
        self.commands.append(cmd)
        body = cmd[:-1]
        if body == "PA":
            for m in self.axes.values():
                m.halt()
        elif body[0] == "U" and body[1] in self.axes:
            self.emit(f"{self.steps(body[1]):+09d}"[:9])
        elif body[0] == "U":
            self.emit("0")
        elif body[0] == "D":
            ch, n = body[1], int(body[2:])
            m = self.axes[ch]
            m.go({"s": m.position()["s"] + n}, self.speed[ch])


class TmclEmulator(Emulator):
    def __init__(self):
        self.frames: list[bytes] = []
        self.steps = 0

    def on_bytes(self, data: bytes) -> None:
        self.frames.append(bytes(data))
        if len(data) == 9:
            addr, instr, typ, _motor = data[0], data[1], data[2], data[3]
            val = int.from_bytes(data[4:8], "big", signed=True)
            if instr == 4 and typ == 1:
                self.steps += val
            reply = [2, addr, 100, instr, 0, 0, 0, 0]
            self.emit(bytes(reply + [sum(reply) & 0xFF]))


class TurretEmulator(Emulator):
    def __init__(self):
        self.position = 1
        self._buf = ""

    def on_bytes(self, data: bytes) -> None:
        self._buf += data.decode(errors="ignore")
        while "\r" in self._buf:
            cmd, self._buf = self._buf.split("\r", 1)
            m = re.match(r"cRDC(\d)", cmd)
            if m:
                self.position = int(m.group(1))
                self.emit("oRDC\r\n")


# --------------------------------------------------------------------------- Kinesis, thermal
class FakeKCube:
    """Stands in for one .NET K-Cube. Records calls like the SDK sequence v1 used."""

    def __init__(self, serial: str, clock: Clock, start_mm: float = 0.0, speed_mm_s: float = 1.0):
        self.serial = serial
        self.calls: list[tuple] = []
        self.motion = LinearMotion({"p": float(start_mm)}, clock)
        self.speed = speed_mm_s
        self.enabled = False

    def connect(self) -> None:
        self.calls.append(("connect", self.serial))

    def enable(self) -> None:
        self.calls.append(("enable",))
        self.enabled = True

    def position(self) -> float:
        return round(self.motion.position()["p"], 5)

    def is_moving(self) -> bool:
        return self.motion.remaining_s() > 0

    def move_to(self, mm: float, timeout_ms: int) -> None:
        self.calls.append(("move_to", float(mm), timeout_ms))
        if not self.enabled:
            raise RuntimeError("device not enabled")
        self.motion.go({"p": float(mm)}, self.speed)

    def set_velocity(self, max_vel: float, accel: float) -> None:
        self.calls.append(("set_velocity", max_vel, accel))
        self.speed = float(max_vel)

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.motion.halt()

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))


class ThermalModel:
    """First-order stage temperature: steady rise = gain_c_per_w * V^2 / R while relay and output are on."""

    def __init__(self, clock: Clock, psu: TP3005PEmulator, relay: RelayEmulator, *, ambient_c: float = 22.0,
                 resistance_ohm: float = 10.0, gain_c_per_w: float = 2.0, tau_s: float = 60.0):
        self.clock, self.psu, self.relay = clock, psu, relay
        self.ambient, self.r, self.gain, self.tau = ambient_c, resistance_ohm, gain_c_per_w, tau_s
        self.temp = ambient_c
        self.t_last = clock.now()
        self.lock = threading.Lock()

    def read(self) -> float:
        with self.lock:
            now = self.clock.now()
            dt = max(0.0, now - self.t_last)
            self.t_last = now
            power = (self.psu.voltage ** 2 / self.r) if (self.psu.output and self.relay.on) else 0.0
            steady = self.ambient + self.gain * power
            self.temp = steady + (self.temp - steady) * math.exp(-dt / self.tau)
            return round(self.temp, 2)
