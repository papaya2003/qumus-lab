"""Level-1/2 actions shared by both modules (contract v1 action names).

Every action: gate check first, then generic driver operations, with a cancel check between steps.
"""

from __future__ import annotations

import math

from ..drivers.base import DriverError
from ..safety.gate import Rejected
from .base import Action, ActionContext, Module

MOVERS = ("arm", "stage", "manipulator", "gantry", "focus")


# ------------------------------------------------------------------------- reads
def read_all(ctx: ActionContext, p: dict) -> dict:
    out, errors = {}, {}
    for dev_id, dev in ctx.module.devices.items():
        if not dev.connected:
            errors[dev_id] = dev.last_error or "not connected"
            continue
        try:
            out[dev_id] = dev.read()
        except DriverError as exc:
            dev.last_error = str(exc)
            errors[dev_id] = str(exc)
    return {"devices": out, "errors": errors}


def read_one(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"])
    return {p["device"]: dev.read()}


def home_query(ctx: ActionContext, p: dict) -> dict:
    """Query only: which devices know their position. Never moves or homes anything."""
    out = {}
    for dev_id, dev in ctx.module.devices.items():
        if dev.kind not in MOVERS + ("turret",):
            continue
        entry = {"connected": dev.connected}
        if dev.connected:
            try:
                entry["position"] = dev.read()
            except DriverError as exc:
                entry["error"] = str(exc)
        if dev.kind == "arm":
            entry["position_trusted"] = getattr(dev, "pose_trusted", False)
            entry["note"] = "v1 never homed the arms (no G28); trust depends on the controller keeping its origin"
        elif dev.kind == "focus":
            entry["position_trusted"] = False
            entry["note"] = "software estimate only (v1 had no read-back)"
        elif dev.kind == "turret":
            entry["position_trusted"] = getattr(dev, "position", None) is not None
            entry["note"] = "no position query in v1: last commanded position"
        else:
            entry["position_trusted"] = dev.connected
        out[dev_id] = entry
    return out


def capture(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["camera"], ("camera",))
    return dev.capture()


# ------------------------------------------------------------------------- motion
def _split_point(module: Module, qualified: str) -> tuple[str, str]:
    if "." not in qualified:
        raise Rejected(f"point must be '<device>.<point>', got {qualified!r}")
    dev_id, name = qualified.split(".", 1)
    if dev_id not in module.devices:
        raise Rejected(f"point {qualified!r}: device {dev_id!r} is not in module {module.name}")
    return dev_id, name


def go_to_point(ctx: ActionContext, p: dict) -> dict:
    dev_id, _ = _split_point(ctx.module, p["point"])
    zones = ctx.module.zones
    if zones is not None:
        zones.check_enter(dev_id, p["point"])
    # Every refusal (device offline, point not measured, z below min_z, range) happens here, before the zone
    # registry counts the arm as inside: a refused move used to leave the arm marked inside the middle board.
    run = _prepare_go_to_point(ctx, p)
    if zones is None:
        return run()
    zones.move_started(dev_id, p["point"])
    ok = False
    try:
        out = run()
        ok = True
        return out
    finally:
        zones.move_finished(dev_id, p["point"], ok)


def _prepare_go_to_point(ctx: ActionContext, p: dict):
    """Check everything for a go_to_point and return the function that moves (no device command yet)."""
    dev_id, name = _split_point(ctx.module, p["point"])
    pt = ctx.gate.check_point(dev_id, name)
    dev = ctx.device(dev_id)
    if dev.kind == "arm":
        x, y, z = (float(v) for v in pt["xyz"])
        feed = ctx.gate.arm_feed(dev_id)
        ctx.gate.check_arm_z(dev_id, z)
        az = float(pt["approach_z"]) if pt.get("approach_z") is not None else None
        if az is not None:
            ctx.gate.check_arm_z(dev_id, az)

        def run_arm():
            if az is not None:
                ctx.check()
                dev.move_to(x, y, az, feed)
            ctx.check()
            return dev.move_to(x, y, z, feed)
        return run_arm
    if dev.kind in ("stage", "manipulator"):
        pos = pt["pos"]
        order = pt.get("order") or list(pos)
        for axis in order:
            ctx.gate.check_axis_target(dev_id, axis, float(pos[axis]), for_point=True)

        def run_axes():
            for axis in order:
                ctx.check()
                dev.move_to(axis, float(pos[axis]))
            return dev.snapshot()
        return run_axes
    raise Rejected(f"{dev_id} has no go_to_point (kind {dev.kind})")



def jog(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"], MOVERS)
    axis, mm = p["axis"], float(p["mm"])
    amount = mm * 1000.0 if dev.kind == "focus" else mm
    ctx.gate.check_jog(dev.kind, dev.id, axis, amount)
    feed = float(ctx.gate.probe.get("jog_feed_mm_min", 300))
    if dev.kind == "arm":
        if axis not in ("x", "y", "z"):
            raise Rejected("arm axis must be x, y or z")
        pose = dev.read()
        cur = pose[f"{axis}_mm"]
        if cur is None:
            raise Rejected("arm position unknown; cannot jog")
        tgt = {"x": None, "y": None, "z": None}
        tgt[axis] = cur + mm
        if axis == "z":
            ctx.gate.check_arm_z(dev.id, tgt["z"])
        return dev.move_to(tgt["x"], tgt["y"], tgt["z"], feed)
    if dev.kind == "stage":
        cur = dev.read_axis_mm(axis)
        ctx.gate.check_axis_target(dev.id, axis, cur + mm, for_point=False)
        dev.move_by(axis, mm)
        return dev.snapshot()
    if dev.kind == "manipulator":
        dev.read()
        cur = dev.position(axis)
        if cur is None:
            raise Rejected(f"{dev.id}.{axis} position unknown")
        ctx.gate.check_axis_target(dev.id, axis, cur + mm, for_point=False)
        dev.move_to(axis, cur + mm)
        return dev.snapshot()
    if dev.kind == "gantry":
        if axis not in ("x", "z"):
            raise Rejected("gantry axis must be x or z")
        dev.move(**{axis: mm}, feed=feed, relative=True)
        return dev.read()
    if dev.kind == "focus":
        dev.move_by_um(amount)
        return dev.snapshot()
    raise Rejected(f"cannot jog {dev.id}")


AXES = {"arm": ("x", "y", "z"), "gantry": ("x", "z"), "focus": ("z",)}


def _current(dev, axis: str) -> float:
    """Read the device and return the axis position (mm; focus: um estimate). Unknown -> Rejected."""
    if dev.kind == "stage":
        return float(dev.read_axis_mm(axis))
    if dev.kind == "manipulator":
        dev.read()
        cur = dev.position(axis)
    else:
        allowed = AXES.get(dev.kind, ())
        if axis not in allowed:
            raise Rejected(f"{dev.id} axis must be one of {', '.join(allowed)}")
        key = "z_um" if dev.kind == "focus" else f"{axis}_mm"
        cur = dev.read().get(key)
    if cur is None:
        raise Rejected(f"{dev.id}.{axis} position unknown; cannot move to a target")
    return float(cur)


def move_axis(ctx: ActionContext, p: dict) -> dict:
    """Move one axis to a typed target (debug page). Same feeds as jog; distance capped by the gate."""
    dev = ctx.device(p["device"], MOVERS)
    axis, target = p["axis"], float(p["target"])
    cur = _current(dev, axis)
    ctx.gate.check_target_move(dev.kind, dev.id, axis, cur, target)
    ctx.check()
    feed = float(ctx.gate.probe.get("jog_feed_mm_min", 300))
    if dev.kind == "arm":
        if axis == "z":
            ctx.gate.check_arm_z(dev.id, target)
        tgt = {"x": None, "y": None, "z": None}
        tgt[axis] = target
        return dev.move_to(tgt["x"], tgt["y"], tgt["z"], feed)
    if dev.kind in ("stage", "manipulator"):
        ctx.gate.check_axis_target(dev.id, axis, target, for_point=False)
        dev.move_to(axis, target)
        return dev.snapshot()
    if dev.kind == "gantry":
        dev.move(**{axis: target}, feed=feed, relative=False)
        return dev.read()
    if dev.kind == "focus":
        dev.move_by_um(target - cur)
        return dev.snapshot()
    raise Rejected(f"cannot move {dev.id}")


def arm_suction(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"], ("arm",))
    dev.set_vacuum(bool(p["on"]))
    return dev.snapshot()


def rotate_nozzle(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"], ("arm",))
    deg = float(p["deg"])
    if not -180 <= deg <= 180:
        raise Rejected("deg must be in [-180, 180]")
    dev.rotate_nozzle(deg)
    return dev.snapshot()


# ------------------------------------------------------------------------- switches & heat
def set_switch(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"], ("switch",))
    on = bool(p["on"])
    auto_off = ctx.gate.switch_auto_off(True, p.get("auto_off_s")) if on else None
    ctx.module.cancel_auto_off(dev.id)
    if on and auto_off:  # armed before 'on' is sent, so it still fires when the relay's reply is lost
        ctx.module.schedule_auto_off(dev, auto_off, ctx.log)
    dev.set(on)
    return dict(dev.snapshot(), auto_off_s=auto_off)


def heat(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"], ("heater",))
    plan = ctx.gate.check_heat(dev, dev.id, float(p["target_c"]), float(p["timeout_s"]), p.get("volts"))
    return dict(dev.heat(float(p["target_c"]), float(p["timeout_s"]), mode=plan["mode"], volts=plan["volts"]),
                mode=plan["mode"])


def heat_off(ctx: ActionContext, p: dict) -> dict:
    dev = ctx.device(p["device"], ("heater",))
    return dev.off("heat_off command")


# ------------------------------------------------------------------------- prechecks (no I/O)
def _dev(m: Module, dev_id: str):
    dev = m.devices.get(dev_id)
    if dev is None:
        raise Rejected(f"unknown device {dev_id!r}")
    return dev


def pre_go_to_point(m: Module, p: dict) -> None:
    dev_id, name = _split_point(m, p["point"])
    pt = m.gate.check_point(dev_id, name)
    if "pos" in pt:
        for axis, val in pt["pos"].items():
            m.gate.check_axis_target(dev_id, axis, float(val), for_point=True)
    if m.zones is not None:
        m.zones.check_enter(dev_id, p["point"])


# ------------------------------------------------------------------------- zones (middle board)
def zone_acquire(ctx: ActionContext, p: dict) -> dict:
    return {p["zone"]: ctx.module.zones.acquire(p["zone"], p["device"])}


def zone_release(ctx: ActionContext, p: dict) -> dict:
    return {p["zone"]: ctx.module.zones.release(p["zone"], p["device"])}


def zone_clear(ctx: ActionContext, p: dict) -> dict:
    return {p["zone"]: ctx.module.zones.clear(p["zone"], ctx.gate_ctx.client)}


def pre_zone_acquire(m: Module, p: dict) -> None:
    m.zones.check_acquire(p["zone"], p["device"])


def pre_zone_release(m: Module, p: dict) -> None:
    m.zones.check_release(p["zone"], p["device"])


def pre_jog(m: Module, p: dict) -> None:
    dev = _dev(m, p["device"])
    amount = float(p["mm"]) * (1000.0 if dev.kind == "focus" else 1.0)
    m.gate.check_jog(dev.kind, dev.id, p["axis"], amount)


def pre_move_axis(m: Module, p: dict) -> None:
    dev = _dev(m, p["device"])
    if dev.kind not in MOVERS:
        raise Rejected(f"{dev.id} ({dev.kind}) cannot be moved")
    if not math.isfinite(float(p["target"])):
        raise Rejected("target must be a finite number")


def pre_set_switch(m: Module, p: dict) -> None:
    if p.get("on"):
        m.gate.switch_auto_off(True, p.get("auto_off_s"))


def pre_heat(m: Module, p: dict) -> None:
    dev = _dev(m, p["device"])
    m.gate.check_heat(dev, dev.id, float(p["target_c"]), float(p["timeout_s"]), p.get("volts"))
    psu = getattr(dev, "psu", None)
    if psu is not None and psu.owner not in (None, dev.id):
        raise Rejected(f"power supply is driving {psu.owner}; turn it off first", 409)


# ------------------------------------------------------------------------- registration
def register_common(m: Module) -> None:
    devs = sorted(m.devices)
    points = [f"{d}.{n}" for d in devs for n in m.points_for(d)]
    of = lambda kinds: sorted(m.device_ids(kinds))  # noqa: E731
    m.add(Action("read_all", read_all, any_client=True, concurrent=True,
                 description="Query every device of this module once (no motion)."))
    m.add(Action("read", read_one, {"device": {"type": "string", "enum": devs}}, ("device",),
                 any_client=True, concurrent=True, description="Query one device."))
    m.add(Action("home_query", home_query, any_client=True, concurrent=True,
                 description="Report which devices know their position. Query only: never homes or moves."))
    m.add(Action("go_to_point", go_to_point, {"point": {"type": "string", "enum": points}}, ("point",), motion=True, precheck=pre_go_to_point,
                 description="Move a device to a named point from config/points.yaml (real mode: measured points only)."))
    m.add(Action("jog", jog, {"device": {"type": "string", "enum": of(MOVERS)},
                              "axis": {"type": "string"}, "mm": {"type": "number"}},
                 ("device", "axis", "mm"), motion=True, local_only=True, precheck=pre_jog,
                 description="Small relative move (caps: arm/stage/gantry 0.5 mm, stamp 0.1 mm, focus 0.05 mm)."))
    m.add(Action("move_axis", move_axis, {"device": {"type": "string", "enum": of(MOVERS)},
                                          "axis": {"type": "string"}, "target": {"type": "number"}},
                 ("device", "axis", "target"), motion=True, local_only=True, precheck=pre_move_axis,
                 description="Move one axis to a typed position (mm; focus: um). At most 10 mm from where it is "
                             "(stamp x/y 1 mm, focus 500 um); stamp z and arm z down keep the jog cap."))
    if of(("arm",)):
        m.add(Action("arm_suction", arm_suction, {"device": {"type": "string", "enum": of(("arm",))},
                                                  "on": {"type": "boolean"}}, ("device", "on"),
                     description="Arm nozzle suction on/off (v1 M888 P2 + M1000/M1003)."))
        m.add(Action("rotate_nozzle", rotate_nozzle, {"device": {"type": "string", "enum": of(("arm",))},
                                                      "deg": {"type": "number", "minimum": -180, "maximum": 180}},
                     ("device", "deg"), motion=True, description="Rotate the arm nozzle (v1 M2101 R<deg>)."))
    if of(("switch",)):
        m.add(Action("set_switch", set_switch, {"device": {"type": "string", "enum": of(("switch",))},
                                                "on": {"type": "boolean"},
                                                "auto_off_s": {"type": "number", "minimum": 0}},
                     ("device", "on"), local_only=True, precheck=pre_set_switch,
                     description="Relay on/off; 'on' turns itself off after auto_off_s (<= 10 s)."))
    if of(("heater",)):
        m.add(Action("heat", heat, {"device": {"type": "string", "enum": of(("heater",))},
                                    "target_c": {"type": "number"}, "timeout_s": {"type": "number"},
                                    "volts": {"type": "number"}},
                     ("device", "target_c", "timeout_s"), precheck=pre_heat,
                     description="Heat with a mandatory timeout. Unmeasured heaters in real mode: <= 40 C; per-heater voltage caps."))
        m.add(Action("heat_off", heat_off, {"device": {"type": "string", "enum": of(("heater",))}}, ("device",),
                     any_client=True, concurrent=True, allowed_when_locked=True,
                     description="Heater output and relay off. Any client, also while the module is locked."))
    zone_names = m.zone_names()
    if zone_names:
        zone_devs = sorted(d for d in m.devices if any(d in m.zones.zones[z]["devices"] for z in zone_names))
        zp = {"zone": {"type": "string", "enum": zone_names}, "device": {"type": "string", "enum": zone_devs}}
        m.add(Action("zone_acquire", zone_acquire, zp, ("zone", "device"), concurrent=True, precheck=pre_zone_acquire,
                     description="Take a shared zone (middle board) for an arm; 409 if the other arm holds it. "
                                 "go_to_point into the zone needs it."))
        m.add(Action("zone_release", zone_release, zp, ("zone", "device"), concurrent=True, precheck=pre_zone_release,
                     description="Give the zone back; refused while the arm is (or may be) inside. A move to a "
                                 "point outside releases it automatically."))
        m.add(Action("zone_clear", zone_clear, {"zone": zp["zone"]}, ("zone",), concurrent=True, local_only=True,
                     any_client=True,
                     description="A person checked the bench: no arm is in the zone. Clears holder and inside."))
    if of(("camera",)):
        m.add(Action("capture", capture, {"camera": {"type": "string", "enum": of(("camera",))}}, ("camera",),
                     any_client=True, concurrent=True, description="Take one picture; returns its image_id."))


__all__ = ["register_common", "MOVERS"]
