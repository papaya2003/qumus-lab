"""Development mode (limits.yaml `dev_mode: true`, operator decision 2026-11-13).

Off: jog / typed-target distance caps, switch auto-off cap, first-heat caps, require_measured_for_motion, slow
debug-page feeds. Kept: heater max_c and mandatory timeout, axis ranges, arm min_z, arm max feed, local-only.
"""

from __future__ import annotations

import math

import pytest

from qumus_lab.config import load_config
from qumus_lab.safety.gate import Rejected, SafetyGate


class _Heater:
    def hold_voltage_for(self, target_c):
        return 10.0


@pytest.fixture
def dev_gate(cfg):
    cfg.limits["dev_mode"] = True
    return SafetyGate(cfg, "stacking", demo=False)  # real mode: the strictest case


def test_repository_config_has_dev_mode_on():
    assert load_config().limits.get("dev_mode") is True


def test_dev_mode_drops_jog_and_typed_target_caps(dev_gate):
    assert dev_gate.check_jog("arm", "arm_left", "x", 25.0) == math.inf
    assert dev_gate.check_jog("manipulator", "stamp", "z", 2.0) == math.inf
    assert dev_gate.check_target_move("manipulator", "stamp", "z", 5.0, 9.0) == math.inf
    assert dev_gate.check_target_move("arm", "arm_left", "z", 80.0, 40.0) == math.inf
    assert dev_gate.check_target_move("focus", "focus", "z", 0.0, 8000.0) == math.inf
    with pytest.raises(Rejected):
        dev_gate.check_jog("arm", "arm_left", "x", float("nan"))
    with pytest.raises(Rejected):
        dev_gate.check_jog("heater", "transfer_heater", "x", 1.0)


def test_dev_mode_keeps_axis_ranges_and_min_z(dev_gate, cfg):
    with pytest.raises(Rejected, match="outside"):
        dev_gate.check_axis_target("stamp", "x", 20.0, for_point=False)
    with pytest.raises(Rejected, match="outside"):
        dev_gate.check_axis_target("sample_stage", "rail", 130.0, for_point=True)
    cfg.limits["arms"]["arm_left"]["min_z_mm"] = 30.0
    with pytest.raises(Rejected, match="min_z"):
        dev_gate.check_arm_z("arm_left", 29.0)


def test_dev_mode_moves_to_unmeasured_points_in_real_mode(dev_gate):
    device, name = next((d, n) for d, pts in dev_gate.points.items() for n, pt in pts.items()
                        if pt.get("source") != "measured")
    assert dev_gate.check_point(device, name)
    dev_gate.check_axis_target("sample_stage", "x", 40.0, for_point=True)  # no measured x range: allowed now


def test_dev_mode_heat_keeps_max_c_and_timeout_but_no_first_heat_cap(dev_gate):
    plan = dev_gate.check_heat(_Heater(), "transfer_heater", 150.0, 1200.0, None)
    assert plan["mode"] == "boost_hold"
    with pytest.raises(Rejected, match="above max"):
        dev_gate.check_heat(_Heater(), "transfer_heater", 176.0, 600.0, None)
    with pytest.raises(Rejected, match="timeout_s is mandatory"):
        dev_gate.check_heat(_Heater(), "transfer_heater", 100.0, 0.0, None)
    with pytest.raises(Rejected, match="explicit volts"):
        dev_gate.check_heat(_Heater(), "transfer_heater", 100.0, 600.0, 99.0)


def test_dev_mode_switch_may_stay_on(dev_gate):
    assert dev_gate.switch_auto_off(True, None) is None
    assert dev_gate.switch_auto_off(True, 3600) == 3600
    with pytest.raises(Rejected):
        dev_gate.switch_auto_off(True, 0)


def test_dev_mode_feed_is_faster_but_never_above_the_arm_max(dev_gate, cfg):
    assert dev_gate.debug_feed("tape_gantry", "gantry", 300.0) == 3000.0
    cfg.limits["arms"]["arm_left"]["max_feed_mm_min"] = 2000
    assert dev_gate.debug_feed("arm_left", "arm", 300.0) == 2000.0


def test_dev_mode_keeps_local_only(dev_gate):
    from qumus_lab.safety.gate import GateContext
    with pytest.raises(Rejected):
        dev_gate.check_local(True, GateContext("remote", loopback=False, demo=False))


def test_strict_mode_is_unchanged(cfg):
    gate = SafetyGate(cfg, "stacking", demo=False)
    assert gate.dev_mode is False
    with pytest.raises(Rejected, match="above the cap"):
        gate.check_jog("arm", "arm_left", "x", 1.0)
    with pytest.raises(Rejected, match="first heats"):
        gate.check_heat(_Heater(), "transfer_heater", 100.0, 600.0, None)
    assert gate.switch_auto_off(True, None) == 10.0
    assert gate.debug_feed("arm_left", "arm", 300.0) == 300.0


def test_service_reports_dev_mode(cfg, tmp_path):
    from qumus_lab.launcher import build_stack

    cfg.limits["dev_mode"] = True
    s = build_stack(cfg, demo=True, demo_speed=50.0)
    try:
        svc = s.services["stacking"]
        assert svc.describe()["dev_mode"] is True
        assert svc.state()["dev_mode"] is True
    finally:
        s.safe_state("test teardown")
        s.close()
