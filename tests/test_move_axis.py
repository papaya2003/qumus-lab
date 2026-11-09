"""move_axis (debug page "move to"): typed target for one axis, capped by distance from the position read."""

from __future__ import annotations

import pytest

from qumus_lab.launcher import build_stack
from qumus_lab.safety.gate import Rejected, SafetyGate
from qumus_lab.service.core import ApiError

from .conftest import wait_done


def _move(svc, device, axis, target, client="probe", loopback=True):
    svc.heartbeat(client)
    r = svc.command({"action": "move_axis", "device": device, "axis": axis, "target": target}, client, loopback=loopback)
    return wait_done(svc, r["command_id"])


def test_describe_lists_move_axis_as_local_motion(stack):
    for svc in stack.services.values():
        d = svc.describe()["commands"]["move_axis"]
        assert d["local_only"] is True and d["motion"] is True and d["any_client"] is False


@pytest.mark.parametrize("module,device,axis,target,key", [
    ("exfoliation", "arm_left", "x", 10.0, "x_mm"),          # from 0: exactly the 10 mm cap
    ("exfoliation", "arm_left", "z", 105.0, "z_mm"),         # arm z up 10 mm
    ("exfoliation", "tape_gantry", "z", -4.0, "z_mm"),
    ("stacking", "sample_stage", "rail", 10.0, "rail_mm"),
    ("stacking", "stamp", "rail", 10.0, "rail_mm"),
    ("stacking", "stamp", "x", 7.0, "x_mm"),                # from 6.0: 1 mm
    ("stacking", "focus", "z", -500.0, "z_um"),
])
def test_move_axis_reaches_target(stack, module, device, axis, target, key):
    lc = _move(stack.services[module], device, axis, target)
    assert lc["result"] == "ok", lc
    assert abs(lc["output"][key] - target) < 1e-6


@pytest.mark.parametrize("module,device,axis,target,why", [
    ("exfoliation", "arm_left", "x", 10.5, "above the cap 10"),
    ("exfoliation", "arm_left", "z", 94.0, "min_z_mm"),        # arm down 1 mm, min_z unknown: jog cap 0.5
    ("stacking", "stamp", "z", 6.3, "stamp z"),               # stamp z 0.2 mm: jog cap 0.1
    ("stacking", "stamp", "y", 8.5, "above the cap 1"),
    ("stacking", "focus", "z", 600.0, "above the cap 500"),
    ("exfoliation", "tape_gantry", "y", 1.0, "axis must be"),
])
def test_move_axis_refuses_beyond_caps_and_nothing_moves(stack, module, device, axis, target, why):
    svc = stack.services[module]
    before = svc.module.devices[device].snapshot()
    lc = _move(svc, device, axis, target)
    assert lc["result"] == "rejected" and why in lc["message"], lc
    assert svc.module.devices[device].snapshot() == before


def test_move_axis_is_local_only_and_needs_the_lease(stack):
    svc = stack.services["exfoliation"]
    svc.heartbeat("agent")
    with pytest.raises(ApiError) as e:
        svc.command({"action": "move_axis", "device": "arm_left", "axis": "x", "target": 1}, "agent", loopback=False)
    assert e.value.status == 403
    with pytest.raises(ApiError) as e:  # another client holds the lease
        svc.command({"action": "move_axis", "device": "arm_left", "axis": "x", "target": 1}, "page", loopback=True)
    assert e.value.status == 409
    with pytest.raises(ApiError) as e:
        svc.command({"action": "move_axis", "device": "exfoliation_fan", "axis": "x", "target": 1}, "agent", loopback=True)
    assert e.value.status == 400


def test_measured_min_z_allows_longer_arm_moves_down_but_not_below_it(cfg):
    cfg.limits["arms"]["arm_left"]["min_z_mm"] = 90.0
    s = build_stack(cfg, demo=True, demo_speed=50.0)
    try:
        svc = s.services["exfoliation"]
        lc = _move(svc, "arm_left", "z", 91.0)  # 4 mm down: allowed now
        assert lc["result"] == "ok" and abs(lc["output"]["z_mm"] - 91.0) < 1e-6, lc
        lc = _move(svc, "arm_left", "z", 89.5)
        assert lc["result"] == "rejected" and "below min_z" in lc["message"], lc
    finally:
        s.safe_state("test")
        s.close()


def test_gate_target_caps_apply_in_real_mode_too(cfg):
    gate = SafetyGate(cfg, "stacking", demo=False)
    assert gate.check_target_move("stage", "sample_stage", "x", 0.0, 10.0) == 10.0
    with pytest.raises(Rejected):
        gate.check_target_move("stage", "sample_stage", "x", 0.0, 10.01)
    with pytest.raises(Rejected):
        gate.check_target_move("arm", "arm_right", "z", 140.0, float("nan"))
    with pytest.raises(Rejected):
        gate.check_target_move("turret", "turret", "x", 0.0, 1.0)
