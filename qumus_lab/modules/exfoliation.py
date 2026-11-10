"""Exfoliation module (left side): left arm, vacuum, fan, heater, tape reels, tape gantry, left desk camera."""

from __future__ import annotations

from ..safety.gate import Rejected
from .actions import register_common
from .base import Action, ActionContext, Module


def _gantry_preset(ctx: ActionContext, name: str) -> tuple:
    dev = ctx.device("tape_gantry", ("gantry",))
    pt = ctx.gate.check_point("tape_gantry", name)
    return dev, pt


def apply_tape(ctx: ActionContext, p: dict) -> dict:
    """v1 apply_tape: approach in X, then the fixed arc (geometry in points.yaml tape_gantry.apply_tape)."""
    dev, pt = _gantry_preset(ctx, "apply_tape")
    pos = dev.read()
    if pos["x_mm"] < -50 or pos["z_mm"] < -20:  # v1 printer_controller.py:151
        raise Rejected(f"gantry at x={pos['x_mm']}, z={pos['z_mm']}: not at the apply start (v1 check)")
    arc = pt["arc"]
    dev.set_relative(True)
    try:
        ctx.check()
        dev.line(x=float(pt["approach_dx"]), feed=float(pt["approach_feed"]))
        dev.finish()
        ctx.check()
        dev.arc_relative(end_x=arc["end_x"], end_z=arc["end_z"], centre_x=arc["centre_x"], centre_z=arc["centre_z"],
                         feed=float(arc["feed"]), clockwise=bool(arc["clockwise"]))
        dev.finish()
    finally:
        dev.set_relative(False)
    return dev.read()


def peel_tape(ctx: ActionContext, p: dict) -> dict:
    """v1 exfoliate_tape: reverse arc at the peel speed, then retreat in X."""
    dev, pt = _gantry_preset(ctx, "peel_tape")
    speed = float(p["speed"])
    lo, hi = 50.0, 500.0  # v1 exfoliation_setpoints.py:14-15
    if not lo <= speed <= hi:
        raise Rejected(f"peel speed {speed} outside [{lo}, {hi}] (v1)")
    arc = pt["arc"]
    dev.set_relative(True)
    try:
        ctx.check()
        dev.arc_relative(end_x=arc["end_x"], end_z=arc["end_z"], centre_x=arc["centre_x"], centre_z=arc["centre_z"],
                         feed=speed, clockwise=bool(arc["clockwise"]))
        dev.finish()
        ctx.check()
        dev.line(x=float(pt["retreat_dx"]), feed=float(pt["retreat_feed"]))
        dev.finish()
    finally:
        dev.set_relative(False)
    return dev.read()


def advance_tape(ctx: ActionContext, p: dict) -> dict:
    """v1 release_new_tape: reel 1 collects (CCW) while reel 2 is unpowered, then reel 2 powered again."""
    dev = ctx.device("tape_reels", ("tape_reels",))
    deg = float(p["deg"])
    if ctx.gate.real and ctx.gate.config.limits.get("tape", {}).get("source") != "measured":
        cap = float(ctx.gate.probe.get("reel_jog_max_deg", 10))
        if deg > cap:
            raise Rejected(f"reel angle {deg} deg above the probe cap {cap} deg until the reels are measured")
    dev.power(1, True)
    dev.power(2, False)
    dev.rotate(1, False, deg)
    dev.power(2, True)
    return dev.snapshot()


def gantry_power(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device("tape_gantry", ("gantry",))
    dev.power(bool(p["on"]))
    return dev.snapshot()


def _register_l4(module: Module) -> None:
    from ..ops import exfoliation as ox

    places = (module.config.places or {}).get("places", {})
    left = sorted(k for k, v in places.items() if v.get("arm_left"))
    chips = [k for k in left if places[k].get("holds") == "chip"]
    carriers = [k for k in left if places[k].get("holds") == "carrier"]
    place = {"type": "string", "enum": left}
    temp = {"type": "number", "minimum": 20, "maximum": 90}
    exf = {"target_c": dict(temp, default=22.0), "hold_v": {"type": "number"},
           "dwell_s": {"type": "number", "minimum": 10, "maximum": 90, "default": 10.0},
           "peel_speed": {"type": "number", "minimum": 50, "maximum": 500, "default": 500.0},
           "rounds": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
           "heat_timeout_s": {"type": "number", "minimum": 60, "maximum": 1800, "default": 900.0}}
    L4 = "L4 (v1 molecule-level sequence; real mode needs measured points). "
    module.add(Action("load_chip", ox.load_chip, {"place": dict(place, enum=chips), "angle_deg": {"type": "number"},
                                                  "sample_id": {"type": "string"}}, ("place",), motion=True,
                      description=L4 + "Chip from a board place onto the exfoliation stage."))
    module.add(Action("load_carrier", ox.load_carrier, {"place": dict(place, enum=carriers),
                                                        "angle_deg": {"type": "number"},
                                                        "sample_id": {"type": "string"}}, ("place",), motion=True,
                      description=L4 + "Crystal carrier (glass slide) onto the exfoliation stage."))
    module.add(Action("unload_chip", ox.unload_chip, {"place": dict(place, enum=chips), "sample_id": {"type": "string"}},
                      ("place",), motion=True, description=L4 + "Chip from the exfoliation stage to a board place."))
    module.add(Action("unload_carrier", ox.unload_carrier, {"place": dict(place, enum=carriers),
                                                            "sample_id": {"type": "string"}},
                      ("place",), motion=True, description=L4 + "Carrier from the stage back to its place."))
    module.add(Action("massage", ox.massage, {"rounds": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1}},
                      motion=True, description=L4 + "Press the tape onto the stage (v1 massage strokes)."))
    module.add(Action("exfoliate_on_stage", ox.exfoliate_on_stage, exf, motion=True,
                      description=L4 + "Tape on, massage, heat cycle if below target, cool below 60 C, peel."))
    module.add(Action("prepare_mother_tape", ox.prepare_mother_tape,
                      {"carrier_place": dict(place, enum=carriers), "rounds": exf["rounds"],
                       "peel_speed": exf["peel_speed"]}, ("carrier_place",), motion=True,
                      description=L4 + "v1 mother-tape molecule: crystal from a carrier onto the tape."))
    module.add(Action("exfoliate_chip", ox.exfoliate_chip,
                      dict(exf, chip_place=dict(place, enum=chips), store_place=dict(place, enum=chips),
                           angle_deg={"type": "number"}, material={"type": "string"},
                           tape_release_deg={"type": "number", "minimum": 1, "maximum": 3600}),
                      ("chip_place",), motion=True,
                      description=L4 + "v1 chip-exfoliation molecule: chip on stage, exfoliate, store, new tape."))
    module.add(Action("clear_exfoliation_stage", ox.clear_stage, motion=True,
                      description=L4 + "Move what the ledger has on the stage to free places, park."))


def build_exfoliation(module: Module) -> Module:
    register_common(module)
    if {"arm_left", "tape_gantry", "tape_reels", "exfoliation_heater"} <= set(module.devices):
        _register_l4(module)
    from ..flows import register_exfoliation

    register_exfoliation(module)
    if "tape_gantry" in module.devices:
        module.add(Action("apply_tape", apply_tape, motion=True,
                          description="Lay the tape with the v1 arc (geometry in points.yaml; real mode needs measured)."))
        module.add(Action("peel_tape", peel_tape, {"speed": {"type": "number", "minimum": 50, "maximum": 500}},
                          ("speed",), motion=True, description="Peel with the reverse v1 arc at speed (mm/min)."))
        module.add(Action("gantry_power", gantry_power, {"on": {"type": "boolean"}}, ("on",),
                          description="Gantry steppers on (M17 X Y Z) or off (M18 X Y Z)."))
    if "tape_reels" in module.devices:
        module.add(Action("advance_tape", advance_tape, {"deg": {"type": "number", "minimum": 0.1, "maximum": 3600}},
                          ("deg",), motion=True, description="Feed new tape (v1 release_new_tape sequence)."))
    return module
