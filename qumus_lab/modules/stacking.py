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


STACKING_L4_DEVICES = {"arm_right", "sample_stage", "stage_vacuum", "turret", "focus", "microscope_camera", "stamp",
                       "stamp_vacuum", "transfer_heater"}


def _register_l4(module: Module) -> None:
    from ..ops import stacking as st

    places = (module.config.places or {}).get("places", {})
    right = sorted(k for k, v in places.items() if v.get("arm_right"))
    chips = [k for k in right if places[k].get("holds") == "chip"]
    stamps = [k for k in right if places[k].get("holds") == "stamp"]
    L4 = "L4 (v1 molecule-level sequence; real mode needs measured points). "
    sid = {"type": "string"}
    acts = [
        Action("place_chip_on_stage", st.place_chip_on_transfer_stage,
               {"place": {"type": "string", "enum": chips}, "angle_deg": {"type": "number"}, "sample_id": sid},
               ("place",), motion=True, description=L4 + "Chip from a board place onto the transfer stage, vacuum on, "
               "stage to the microscope."),
        Action("store_chip", st.store_chip, {"place": {"type": "string", "enum": chips}, "sample_id": sid}, ("place",),
               motion=True, description=L4 + "Chip from the transfer stage back to a board place."),
        Action("load_stamp", st.load_stamp, {"place": {"type": "string", "enum": stamps}, "sample_id": sid},
               ("place",), motion=True, description=L4 + "Stamp from the right board onto the stamp holder."),
        Action("initialize_stamp", st.initialize_stamp, {"focus": {"type": "boolean", "default": True}}, motion=True,
               description=L4 + "Stamp over the transfer stage (L 120, z 5.4), focus +25 mm, autofocus."),
        Action("remove_stamp", st.remove_stamp, {"focus": {"type": "boolean", "default": True}}, motion=True,
               description=L4 + "Stamp up (z 6.5) and away (L 160), focus -25 mm."),
        Action("recycle_stamp", st.recycle_stamp, motion=True, description=L4 + "Used stamp into the bin."),
        Action("set_initial_temp", st.set_initial_temp, {"timeout_s": {"type": "number", "minimum": 60,
                                                                       "maximum": 3600}},
               motion=True, description=L4 + "Transfer stage to 90 C (20 V, then 10 V hold)."),
        Action("transfer_heat_off", st.transfer_heat_off, description="Transfer heater off."),
        Action("adjust_touching_point", st.adjust_touching_point, motion=True,
               description=L4 + "Find the first stamp contact (Newton rings) and move it 300 px above the target."),
        Action("pick_up", st.pick_up, motion=True,
               description=L4 + "Lower until the contact front covers the target flake, lift to safe z."),
        Action("drop_down", st.drop_down, motion=True,
               description=L4 + "Heat to 155 C, stamp to z 4.9, wait > 170 C, lift 5 mm, heater off."),
        Action("scan_chip", st.scan_chip, {"material": sid, "objective": {"type": "integer", "enum": [5, 10]},
                                           "min_area_um2": {"type": "number", "minimum": 0},
                                           "area_mm": {"type": "array"}},
               motion=True, description="Tile the chip on the transfer stage; flakes go to the ledger "
               "(placeholder detector, see docs/vision.md)."),
        Action("revisit_flake", st.revisit_flake, {"flake_id": sid, "objective": {"type": "integer",
                                                                                   "enum": [20, 100]}},
               ("flake_id",), motion=True, description="Re-find one flake at 20x, refine its position."),
        Action("go_to_flake", st.go_to_flake, {"flake_id": sid}, ("flake_id",), motion=True,
               description="Put a recorded flake on the 5x pick-up target."),
        Action("go_to_target", st.go_to_target, {"target_xy_mm": {"type": "array"}}, motion=True,
               description="Put the drop point (mm from the chip centre) on the 5x target."),
        Action("check_pickup", st.check_pickup, {"flake_id": sid, "radius_um": {"type": "number"}}, motion=True,
               description="With the stamp away: is the flake gone from the pick-up spot? Ledger follows."),
        Action("record_device", st.record_device, motion=True, description=L4 + "Photos at 5x, 10x, 20x, 100x."),
    ]
    for a in acts:
        module.add(a)


def build_stacking(module: Module) -> Module:
    register_common(module)
    if STACKING_L4_DEVICES <= set(module.devices):
        _register_l4(module)
    from ..flows import register_stacking

    register_stacking(module)
    if "turret" in module.devices:
        module.add(Action("set_objective", set_objective,
                          {"objective": {"type": "integer", "enum": [5, 10, 20, 100]}}, ("objective",), motion=True,
                          description="Rotate the turret to an objective (v1 cRDC<n>)."))
    if "focus" in module.devices:
        module.add(Action("focus_by", focus_by, {"um": {"type": "number"}}, ("um",), motion=True,
                          description="Relative focus move in um (real mode before L3: <= 50 um)."))
    return module
