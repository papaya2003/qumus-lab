"""Dracal USB thermometer read through the vendor CLI ``dracal-usb-get.exe``.

Command line from v1 common/hardware/temp_controller.py:
``<exe> -s <serial> -i <index> -T C -x 2 -7``. v1 had no timeout; here the call times out (default 5 s).
The exe path is in config/devices.yaml (lab PC only).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from .base import Device, NoReply

NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def dracal_argv(exe: str, serial: str, index: str = "0", unit: str = "C") -> list[str]:
    return [exe, "-s", str(serial), "-i", str(index), "-T", unit, "-x", "2", "-7"]


class DracalThermometer(Device):
    kind = "thermometer"
    fields = {"temp_c": "C"}

    def __init__(self, device_id: str, cfg: dict, runner: Callable[[list[str], float], str] | None = None):
        super().__init__(device_id, cfg)
        self.exe = str(cfg.get("exe_path", "dracal-usb-get.exe"))
        self.serial = str(cfg["sensor_serial"])
        self.index = str(cfg.get("index", "0"))
        self.unit = str(cfg.get("unit", "C"))
        self.timeout_s = float(cfg.get("timeout_s", 5.0))
        self.runner = runner or _run_cli
        self.temp_c: float | None = None

    def connect(self) -> None:
        self.connected = True
        self.read()

    def read(self) -> dict:
        out = self.runner(dracal_argv(self.exe, self.serial, self.index, self.unit), self.timeout_s)
        m = NUM_RE.search(out or "")
        if not m:
            raise NoReply(f"{self.id}: Dracal returned {out!r}")
        self.temp_c = float(m.group())
        self.stamp_read()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"temp_c": self.temp_c}


def _run_cli(argv: list[str], timeout_s: float) -> str:
    try:
        return subprocess.check_output(argv, stderr=subprocess.STDOUT, text=True, errors="replace",
                                       timeout=timeout_s).strip()
    except FileNotFoundError as exc:
        raise NoReply(f"Dracal CLI not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NoReply(f"Dracal CLI timed out after {timeout_s} s") from exc
    except subprocess.CalledProcessError as exc:
        raise NoReply(f"Dracal CLI failed: {exc.output!r}") from exc
