"""v1 hazards that the byte tests do not cover: Kinesis enable, arm stop, feed and focus limits."""

from __future__ import annotations

import pytest

from qumus_lab.drivers.base import Refused
from qumus_lab.launcher import build_stack

from .conftest import wait_done, wait_for


def test_kinesis_enables_on_first_move_and_moves_without_blocking(stack):
    kcubes = stack.lab.backends.world.kcubes
    assert kcubes and all(("enable",) not in k.calls for k in kcubes.values())  # connect does not energise
    svc = stack.services["stacking"]
    svc.heartbeat("probe")
    r = svc.command({"action": "jog", "device": "stamp", "axis": "z", "mm": 0.05}, "probe", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "ok"
    moved = [k for k in kcubes.values() if any(c[0] == "move_to" for c in k.calls)]
    assert len(moved) == 1
    calls = moved[0].calls
    assert calls.index(("enable",)) < next(i for i, c in enumerate(calls) if c[0] == "move_to")
    assert all(c[2] == 0 for c in calls if c[0] == "move_to")  # v1 blocked 60 s; we poll so stop works


def test_stopping_a_moving_arm_is_unconfirmed_and_locks(cfg):
    stack = build_stack(cfg, demo=True, demo_speed=8.0, modules=["exfoliation"])
    try:
        svc = stack.services["exfoliation"]
        svc.heartbeat("agent")
        r = svc.command({"action": "zone_acquire", "zone": "middle_board", "device": "arm_left"}, "agent", loopback=True)
        assert wait_done(svc, r["command_id"])["result"] == "ok"
        svc.command({"action": "go_to_point", "point": "arm_left.slot_S7"}, "agent", loopback=True)
        wait_for(lambda: stack.lab.devices["arm_left"].moving, timeout=5)
        out = svc.stop(wait_s=0.5)
        assert out["stop"] == "unconfirmed"
        assert svc.locked and svc.fault["code"] == "stop_unconfirmed"
    finally:
        stack.safe_state("t")
        stack.close()


def test_arm_move_needs_a_feed(stack):
    arm = stack.lab.devices["arm_left"]
    with pytest.raises(Refused):
        arm.move_to(0, 220, 95, feed=None)


def test_focus_refuses_moves_over_30_mm(stack):
    with pytest.raises(Refused):
        stack.lab.devices["focus"].move_by_um(30001)
