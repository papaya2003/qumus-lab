"""Thorlabs Kinesis K-Cubes (KST201 stepper x/y/z, KDC101 goniometer) through the .NET Kinesis SDK.

Call sequence from v1 common/hardware/KST201_controller.py / KDC101_controller.py:
BuildDeviceList -> Create<KCube>(serial) -> Connect(serial) -> WaitForSettingsInitialized(10000)
-> LoadMotorConfiguration(serial) -> StartPolling(200 ms) -> EnableDevice; MoveTo(Decimal(mm), timeout_ms);
velocity via GetVelocityParams/SetVelocityParams; positions via ``Position``.

Changed on purpose: EnableDevice is sent on the first move, not on connect; MoveTo is issued non-blocking
(timeout 0) and polled so Stop can interrupt it, with a ``move_timeout_s`` limit (v1 blocked up to 600 s);
the goniometer is read-only (no v1 flow moved it).
pythonnet and the Kinesis DLLs are imported lazily and only on Windows (path in config/devices.yaml).
"""

from __future__ import annotations

import time
from typing import Protocol

from .base import Device, Interrupted, NoReply, OutcomeUnknown, Refused


class KCubeBackend(Protocol):
    def connect(self) -> None: ...
    def enable(self) -> None: ...
    def position(self) -> float: ...
    def is_moving(self) -> bool: ...
    def move_to(self, mm: float, timeout_ms: int) -> None: ...
    def set_velocity(self, max_vel: float, accel: float) -> None: ...
    def stop(self) -> None: ...
    def disconnect(self) -> None: ...


class DotNetKCube:
    """Real K-Cube via pythonnet. Constructed only on the lab PC."""

    def __init__(self, serial: str, model: str, kinesis_dir: str, polling_ms: int = 200):
        self.serial = str(serial)
        self.model = model
        self.kinesis_dir = kinesis_dir
        self.polling_ms = polling_ms
        self.dev = None
        self._Decimal = None

    def _load(self):
        import os
        import sys

        if os.name != "nt":
            raise NoReply("Kinesis SDK is Windows-only")
        os.add_dll_directory(self.kinesis_dir)
        sys.path.append(self.kinesis_dir)
        import clr  # pythonnet

        clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.DeviceManagerCLI.dll"))
        clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.GenericMotorCLI.dll"))
        if self.model == "kst201":
            clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.KCube.StepperMotorCLI.dll"))
            from Thorlabs.MotionControl.KCube.StepperMotorCLI import KCubeStepper as Cube  # type: ignore

            create = Cube.CreateKCubeStepper
        else:
            clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.KCube.DCServoCLI.dll"))
            from Thorlabs.MotionControl.KCube.DCServoCLI import KCubeDCServo as Cube  # type: ignore

            create = Cube.CreateKCubeDCServo
        from System import Decimal  # type: ignore
        from System.Globalization import CultureInfo  # type: ignore
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI  # type: ignore

        self._Decimal = lambda x: Decimal.Parse(str(x), CultureInfo.InvariantCulture)
        return DeviceManagerCLI, create

    def connect(self) -> None:
        manager, create = self._load()
        manager.BuildDeviceList()
        d = create(self.serial)
        d.Connect(self.serial)
        if not d.IsSettingsInitialized():
            d.WaitForSettingsInitialized(10000)
        try:
            d.LoadMotorConfiguration(self.serial)
        except Exception:
            pass
        d.StartPolling(self.polling_ms)
        time.sleep(0.1)
        self.dev = d

    def enable(self) -> None:
        self.dev.EnableDevice()
        time.sleep(0.1)

    def position(self) -> float:
        return float(str(self.dev.Position))

    def is_moving(self) -> bool:
        return bool(self.dev.Status.IsMoving)

    def move_to(self, mm: float, timeout_ms: int) -> None:
        self.dev.MoveTo(self._Decimal(mm), int(timeout_ms))

    def set_velocity(self, max_vel: float, accel: float) -> None:
        vp = self.dev.GetVelocityParams()
        vp.MinVelocity = self._Decimal(0.0)
        vp.MaxVelocity = self._Decimal(max_vel)
        vp.Acceleration = self._Decimal(accel)
        self.dev.SetVelocityParams(vp)

    def stop(self) -> None:
        self.dev.StopImmediate()

    def disconnect(self) -> None:
        if self.dev is None:
            return
        try:
            self.dev.StopPolling()
        finally:
            self.dev.Disconnect(True)
            self.dev = None


class KinesisAxis:
    """One K-Cube axis with limits and the enable-on-first-move rule."""

    def __init__(self, name: str, cfg: dict, backend: KCubeBackend, move_timeout_s: float):
        self.name = name
        self.cfg = cfg
        self.backend = backend
        self.min_mm = float(cfg.get("min_mm", 0.0))
        self.max_mm = float(cfg.get("max_mm", 13.0))
        self.read_only = bool(cfg.get("read_only", False))
        self.move_timeout_s = move_timeout_s
        self.enabled = False
        self.pos_mm: float | None = None

    def read(self) -> float:
        self.pos_mm = float(self.backend.position())
        return self.pos_mm

    def move_to(self, mm: float, stop_evt=None) -> float:
        if self.read_only:
            raise Refused(f"axis {self.name} is read-only")
        mm = float(mm)
        if not self.min_mm <= mm <= self.max_mm:
            raise Refused(f"axis {self.name}: {mm} mm outside [{self.min_mm}, {self.max_mm}]")
        if stop_evt is not None and stop_evt.is_set():
            raise Interrupted(f"axis {self.name}: stop requested; move not sent")
        if not self.enabled:
            self.backend.enable()
            self.enabled = True
        try:
            # timeout 0 = return at once (v1 blocked for up to 600 s); we poll so Stop can interrupt.
            self.backend.move_to(mm, 0)
        except Exception as exc:
            raise OutcomeUnknown(f"axis {self.name}: {exc}") from exc
        t0 = time.monotonic()
        while self.backend.is_moving():
            if stop_evt is not None and stop_evt.is_set():
                # the stop may have reached the K-Cube just before this move: stop again, then report it
                self.backend.stop()
                self.read()
                raise Interrupted(f"axis {self.name}: stopped during the move")
            if time.monotonic() - t0 > self.move_timeout_s:
                raise OutcomeUnknown(f"axis {self.name}: still moving after {self.move_timeout_s} s")
            time.sleep(0.05)
        return self.read()


class StampManipulator(Device):
    """Stamp holder: Kinesis x/y/z (+ read-only goniometer) and the KZ-100 L rail (COM19)."""

    kind = "manipulator"

    def __init__(self, device_id: str, cfg: dict, backends: dict[str, KCubeBackend], rail):
        super().__init__(device_id, cfg)
        move_timeout_s = float(cfg.get("move_timeout_s", 120.0))
        self.axes: dict[str, KinesisAxis] = {}
        for name, acfg in (cfg.get("kinesis") or {}).items():
            self.axes[name] = KinesisAxis(name, acfg, backends[name], move_timeout_s)
        gcfg = cfg.get("goniometer")
        if gcfg:
            gcfg = dict(gcfg, read_only=True)
            self.axes["goni"] = KinesisAxis("goni", gcfg, backends["goni"], move_timeout_s)
            cal = gcfg.get("calibration") or {}
            self._goni_cal = (float(cal.get("pos_left_mm", 0.0)), float(cal.get("deg_left", -8.6)),
                              float(cal.get("pos_right_mm", 12.0)), float(cal.get("deg_right", 6.9)))
        self.rail = rail  # KZ100Stage with one axis "rail"
        self.fields = {f"{a}_mm": "mm" for a in self.axes if a != "goni"}
        self.fields.update({"goni_deg": "deg", "rail_mm": "mm"})
        self.moving = False

    def clear_stop(self) -> None:
        super().clear_stop()
        self.rail.clear_stop()

    def goni_deg(self, pos_mm: float | None) -> float | None:
        if pos_mm is None:
            return None
        pl, dl, pr, dr = self._goni_cal
        return dl + (dr - dl) / (pr - pl) * (pos_mm - pl)

    def connect(self) -> None:
        errors = []
        for name, ax in self.axes.items():
            try:
                ax.backend.connect()
                ax.read()
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        try:
            self.rail.connect()
        except Exception as exc:
            errors.append(f"rail: {exc}")
        if errors:
            self.last_error = "; ".join(errors)
            raise NoReply(f"{self.id}: " + self.last_error)
        self.connected = True

    def close(self) -> None:
        for ax in self.axes.values():
            try:
                ax.backend.disconnect()
            except Exception:
                pass
        self.rail.close()
        self.connected = False

    def read(self) -> dict:
        self.require_connected()
        for ax in self.axes.values():
            ax.read()
        self.rail.read()
        self.stamp_read()
        return self.snapshot()

    def snapshot(self) -> dict:
        out = {f"{n}_mm": a.pos_mm for n, a in self.axes.items() if n != "goni"}
        if "goni" in self.axes:
            out["goni_deg"] = self.goni_deg(self.axes["goni"].pos_mm)
        out["rail_mm"] = self.rail.pos_mm.get("rail")
        out["moving"] = self.moving
        return out

    def set_velocity(self, axis: str, max_vel: float, accel: float) -> None:
        self.axes[axis].backend.set_velocity(float(max_vel), float(accel))

    def move_to(self, axis: str, mm: float) -> float:
        self.require_connected()
        self.check_not_stopped()
        self.moving = True
        try:
            if axis == "rail":
                return self.rail.move_to("rail", mm)
            if axis not in self.axes:
                raise Refused(f"{self.id}: unknown axis {axis!r}")
            return self.axes[axis].move_to(mm, self._stop_evt)
        finally:
            self.moving = False

    def position(self, axis: str) -> float | None:
        if axis == "rail":
            return self.rail.pos_mm.get("rail")
        return self.axes[axis].pos_mm

    def stop(self) -> bool | None:
        if not self.connected:
            return None
        self._stop_evt.set()
        ok = True
        for name, ax in self.axes.items():
            if name == "goni":
                continue
            try:
                ax.backend.stop()
            except Exception as exc:
                self.last_error = str(exc)
                ok = False
        r = self.rail.stop()
        return ok and r is not False
