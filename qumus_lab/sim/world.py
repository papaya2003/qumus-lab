"""SimWorld: one emulator per configured port, fake K-Cubes, thermal models and camera scenes."""

from __future__ import annotations

from ..config import LabConfig
from ..drivers.cameras import FakeCameraBackend
from .emulators import (
    ArmEmulator,
    Clock,
    FakeKCube,
    KZ100Emulator,
    PrinterEmulator,
    ReelsEmulator,
    RelayEmulator,
    ThermalModel,
    TmclEmulator,
    TP3005PEmulator,
    TurretEmulator,
)


class SimWorld:
    def __init__(self, config: LabConfig, speed: float = 5.0):
        self.clock = Clock(speed)
        self.emulators: dict[str, object] = {}
        self.by_device: dict[str, object] = {}
        self.thermal_by_sensor: dict[str, ThermalModel] = {}
        self.kcubes: dict[str, FakeKCube] = {}
        shared = config.devices.get("shared", {})
        self.psu = TP3005PEmulator()
        self.emulators[shared["power_supply"]["port"]["port"]] = self.psu
        park = {}
        for arm, pts in config.points.get("points", {}).items():
            if "park" in pts:
                park[arm] = pts["park"]["xyz"]
        for mod in config.module_names:
            for dev_id, cfg in config.module(mod)["devices"].items():
                self._add(dev_id, cfg, park)
        from .optics import DemoOptics

        self.optics = DemoOptics(self, config)  # microscope frames; launcher sets optics.ledger

    def _add(self, dev_id: str, cfg: dict, park: dict) -> None:
        drv = cfg["driver"]
        port = (cfg.get("port") or {}).get("port")
        emu = None
        if drv == "gcode_arm":
            emu = ArmEmulator(self.clock, start=tuple(float(v) for v in park.get(dev_id, (0, 200, 100))))
        elif drv == "relay":
            emu = RelayEmulator()
        elif drv == "tape_reels":
            emu = ReelsEmulator()
        elif drv == "printer_gantry":
            emu = PrinterEmulator(self.clock)
        elif drv == "kz100_stage":
            emu = KZ100Emulator(self.clock)
        elif drv == "turret":
            emu = TurretEmulator()
        elif drv == "tmcl_focus":
            emu = TmclEmulator()
        elif drv == "heater":
            relay = RelayEmulator()
            self.emulators[cfg["relay"]["port"]["port"]] = relay
            self.by_device[f"{dev_id}.relay"] = relay
            # transfer stage: v1 reaches the 170 C melt at 27 V, so its steady state must lie above that
            gain = 3.0 if "transfer" in dev_id else 2.0
            self.thermal_by_sensor[str(cfg["sensor_serial"])] = ThermalModel(self.clock, self.psu, relay,
                                                                             gain_c_per_w=gain)
        elif drv == "manipulator":
            rail = KZ100Emulator(self.clock, start_steps={"L": 0})
            self.emulators[cfg["rail"]["port"]["port"]] = rail
            self.by_device[f"{dev_id}.rail"] = rail
            starts = {"x": 6.0, "y": 7.0, "z": 6.5}
            for name, acfg in cfg.get("kinesis", {}).items():
                self.kcubes[str(acfg["serial"])] = FakeKCube(str(acfg["serial"]), self.clock, starts.get(name, 0.0), 0.5)
            if cfg.get("goniometer"):
                g = str(cfg["goniometer"]["serial"])
                self.kcubes[g] = FakeKCube(g, self.clock, 6.6, 0.5)
        if emu is not None and port:
            self.emulators[port] = emu
            self.by_device[dev_id] = emu

    # factories used by lab.Backends -----------------------------------------
    def kcube(self, name: str, acfg: dict, dcfg: dict) -> FakeKCube:
        return self.kcubes[str(acfg["serial"])]

    def camera(self, dev_id: str, cfg: dict) -> FakeCameraBackend:
        if "microscope" in dev_id:
            try:
                import cv2  # noqa: F401
                import numpy  # noqa: F401
            except ImportError:  # minimal installs: marker frames only (no vision)
                return FakeCameraBackend(scene=self._microscope_scene, tint=(70, 60, 90))
            from .optics import RenderCameraBackend

            return RenderCameraBackend(self.optics)
        arm = self.by_device.get("arm_left" if "left" in dev_id else "arm_right")
        return FakeCameraBackend(scene=lambda: self._arm_scene(arm), tint=(50, 70, 60) if "left" in dev_id else (50, 60, 80))

    def _arm_scene(self, arm) -> list[tuple[float, float]]:
        if arm is None:
            return []
        p = arm.motion.position()
        return [(0.5 + p["x"] / 800.0, 1.0 - p["y"] / 400.0)]

    def _microscope_scene(self) -> list[tuple[float, float]]:
        stage = next((e for k, e in self.by_device.items() if k == "sample_stage"), None)
        if stage is None:
            return [(0.5, 0.5)]
        x = stage.steps("X") / 12800.0
        y = stage.steps("Y") / 12800.0
        return [(0.5 - x / 4.0, 0.5 + y / 4.0)]

    def dracal(self, argv: list[str], timeout_s: float) -> str:
        serial = argv[argv.index("-s") + 1]
        model = self.thermal_by_sensor.get(serial)
        if model is None:
            from ..drivers.base import NoReply

            raise NoReply(f"no simulated sensor {serial}")
        return f"{model.read():.2f}"
