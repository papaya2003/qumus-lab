"""Fault injection for the 2026-11-13 code review fixes. Each test makes its fault happen and fails on the old
code (checked by running it against main before the fix).

1. A stacking job that fails, is stopped or cancelled leaves no heater heating (runner), and drop_down
   switches its 27 V boost off when it does not finish.
2. The heater never drives above max_c (target + overshoot_c could exceed it).
3. massage and the right-arm slot pick take their height from the (measured) point, not a fixed z.
4. stop() cancels and drops the queue under the job lock, so a queued job can not start after a stop.
5. A malformed wait_s never prevents the stop (contract v1 and contract 2 routes).
6. advance_tape powers reel 2 again when the rotation fails.
7. A refused go_to_point (device offline) does not leave the arm counted inside the middle board.
8. A job submitted while the previous worker thread is still finishing gets a worker (was stuck queued).
9. exfoliate_chips with chip_ids and no num_chips exfoliates every given chip (as the sim lab does).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from qumus_lab.drivers.base import DriverError
from qumus_lab.jobs.runner import StepFailed, _Job
from qumus_lab.modules.base import ActionContext
from qumus_lab.safety.gate import GateContext
from qumus_lab.service.core import ApiError
from qumus_lab.service.http import make_server, serve_in_thread

from .conftest import Http, wait_done, wait_for


def _ctx(module):
    return ActionContext(module, module.gate, None, threading.Event(), lambda *a: None)


# ------------------------------------------------------------------------------------------- heaters
def test_failed_job_switches_off_the_heater_it_left_on(stack):
    svc = stack.services["stacking"]
    heater = svc.module.devices["transfer_heater"]

    def flow(ctx, p):  # like stack_device: set_initial_temp, then a later step fails
        heater.heat(90, 600)
        raise StepFailed("pick-up check failed")

    run_id = f"run_test_{uuid.uuid4().hex[:6]}"
    if svc.store is not None:
        svc.store.create_run(run_id, "fake_flow", svc.name, {}, project_id=None, requested_by="pytest", meta={})
    job = _Job(run_id, "fake_flow", {}, "pytest", None, flow, GateContext("pytest", True, True))
    svc.jobs._run(job)
    assert not heater.heating, "the job failed but the transfer heater is still heating"
    assert heater.phase == "off" and run_id in (heater.last_off_reason or "")
    assert any(ev["message"] == "job.heater_off" for ev in svc.get_events(0)["events"])


def test_drop_down_switches_the_boost_off_when_it_does_not_finish(stack, monkeypatch):
    pytest.importorskip("cv2")
    from qumus_lab.ops import common as c
    from qumus_lab.ops import stacking as so

    m = stack.services["stacking"].module
    heater = m.devices["transfer_heater"]

    def timeout(*a, **k):
        assert heater.heating and heater.auto_hold is False  # the fault: 27 V boost is running
        raise StepFailed("timed out after 3600 s waiting for transfer stage >= 155 C")

    monkeypatch.setattr(c, "wait_until", timeout)
    with pytest.raises(StepFailed):
        so.drop_down(_ctx(m), {})
    assert not heater.heating and heater.phase == "off"
    assert "drop_down did not finish" in heater.last_off_reason


def test_heater_cuts_the_output_at_max_c_even_below_target_plus_overshoot(stack):
    heater = stack.services["stacking"].module.devices["transfer_heater"]
    assert heater.target_c is None
    assert 170 + heater.overshoot_c > heater.max_c  # the configuration that allowed 185 C over a 175 C limit
    reading = heater.max_c + 3.0                     # 178 C: over max_c, under 170 + 15
    heater.thermo.read = lambda: {"temp_c": reading}
    heater.poll_s = 0.01
    heater.heat(170, 600, auto_hold=False)
    try:
        wait_for(lambda: heater.phase == "overshoot_off", timeout=3)
        assert heater.psu.output_on is False
    finally:
        heater.off("test")


# ------------------------------------------------------------------------------------- arm heights
def _record_arm_moves(arm):
    calls = []
    orig = arm.move_to

    def move_to(x=None, y=None, z=None, feed=None, **kw):
        calls.append(z)
        return orig(x, y, z, feed, **kw)
    arm.move_to = move_to
    return calls


def test_massage_presses_at_the_measured_point_height(stack):
    m = stack.services["exfoliation"].module
    pt = dict(m.gate.point("arm_left", "massage_start"))
    pt["xyz"] = [pt["xyz"][0], pt["xyz"][1], 72.0]          # bench measured the tape 2 mm lower than v1
    m.gate.points["arm_left"]["massage_start"] = pt
    calls = _record_arm_moves(m.devices["arm_left"])
    from qumus_lab.ops import exfoliation as ox

    ox.massage(_ctx(m), {"rounds": 1})
    assert 72.0 in calls and 74.0 not in calls


def test_right_arm_picks_from_a_slot_one_mm_above_its_measured_z(stack, monkeypatch):
    from qumus_lab.ops import common as c
    from qumus_lab.ops import stacking as so

    m = stack.services["stacking"].module
    pt = dict(m.gate.point("arm_right", "slot_S3"))
    pt["xyz"] = [pt["xyz"][0], pt["xyz"][1], 12.0]          # v1 places at Z14 and picks at Z15
    m.gate.points["arm_right"]["slot_S3"] = pt
    zs = []
    orig = c.move_point
    monkeypatch.setattr(c, "move_point", lambda ctx, arm, name, **kw: (zs.append(kw.get("z")), orig(ctx, arm, name, **kw))[1])

    class Picked(Exception):
        pass

    def stop_at_pick(ctx, arm):
        raise Picked
    monkeypatch.setattr(c, "pick_item", stop_at_pick)
    m.zones.acquire("middle_board", "arm_right")
    with pytest.raises(Picked):
        so.place_chip_on_transfer_stage(_ctx(m), {"place": "mid_chip_3"})
    assert zs[-1] == 13.0, zs


# ------------------------------------------------------------------------------------------- stop
def test_stop_cannot_lose_its_cancel_to_the_worker_starting_the_next_job(stack):
    svc = stack.services["stacking"]
    done = threading.Event()
    with svc.lock:  # the job worker holds the lock: it has popped the next job and now clears the cancel flag
        t = threading.Thread(target=lambda: (svc.stop(), done.set()), daemon=True)
        t.start()
        time.sleep(0.2)
        svc._cancel.clear()  # what the worker does for the job it is starting
    t.join(5)
    assert done.is_set()
    assert svc._cancel.is_set(), "the stop's cancel was cleared by a job that started after the stop"


@pytest.fixture
def http(stack):
    srv = make_server("127.0.0.1", 0, services=stack.services, web=True)
    serve_in_thread(srv)
    yield {name: Http(f"http://127.0.0.1:{srv.server_address[1]}/m/{name}") for name in stack.services}
    srv.shutdown()


@pytest.mark.parametrize("wait_s", ["now", [1], {"s": 1}])
def test_malformed_wait_s_still_stops(stack, http, wait_s):
    svc = stack.services["stacking"]
    status, out = http["stacking"].post("/api/stop", {"wait_s": wait_s}, client="someone")
    assert status == 200 and out["stop"] in ("confirmed", "pending")
    assert any(ev["message"] == "stop" for ev in svc.get_events(0)["events"])


def test_malformed_wait_s_still_stops_on_contract_2(stack):
    import json
    import urllib.request

    from qumus_lab.service.contract2 import Station

    srv = make_server("127.0.0.1", 0, service=stack.services["stacking"], station=Station(stack.services))
    serve_in_thread(srv)
    try:
        for bad in ("now", 99, -1, True):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/stacking/api/stop",
                                         data=json.dumps({"wait_s": bad}).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Lab-Client", "anyone")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=20) as r:
                body = json.loads(r.read())
            assert r.status == 200 and body["stop"] in ("confirmed", "pending"), (bad, body)
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------------------------------- reels
def test_advance_tape_powers_reel_2_again_when_the_rotation_fails(stack):
    from qumus_lab.modules import exfoliation as ex

    m = stack.services["exfoliation"].module
    reels = m.devices["tape_reels"]

    def broken(*a, **k):
        assert reels.powered[2] is False  # the fault happens while reel 2 is unpowered
        raise DriverError("serial write failed")
    reels.rotate = broken
    with pytest.raises(DriverError):
        ex.advance_tape(_ctx(m), {"deg": 5})
    assert reels.powered[2] is True


# ------------------------------------------------------------------------------------------- zones
def test_refused_go_to_point_does_not_count_the_arm_inside(stack):
    svc = stack.services["stacking"]
    svc.heartbeat("agent")
    r = svc.command({"action": "zone_acquire", "zone": "middle_board", "device": "arm_right"}, "agent", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "ok"
    arm = svc.module.devices["arm_right"]
    arm.connected = False                      # the fault: the arm's port is not open
    try:
        r = svc.command({"action": "go_to_point", "point": "arm_right.slot_S1"}, "agent", loopback=True)
        assert wait_done(svc, r["command_id"])["result"] != "ok"
    except ApiError:
        pass
    finally:
        arm.connected = True
    assert svc.state()["zones"]["middle_board"]["inside"] == []
    r = svc.command({"action": "zone_release", "zone": "middle_board", "device": "arm_right"}, "agent", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "ok"


# ------------------------------------------------------------------------------------- jobs (L5)
class _Flow:
    def __init__(self, name, fn):
        self.name, self.fn = name, fn


def test_job_submitted_while_the_worker_is_finishing_still_runs(stack):
    svc = stack.services["stacking"]
    runner = svc.jobs
    orig_loop = runner._loop
    tail = threading.Event()
    release = threading.Event()

    def loop_then_linger():  # the worker returns from _loop, but its thread is still alive for a moment
        orig_loop()
        tail.set()
        release.wait(5)
    runner._loop = loop_then_linger
    ran = []
    gctx = GateContext("pytest", True, True)
    try:
        with svc.lock:
            runner.submit_locked(_Flow("first", lambda ctx, p: ran.append("first") or {}), {}, "pytest", gctx)
        assert tail.wait(5)                                    # first job done, worker thread still alive
        with svc.lock:
            runner.submit_locked(_Flow("second", lambda ctx, p: ran.append("second") or {}), {}, "pytest", gctx)
        wait_for(lambda: "second" in ran, timeout=3)
    finally:
        release.set()


def test_exfoliate_chips_takes_the_count_from_chip_ids(stack, monkeypatch):
    from qumus_lab.flows import exfoliate_chips
    from qumus_lab.ops import exfoliation as ox
    from qumus_lab.sim.bench import seed_demo_bench

    seed_demo_bench(stack.store)
    m = stack.services["exfoliation"].module
    done = []
    monkeypatch.setattr(ox, "prepare_mother_tape", lambda ctx, p: {})
    monkeypatch.setattr(ox, "exfoliate_chip", lambda ctx, p: done.append(p["chip_place"]) or {})
    out = exfoliate_chips(_ctx(m), {"material": "hBN", "chip_ids": ["chip_001", "chip_002", "chip_003"]})
    assert done == ["left_chip_1", "left_chip_2", "left_chip_3"]
    assert [c["chip_id"] for c in out["chips"]] == ["chip_001", "chip_002", "chip_003"]
