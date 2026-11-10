"""Build the device objects of the requested modules from config, for the real lab or for --demo.

Real: real serial ports, .NET Kinesis, OpenCV/Uvcham cameras, the Dracal exe.
Demo: the *same drivers* talking to byte-level emulators (qumus_lab/sim/emulators.py), fake K-Cubes,
synthetic camera frames and a thermal model behind a fake Dracal CLI.
Nothing here sends a byte: devices connect only when ``connect_all()`` is called.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import LabConfig
from .drivers.base import Device
from .drivers.cameras import Camera, FakeCameraBackend, OpenCVBackend, UvchamBackend
from .drivers.focus_tmcl import TmclFocus
from .drivers.gcode_arm import GcodeArm
from .drivers.heater import Heater
from .drivers.kinesis import DotNetKCube, StampManipulator
from .drivers.kz100 import KZ100Stage
from .drivers.power_supply import TP3005P
from .drivers.printer_gantry import PrinterGantry
from .drivers.relay import RelaySwitch
from .drivers.tape_reels import TapeReels
from .drivers.thermometer import DracalThermometer
from .drivers.turret import Turret
from .transport.fake_serial import FakeSerialFactory
from .transport.serial_port import RealSerialFactory

log = logging.getLogger(__name__)


@dataclass
class Backends:
    demo: bool
    serial_factory: object
    kcube: Callable[[str, dict, dict], object]          # (axis name, axis cfg, device cfg) -> backend
    camera: Callable[[str, dict], object]               # (device id, cfg) -> backend
    dracal_runner: Callable[[list[str], float], str] | None = None
    world: object | None = None


def real_backends() -> Backends:
    def kcube(name, acfg, dcfg):
        return DotNetKCube(acfg["serial"], acfg.get("model", "kst201"), dcfg.get("kinesis_dir", ""))

    def camera(dev_id, cfg):
        if cfg["driver"] == "uvcham_camera":
            return UvchamBackend(cfg.get("index", 0), cfg.get("sdk_dir", ""))
        return OpenCVBackend(cfg.get("index", 0), cfg.get("width", 1920), cfg.get("height", 1080))

    return Backends(demo=False, serial_factory=RealSerialFactory(), kcube=kcube, camera=camera)


@dataclass
class Lab:
    config: LabConfig
    backends: Backends
    modules: list[str]
    devices: dict[str, Device] = field(default_factory=dict)
    device_module: dict[str, str] = field(default_factory=dict)
    shared: dict[str, Device] = field(default_factory=dict)

    def module_devices(self, module: str) -> dict[str, Device]:
        return {k: v for k, v in self.devices.items() if self.device_module.get(k) == module}

    def connect_all(self) -> dict[str, str | None]:
        """Open every link and query once. Failures are recorded on the device, never raised."""
        result = {}
        for dev_id, dev in list(self.shared.items()) + list(self.devices.items()):
            if dev.connected:
                result[dev_id] = None
                continue
            try:
                dev.connect()
                dev.last_error = None
                result[dev_id] = None
            except Exception as exc:  # noqa: BLE001 - report every failure kind
                dev.connected = False
                dev.last_error = f"{type(exc).__name__}: {exc}"
                result[dev_id] = dev.last_error
                log.warning("connect %s failed: %s", dev_id, dev.last_error)
        return result

    def close_all(self) -> None:
        for dev in list(self.devices.values()) + list(self.shared.values()):
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass


def build_lab(config: LabConfig, backends: Backends, modules: list[str] | None = None) -> Lab:
    modules = modules or config.module_names
    lab = Lab(config=config, backends=backends, modules=modules)
    shared_cfg = config.devices.get("shared", {})
    sf = backends.serial_factory
    psu_cfg = dict(shared_cfg["power_supply"])
    if backends.demo:
        psu_cfg.update(open_settle_s=0, reply_window_s=0.05)
    psu = TP3005P("power_supply", psu_cfg, sf)
    lab.shared["power_supply"] = psu
    dracal_cfg = shared_cfg.get("dracal", {})
    heat_limits = config.limits.get("heaters", {})

    # Heater relays are built first so each heater can switch the other one's relay off.
    heater_relays: dict[str, RelaySwitch] = {}
    for mod in modules:
        for dev_id, cfg in config.module(mod)["devices"].items():
            if cfg["driver"] == "heater":
                rcfg = dict(cfg["relay"], open_settle_s=0) if backends.demo else cfg["relay"]
                heater_relays[dev_id] = RelaySwitch(f"{dev_id}.relay", rcfg, sf)

    for mod in modules:
        for dev_id, cfg in config.module(mod)["devices"].items():
            dev = _build_device(dev_id, cfg, backends, config, psu, dracal_cfg, heater_relays, heat_limits)
            lab.devices[dev_id] = dev
            lab.device_module[dev_id] = mod
    return lab


def _build_device(dev_id, cfg, backends: Backends, config: LabConfig, psu, dracal_cfg, heater_relays, heat_limits):
    sf = backends.serial_factory
    drv = cfg["driver"]
    if backends.demo:  # emulators need no settle time after opening
        speed = float(getattr(getattr(backends.world, "clock", None), "speed", 1.0) or 1.0)
        cfg = dict(cfg, open_settle_s=0, poll_s=max(0.02, 0.3 / max(1.0, speed / 5.0)))
    if drv == "gcode_arm":
        return GcodeArm(dev_id, cfg, sf)
    if drv == "relay":
        return RelaySwitch(dev_id, cfg, sf)
    if drv == "tape_reels":
        return TapeReels(dev_id, cfg, sf)
    if drv == "printer_gantry":
        return PrinterGantry(dev_id, cfg, sf)
    if drv == "kz100_stage":
        stage_cfg = dict(cfg)
        lim = config.limits.get("axes", {}).get(dev_id, {})
        stage_cfg["axes"] = {a: dict(acfg, **{k: lim.get(a, {}).get(k) for k in ("min_mm", "max_mm")})
                             for a, acfg in cfg["axes"].items()}
        return KZ100Stage(dev_id, stage_cfg, sf)
    if drv == "turret":
        return Turret(dev_id, cfg, sf)
    if drv == "tmcl_focus":
        return TmclFocus(dev_id, cfg, sf)
    if drv in ("opencv_camera", "uvcham_camera"):
        return Camera(dev_id, cfg, backends.camera(dev_id, cfg), config.runs_dir / "captures")
    if drv == "manipulator":
        lim = config.limits.get("axes", {}).get(dev_id, {})
        mcfg = dict(cfg)
        mcfg["kinesis"] = {a: dict(acfg, min_mm=lim.get(a, {}).get("min_mm", 0.0), max_mm=lim.get(a, {}).get("max_mm", 13.0))
                           for a, acfg in cfg["kinesis"].items()}
        names = list(mcfg["kinesis"]) + (["goni"] if cfg.get("goniometer") else [])
        backends_map = {}
        for name in names:
            acfg = mcfg["kinesis"].get(name) or cfg["goniometer"]
            backends_map[name] = backends.kcube(name, acfg, cfg)
        rail_cfg = dict(cfg["rail"])
        rl = lim.get("rail", {})
        rail_cfg["axes"] = {"rail": dict(cfg["rail"]["axes"]["rail"], min_mm=rl.get("min_mm"), max_mm=rl.get("max_mm"))}
        rail = KZ100Stage(f"{dev_id}.rail", rail_cfg, sf)
        return StampManipulator(dev_id, mcfg, backends_map, rail)
    if drv == "heater":
        thermo_cfg = dict(dracal_cfg, sensor_serial=cfg["sensor_serial"])
        thermo = DracalThermometer(f"{dev_id}.thermometer", thermo_cfg, runner=backends.dracal_runner)
        hcfg = dict(cfg)
        lim = heat_limits.get(dev_id, {})
        for k in ("max_c", "overshoot_c"):
            if lim.get(k) is not None:
                hcfg[k] = lim[k]
        if lim.get("max_timeout_s") is not None:
            hcfg["max_timeout_s"] = lim["max_timeout_s"]
        others = [heater_relays[o] for o in cfg.get("other_heaters", []) if o in heater_relays]
        return Heater(dev_id, hcfg, heater_relays[dev_id], psu, thermo, others)
    raise ValueError(f"{dev_id}: unknown driver {drv!r}")


def demo_backends(config: LabConfig, speed: float = 5.0) -> Backends:
    from .sim.world import SimWorld

    world = SimWorld(config, speed=speed)
    return Backends(demo=True, serial_factory=FakeSerialFactory(world.emulators), kcube=world.kcube,
                    camera=world.camera, dracal_runner=world.dracal, world=world)


__all__ = ["Backends", "Lab", "build_lab", "demo_backends", "real_backends", "FakeCameraBackend"]
