"""Load config/devices.yaml, points.yaml and limits.yaml (UTF-8, pathlib; works on Windows)."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_RUNS_DIR = REPO_ROOT / "runs" / "real-lab"
DEFAULT_DEMO_RUNS_DIR = REPO_ROOT / "runs" / "demo"  # --demo never writes into the real lab's records


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class LabConfig:
    devices: dict
    points: dict
    limits: dict
    config_dir: Path
    runs_dir: Path
    places: dict | None = None

    @property
    def module_names(self) -> list[str]:
        return list(self.devices.get("modules", {}))

    def module(self, name: str) -> dict:
        return self.devices["modules"][name]

    def module_of(self, device_id: str) -> str | None:
        for name, mod in self.devices.get("modules", {}).items():
            if device_id in mod.get("devices", {}):
                return name
        return None

    def device_cfg(self, device_id: str) -> dict:
        mod = self.module_of(device_id)
        if mod is None:
            raise KeyError(device_id)
        return self.devices["modules"][mod]["devices"][device_id]

    def all_device_ids(self) -> list[str]:
        return [d for m in self.module_names for d in self.module(m)["devices"]]

    def configured_ports(self) -> list[tuple[str, str, str]]:
        """(device, COM port, driver) for every serial port in devices.yaml, shared supply first."""
        out = []
        sh = self.devices.get("shared", {})
        if "power_supply" in sh:
            out.append(("power_supply", sh["power_supply"]["port"]["port"], "tp3005p"))
        for dev_id in self.all_device_ids():
            cfg = self.device_cfg(dev_id)
            if "port" in cfg:
                out.append((dev_id, cfg["port"]["port"], cfg["driver"]))
            if cfg.get("relay"):
                out.append((f"{dev_id}.relay", cfg["relay"]["port"]["port"], "relay"))
            if cfg.get("rail"):
                out.append((f"{dev_id}.rail", cfg["rail"]["port"]["port"], "kz100"))
        return out

    def copy(self) -> LabConfig:
        return LabConfig(copy.deepcopy(self.devices), copy.deepcopy(self.points), copy.deepcopy(self.limits),
                         self.config_dir, self.runs_dir, copy.deepcopy(self.places))


def load_config(config_dir: str | Path | None = None, runs_dir: str | Path | None = None, *,
                demo: bool = False) -> LabConfig:
    """``runs_dir`` default: runs/real-lab, or runs/demo for --demo (QUMUS_LAB_RUNS overrides both)."""
    cdir = Path(config_dir or os.environ.get("QUMUS_LAB_CONFIG") or DEFAULT_CONFIG_DIR)
    rdir = Path(runs_dir or os.environ.get("QUMUS_LAB_RUNS") or (DEFAULT_DEMO_RUNS_DIR if demo else DEFAULT_RUNS_DIR))
    return LabConfig(
        devices=load_yaml(cdir / "devices.yaml"),
        points=load_yaml(cdir / "points.yaml"),
        limits=load_yaml(cdir / "limits.yaml"),
        config_dir=cdir,
        runs_dir=rdir,
        places=load_yaml(cdir / "places.yaml") if (cdir / "places.yaml").exists() else {},
    )
