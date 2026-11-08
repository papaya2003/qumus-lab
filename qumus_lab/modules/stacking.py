"""Stacking module (right side): right arm, sample stage, microscope (turret, focus, camera), stamp holder,
stage and stamp vacuums, transfer heater, right desk camera."""

from __future__ import annotations

from ..safety.gate import Rejected
from .actions import register_common
from .base import Action, ActionContext, Module


def set_objective(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device("turret", ("turret",))
    mag = int(p["objective"])
    if mag not in dev.positions:
        raise Rejected(f"objective {mag}x not in {sorted(dev.positions)}")
    return dev.rotate_to(dev.positions[mag])


def focus_by(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device("focus", ("focus",))
    um = float(p["um"])
    lim = ctx.gate.config.limits.get("axes", {}).get("focus", {}).get("z", {})
    max_um = float(lim.get("max_move_um", 30000))
    if ctx.gate.real and lim.get("source") != "measured":
        max_um = min(max_um, float(ctx.gate.probe.get("focus_jog_max_um", 50)))
    if abs(um) > max_um:
        raise Rejected(f"focus move {um} um above {max_um} um")
    dev.move_by_um(um)
    return dev.snapshot()


def build_stacking(module: Module) -> Module:
    register_common(module)
    if "turret" in module.devices:
        module.add(Action("set_objective", set_objective,
                          {"objective": {"type": "integer", "enum": [5, 10, 20, 100]}}, ("objective",), motion=True,
                          description="Rotate the turret to an objective (v1 cRDC<n>)."))
    if "focus" in module.devices:
        module.add(Action("focus_by", focus_by, {"um": {"type": "number"}}, ("um",), motion=True,
                          description="Relative focus move in um (real mode before L3: <= 50 um)."))
    return module
