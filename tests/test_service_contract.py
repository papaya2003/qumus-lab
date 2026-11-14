"""Contract v1 behaviour of the module services (demo devices)."""

from __future__ import annotations

import time

import pytest

from qumus_lab.service.core import ApiError
from qumus_lab.service.http import make_server, serve_in_thread

from .conftest import Http, wait_done, wait_for


@pytest.fixture
def http(stack):
    srv = make_server("127.0.0.1", 0, services=stack.services, web=True)
    serve_in_thread(srv)
    yield {name: Http(f"http://127.0.0.1:{srv.server_address[1]}/m/{name}") for name in stack.services}
    srv.shutdown()


def test_describe_wire_format(http):
    for name, h in http.items():
        status, d = h.get("/api/describe")
        assert status == 200
        assert d["contract"] == 1 and d["demo"] is True and d["client_header"] == "X-Qumus-Client"
        assert d["module"] == name
        cmds = d["commands"]
        for required in ("read_all", "home_query", "go_to_point", "jog", "set_switch", "heat", "heat_off", "capture"):
            assert required in cmds, required
            assert set(cmds[required]) >= {"params", "any_client", "motion", "local_only"}
        assert cmds["jog"]["local_only"] is True and cmds["jog"]["motion"] is True
        assert cmds["read_all"]["any_client"] is True


def test_state_and_flat_command(http):
    h = http["stacking"]
    status, st = h.get("/api/state")
    assert status == 200 and st["connected"] is True and st["locked"] is False and st["lease"] is None
    status, r = h.post("/api/command", {"action": "read_all"})
    assert status == 202 and "command_id" in r and "state" in r
    status, ev = h.get("/api/events?since=0")
    assert set(ev) == {"events", "next"} and ev["events"]
    assert set(ev["events"][0]) == {"seq", "ts_unix", "level", "message", "fields"}
    wait_for(lambda: h.get("/api/state")[1]["last_command"] and h.get("/api/state")[1]["last_command"]["result"] == "ok")
    lc = h.get("/api/state")[1]["last_command"]
    assert set(lc) >= {"id", "action", "client", "result", "message", "finished_at"}


def test_post_needs_json_and_client_header(http):
    h = http["exfoliation"]
    assert h.post("/api/command", {"action": "read_all"}, client=False)[0] == 400
    assert h.post("/api/command", {"action": "read_all"}, ctype="text/plain")[0] == 415
    assert h.post("/api/command", {"action": "nope"})[0] == 400
    assert h.post("/api/command", {"action": "read", "device": "arm_left", "bogus": 1})[0] == 400


def test_motion_needs_lease_and_second_client_gets_409(http):
    h = http["stacking"]
    body = {"action": "go_to_point", "point": "sample_stage.microscope"}
    assert h.post("/api/command", body)[0] == 409  # no lease
    assert h.post("/api/heartbeat")[0] == 200
    assert h.post("/api/heartbeat", client="other")[0] == 409
    assert h.post("/api/renew", client="other")[0] == 409
    assert h.post("/api/renew")[0] == 200
    status, r = h.post("/api/command", body)
    assert status == 202
    assert h.post("/api/release")[0] == 200


def test_stop_from_non_holder_is_confirmed(stack, http):
    svc = stack.services["stacking"]
    h = http["stacking"]
    h.post("/api/heartbeat")
    st, r = h.post("/api/command", {"action": "go_to_point", "point": "stamp.remove"})
    assert st == 202
    time.sleep(0.05)
    st, out = h.post("/api/stop", {"wait_s": 5}, client="someone-else")
    assert st == 200 and out["stop"] == "confirmed"
    lc = wait_done(svc, r["command_id"])
    assert lc["result"] in ("stopped", "ok")
    assert svc.locked is False


def test_lease_expiry_during_motion_stops_it(cfg):
    from qumus_lab.launcher import build_stack

    stack = build_stack(cfg, demo=True, demo_speed=1.0, modules=["stacking"])
    try:
        svc = stack.services["stacking"]
        svc.lease_s = 0.3
        svc.heartbeat("agent")
        r = svc.command({"action": "go_to_point", "point": "sample_stage.microscope"}, "agent", loopback=True)
        lc = wait_done(svc, r["command_id"], timeout=10)
        assert lc["result"] == "stopped"
        rail = stack.lab.backends.world.by_device["sample_stage"].steps("L") / 3280.0
        assert 0 < rail < 79.0  # stopped part-way (a full move takes about 4 s)
        assert any(e["message"] == "lease expired" for e in svc.get_events(0)["events"])
    finally:
        stack.safe_state("t")
        stack.close()


def test_unknown_outcome_locks_until_local_unlock(stack):
    svc = stack.services["exfoliation"]
    stack.lab.backends.world.by_device["exfoliation_vacuum"].reply = False  # relay goes silent
    svc.heartbeat("probe")
    r = svc.command({"action": "set_switch", "device": "exfoliation_vacuum", "on": True}, "probe", loopback=True)
    lc = wait_done(svc, r["command_id"])
    assert lc["result"] == "failed" and "unknown outcome" in lc["message"]
    assert svc.locked and svc.fault["code"] == "outcome_unknown"
    with pytest.raises(ApiError) as e:
        svc.command({"action": "jog", "device": "arm_left", "axis": "x", "mm": 0.1}, "probe", loopback=True)
    assert e.value.status == 409
    # never retried: exactly one AT+CH1=1 reached the relay
    assert stack.lab.backends.world.by_device["exfoliation_vacuum"].history == [True]
    with pytest.raises(ApiError) as e:
        svc.unlock("remote", loopback=False)
    assert e.value.status == 403
    svc.unlock("person", loopback=True)
    assert not svc.locked and svc.fault is None


def test_local_only_refused_from_network(stack):
    svc = stack.services["exfoliation"]
    svc.heartbeat("agent")
    with pytest.raises(ApiError) as e:
        svc.command({"action": "jog", "device": "arm_left", "axis": "x", "mm": 0.1}, "agent", loopback=False)
    assert e.value.status == 403


def test_jog_caps(stack):
    svc = stack.services["stacking"]
    svc.heartbeat("probe")
    with pytest.raises(ApiError):
        svc.command({"action": "jog", "device": "stamp", "axis": "z", "mm": 5.1}, "probe", loopback=True)
    r = svc.command({"action": "jog", "device": "stamp", "axis": "z", "mm": 0.05}, "probe", loopback=True)
    lc = wait_done(svc, r["command_id"])
    assert lc["result"] == "ok", lc
    assert abs(lc["output"]["z_mm"] - 6.55) < 1e-3


def test_switch_auto_off(stack):
    svc = stack.services["exfoliation"]
    svc.heartbeat("probe")
    r = svc.command({"action": "set_switch", "device": "exfoliation_fan", "on": True, "auto_off_s": 0.3}, "probe",
                    loopback=True)
    wait_done(svc, r["command_id"])
    fan = stack.lab.backends.world.by_device["exfoliation_fan"]
    assert fan.on is True
    wait_for(lambda: fan.on is False, timeout=3)
    with pytest.raises(ApiError) as e:
        svc.command({"action": "set_switch", "device": "exfoliation_fan", "on": True, "auto_off_s": 60}, "probe",
                    loopback=True)
    assert e.value.status == 400 and svc.last_command["result"] == "rejected"


def test_heat_runs_with_timeout_and_heat_off_any_client(stack):
    svc = stack.services["exfoliation"]
    svc.heartbeat("probe")
    r = svc.command({"action": "heat", "device": "exfoliation_heater", "target_c": 30, "timeout_s": 60}, "probe",
                    loopback=True)
    lc = wait_done(svc, r["command_id"])
    assert lc["result"] == "ok", lc
    heater = stack.lab.devices["exfoliation_heater"]
    assert heater.heating and heater.deadline_unix is not None
    # missing timeout is refused
    with pytest.raises(ApiError):
        svc.command({"action": "heat", "device": "exfoliation_heater", "target_c": 30, "timeout_s": 0}, "probe",
                    loopback=True)
    r3 = svc.command({"action": "heat_off", "device": "exfoliation_heater"}, "anyone", loopback=False)
    wait_done(svc, r3["command_id"])
    assert not heater.heating and heater.phase == "off"
    assert stack.lab.backends.world.psu.output is False


def test_heaters_share_supply_one_at_a_time(stack):
    ex, st = stack.services["exfoliation"], stack.services["stacking"]
    ex.heartbeat("p")
    st.heartbeat("p")
    r = ex.command({"action": "heat", "device": "exfoliation_heater", "target_c": 30, "timeout_s": 60}, "p", loopback=True)
    assert wait_done(ex, r["command_id"])["result"] == "ok"
    with pytest.raises(ApiError) as e:
        st.command({"action": "heat", "device": "transfer_heater", "target_c": 155, "timeout_s": 60}, "p", loopback=True)
    assert e.value.status == 409 and "power supply" in e.value.message


def test_real_mode_gate_refuses_v1_points(cfg):
    from qumus_lab.modules.base import Module
    from qumus_lab.safety.gate import Rejected, SafetyGate

    gate = SafetyGate(cfg, "exfoliation", demo=False)
    with pytest.raises(Rejected):
        gate.check_point("arm_left", "park")
    cfg2 = cfg.copy()
    cfg2.points["points"]["arm_left"]["park"]["source"] = "measured"
    assert SafetyGate(cfg2, "exfoliation", demo=False).check_point("arm_left", "park")["xyz"] == [0, 230, 95]
    assert Module  # imported for side effects only
