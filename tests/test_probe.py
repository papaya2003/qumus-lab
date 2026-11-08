"""The L1-L2 probe on simulated devices (both modules together)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qumus_lab.drivers.base import NoReply
from qumus_lab.probe.cli import Probe, main
from qumus_lab.safety.gate import Rejected, SafetyGate


def _probe(cfg, answers=None):
    out = []
    answers = list(answers or [])
    p = Probe(cfg, demo=True, out=out.append, ask=lambda q: answers.pop(0) if answers else "")
    return p, out


def test_l1_reads_every_device_of_both_modules(cfg):
    p, out = _probe(cfg)
    try:
        res = p.l1()
    finally:
        p.close()
    assert set(res) == set(cfg.all_device_ids()) | {"power_supply"}
    assert all(r["ok"] for r in res.values()), {k: v for k, v in res.items() if not v["ok"]}
    assert any("L1 read: 18/18" in line for line in out)
    cams = [d for d in res if "camera" in d]
    assert len(cams) == 3 and all(res[c]["picture"] and __import__("pathlib").Path(res[c]["picture"]).is_file() for c in cams)
    assert not any("MISSING" in line for line in out)  # demo: the simulated world lists every configured port
    log = (cfg.runs_dir / "probe").glob("*-probe.jsonl")
    assert any(json.loads(line)["step"] == "command" for f in log for line in f.read_text(encoding="utf-8").splitlines())


def test_l2_walk_without_prompts_both_modules(cfg):
    p, out = _probe(cfg)
    try:
        done = p.l2(confirm=False)
    finally:
        p.safe_state("test end")
        p.close()
    results = {d["step"]: d["result"] for d in done}
    assert len(results) == len(p.l2_plan())
    assert all(r == "ok" for r in results.values()), results
    world = p.stack.lab.backends.world
    assert world.by_device["exfoliation_vacuum"].on is False  # auto-off or safe_state switched it off
    assert world.psu.output is False


def test_jog_over_cap_is_refused(cfg):
    p, out = _probe(cfg)
    try:
        r = p.jog("arm_left", "z", 10.1, confirm=False)
    finally:
        p.close()
    assert r["result"] == "rejected"
    assert any("REFUSED" in line for line in out)


@pytest.mark.parametrize("device", ["arm_left", "arm_right"])
def test_larger_arm_z_test_returns_to_start(cfg, device):
    p, out = _probe(cfg)
    try:
        steps = [step for _, _, step in p.l2_plan() if step["device"] == device]
        assert [step["mm"] for step in steps] == [10.0, -10.0]
        assert p.connect(device)
        arm = p.stack.lab.devices[device]
        start = arm.read()["z_mm"]
        for step in steps:
            result = p.jog(device, "z", step["mm"], confirm=False)
            assert result["result"] == "ok"
            expected = start + 10.0 if step["mm"] > 0 else start
            assert result["output"]["z_mm"] == pytest.approx(expected)
    finally:
        p.close()


@pytest.mark.parametrize("device", ["arm_left", "arm_right"])
@pytest.mark.parametrize("axis", ["x", "y"])
def test_larger_z_test_does_not_raise_xy_limits(cfg, device, axis):
    gate = SafetyGate(cfg, cfg.module_of(device), demo=False)
    assert gate.check_jog("arm", device, axis, 0.5) == 0.5
    with pytest.raises(Rejected):
        gate.check_jog("arm", device, axis, 0.6)


def test_arm_z_limit_defaults_to_small_cap_without_override(cfg):
    cfg.limits["arms"]["arm_left"].pop("probe_z_jog_max_mm", None)
    gate = SafetyGate(cfg, "exfoliation", demo=False)
    with pytest.raises(Rejected):
        gate.check_jog("arm", "arm_left", "z", 0.6)


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf")])
def test_invalid_arm_z_cap_is_refused(cfg, cap):
    cfg.limits["arms"]["arm_left"]["probe_z_jog_max_mm"] = cap
    gate = SafetyGate(cfg, "exfoliation", demo=False)
    with pytest.raises(Rejected):
        gate.check_jog("arm", "arm_left", "z", 0.5)


@pytest.mark.parametrize("first_sign", [-1, 1])
def test_gantry_moves_configured_direction_then_back_to_start(cfg, first_sign):
    cfg.limits["probe"]["gantry_first_sign"] = first_sign
    p, out = _probe(cfg)
    try:
        steps = [step for _, _, step in p.l2_plan() if step["device"] == "tape_gantry"]
        assert [step["mm"] for step in steps] == [first_sign * 10.0, -first_sign * 10.0]
        assert all(step["axis"] == "x" for step in steps)
        assert p.connect("tape_gantry")
        gantry = p.stack.lab.devices["tape_gantry"]
        start = gantry.read()
        for index, step in enumerate(steps):
            result = p.jog("tape_gantry", "x", step["mm"], confirm=False)
            assert result["result"] == "ok"
            expected = start["x_mm"] + first_sign * 10.0 if index == 0 else start["x_mm"]
            assert result["output"]["x_mm"] == pytest.approx(expected)
            assert result["output"]["z_mm"] == start["z_mm"]
    finally:
        p.close()


@pytest.mark.parametrize("axis,amount", [("x", 10.1), ("z", 10.1), ("z", -10.1)])
def test_gantry_jog_limits_still_apply(cfg, axis, amount):
    gate = SafetyGate(cfg, "exfoliation", demo=False)
    with pytest.raises(Rejected):
        gate.check_jog("gantry", "tape_gantry", axis, amount)


def test_gantry_z_limit_defaults_to_small_cap_without_override(cfg):
    cfg.limits["probe"].pop("gantry_z_jog_max_mm", None)
    gate = SafetyGate(cfg, "exfoliation", demo=False)
    assert gate.check_jog("gantry", "tape_gantry", "z", 0.5) == 0.5
    with pytest.raises(Rejected):
        gate.check_jog("gantry", "tape_gantry", "z", 0.6)


def test_skip_does_nothing(cfg):
    p, out = _probe(cfg, answers=["s"])
    try:
        r = p.jog("arm_right", "x", 0.2)
    finally:
        p.close()
    assert r["result"] == "skipped"
    assert p.stack.lab.backends.world.by_device["arm_right"].motion.position()["x"] == 0.0


def test_real_heat_capped_at_40c(cfg):
    gate = SafetyGate(cfg, "exfoliation", demo=False)

    class H:
        def hold_voltage_for(self, t):
            return 6.5

    try:
        gate.check_heat(H(), "exfoliation_heater", 50, 300, None)
        raise AssertionError("50 C should be refused before the heater is measured")
    except Rejected:
        pass
    assert gate.check_heat(H(), "exfoliation_heater", 30, 300, None) == {"mode": "until", "volts": 6.5}


def test_cli_ports_runs_in_demo(cfg, capsys):
    assert main(["--demo", "--runs-dir", str(cfg.runs_dir), "ports"]) == 0
    assert "COM7" in capsys.readouterr().out


def test_l2_walk_stops_at_a_lock(cfg):
    p, out = _probe(cfg)
    p.stack.lab.backends.world.by_device["exfoliation_vacuum"].reply = False  # first step: relay goes silent
    try:
        done = p.l2(confirm=False)
    finally:
        p.close()
    assert len(done) == 1 and done[0]["result"] == "failed"
    assert any("L2 walk stopped" in line for line in out)


def _two_device_plan(p, first="tape_gantry", second="arm_right"):
    return [entry for entry in p.l2_plan() if entry[2]["device"] in (first, second)]


def test_l2_problem_skips_device_return_and_continues_other_devices(cfg, monkeypatch):
    p, out = _probe(cfg, answers=["", "n", "", "y", "", "y"])
    plan = _two_device_plan(p)
    monkeypatch.setattr(p, "l2_plan", lambda: plan)
    try:
        done = p.l2()
        assert [r["result"] for r in done] == ["not_as_expected", "skipped", "ok", "ok"]
        assert p.stack.lab.devices["tape_gantry"].read()["x_mm"] == 10.0  # return was not sent
        report = json.loads(next(p.log.dir.glob("*-l2-report.json")).read_text(encoding="utf-8"))
        assert report["steps"][0]["command_result"] == "ok"
        assert report["steps"][0]["observed"] == "NOT as expected"
        assert report["stopped_reason"] is None
    finally:
        p.close()


def test_l2_skip_skips_both_halves_of_device_test(cfg, monkeypatch):
    p, out = _probe(cfg, answers=["s", "", "y", "", "y"])
    plan = _two_device_plan(p)
    monkeypatch.setattr(p, "l2_plan", lambda: plan)
    try:
        done = p.l2()
        assert [r["result"] for r in done] == ["skipped", "skipped", "ok", "ok"]
        assert not p.stack.lab.devices["tape_gantry"].connected
    finally:
        p.close()


def test_l2_read_failure_continues_without_moving_failed_device(cfg, monkeypatch):
    p, out = _probe(cfg)
    plan = _two_device_plan(p)
    monkeypatch.setattr(p, "l2_plan", lambda: plan)
    try:
        assert p.connect("tape_gantry")
        emulator = p.stack.lab.backends.world.by_device["tape_gantry"]
        def no_reply():
            raise NoReply("position query failed")
        monkeypatch.setattr(p.stack.lab.devices["tape_gantry"], "read", no_reply)
        done = p.l2(confirm=False)
        assert [r["result"] for r in done] == ["failed", "skipped", "ok", "ok"]
        assert emulator.motion.position()["x"] == emulator.motion.position()["z"] == 0.0
        assert "pre-command check failed" in done[0]["message"]
    finally:
        p.close()


@pytest.mark.parametrize("failure", ["not_connected", "rejected"])
def test_l2_known_failure_skips_device_and_continues(cfg, monkeypatch, failure):
    p, out = _probe(cfg)
    plan = _two_device_plan(p)
    monkeypatch.setattr(p, "l2_plan", lambda: plan)
    original_step = p._l2_step
    monkeypatch.setattr(p, "_l2_step", lambda step, confirm: {"result": failure}
                        if step["device"] == "tape_gantry" else original_step(step, confirm))
    try:
        done = p.l2(confirm=False)
        assert [r["result"] for r in done] == [failure, "skipped", "ok", "ok"]
    finally:
        p.close()


def test_l2_timeout_stops_without_running_next_device(cfg, monkeypatch):
    p, out = _probe(cfg)
    monkeypatch.setattr(p, "_l2_step", lambda step, confirm: {"result": "timeout"})
    try:
        done = p.l2(confirm=False)
        assert len(done) == 1 and done[0]["result"] == "timeout"
        assert all(not dev.connected for dev in p.stack.lab.devices.values())
    finally:
        p.close()


def test_l2_quit_at_observation_sends_no_further_commands(cfg, monkeypatch):
    p, out = _probe(cfg, answers=["", "q"])
    plan = _two_device_plan(p)
    monkeypatch.setattr(p, "l2_plan", lambda: plan)
    try:
        done = p.l2()
        assert len(done) == 1 and done[0]["observed"] == "not verified"
        assert not p.stack.lab.devices["arm_right"].connected
    finally:
        p.close()


def test_l2_interrupt_saves_partial_report(cfg, monkeypatch):
    p, out = _probe(cfg)
    def interrupt(prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr(p, "ask", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            p.l2()
        report = json.loads(next(p.log.dir.glob("*-l2-report.json")).read_text(encoding="utf-8"))
        assert report["stopped_reason"] == "Ctrl+C"
        assert all(not dev.connected for dev in p.stack.lab.devices.values())
    finally:
        p.close()


@pytest.mark.parametrize("reason,phase,expected", [("timeout", "off", "failed"),
                                                 ("target reached", "reached", "ok")])
def test_watched_heat_reports_actual_outcome(cfg, monkeypatch, reason, phase, expected):
    p, out = _probe(cfg)
    try:
        assert p.connect("exfoliation_heater")
        heater = p.stack.lab.devices["exfoliation_heater"]
        def completed_heat(*args, **kwargs):
            heater.last_off_reason = reason
            heater.phase = phase
            heater.thermo.temp_c = 30.0 if expected == "ok" else 24.81
            return {"result": "ok", "message": "", "output": {"phase": "heating"}}
        monkeypatch.setattr(p, "run", completed_heat)
        result = p.heat("exfoliation_heater", 30, 300, 6.5, confirm=False, watch=True)
        assert result["result"] == expected
        assert result["output"]["phase"] == phase
        if expected == "failed":
            assert "not reached" in result["message"] and "24.81" in result["message"]
    finally:
        p.close()


def test_focus_test_records_before_and_after_microscope_images(cfg, monkeypatch):
    p, out = _probe(cfg)
    try:
        monkeypatch.setattr(p.stack.lab.devices["focus"], "expected_move_s", lambda amount: 0)
        step = next(step for _, _, step in p.l2_plan() if step["device"] == "focus")
        result = p._l2_step(step, confirm=True)
        assert result["result"] == "ok"
        assert result["output"]["before_image"] != result["output"]["after_image"]
        assert Path(result["output"]["before_image"]).is_file()
        assert Path(result["output"]["after_image"]).is_file()
    finally:
        p.close()


def test_visible_camera_capture_opens_saved_image(cfg, monkeypatch):
    p, out = _probe(cfg)
    opened = []
    # Keep the emulated service/backends while exercising Windows' real-run viewer branch.
    p.demo = False
    monkeypatch.setattr("qumus_lab.probe.cli.sys.platform", "win32")
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    try:
        result = p.capture("desk_camera_left", show=True)
        assert result["result"] == "ok"
        assert opened == [result["path"]]
        assert Path(opened[0]).is_file()
    finally:
        p.close()


def test_larger_focus_and_stamp_tests_return_to_start(cfg):
    p, out = _probe(cfg)
    try:
        for device, expected_steps in (("stamp", [-5.0, 5.0]), ("focus", [-5.0, 5.0])):
            steps = [step for _, _, step in p.l2_plan() if step["device"] == device]
            assert [step["mm"] for step in steps] == expected_steps
            assert p.connect(device)
            mover = p.stack.lab.devices[device]
            key = "z_um" if device == "focus" else "z_mm"
            factor = 1000 if device == "focus" else 1
            start = mover.read()[key]
            for step in steps:
                result = p._l2_step(step, confirm=False)
                assert result["result"] == "ok"
                expected = start + expected_steps[0] * factor if step is steps[0] else start
                assert mover.read()[key] == pytest.approx(expected)
    finally:
        p.close()


@pytest.mark.parametrize("axis", ["x", "y", "rail"])
def test_larger_stamp_z_does_not_raise_other_stamp_axis_limits(cfg, axis):
    gate = SafetyGate(cfg, "stacking", demo=False)
    with pytest.raises(Rejected):
        gate.check_jog("manipulator", "stamp", axis, 0.2)


@pytest.mark.parametrize("kind,device,amount", [("manipulator", "stamp", 5.1), ("focus", "focus", 5001)])
def test_larger_focus_stamp_z_tests_still_enforce_caps(cfg, kind, device, amount):
    gate = SafetyGate(cfg, "stacking", demo=False)
    with pytest.raises(Rejected):
        gate.check_jog(kind, device, "z", amount)


def test_exfoliation_l2_uses_15v_without_raising_transfer_voltage(cfg):
    probe = object.__new__(Probe)
    probe.config = cfg
    steps = {step["device"]: step for _, _, step in probe.l2_plan() if step["kind"] == "heat"}
    gate = SafetyGate(cfg, "exfoliation", demo=False)
    step = steps["exfoliation_heater"]
    assert (step["target_c"], step["volts"], step["timeout_s"]) == (40, 15.0, 300)
    assert gate.check_heat(None, "exfoliation_heater", 40, 300, 15) == {"mode": "until", "volts": 15.0}
    with pytest.raises(Rejected):
        gate.check_heat(None, "exfoliation_heater", 40, 300, 15.1)
    transfer_gate = SafetyGate(cfg, "stacking", demo=False)
    assert steps["transfer_heater"]["volts"] == 6.5
    with pytest.raises(Rejected):
        transfer_gate.check_heat(None, "transfer_heater", 30, 300, 15)
