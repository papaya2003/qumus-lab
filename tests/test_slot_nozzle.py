"""One point per stage place and the nozzle angle as a slot property (operator decisions 2026-11-13).

The nozzle turns BY an angle (v1 M2101 R is relative). A slot angle a = the turn after picking from the slot so
the object sits square on the stage; going back to the slot turns -a.
"""

from __future__ import annotations

import threading

import pytest

from qumus_lab.modules.base import ActionContext


def _ctx(module):
    return ActionContext(module, module.gate, None, threading.Event(), lambda *a: None)


def _record(arm):
    calls = []
    orig_move, orig_rot = arm.move_to, arm.rotate_nozzle

    def move_to(x=None, y=None, z=None, feed=None, **kw):
        calls.append(("move", x, y, z))
        return orig_move(x, y, z, feed, **kw)

    def rotate_nozzle(deg):
        calls.append(("rot", float(deg)))
        return orig_rot(deg)
    arm.move_to, arm.rotate_nozzle = move_to, rotate_nozzle
    return calls


def test_pick_and_place_points_are_merged(cfg):
    pts = cfg.points["points"]
    for gone in ("stage_chip_place", "stage_chip_pick", "stage_carrier_place", "stage_carrier_pick"):
        assert gone not in pts["arm_left"]
    for gone in ("stage_chip_place", "stage_chip_pick", "stamp_place", "stamp_recycle_pick"):
        assert gone not in pts["arm_right"]
    assert {"stage_chip", "stage_carrier"} <= set(pts["arm_left"])
    assert {"stage_chip", "stamp_holder"} <= set(pts["arm_right"])


def test_load_chip_turns_by_the_slot_angle_after_the_pick(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    pt = dict(m.gate.point("arm_left", "left_chip_1"), stage_nozzle_deg=37.0)
    m.gate.points["arm_left"]["left_chip_1"] = pt
    calls = _record(m.devices["arm_left"])
    ox.load_chip(_ctx(m), {"place": "left_chip_1"})
    rots = [c[1] for c in calls if c[0] == "rot"]
    assert rots == [37.0]  # no turn "to 0" before the pick: R0 would not move a relative nozzle anyway
    pick_z = float(pt["xyz"][2])
    down = next(i for i, c in enumerate(calls) if c[0] == "move" and c[3] == pick_z)
    assert calls.index(("rot", 37.0)) > down, "turned while holding the chip"
    arm = m.devices["arm_left"]
    assert arm.nozzle_since_pick_deg == 37.0 and arm.nozzle_turned_deg == 37.0


def test_load_chip_without_a_measured_angle_keeps_v1(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    calls = _record(m.devices["arm_left"])
    ox.load_chip(_ctx(m), {"place": "left_chip_1"})
    assert [c[1] for c in calls if c[0] == "rot"] == [0.0]   # v1: rotate_nose(angle_deg=0) once, at the stage


def test_unload_chip_turns_back_by_the_slot_angle_and_skips_the_v1_offset_for_measured_slots(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    ox.load_chip(_ctx(m), {"place": "left_chip_1"})
    pt = dict(m.gate.point("arm_left", "left_chip_1"), stage_nozzle_deg=-20.0, source="measured")
    m.gate.points["arm_left"]["left_chip_1"] = pt
    calls = _record(m.devices["arm_left"])
    out = ox.unload_chip(_ctx(m), {"place": "left_chip_1"})
    assert [c[1] for c in calls if c[0] == "rot"] == [20.0]   # load turned -20, so back is +20
    x, y, z = (float(v) for v in pt["xyz"])
    assert ("move", x, y, z) in calls, "a measured slot is used without v1's (+10, -10) store offset"
    assert calls.index(("rot", 20.0)) < calls.index(("move", x, y, z)), "turned before putting it down"
    assert out["nozzle_turn_deg"] == 20.0


def test_load_then_unload_puts_the_nozzle_back(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.ops import stacking as so
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    for svc, arm_id, slot, load, unload in (("exfoliation", "arm_left", "left_chip_1", ox.load_chip, ox.unload_chip),
                                            ("stacking", "arm_right", "right_chip_1", so.place_chip_on_transfer_stage, so.store_chip)):
        m = stack.services[svc].module
        m.gate.points[arm_id][slot] = dict(m.gate.point(arm_id, slot), stage_nozzle_deg=33.0)
        load(_ctx(m), {"place": slot})
        unload(_ctx(m), {"place": slot})
        assert m.devices[arm_id].nozzle_turned_deg == 0.0, svc


def test_the_simulated_rotary_module_turns_relative(stack):
    m = stack.services["exfoliation"].module
    arm = m.devices["arm_left"]
    emu = arm.ser.emulator
    start = emu.nozzle_deg
    arm.rotate_nozzle(30)
    arm.rotate_nozzle(30)
    assert arm.nozzle_turned_deg == 60.0 and emu.nozzle_deg == start + 60.0
    arm.set_vacuum(True)
    assert arm.nozzle_since_pick_deg == 0.0 and arm.nozzle_turned_deg == 60.0


def test_load_stamp_uses_the_holder_angle(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import stacking as so
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["stacking"].module
    pt = dict(m.gate.point("arm_right", "right_stamp_1"), holder_nozzle_deg=142.0)
    m.gate.points["arm_right"]["right_stamp_1"] = pt
    calls = _record(m.devices["arm_right"])
    so.load_stamp(_ctx(m), {"place": "right_stamp_1"})
    assert [c[1] for c in calls if c[0] == "rot"] == [142.0]
