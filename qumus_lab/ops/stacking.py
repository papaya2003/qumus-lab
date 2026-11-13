"""Stacking L4 operations (right arm, sample stage, microscope, stamp holder, transfer heater).

Sources (v1, read-only copy): common/hardware/robot_arm_controller.py (right-arm sequences) and
agents/processing_agent/workflows/stacking/molecule_flows/*.py (stamp, touch, pick-up, drop-down). The
numbers below carry their v1 file:line. Two Qumus additions, both safety-only:
- a stamp z floor (limits.yaml stacking.stamp_z_floor_mm, = v1's lowest commanded z 4.9): the touch
  search and the pick-up approach stop with a failed step instead of pressing further (anti-crash cap);
- ``check_pickup``: after the stamp has left, the camera looks for the flake at the pick-up spot (v1
  only archived photos).

Search and flake centring are not v1 code: v1 searched with an ML observer that is not in this repo, so
they use the placeholder detector in qumus_lab.vision (replaceable, docs/vision.md).
"""

from __future__ import annotations

import math

from .. import vision
from ..jobs.runner import StepFailed
from ..modules.base import ActionContext
from ..safety.gate import Rejected
from . import common as c

ARM = "arm_right"
STAGE = "sample_stage"
STAGE_VACUUM = "stage_vacuum"
STAMP = "stamp"
STAMP_VACUUM = "stamp_vacuum"
HEATER = "transfer_heater"
CAMERA = "microscope_camera"
TURRET = "turret"
FOCUS = "focus"
STATION = "transfer_stage"
HOLDER = "stamp_holder"

# right arm (robot_arm_controller.py:225-240, 315-345)
# v1 picks a chip from a board slot 1 mm above the height it places it at (:230 "G0 Z15", :342 "G0 Z14").
# The pick height is the slot point's z + this offset, so a measured slot z moves the pick with it (it used to
# be a fixed Z15, which ignored the measured z).
RIGHT_PICK_ABOVE_SLOT_MM = 1.0
SLOT_SAFE_Z = 30.0          # :229; used only when a slot point has no approach_z
STAMP_NOZZLE_DEG = 50.0     # :275 rotate_nose(angle_deg=50)
# v1 pixel targets on the 3840 x 2160 frame
ADJUST_TARGET_PT = (1920, 1080)   # molecule_adjust_touching_point.py:94
TARGET_TP_DIST = 300              # :46
PICKUP_TARGET_PT = (1900, 1100)   # molecule_pick_up.py:150
SAFE_DIST = 240                   # molecule_pick_up.py:72
DELTA_Z = 0.2                     # adjust :45, pick-up :70
SAFE_Z_OFFSET = 0.2               # pick-up :71
FOCUS_WAIT_S = 5.0                # adjust :47
TOUCH_STEP_MM = -0.01             # touching_detection.py:9 STEP
APPROACH_STEP_MM = -0.1           # touching_detection.py:11 APPROACHING_STEP
# initialize / remove stamp
FOCUS_DELTA_UM = 25000.0          # molecule_initialize_stamp.py:32
FOCUS_SETTLE_S = 90.0             # :33
# heater (molecule_set_initial_temp.py:22, molecule_drop_down.py:58-60, :90)
INITIAL_TEMP_C = 90.0
DROP_DOWN_TEMP_C = 155.0
MELT_TEMP_C = 170.0
DROP_DOWN_Z = 4.9
MELT_LIFT_DELTA_Z = 5.0
TEMP_CHECK_PERIOD_S = 10.0
HEAT_TIMEOUT_S = 7200.0           # v1 left the transfer heater on for the whole stacking run
# image -> stage: a feature at pixel p is brought to pixel T by moving the stage IMAGE_SIGN * (p - T) * mm/px.
# Matches the --demo renderer; CHECK AT L3 (jog the stage 0.5 mm and watch which way the picture moves).
IMAGE_SIGN = (1.0, 1.0)


# ------------------------------------------------------------------------------------------- devices
def _stamp(ctx):
    return ctx.device(STAMP, ("manipulator",))


def _stage(ctx):
    return ctx.device(STAGE, ("stage",))


def _camera(ctx):
    return ctx.device(CAMERA, ("camera",))


def _speed(ctx, axis: str, vel: float, accel: float) -> None:
    """v1 set_kst_speed(axis, max_vel, accel)."""
    _stamp(ctx).set_velocity(axis, vel, accel)


def _stamp_pos(ctx, axis: str) -> float:
    dev = _stamp(ctx)
    dev.read()
    v = dev.position(axis)
    if v is None:
        raise Rejected(f"stamp {axis} position unknown")
    return float(v)


def _stamp_to(ctx, axis: str, mm: float) -> float:
    ctx.check()
    if axis == "z":
        floor = z_floor(ctx)
        if mm < floor - 1e-9:
            raise StepFailed(f"stamp z {mm:.4f} below the floor {floor} mm (anti-crash cap)")
    ctx.gate.check_axis_target(STAMP, axis, float(mm), for_point=False)
    return _stamp(ctx).move_to(axis, float(mm))


def _stamp_by(ctx, axis: str, delta: float) -> float:
    return _stamp_to(ctx, axis, _stamp_pos(ctx, axis) + float(delta))


def z_floor(ctx) -> float:
    return float(ctx.gate.limits.get("stacking", {}).get("stamp_z_floor_mm", DROP_DOWN_Z))


def _device_point(ctx, dev_id: str, name: str) -> dict:
    """Go to a stage / stamp point in its v1 axis order (same checks as go_to_point)."""
    pt = ctx.gate.check_point(dev_id, name)
    pos = pt["pos"]
    order = pt.get("order") or list(pos)
    for axis in order:
        ctx.gate.check_axis_target(dev_id, axis, float(pos[axis]), for_point=True)
    dev = ctx.device(dev_id)
    for axis in order:
        ctx.check()
        dev.move_to(axis, float(pos[axis]))
    return dev.snapshot()


def _stage_xy(ctx, x: float, y: float) -> tuple[float, float]:
    """Computed stage move (search tiles, centring). Real mode needs measured x/y ranges in limits.yaml."""
    for axis, v in (("x", x), ("y", y)):
        lo, hi, src = ctx.gate.axis_range(STAGE, axis)
        if ctx.gate.real and src != "measured":
            raise Rejected(f"sample_stage.{axis} has no measured range; computed stage moves are refused", 409)
        ctx.gate.check_axis_target(STAGE, axis, float(v), for_point=False)
    dev = _stage(ctx)
    ctx.check()
    dev.move_to("x", float(x))
    ctx.check()
    dev.move_to("y", float(y))
    return float(dev.read_axis_mm("x")), float(dev.read_axis_mm("y"))


def _stage_now(ctx) -> tuple[float, float]:
    dev = _stage(ctx)
    return float(dev.read_axis_mm("x")), float(dev.read_axis_mm("y"))


def microscope_xy(ctx) -> tuple[float, float]:
    pos = ctx.gate.point(STAGE, "microscope")["pos"]
    return float(pos["x"]), float(pos["y"])


def set_objective(ctx, mag: int) -> None:
    dev = ctx.device(TURRET, ("turret",))
    if mag not in dev.positions:
        raise Rejected(f"objective {mag}x not in {sorted(dev.positions)}")
    ctx.check()
    dev.rotate_to(dev.positions[mag])


def autofocus(ctx, wait_s: float = FOCUS_WAIT_S) -> None:
    """v1 cam_autofocus_once + FOCUS_WAIT_S."""
    _camera(ctx).autofocus_once()
    c.sleep(ctx, wait_s)


def snap(ctx, role: str):
    """Capture one microscope frame; returns (image array, capture info). Jobs list it in the run."""
    ctx.check()
    cam = _camera(ctx)
    info = dict(cam.capture(), camera=CAMERA, role=role)
    img = vision.load(cam.path_of(info["image_id"]))
    if hasattr(ctx, "image") and getattr(ctx, "run_id", ""):
        ctx.image(info, role)
    return img, info


def _stage_delta(dx_px: float, dy_px: float, mag: int, width: int) -> tuple[float, float]:
    mx, my = vision.px_to_mm(dx_px, dy_px, mag, width)
    return IMAGE_SIGN[0] * mx, IMAGE_SIGN[1] * my


# ------------------------------------------------------------------------------------------- ledger
def _store(ctx):
    return c._store(ctx)


def _on(ctx, place: str) -> dict | None:
    return c.sample_at(ctx, place)


def _stamp_sample(ctx) -> dict | None:
    return _on(ctx, HOLDER)


# ------------------------------------------------------------------------------------------- chips
def _slot_safe_z(pt: dict) -> float:
    """Travel height over a board slot: the slot point's approach_z (v1 Z30), else SLOT_SAFE_Z."""
    az = pt.get("approach_z")
    return float(az) if az is not None else SLOT_SAFE_Z


def place_chip_on_transfer_stage(ctx: ActionContext, p: dict) -> dict:
    """v1 place_silicon_chip_on_transfer_stage (robot_arm_controller.py:225-240) with the stage at load
    (stage_atoms.py:134) and the stage vacuum on, then the stage to the microscope (stage_atoms.py:151)."""
    place = p["place"]
    if _on(ctx, STATION) is not None:
        raise Rejected(f"the ledger already has {_on(ctx, STATION)['id']} on the transfer stage")
    name, pt = c.place_point(ctx, place, ARM)
    safe_z = _slot_safe_z(pt)
    _device_point(ctx, STAGE, "load")
    measured = c.slot_angle(pt)
    c.move_point(ctx, ARM, name, z=safe_z)                           # G0 X Y Z30 F5000
    if measured is not None:  # the slot angle was measured with the chip picked at nozzle 0
        c.rotate(ctx, ARM, 0.0)
    c.move_point(ctx, ARM, name, z=float(pt["xyz"][2]) + RIGHT_PICK_ABOVE_SLOT_MM)  # G0 Z15 F5000
    c.pick_item(ctx, ARM)
    c.move_point(ctx, ARM, name, z=safe_z)                           # G0 Z30 F5000
    c.park(ctx, ARM)                                                 # home_to_search_stage
    if p.get("angle_deg") is not None:
        angle = float(p["angle_deg"])
    else:
        angle = 0.0 if measured is None else measured
    c.rotate(ctx, ARM, angle)
    c.move_point(ctx, ARM, "stage_chip")                             # G0 X{sx-10} Y{sy} Z132 F5000 (v1 place z)
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, "stage_chip", z=float(ctx.gate.point(ARM, "stage_chip")["approach_z"]),
                 feed=c.SLOW_F)                                       # G0 Z140 F1000
    c.park(ctx, ARM)
    ctx.device(STAGE_VACUUM, ("switch",)).set(True)
    _device_point(ctx, STAGE, "microscope")
    sid = c.move_sample(ctx, place, STATION, sample_id=p.get("sample_id"))
    return {"chip": sid, "from": place, "to": STATION}


def store_chip(ctx: ActionContext, p: dict) -> dict:
    """v1 store_search_silicon_chip (robot_arm_controller.py:315-345), stage at load, stage vacuum off."""
    place = p["place"]
    name, pt = c.place_point(ctx, place, ARM)
    x, y = float(pt["xyz"][0]), float(pt["xyz"][1])
    spt = ctx.gate.check_point(ARM, "stage_chip")
    measured = c.slot_angle(pt)
    # measured slot angle: pick on the stage at that angle, put down in the slot at 0 (reverse of placing)
    angle = 0.0 if measured is not None else c.store_rotation(x + 5, y - 10, float(spt["xyz"][0]), float(spt["xyz"][1]))  # :319-330
    _device_point(ctx, STAGE, "load")
    ctx.device(STAGE_VACUUM, ("switch",)).set(False)
    c.park(ctx, ARM)
    c.move_point(ctx, ARM, "stage_chip", z=float(spt["approach_z"]))         # G0 X{sx-10} Y{sy} Z140 F5000
    if measured is not None:
        c.rotate(ctx, ARM, measured)
    c.move_point(ctx, ARM, "stage_chip", feed=c.SLOW_F)                      # G0 Z130 F1000
    c.pick_item(ctx, ARM)
    c.park(ctx, ARM)
    safe_z = _slot_safe_z(ctx.gate.check_point(ARM, name))
    c.move_point(ctx, ARM, name, z=safe_z)                                   # G0 X Y Z30 F5000
    c.rotate(ctx, ARM, -angle)
    c.move_point(ctx, ARM, name)                                             # G0 Z14 F5000
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, name, z=safe_z)
    c.park(ctx, ARM)
    sid = c.move_sample(ctx, STATION, place, sample_id=p.get("sample_id"))
    return {"chip": sid, "from": STATION, "to": place, "nozzle_deg": round(-angle, 3)}


# ------------------------------------------------------------------------------------------- stamp
def load_stamp(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_load_stamp: holder to load (L 0, x 6, y 7, z 6.5), stamp vacuum on, then the arm
    sequence place_stamp_on_transfer_arm (robot_arm_controller.py:266-280) from the stamp's board place."""
    place = p["place"]
    if _stamp_sample(ctx) is not None:
        raise Rejected(f"the ledger already has stamp {_stamp_sample(ctx)['id']} on the holder")
    name, pt = c.place_point(ctx, place, ARM)
    _device_point(ctx, STAMP, "load")
    ctx.device(STAMP_VACUUM, ("switch",)).set(True)
    measured = c.slot_angle(pt, "holder_nozzle_deg")
    c.move_point(ctx, ARM, name, z=float(pt.get("approach_z", 150)))         # G0 X254 Y190 Z150 F5000
    if measured is not None:  # the holder angle was measured with the stamp picked at nozzle 0
        c.rotate(ctx, ARM, 0.0)
    c.move_point(ctx, ARM, name)                                             # G0 Z16 F5000
    c.pick_item(ctx, ARM)
    c.move_point(ctx, ARM, name, z=float(pt.get("approach_z", 150)))         # G0 Z150 F5000
    c.move_point(ctx, ARM, "stamp_via")                                      # G0 X200 Y275 Z150 F5000
    c.rotate(ctx, ARM, STAMP_NOZZLE_DEG if measured is None else measured)
    sp = ctx.gate.check_point(ARM, "stamp_holder")
    c.move_point(ctx, ARM, "stamp_holder", z=float(sp["approach_z"]))        # G0 X42 Y275 Z150 F5000
    c.move_point(ctx, ARM, "stamp_holder")                                   # G0 Z134 F5000
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, "stamp_holder", z=float(sp["approach_z"]))        # G0 Z150 F5000
    c.park(ctx, ARM)
    sid = c.move_sample(ctx, place, HOLDER, kind="stamp", sample_id=p.get("sample_id"))
    return {"stamp": sid, "from": place, "to": HOLDER}


def initialize_stamp(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_initialize_stamp: 5x, speeds (x/y 0.2, z 0.1, accel 0.5), L 120, z 5.4, focus +25000 um,
    wait 90 s, autofocus."""
    set_objective(ctx, 5)
    _speed(ctx, "x", 0.2, 0.5)
    _speed(ctx, "y", 0.2, 0.5)
    _speed(ctx, "z", 0.1, 0.5)
    _device_point(ctx, STAMP, "initialize")
    if p.get("focus", True):
        _focus_by(ctx, FOCUS_DELTA_UM)
        c.sleep(ctx, FOCUS_SETTLE_S)
    autofocus(ctx, 3.0)                                       # AUTOFOCUS_WAIT_S = 3.0 (:34)
    return _stamp(ctx).snapshot()


def _focus_by(ctx, um: float) -> None:
    dev = ctx.device(FOCUS, ("focus",))
    lim = ctx.gate.config.limits.get("axes", {}).get("focus", {}).get("z", {})
    if ctx.gate.real and lim.get("source") != "measured":
        raise Rejected("focus moves of 25 mm need a measured focus range (L3)", 409)
    ctx.check()
    dev.move_by_um(float(um))


def remove_stamp(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_remove_stamp: z speed 0.05 (accel 0.5), z 6.5, L 160, focus -25000 um."""
    _speed(ctx, "z", 0.05, 0.5)
    _device_point(ctx, STAMP, "remove")
    if p.get("focus", True):
        _focus_by(ctx, -FOCUS_DELTA_UM)
    return _stamp(ctx).snapshot()


def recycle_stamp(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_recycle_stamp: holder to load, arm picks at (42, 276, 134), holder vacuum off, arm drops
    the stamp at (-250, 250, 15), home. One stamp_holder point serves placing and picking (v1 placed at
    Y275 and picked at Y276)."""
    stamp = _stamp_sample(ctx)
    _device_point(ctx, STAMP, "load")
    rp = ctx.gate.check_point(ARM, "stamp_holder")                            # v1 picked at Y276
    c.move_point(ctx, ARM, "stamp_holder", z=float(rp["approach_z"]))
    c.move_point(ctx, ARM, "stamp_holder")
    c.pick_item(ctx, ARM)
    ctx.device(STAMP_VACUUM, ("switch",)).set(False)
    c.move_point(ctx, ARM, "stamp_holder", z=float(rp["approach_z"]))
    bp = ctx.gate.check_point(ARM, "stamp_bin")
    c.move_point(ctx, ARM, "stamp_bin", z=float(bp["approach_z"]))
    c.move_point(ctx, ARM, "stamp_bin")
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, "stamp_bin", z=float(bp["approach_z"]))
    c.park(ctx, ARM)
    if stamp is not None:
        c.update_sample(ctx, stamp["id"], status="recycled", location="stamp_bin")
    return {"stamp": stamp["id"] if stamp else None, "to": "stamp_bin"}


# ------------------------------------------------------------------------------------------- heater
def _heater(ctx):
    return ctx.device(HEATER, ("heater",))


def _start_heat(ctx, target_c: float, auto_hold: bool = True) -> dict:
    dev = _heater(ctx)
    plan = ctx.gate.check_heat(dev, HEATER, target_c, HEAT_TIMEOUT_S, None)
    if plan["mode"] != "boost_hold":
        raise Rejected(f"{HEATER}: the v1 stacking temperatures need the measured heater (boost/hold), "
                       f"not the first-heat mode")
    return dev.heat(target_c, HEAT_TIMEOUT_S, mode=plan["mode"], volts=plan["volts"], auto_hold=auto_hold)


def _temp(ctx) -> float:
    t = _heater(ctx).read().get("temp_c")
    if t is None:
        raise StepFailed("transfer stage temperature unknown")
    return float(t)


def set_initial_temp(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_set_initial_temp: transfer relay on (exfoliation relay off), 20 V until 90 C, then 10 V."""
    _start_heat(ctx, INITIAL_TEMP_C)
    t = c.wait_until(ctx, lambda: _temp(ctx) >= INITIAL_TEMP_C and _temp(ctx), float(p.get("timeout_s") or 1800),
                     poll_s=TEMP_CHECK_PERIOD_S, what=f"transfer stage >= {INITIAL_TEMP_C} C")
    return {"temp_c": t, "heater": _heater(ctx).snapshot()}


def transfer_heat_off(ctx: ActionContext, p: dict) -> dict:
    return _heater(ctx).off("stacking op: heater off")


# ------------------------------------------------------------------------------------------- touch, pick, drop
def adjust_touching_point(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_adjust_touching_point + detect_first_touching_point."""
    _speed(ctx, "x", 0.2, 0.5)
    _speed(ctx, "y", 0.2, 0.5)
    _speed(ctx, "z", 0.005, 0.1)
    set_objective(ctx, 5)
    c.sleep(ctx, FOCUS_WAIT_S)
    snap(ctx, "initial_view_5x")
    ref, _ = snap(ctx, "touch_ref")
    touch = None
    steps = 0
    while touch is None:
        z = _stamp_pos(ctx, "z")
        if z + TOUCH_STEP_MM < z_floor(ctx) - 1e-9:
            raise StepFailed(f"no touch found down to the stamp z floor {z_floor(ctx)} mm after {steps} steps")
        steps += 1
        _stamp_by(ctx, "z", TOUCH_STEP_MM)                    # arm.jog_axis(axis="z", step=STEP)
        c.sleep(ctx, 1.0)                                      # time.sleep(1)
        cur, info = snap(ctx, f"touch_step_{steps}")
        touch = vision.first_touch(ref, cur)
    width = cur.shape[1]
    z_touch = _stamp_pos(ctx, "z")
    autofocus(ctx)
    _speed(ctx, "z", 0.05, 0.1)
    _stamp_by(ctx, "z", DELTA_Z)
    tx, ty = vision.ref_point(ADJUST_TARGET_PT, width)
    aim = (tx, ty - int(round(TARGET_TP_DIST * vision.scale(width))))
    mm = vision.um_per_px(5, width) / 1000.0
    dx = -(aim[0] - touch[0]) * mm                             # delta_x = -convert_dist_unit(aim - touch)
    dy = -(aim[1] - touch[1]) * mm
    _stamp_by(ctx, "x", dx)
    _stamp_by(ctx, "y", dy)
    return {"touch_px": list(touch), "steps": steps, "z_touch": round(z_touch, 4), "aim_px": list(aim),
            "dx_mm": round(dx, 4), "dy_mm": round(dy, 4), "image_width": width}


def pick_up(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_pick_up + stake_target_flake: lower in 0.1 mm steps at 0.005 mm/s until the pick-up
    target is inside the contact front by more than SAFE_DIST px, then back to safe z at 0.01 mm/s and
    to z 5.5 at 0.1 mm/s."""
    _speed(ctx, "x", 0.2, 0.5)
    _speed(ctx, "z", 0.002, 0.1)
    set_objective(ctx, 5)
    safe_z = _stamp_pos(ctx, "z") + SAFE_Z_OFFSET
    c.sleep(ctx, FOCUS_WAIT_S)
    ref, _ = snap(ctx, "pickup_ref")
    width = ref.shape[1]
    target = vision.ref_point(PICKUP_TARGET_PT, width)
    safe_px = SAFE_DIST * vision.scale(width)
    _speed(ctx, "z", 0.005, 0.5)
    _stamp_by(ctx, "z", -DELTA_Z + 0.1)                        # pre-approach
    steps, dist_px = 0, 0
    while True:
        if _stamp_pos(ctx, "z") + APPROACH_STEP_MM < z_floor(ctx) - 1e-9:
            raise StepFailed(f"target not covered before the stamp z floor {z_floor(ctx)} mm "
                             f"(last inside distance {dist_px} px)")
        steps += 1
        _speed(ctx, "z", 0.005, 0.5)
        _stamp_by(ctx, "z", APPROACH_STEP_MM)
        cur, _ = snap(ctx, f"pickup_step_{steps}")
        contour = vision.contact_contour(ref, cur)
        dist_px = vision.inside_distance(target, contour)
        if dist_px > safe_px:
            autofocus(ctx, 0.0)
            break
    z_low = _stamp_pos(ctx, "z")
    _speed(ctx, "z", 0.01, 0.1)
    _stamp_to(ctx, "z", safe_z)
    _speed(ctx, "z", 0.1, 0.1)
    _stamp_to(ctx, "z", 5.5)
    return {"steps": steps, "inside_px": dist_px, "safe_px": round(safe_px, 1), "z_lowest": round(z_low, 4),
            "safe_z": round(safe_z, 4)}


def drop_down(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_drop_down: 27 V until 155 C, z to 4.9, wait until > 170 C (then 16 V), lift 5 mm at
    0.1 mm/s, heater off."""
    _speed(ctx, "x", 0.2, 0.5)
    _speed(ctx, "y", 0.2, 0.5)
    _speed(ctx, "z", 0.02, 0.1)
    set_objective(ctx, 5)
    autofocus(ctx)
    _start_heat(ctx, MELT_TEMP_C, auto_hold=False)   # 27 V until this op has seen > 170 C (v1)
    try:
        def reached(limit):
            def f():
                autofocus(ctx, 0.0)
                t = _temp(ctx)
                return t >= limit and t
            return f

        t1 = c.wait_until(ctx, reached(DROP_DOWN_TEMP_C), 3600, poll_s=TEMP_CHECK_PERIOD_S,
                          what=f"transfer stage >= {DROP_DOWN_TEMP_C} C")
        _stamp_to(ctx, "z", DROP_DOWN_Z)
        snap(ctx, "drop_contact")
        t2 = c.wait_until(ctx, lambda: _temp(ctx) > MELT_TEMP_C and _temp(ctx), 3600, poll_s=TEMP_CHECK_PERIOD_S,
                          what=f"transfer stage > {MELT_TEMP_C} C")
        _heater(ctx).hold_now()                               # heater_set(voltage=HOLD_VOLTAGE) 16 V
        _speed(ctx, "z", 0.1, 1.0)
        _stamp_by(ctx, "z", MELT_LIFT_DELTA_Z)
    except BaseException as exc:
        # Timeout, stop, a failed stamp move: never leave the heater at the 27 V boost (auto_hold off) for the
        # rest of its 2 h timeout. An off that can not be confirmed raises OutcomeUnknown and locks the module.
        _heater(ctx).off(f"drop_down did not finish: {type(exc).__name__}")
        raise
    _heater(ctx).off("drop_down finished")
    return {"drop_temp_c": t1, "melt_temp_c": t2}


# ------------------------------------------------------------------------------------------- microscope work
def center_on(ctx, stage_xy, *, mag: int = 20, target_v1px=PICKUP_TARGET_PT, find=True,
              material: str | None = None, tries: int = 2, max_err_um: float = 40.0) -> dict:
    """Bring a flake (or a point) to the 5x pick-up target pixel. ``stage_xy`` = stage position that puts it
    on the optical axis. With ``find`` the nearest flake is re-found at ``mag`` and the stage corrected."""
    x0, y0 = float(stage_xy[0]), float(stage_xy[1])
    _stage_xy(ctx, x0, y0)
    set_objective(ctx, mag)
    autofocus(ctx)
    last = None
    if find:
        for i in range(tries):
            img, info = snap(ctx, f"centre_{mag}x_{i + 1}")
            h, w = img.shape[:2]
            flakes = vision.find_flakes(img, objective=mag, material=material, min_area_um2=20.0)
            if not flakes:
                raise StepFailed(f"no flake found at {mag}x near stage {x0:.4f}, {y0:.4f}")
            near = min(flakes, key=lambda f: math.hypot(f.x_px - w / 2, f.y_px - h / 2))
            err_um = math.hypot(near.x_px - w / 2, near.y_px - h / 2) * vision.um_per_px(mag, w)
            if err_um > max_err_um and i == 0:
                raise StepFailed(f"nearest flake is {err_um:.0f} um from the recorded position (> {max_err_um})")
            dx, dy = _stage_delta(near.x_px - w / 2, near.y_px - h / 2, mag, w)
            x0, y0 = _stage_xy(ctx, x0 + dx, y0 + dy)
            last = {"area_um2": round(near.area_px * vision.um_per_px(mag, w) ** 2, 1), "label": near.label,
                    "image_id": info["image_id"], "err_um": round(err_um, 2)}
    # the 5x pick-up target is not the image centre: shift so the flake sits there at 5x
    t = target_v1px
    dx, dy = _stage_delta(vision.REF_WIDTH / 2 - t[0], vision.REF_HEIGHT / 2 - t[1], 5, vision.REF_WIDTH)
    xs, ys = _stage_xy(ctx, x0 + dx, y0 + dy)
    return {"axis_stage_xy": [round(x0, 5), round(y0, 5)], "stage_xy": [round(xs, 5), round(ys, 5)],
            "found": last}


def scan_chip(ctx: ActionContext, p: dict) -> dict:
    """Tile the chip at 10x, detect flakes (placeholder detector), record them as ``flake`` samples
    (id <chip>_fNNN, location = chip) like simlab's search_chip."""
    chip = _on(ctx, STATION)
    if chip is None:
        raise Rejected("the ledger has no chip on the transfer stage")
    mag = int(p.get("objective") or 10)
    material = p.get("material") or (chip.get("props") or {}).get("material")
    area = p.get("area_mm") or ((ctx.module.config.places or {}).get("stations", {}).get(STATION, {})
                                .get("scan_area_mm") or [8.0, 6.0])
    overlap = float(p.get("overlap_mm") or 0.15)
    min_area = float(p.get("min_area_um2") or 50.0)
    cx, cy = microscope_xy(ctx)
    set_objective(ctx, mag)
    _stage_xy(ctx, cx, cy)
    autofocus(ctx)                                             # focus once per chip
    img, info = snap(ctx, f"scan_{mag}x_probe")
    h, w = img.shape[:2]
    fov = (w * vision.um_per_px(mag, w) / 1000.0, h * vision.um_per_px(mag, w) / 1000.0)
    nx = max(1, math.ceil((area[0] - overlap) / (fov[0] - overlap)))
    ny = max(1, math.ceil((area[1] - overlap) / (fov[1] - overlap)))
    found: list[dict] = []
    tiles = 0
    for j in range(ny):
        for i in range(nx):
            tx = cx - area[0] / 2 + fov[0] / 2 + i * (fov[0] - overlap)
            ty = cy - area[1] / 2 + fov[1] / 2 + j * (fov[1] - overlap)
            sx, sy = _stage_xy(ctx, tx, ty)
            img, info = snap(ctx, f"scan_{mag}x_{j}_{i}")
            tiles += 1
            for f in vision.find_flakes(img, objective=mag, material=material, min_area_um2=min_area):
                bx, by, bw, bh = f.bbox
                if bx <= 1 or by <= 1 or bx + bw >= w - 1 or by + bh >= h - 1:
                    continue                                   # cut by the tile edge: the neighbour sees it
                dx, dy = _stage_delta(f.x_px - w / 2, f.y_px - h / 2, mag, w)
                fx, fy = sx + dx, sy + dy
                if any(math.hypot(fx - g["stage_xy"][0], fy - g["stage_xy"][1]) < 0.05 for g in found):
                    continue
                found.append({"stage_xy": [round(fx, 5), round(fy, 5)],
                              "rel_centre_mm": [round(fx - cx, 4), round(fy - cy, 4)],
                              "area_um2": round(f.area_px * vision.um_per_px(mag, w) ** 2, 1),
                              "thickness_label": f.label, "contrast": round(f.contrast, 1),
                              "image_id": info["image_id"]})
    found.sort(key=lambda g: -g["area_um2"])
    store = _store(ctx)
    ids = []
    if store is not None:
        for old in store.list_samples():
            if old.get("kind") == "flake" and old.get("location") == chip["id"] and old.get("status") == "candidate":
                store.remove_sample(old["id"])
        for n, g in enumerate(found, start=1):
            fid = f"{chip['id']}_f{n:03d}"
            store.upsert_sample(fid, "flake", location=chip["id"], status="candidate", props={
                "material": material, "chip_id": chip["id"], "thickness_label": g["thickness_label"],
                "labels": {f"{mag}x": g["thickness_label"]}, "area_um2": g["area_um2"],
                "stage_xy": g["stage_xy"], "rel_centre_mm": g["rel_centre_mm"], "contrast": g["contrast"],
                "images": {f"{mag}x": g["image_id"]}, "detector": "placeholder_contrast"},
                location_source="camera")
            ids.append(fid)
    return {"chip": chip["id"], "tiles": tiles, "grid": [nx, ny], "flakes": ids, "detections": found[:50]}


def revisit_flake(ctx: ActionContext, p: dict) -> dict:
    """20x look at one flake: re-find it, refine its stage position, record area / label / image."""
    store = _store(ctx)
    fid = p["flake_id"]
    row = store.get_sample(fid) if store is not None else None
    if row is None:
        raise Rejected(f"unknown flake {fid}")
    props = dict(row.get("props") or {})
    mag = int(p.get("objective") or 20)
    try:
        out = center_on(ctx, props["stage_xy"], mag=mag, material=props.get("material"), target_v1px=(1920, 1080))
    except StepFailed as exc:
        c.update_sample(ctx, fid, status="lost", props={"lost_reason": str(exc)})
        return {"flake": fid, "found": False, "reason": str(exc)}
    found = out["found"] or {}
    labels = dict(props.get("labels") or {}, **{f"{mag}x": found.get("label")})
    images = dict(props.get("images") or {}, **{f"{mag}x": found.get("image_id")})
    c.update_sample(ctx, fid, status="catalogued", props={
        "stage_xy": out["axis_stage_xy"], f"area_um2_{mag}x": found.get("area_um2"), "labels": labels,
        "images": images, "thickness_label": found.get("label") or props.get("thickness_label")})
    return {"flake": fid, "found": True, **out}


def go_to_flake(ctx: ActionContext, p: dict) -> dict:
    """Put a recorded flake on the 5x pick-up target (re-found at 20x first), ready for pick_up."""
    store = _store(ctx)
    row = store.get_sample(p["flake_id"]) if store is not None else None
    if row is None:
        raise Rejected(f"unknown flake {p['flake_id']}")
    props = row.get("props") or {}
    out = center_on(ctx, props["stage_xy"], mag=int(p.get("objective") or 20), material=props.get("material"))
    set_objective(ctx, 5)
    return {"flake": row["id"], **out}


def go_to_target(ctx: ActionContext, p: dict) -> dict:
    """Put the drop point of the chip on the transfer stage on the 5x target. ``target_xy_mm`` = mm from the
    chip centre (the chip centre is on the axis with the stage at points.yaml sample_stage.microscope)."""
    cx, cy = microscope_xy(ctx)
    t = p.get("target_xy_mm") or [0.0, 0.0]
    out = center_on(ctx, (cx + float(t[0]), cy + float(t[1])), mag=5, find=False)
    set_objective(ctx, 5)
    return out


def check_pickup(ctx: ActionContext, p: dict) -> dict:
    """Qumus addition (v1 archived photos only): with the stamp away, look for a flake at the 5x pick-up
    target. None there = picked up. The ledger follows (flake -> the stamp, source camera)."""
    set_objective(ctx, 5)
    img, info = snap(ctx, "pickup_check")
    h, w = img.shape[:2]
    t = vision.ref_point(PICKUP_TARGET_PT, w)
    radius_px = float(p.get("radius_um") or 25.0) / vision.um_per_px(5, w)
    left = [f for f in vision.find_flakes(img, objective=5, min_area_um2=20.0)
            if math.hypot(f.x_px - t[0], f.y_px - t[1]) <= radius_px]
    picked = not left
    fid = p.get("flake_id")
    stamp = _stamp_sample(ctx)
    if picked and fid and stamp is not None:
        st = _store(ctx)
        st.set_sample_location(fid, stamp["id"], "camera", held_by=None)
        c.update_sample(ctx, fid, status="on_stamp")
    return {"picked": picked, "image_id": info["image_id"], "flake": fid,
            "still_there": [{"x_px": round(f.x_px, 1), "y_px": round(f.y_px, 1)} for f in left]}


def record_device(ctx: ActionContext, p: dict) -> dict:
    """v1 molecule_record_device: photos at 5x, 10x, 20x and 100x (autofocus before each)."""
    images = {}
    for mag in (5, 10, 20, 100):
        set_objective(ctx, mag)
        c.sleep(ctx, 1.0)                                      # TURRET_SETTLE_S
        autofocus(ctx)
        _, info = snap(ctx, f"device_{mag}x")
        images[f"{mag}x"] = info["image_id"]
    set_objective(ctx, 5)
    chip = _on(ctx, STATION)
    if chip is not None:
        c.update_sample(ctx, chip["id"], props={"device_images": images})
    return {"chip": chip["id"] if chip else None, "images": images}
