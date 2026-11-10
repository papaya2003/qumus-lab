"""Exfoliation L4 operations (left arm, exfoliation stage, tape gantry and reels, heater, fan, stage vacuum).

Source: v1 common/hardware/robot_arm_controller.py (arm sequences) and
agents/processing_agent/workflows/exfoliation/molecule_flows/*.py (order of the molecule steps).
"""

from __future__ import annotations

from ..modules.base import ActionContext
from ..safety.gate import Rejected
from . import common as c

ARM = "arm_left"
STAGE = "exfoliation_stage"
VACUUM = "exfoliation_vacuum"
FAN = "exfoliation_fan"
HEATER = "exfoliation_heater"
TAPE_SAMPLE = "tape"
# v1 chip_exfoliation_molecule.py:168 waits until the stage is below 60 C before peeling
PEEL_BELOW_C = 60.0
# v1 tape_atoms.py:73 AtomTapeRelease default angle; mother_tape_molecule.py releases 120 deg first
TAPE_RELEASE_DEG = 3600.0
MOTHER_TAPE_RELEASE_DEG = 120.0
# v1 massage (robot_arm_controller.py:394-409): centre = stage + (0, 42); strokes x -13 .. +15 at F500,
# rows y + 0, 3, ... 15, each row `rounds` times
MASSAGE_DX = (-13.0, 15.0)
MASSAGE_ROWS = tuple(range(0, 16, 3))
MASSAGE_Z = 74.0


def _point(ctx, name):
    return ctx.gate.check_point(ARM, name)


def _stage_centre(ctx) -> tuple[float, float]:
    """v1 get_exfoliation_stage_position: fixed (0, 256). Stored as the stage_chip_place point - (10, 0)."""
    x, y, _ = _point(ctx, "stage_chip_place")["xyz"]
    return float(x) - 10.0, float(y)


def _pick_from_place(ctx, place: str) -> str:
    """v1: G0 X Y Z30 F5000, G0 Z15 F5000, pick, G0 Z30 F5000 (robot_arm_controller.py:208-211)."""
    name, pt = c.place_point(ctx, place, ARM)
    az, z = float(pt.get("approach_z", 30)), float(pt["xyz"][2])
    c.move_point(ctx, ARM, name, z=az)
    c.move_point(ctx, ARM, name, z=z)
    c.pick_item(ctx, ARM)
    c.move_point(ctx, ARM, name, z=az)
    return name


def load_chip(ctx: ActionContext, p: dict) -> dict:
    """v1 place_silicon_chip_on_exfoliation_stage (robot_arm_controller.py:205-221)."""
    place = p["place"]
    _pick_from_place(ctx, place)
    c.park(ctx, ARM)
    c.rotate(ctx, ARM, float(p.get("angle_deg") or 0.0))
    c.move_point(ctx, ARM, "stage_chip_place")                       # G0 X{sx+10} Y{sy} Z80 F5000
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, "stage_chip_place", z=float(_point(ctx, "stage_chip_place")["approach_z"]),
                 feed=c.SLOW_F)                                       # G0 Z95 F1000
    c.park(ctx, ARM)
    sid = c.move_sample(ctx, place, STAGE, sample_id=p.get("sample_id"))
    return {"chip": sid, "from": place, "to": STAGE}


def load_carrier(ctx: ActionContext, p: dict) -> dict:
    """v1 place_raw_crystal_carrier_on_exfoliation_stage (robot_arm_controller.py:244-262)."""
    place = p["place"]
    _pick_from_place(ctx, place)
    c.park(ctx, ARM)
    c.rotate(ctx, ARM, float(p.get("angle_deg") or 0.0))
    c.move_point(ctx, ARM, "stage_carrier_place")                    # G0 X{sx} Y{sy-27} Z85 F5000
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, "stage_carrier_place", z=float(_point(ctx, "stage_carrier_place")["approach_z"]),
                 feed=c.SLOW_F)                                       # G0 Z95 F1000
    c.park(ctx, ARM)
    sid = c.move_sample(ctx, place, STAGE, kind="carrier", sample_id=p.get("sample_id"))
    return {"carrier": sid, "from": place, "to": STAGE}


def unload_chip(ctx: ActionContext, p: dict) -> dict:
    """v1 store_exfoliation_silicon_chip (robot_arm_controller.py:281-312): stage -> place (x+10, y-10)."""
    place = p["place"]
    name, pt = c.place_point(ctx, place, ARM)
    x, y, z = (float(v) for v in pt["xyz"])
    az = float(pt.get("approach_z", 30))
    sx, sy = _stage_centre(ctx)
    angle = c.store_rotation(x + 10, y - 10, sx + 12, sy)
    c.park(ctx, ARM)
    c.move_point(ctx, ARM, "stage_chip_pick", z=float(_point(ctx, "stage_chip_pick")["approach_z"]))  # Z95
    c.move_point(ctx, ARM, "stage_chip_pick", feed=c.SLOW_F)         # G0 Z80 F1000
    c.pick_item(ctx, ARM)
    c.park(ctx, ARM)
    c.move_point(ctx, ARM, name, z=az, dx=10.0, dy=-10.0)            # G0 X{x+10} Y{y-10} Z30 F5000
    c.rotate(ctx, ARM, angle)
    c.move_point(ctx, ARM, name, z=z, dx=10.0, dy=-10.0)             # G0 Z15 F5000
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, name, z=az, dx=10.0, dy=-10.0)            # G0 Z30 F5000
    c.park(ctx, ARM)
    sid = c.move_sample(ctx, STAGE, place, sample_id=p.get("sample_id"))
    return {"chip": sid, "from": STAGE, "to": place, "nozzle_deg": round(angle, 2)}


def unload_carrier(ctx: ActionContext, p: dict) -> dict:
    """v1 store_raw_crystal_carrier (robot_arm_controller.py:347-375); v1 ends without parking."""
    place = p["place"]
    name, pt = c.place_point(ctx, place, ARM)
    x, y, z = (float(v) for v in pt["xyz"])
    az = float(pt.get("approach_z", 30))
    sx, sy = _stage_centre(ctx)
    angle = c.store_rotation(x, y, sx, sy - 27)
    c.move_point(ctx, ARM, "stage_carrier_pick", z=float(_point(ctx, "stage_carrier_pick")["approach_z"]))
    c.move_point(ctx, ARM, "stage_carrier_pick", feed=c.SLOW_F)      # G0 Z82 F1000
    c.pick_item(ctx, ARM)
    c.park(ctx, ARM)
    c.move_point(ctx, ARM, name, z=az)
    c.rotate(ctx, ARM, -angle)
    c.move_point(ctx, ARM, name, z=z)
    c.place_item(ctx, ARM)
    c.move_point(ctx, ARM, name, z=az)
    sid = c.move_sample(ctx, STAGE, place, kind="carrier", sample_id=p.get("sample_id"))
    return {"carrier": sid, "from": STAGE, "to": place}


def massage(ctx: ActionContext, p: dict) -> dict:
    """v1 massage (robot_arm_controller.py:390-412)."""
    rounds = int(p.get("rounds") or 1)
    if not 1 <= rounds <= 5:
        raise Rejected("rounds must be 1..5")
    start = _point(ctx, "massage_start")
    mx, my = float(start["xyz"][0]) - MASSAGE_DX[0], float(start["xyz"][1])
    top = float(start.get("approach_z", 95))
    c.park(ctx, ARM)
    c.move(ctx, ARM, mx, my, top, c.TRAVEL_F, point="massage_start")
    c.move(ctx, ARM, mx + MASSAGE_DX[0], my, MASSAGE_Z, c.TRAVEL_F, point="massage_start")
    strokes = 0
    for dy in MASSAGE_ROWS:
        for _ in range(rounds):
            c.move(ctx, ARM, mx + MASSAGE_DX[1], my + dy, None, c.MASSAGE_F, linear=True)
            c.move(ctx, ARM, mx + MASSAGE_DX[0], my + dy, None, c.MASSAGE_F, linear=True)
            strokes += 2
    c.move(ctx, ARM, mx, my, top, c.TRAVEL_F, point="massage_start")
    c.park(ctx, ARM)
    return {"rounds": rounds, "strokes": strokes}


# ----------------------------------------------------------------------------------------- tape + heat
def _gantry(ctx):
    from ..modules import exfoliation as ex

    return ex


def tape_apply(ctx: ActionContext) -> dict:
    return _gantry(ctx).apply_tape(ctx, {})


def tape_peel(ctx: ActionContext, speed: float) -> dict:
    return _gantry(ctx).peel_tape(ctx, {"speed": float(speed)})


def tape_release(ctx: ActionContext, deg: float) -> dict:
    return _gantry(ctx).advance_tape(ctx, {"deg": float(deg)})


def _switch(ctx, dev_id: str, on: bool) -> None:
    ctx.check()
    ctx.device(dev_id, ("switch",)).set(bool(on))


def stage_temp(ctx) -> float:
    return float(ctx.device(HEATER, ("heater",)).thermo.read()["temp_c"])


def exfoliate_on_stage(ctx: ActionContext, p: dict) -> dict:
    """v1 chip_exfoliation_molecule.py:140-170: tape on, massage, (heat cycle), cool below 60 C, peel.

    Room temperature (stage already at or above target): wait dwell_s with the tape on.
    Heated: vacuum off, peel, heat (boost then hold), tape on again, vacuum on, heater off, fan on, dwell.
    """
    target = float(p.get("target_c") or 22.0)
    dwell = float(p.get("dwell_s") or 10.0)
    speed = float(p.get("peel_speed") or 500.0)
    rounds = int(p.get("rounds") or 1)
    timeout = float(p.get("heat_timeout_s") or 900.0)
    out = {"target_c": target}
    c.step(ctx, "tape on", lambda: tape_apply(ctx))
    c.step(ctx, "massage", lambda: massage(ctx, {"rounds": rounds}))
    t0 = stage_temp(ctx)
    out["start_c"] = t0
    if t0 < target:
        heater = ctx.device(HEATER, ("heater",))

        def heat_cycle():
            _switch(ctx, VACUUM, False)
            tape_peel(ctx, speed)
            plan = ctx.gate.check_heat(heater, HEATER, target, timeout, p.get("hold_v"))
            heater.heat(target, timeout, mode=plan["mode"], volts=plan["volts"])
            c.wait_until(ctx, lambda: heater.phase in ("hold", "reached") or stage_temp(ctx) >= target,
                         timeout, 1.0, f"{HEATER} to reach {target} C")
            reached = stage_temp(ctx)
            tape_apply(ctx)
            _switch(ctx, VACUUM, True)
            heater.off("exfoliation: target reached, tape on (v1)")
            _switch(ctx, FAN, True)
            return {"reached_c": reached, "mode": plan["mode"]}

        out["heat"] = c.step(ctx, "heat cycle", heat_cycle)
    c.step(ctx, "dwell", lambda: c.sleep(ctx, dwell))
    c.step(ctx, "cool below 60 C", lambda: c.wait_until(ctx, lambda: stage_temp(ctx) <= PEEL_BELOW_C, 1800, 2.0,
                                                         f"{STAGE} below {PEEL_BELOW_C} C"))
    _switch(ctx, FAN, False)
    c.step(ctx, "peel", lambda: tape_peel(ctx, speed))
    try:
        ctx.device(HEATER, ("heater",)).off("exfoliation done")
    except Exception:  # noqa: BLE001 - off already; the watchdog reports an unconfirmed off
        pass
    out["end_c"] = stage_temp(ctx)
    return out


# ----------------------------------------------------------------------------------------- molecules
def prepare_mother_tape(ctx: ActionContext, p: dict) -> dict:
    """v1 mother_tape_molecule.py: release 120 deg, park, gantry on, carrier on stage, vacuum on, tape on,
    massage, peel, vacuum off, carrier back, park."""
    place = p["carrier_place"]
    speed = float(p.get("peel_speed") or 500.0)
    rounds = int(p.get("rounds") or 1)
    c.step(ctx, "release tape", lambda: tape_release(ctx, MOTHER_TAPE_RELEASE_DEG))
    c.step(ctx, "park", lambda: c.park(ctx, ARM))
    c.step(ctx, "gantry on", lambda: ctx.device("tape_gantry", ("gantry",)).power(True))
    carrier = c.step(ctx, "carrier to stage", lambda: load_carrier(ctx, {"place": place}))
    _switch(ctx, VACUUM, True)
    c.step(ctx, "tape on", lambda: tape_apply(ctx))
    c.step(ctx, "massage", lambda: massage(ctx, {"rounds": rounds}))
    c.step(ctx, "peel", lambda: tape_peel(ctx, speed))
    _switch(ctx, VACUUM, False)
    c.step(ctx, "carrier back", lambda: unload_carrier(ctx, {"place": place}))
    c.step(ctx, "park", lambda: c.park(ctx, ARM))
    material = None
    store = c._store(ctx)
    if store is not None and carrier.get("carrier"):
        material = ((store.get_sample(carrier["carrier"]) or {}).get("props") or {}).get("material")
    c.update_sample(ctx, TAPE_SAMPLE, kind="tape", location="tape", status="loaded",
                    props={"material": material, "carrier": carrier.get("carrier")})
    return {"carrier": carrier.get("carrier"), "material": material}


def exfoliate_chip(ctx: ActionContext, p: dict) -> dict:
    """v1 chip_exfoliation_molecule.py: park, gantry on, chip on stage, vacuum on, exfoliate, vacuum off,
    chip to a free middle-board slot, new tape, park."""
    src = p["chip_place"]
    dst = p.get("store_place")
    if not dst:
        free = c.free_places(ctx, ARM, station="middle_storage")
        if not free:
            raise Rejected("no free middle-board slot for the exfoliated chip")
        dst = free[0]
    c.step(ctx, "park", lambda: c.park(ctx, ARM))
    c.step(ctx, "gantry on", lambda: ctx.device("tape_gantry", ("gantry",)).power(True))
    chip = c.step(ctx, "chip to stage", lambda: load_chip(ctx, {"place": src, "angle_deg": p.get("angle_deg", 0.0)}))
    _switch(ctx, VACUUM, True)
    ex = exfoliate_on_stage(ctx, p)
    _switch(ctx, VACUUM, False)
    stored = c.step(ctx, "chip to storage", lambda: unload_chip(ctx, {"place": dst}))
    deg = float(p.get("tape_release_deg") or TAPE_RELEASE_DEG)
    c.step(ctx, "new tape", lambda: tape_release(ctx, deg))
    c.step(ctx, "park", lambda: c.park(ctx, ARM))
    store = c._store(ctx)
    material = None
    if store is not None:
        material = ((store.get_sample(TAPE_SAMPLE) or {}).get("props") or {}).get("material")
    if chip.get("chip"):
        c.update_sample(ctx, chip["chip"], status="exfoliated",
                        props={"material": p.get("material") or material, "exfoliation": ex})
    return {"chip": chip.get("chip"), "from": src, "to": dst, "exfoliation": ex}


def clear_stage(ctx: ActionContext, p: dict) -> dict:
    """v1 clear_stage_molecule.py: anything the ledger has on the stage goes to a free slot, then park."""
    store = c._store(ctx)
    moved = []
    if store is not None:
        for s in [s for s in store.list_samples() if s["location"] == STAGE]:
            holds = "carrier" if s["kind"] == "carrier" else "chip"
            station = "left_storage" if holds == "carrier" else "middle_storage"
            free = c.free_places(ctx, ARM, station=station, holds=holds)
            if not free:
                raise Rejected(f"no free {station} place for {s['id']}")
            fn = unload_carrier if holds == "carrier" else unload_chip
            fn(ctx, {"place": free[0], "sample_id": s["id"]})
            moved.append({"id": s["id"], "to": free[0]})
    c.park(ctx, ARM)
    return {"moved": moved}
