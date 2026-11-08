"""Run the ORIGINAL v1 driver classes (read-only, never copied) against fake serial ports.

Used only by tests to record what v1 wrote to each port, so the new drivers can be compared byte for
byte. Needs the v1 tree (default /mnt/project-files/qumus/v1-original/Qumus-lihan-graphene or
$QUMUS_V1_ROOT); tests that need it are skipped when it is absent (e.g. on the lab PC), and the
recorded bytes are also kept in tests/golden/v1_bytes.json.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
import types
from pathlib import Path

from qumus_lab.transport.fake_serial import FakeSerial
from qumus_lab.transport.serial_port import PortSpec

V1_ROOT = Path(os.environ.get("QUMUS_V1_ROOT", "/mnt/project-files/qumus/v1-original/Qumus-lihan-graphene"))


def v1_tree_present() -> bool:
    return (V1_ROOT / "common" / "hardware" / "robot_arm_controller.py").is_file()


def v1_missing() -> str | None:
    """Why v1 code can not be run here, or None. The golden-file comparison always runs."""
    if not v1_tree_present():
        return "v1 source tree not present"
    try:
        import numpy  # noqa: F401  (v1 hardware modules import numpy at module level)
    except ImportError:
        return "numpy not installed (pip install -e .[dev])"
    return None


def v1_available() -> bool:
    return v1_missing() is None


class _Status:
    def update_true_instrument(self, *a, **k):
        pass

    def get_latest_status(self, *a, **k):
        return None


def _pkg(name: str, path: Path | None) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)] if path else []
    sys.modules[name] = mod
    return mod


_loaded = False


def load_v1():
    """Import v1 hardware modules without running v1 package __init__ files (they import cv2, agents...)."""
    global _loaded
    if not _loaded:
        _pkg("common", V1_ROOT / "common")
        _pkg("common.hardware", V1_ROOT / "common" / "hardware")
        _pkg("common.hardware.drivers", V1_ROOT / "common" / "hardware" / "drivers")
        _pkg("common.logic", V1_ROOT / "common" / "logic")
        _pkg("common.services", None)
        status_mod = types.ModuleType("common.services.instrument_status_service")
        status_mod.get_instrument_status_service = lambda: _Status()
        sys.modules["common.services.instrument_status_service"] = status_mod
        if "tqdm" not in sys.modules:
            tq = types.ModuleType("tqdm")
            tq.tqdm = lambda it=None, **k: it
            sys.modules["tqdm"] = tq
        _loaded = True
    names = ["robot_arm_controller", "relay_controller", "power_controller", "motor_controller",
             "printer_controller", "sample_PDV_controller", "focus_controller", "turret_controller",
             "temp_controller", "drivers.kz100_pdv_api", "stamp_PDV_controller"]
    return {n: importlib.import_module(f"common.hardware.{n}") for n in names}


class PortRecorder:
    """Patches serial.Serial so every open of port P gets a FakeSerial wired to emulators[P].
    ``tx[P]`` accumulates the bytes written across all opens (v1 KZ-100 code reopens per command)."""

    def __init__(self, emulators: dict):
        self.emulators = emulators
        self.tx: dict[str, bytearray] = {p: bytearray() for p in emulators}

    def serial_ctor(self, port=None, baudrate=9600, *args, **kwargs):
        timeout = kwargs.get("timeout", 1.0)
        fs = FakeSerial(PortSpec(port=port, baudrate=int(kwargs.get("baud", baudrate)), timeout=float(timeout or 0.2)),
                        self.emulators[port])
        rec = self.tx[port]
        orig_write = fs.write

        def write(data):
            rec.extend(bytes(data))
            return orig_write(data)

        fs.write = write
        return fs

    def clear(self) -> None:
        for buf in self.tx.values():
            buf.clear()


def patched_serial(monkeypatch, recorder: PortRecorder):
    import serial

    monkeypatch.setattr(serial, "Serial", recorder.serial_ctor)
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(min(s, 0.001)))
