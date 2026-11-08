"""The L1-L2 probe on simulated devices (both modules together)."""

from __future__ import annotations

import json

from qumus_lab.probe.cli import Probe, main


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
        r = p.jog("arm_left", "z", 2.0, confirm=False)
    finally:
        p.close()
    assert r["result"] == "rejected"
    assert any("REFUSED" in line for line in out)


def test_skip_does_nothing(cfg):
    p, out = _probe(cfg, answers=["s"])
    try:
        r = p.jog("arm_right", "x", 0.2)
    finally:
        p.close()
    assert r["result"] == "skipped"
    assert p.stack.lab.backends.world.by_device["arm_right"].motion.position()["x"] == 0.0


def test_real_heat_capped_at_40c(cfg):
    from qumus_lab.safety.gate import Rejected, SafetyGate

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
