"""One point per stage place and the nozzle angle as a slot property (operator decision 2026-11-13)."""

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


def test_load_chip_uses_the_slot_angle_and_picks_at_nozzle_zero(stack):
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
    assert rots == [0.0, 37.0]
    first_rot = calls.index(("rot", 0.0))
    pick_z = float(pt["xyz"][2])
    assert any(c[0] == "move" and c[3] == pick_z for c in calls[first_rot:]), "rotated to 0 before going down"


def test_load_chip_without_a_measured_angle_keeps_v1(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    calls = _record(m.devices["arm_left"])
    ox.load_chip(_ctx(m), {"place": "left_chip_1"})
    assert [c[1] for c in calls if c[0] == "rot"] == [0.0]   # v1: rotate_nose(angle_deg=0) once, at the stage


def test_store_chip_reverses_the_slot_angle_and_skips_the_v1_offset_for_measured_slots(stack):
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
    assert [c[1] for c in calls if c[0] == "rot"] == [-20.0, 0.0]
    x, y, z = (float(v) for v in pt["xyz"])
    assert ("move", x, y, z) in calls, "a measured slot is used without v1's (+10, -10) store offset"
    assert out["nozzle_deg"] == 0.0


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
    assert [c[1] for c in calls if c[0] == "rot"] == [0.0, 142.0]
