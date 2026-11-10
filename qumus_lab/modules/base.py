"""Module = one contract v1 window (exfoliation or stacking): its devices, its gate and its actions.

Actions are small functions that compose generic driver operations with named points. Each action
declares its params (JSON schema), whether it moves something (``motion``), whether it is only for the
debug page / probe (``local_only``) and whether a client without the lease may send it (``any_client``).
``concurrent`` actions (reads, capture, heat_off) run beside a busy executor; everything else runs one
at a time.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import LabConfig
from ..drivers.base import Device
from ..safety.gate import GateContext, Rejected, SafetyGate
from ..safety.zones import ZoneRegistry


class Cancelled(Exception):
    """The running action saw a stop request between two driver calls."""


@dataclass
class ActionContext:
    module: Module
    gate: SafetyGate
    gate_ctx: GateContext
    cancel: threading.Event
    log: Callable[[str, dict], None]

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("stopped")

    def device(self, device_id: str, kinds: tuple[str, ...] | None = None) -> Device:
        dev = self.module.devices.get(device_id)
        if dev is None:
            raise Rejected(f"unknown device {device_id!r} in module {self.module.name}; have {sorted(self.module.devices)}")
        if kinds and dev.kind not in kinds:
            raise Rejected(f"{device_id} is a {dev.kind}, expected {', '.join(kinds)}")
        if not dev.connected:
            raise Rejected(f"{device_id} is not connected ({dev.last_error})", 503)
        return dev


@dataclass
class Action:
    name: str
    fn: Callable[[ActionContext, dict], Any]
    params: dict = field(default_factory=dict)  # JSON schema "properties"
    required: tuple[str, ...] = ()
    motion: bool = False
    local_only: bool = False
    any_client: bool = False
    allowed_when_locked: bool = False  # heat_off: switching heat off must work while the module is locked
    concurrent: bool = False
    #: a whole job (L5 flow): accepted at once with a run_id, then runs in the module's job worker
    job: bool = False
    description: str = ""
    #: synchronous gate checks (no I/O) run before the command is accepted, so caps fail with 400 at once
    precheck: Callable[[Module, dict], None] | None = None

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required),
                "additionalProperties": False}

    def describe(self) -> dict:
        return {"params": self.schema(), "any_client": self.any_client, "motion": self.motion,
                "local_only": self.local_only, "allowed_when_locked": self.allowed_when_locked,
                "job": self.job, "description": self.description}

    def validate(self, params: dict) -> dict:
        out = {}
        for key in self.required:
            if key not in params:
                raise Rejected(f"{self.name}: missing param {key!r}")
        for key, val in params.items():
            spec = self.params.get(key)
            if spec is None:
                raise Rejected(f"{self.name}: unknown param {key!r}")
            out[key] = _check_type(self.name, key, val, spec)
        for key, spec in self.params.items():
            if key not in out and "default" in spec:
                out[key] = spec["default"]
        return out


def _check_type(action: str, key: str, val: Any, spec: dict) -> Any:
    t = spec.get("type")
    ok = {
        "string": isinstance(val, str),
        "boolean": isinstance(val, bool),
        "integer": isinstance(val, int) and not isinstance(val, bool),
        "number": isinstance(val, int | float) and not isinstance(val, bool) and math.isfinite(float(val)),
        "object": val is None or isinstance(val, dict),
        "array": isinstance(val, list),
    }.get(t, True)
    if not ok:
        raise Rejected(f"{action}: param {key!r} must be {t}")
    if "enum" in spec and val not in spec["enum"]:
        raise Rejected(f"{action}: param {key!r} must be one of {spec['enum']}")
    if t == "number":
        val = float(val)
    for bound, cmp in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b)):
        if bound in spec and not cmp(val, spec[bound]):
            raise Rejected(f"{action}: param {key!r} must be {bound} {spec[bound]}")
    return val


class Module:
    def __init__(self, name: str, config: LabConfig, devices: dict[str, Device], demo: bool,
                 shared: dict[str, Device] | None = None, zones: ZoneRegistry | None = None):
        self.name = name
        self.zones = zones
        self.config = config
        self.devices = devices
        self.shared = shared or {}
        self.demo = demo
        self.gate = SafetyGate(config, name, demo)
        self.actions: dict[str, Action] = {}
        self._switch_timers: dict[str, threading.Timer] = {}
        self.lab_store = None  # qumus.db (set by the launcher); L4 operations keep the sample ledger in it
        self.time_scale = 1.0  # --demo: v1 waits inside L4 operations run this many times faster
        self.world = None      # --demo only: the SimWorld (never used by operations, only by tests)
        #: set by the service: lock the module (code, message) when an off can not be confirmed
        self.on_fault: Callable[[str, str], None] | None = None

    def add(self, action: Action) -> None:
        self.actions[action.name] = action

    def device_ids(self, kinds: tuple[str, ...] | None = None) -> list[str]:
        return [k for k, d in self.devices.items() if kinds is None or d.kind in kinds]

    def zone_names(self) -> list[str]:
        return self.zones.zones_for_devices(self.devices) if self.zones is not None else []

    def points_for(self, device_id: str) -> list[str]:
        return sorted(self.config.points.get("points", {}).get(device_id, {}))

    def describe_devices(self) -> list[dict]:
        out = []
        for dev_id, dev in self.devices.items():
            cfg = dev.cfg
            out.append({"id": dev_id, "kind": dev.kind, "fields": dict(getattr(dev, "fields", {})),
                        "source": cfg.get("source"), "points": self.points_for(dev_id)})
        return out

    def motion_devices(self) -> list[Device]:
        return [d for d in self.devices.values() if d.kind in ("arm", "stage", "manipulator", "gantry", "focus", "turret")]

    # auto-off timers for switches set from the probe / debug page
    def schedule_auto_off(self, dev, seconds: float, log) -> None:
        old = self._switch_timers.pop(dev.id, None)
        if old:
            old.cancel()

        def fire():
            try:
                dev.set(False)
                log("switch_auto_off", {"device": dev.id, "after_s": seconds})
            except Exception as exc:  # noqa: BLE001
                log("switch_auto_off_failed", {"device": dev.id, "error": str(exc)})
                if self.on_fault is not None:  # the switch may still be on: same rule as any unknown outcome
                    self.on_fault("switch_auto_off_failed", f"{dev.id}: auto-off not confirmed ({exc})")

        t = threading.Timer(seconds, fire)
        t.daemon = True
        self._switch_timers[dev.id] = t
        t.start()

    def cancel_auto_off(self, dev_id: str) -> None:
        t = self._switch_timers.pop(dev_id, None)
        if t:
            t.cancel()
