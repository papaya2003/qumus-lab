"""Left-arm travel routes (operator decision 2026-11-14, qumus_lab/ops/route.py).

The tape stands tall beside the exfoliation stage, so the arm never swings straight between the left board, the
stage and the middle board: it goes straight up, round the ring near its base (Z95), above the target, straight down.
"""

from __future__ import annotations

import itertools
import threading

import pytest

from qumus_lab.config import load_config
from qumus_lab.modules.base import ActionContext
from qumus_lab.ops import route
from qumus_lab.safety.gate import Rejected

from .conftest import wait_done

RING = ["travel_L", "travel_L1", "park", "travel_R1", "travel_R"]


@pytest.fixture(scope="module")
def pcfg():
    return load_config().points


def _at(pcfg, name, z=None):
    x, y, pz = pcfg["points"]["arm_left"][name]["xyz"]
    return {"x": float(x), "y": float(y), "z": float(pz if z is None else z)}


def _names(steps):
    return [(s.kind, s.point) for s in steps]


def test_the_ring_is_configured_as_agreed(pcfg):
    cfg = route.config(pcfg, "arm_left")
    assert cfg["ring"] == RING and float(cfg["ring_z"]) == 95.0
    pts = pcfg["points"]["arm_left"]
    assert [pts[n]["xyz"] for n in RING] == [[-163, 163, 95], [-88, 212, 95], [0, 230, 95], [88, 212, 95],
                                            [163, 163, 95]]
    assert route.config(pcfg, "arm_right") is None  # the stacking arm keeps its v1 moves


def test_left_board_to_stage_goes_up_round_the_ring_and_down_slowly(pcfg):
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "left_chip_1"), "stage_chip")
    assert _names(steps) == [("lift", ""), ("ring", "travel_L"), ("ring", "travel_L1"), ("ring", "park"),
                             ("above", "stage_chip"), ("down", "stage_chip")]
    lift, *_, above, down = steps
    assert (lift.x, lift.y, lift.z) == (-320.0, 168.0, 30.0)          # straight up at the slot first
    assert (above.x, above.y, above.z) == (10.0, 252.0, 95.0) and not above.slow
    assert (down.z, down.slow) == (85.0, True)                         # down onto the stage at F1000


def test_stage_to_middle_board_lifts_slowly_first(pcfg):
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "stage_chip"), "slot_S1")
    assert _names(steps) == [("lift", ""), ("ring", "park"), ("ring", "travel_R1"), ("ring", "travel_R"),
                             ("above", "slot_S1"), ("down", "slot_S1")]
    assert (steps[0].x, steps[0].y, steps[0].z, steps[0].slow) == (10.0, 252.0, 95.0, True)
    assert (steps[-2].z, steps[-1].z) == (30.0, 15.0)


def test_left_board_to_middle_board_passes_every_ring_point(pcfg):
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "left_chip_9", z=30), "slot_S4")
    assert [s.point for s in steps if s.kind == "ring"] == RING
    assert steps[0].kind == "ring"                                     # already at the slot's approach height


def test_same_area_moves_across_at_the_approach_height(pcfg):
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "left_chip_1"), "left_chip_2")
    assert [(s.kind, s.z) for s in steps] == [("lift", 30.0), ("across", 30.0), ("down", 15.0)]
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "stage_chip"), "stage_carrier")
    assert [(s.kind, s.z, s.slow) for s in steps] == [("lift", 95.0, True), ("across", 95.0, False),
                                                      ("down", 82.0, True)]


def test_going_to_park_or_stopping_above(pcfg):
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "slot_S2"), "park")
    assert _names(steps) == [("lift", ""), ("ring", "travel_R"), ("ring", "travel_R1"), ("ring", "park")]
    steps = route.plan(pcfg, "arm_left", _at(pcfg, "park"), "left_chip_5", stop_above=True)
    assert _names(steps)[-1] == ("above", "left_chip_5") and steps[-1].z == 30.0


def test_unknown_or_far_poses_are_refused(pcfg):
    with pytest.raises(Rejected, match="unknown"):
        route.plan(pcfg, "arm_left", {"x": None, "y": None, "z": None}, "park")
    with pytest.raises(Rejected, match="far from every named point"):
        route.plan(pcfg, "arm_left", {"x": 0.0, "y": 0.0, "z": 50.0}, "park")


def test_no_route_ever_swings_between_areas(pcfg):
    """Every x/y move joins two ring points, a ring point and a point it serves, or two points of one area."""
    cfg = route.config(pcfg, "arm_left")
    pts = pcfg["points"]["arm_left"]
    served = [n for n in pts if n not in RING]
    area = {n: route.ring_point_for(cfg, n) for n in pts}
    for a, b in itertools.permutations(served + ["park"], 2):
        pose = _at(pcfg, a)
        steps = route.plan(pcfg, "arm_left", pose, b)
        prev = (a, pose["x"], pose["y"])
        for s in steps:
            here = s.point or prev[0]
            if (s.x, s.y) != (prev[1], prev[2]):
                ok = (here in RING and prev[0] in RING) or area[here] == area[prev[0]]
                assert ok, f"{a} -> {b}: swings from {prev[0]} to {here}"
                if here not in RING and prev[0] not in RING:
                    assert s.z >= float(pts[here].get("approach_z", 0)) - 1e-6, f"{a} -> {b}: low across"
            prev = (here, s.x, s.y)


# --------------------------------------------------------------------------------------- in the module
def _cmd(svc, body):
    r = svc.command(body, "agent", loopback=True)
    return wait_done(svc, r["command_id"], timeout=20)


def _ctx(module):
    return ActionContext(module, module.gate, None, threading.Event(), lambda *a: None)


def _record(arm):
    calls = []
    orig_move, orig_rot = arm.move_to, arm.rotate_nozzle

    def move_to(x=None, y=None, z=None, feed=None, **kw):
        calls.append(("move", x, y, z, feed))
        return orig_move(x, y, z, feed, **kw)

    def rotate_nozzle(deg):
        calls.append(("rot", float(deg)))
        return orig_rot(deg)
    arm.move_to, arm.rotate_nozzle = move_to, rotate_nozzle
    return calls


def test_load_chip_turns_the_nozzle_at_park_and_goes_round(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    calls = _record(m.devices["arm_left"])
    ox.load_chip(_ctx(m), {"place": "left_chip_1"})
    xy = [(c[1], c[2], c[3]) for c in calls if c[0] == "move"]
    pick = xy.index((-320.0, 168.0, 15.0))
    assert xy[pick + 1] == (-320.0, 168.0, 30.0), "straight up off the slot"
    assert xy[pick + 2:pick + 5] == [(-163.0, 163.0, 95.0), (-88.0, 212.0, 95.0), (0.0, 230.0, 95.0)], xy
    rot = calls.index(next(c for c in calls if c[0] == "rot"))
    assert calls[rot - 1][1:4] == (0.0, 230.0, 95.0), "the nozzle turns at park"
    down = calls[rot + 2]
    assert calls[rot + 1][1:4] == (10.0, 252.0, 95.0) and down[1:4] == (10.0, 252.0, 85.0) and down[4] == 1000.0


def test_unload_chip_lifts_straight_up_off_the_stage(stack):
    pytest.importorskip("cv2")
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    ox.load_chip(_ctx(m), {"place": "left_chip_1"})
    calls = _record(m.devices["arm_left"])
    ox.unload_chip(_ctx(m), {"place": "left_chip_1"})
    moves = [c for c in calls if c[0] == "move"]
    pick = next(i for i, c in enumerate(moves) if c[1:4] == (10.0, 252.0, 85.0))
    assert moves[pick + 1][1:5] == (10.0, 252.0, 95.0, 1000.0), "straight up, slowly, before leaving the stage"
    assert moves[pick + 2][1:4] == (0.0, 230.0, 95.0)


def test_go_to_point_follows_the_route(stack):
    svc = stack.services["exfoliation"]
    arm = svc.module.devices["arm_left"]
    svc.heartbeat("agent")
    assert _cmd(svc, {"action": "zone_acquire", "zone": "middle_board", "device": "arm_left"})["result"] == "ok"
    calls = _record(arm)
    r = _cmd(svc, {"action": "go_to_point", "point": "arm_left.slot_S1"})
    assert r["result"] == "ok", r
    xy = [c[1:4] for c in calls if c[0] == "move"]
    assert xy == [(0.0, 230.0, 95.0), (88.0, 212.0, 95.0), (163.0, 163.0, 95.0), (210.0, 154.0, 30.0),
                  (210.0, 154.0, 15.0)], xy
