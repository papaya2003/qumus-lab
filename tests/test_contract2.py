"""Contract 2 next to contract v1 on the module ports (service/contract2.py), demo devices only.

papaya2003/lab-contract spec/contract-2.md. When lab-contract's conformance package can be found
(LAB_CONTRACT=<checkout>, or a lab-contract checkout beside this repository) its full check runs too.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from qumus_lab.service.contract2 import Station
from qumus_lab.service.http import make_server, serve_in_thread

from .conftest import wait_for

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Http2:
    def __init__(self, base: str, client: str | None = "brain") -> None:
        self.base, self.client = base, client

    def call(self, method: str, path: str, body=None, *, ctype="application/json", client=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None and ctype:
            req.add_header("Content-Type", ctype)
        who = self.client if client is None else client
        if who:
            req.add_header("X-Lab-Client", who)
        try:
            with OPENER.open(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)


@pytest.fixture
def ports(stack):
    """Both module ports, as the launcher serves them (loopback is not trusted there)."""
    station = Station(stack.services)
    servers = {}
    for name, svc in stack.services.items():
        srv = make_server("127.0.0.1", 0, service=svc, station=station)
        serve_in_thread(srv)
        servers[name] = srv
    yield {n: f"http://127.0.0.1:{s.server_address[1]}" for n, s in servers.items()}, station
    for s in servers.values():
        s.shutdown()
        s.server_close()


def record(h, module, cid):
    def done():
        status, rec = h.get(f"/{module}/api/commands/{cid}")
        return rec if status == 200 and rec["result"] is not None else None
    return wait_for(done, timeout=20)


def test_entry_describe_state_on_every_port(ports):
    urls, station = ports
    for url in urls.values():
        h = Http2(url)
        status, entry = h.get("/api/station")
        assert status == 200 and entry["contract"] == 2 and entry["station"] == "qumus-lab"
        assert [s["path"] for s in entry["services"]] == [f"{n}/" for n in station.windows]
    h = Http2(urls["stacking"])
    d = h.get("/stacking/api/describe")[1]
    assert d["boot_id"] == station.boot_id and d["lease"]["duration_s"] == 3.0
    assert "lease_seconds" not in d and d["lease"]["orphan_timeout_s"] > 0
    heat = d["commands"]["heat"]
    assert heat["motion"] and heat["hazard"] == ["heat"] and heat["lease"]
    assert heat["params"]["properties"]["target_c"]["unit"] == "C"
    assert d["commands"]["jog"]["local_only"] and d["commands"]["heat_off"]["allowed_when_locked"]
    assert d["commands"]["search_chip"]["job"] and d["commands"]["search_chip"]["deadline_s"]["default"] == 48 * 3600
    st = h.get("/exfoliation/api/state")[1]
    assert set(st) == {"service", "boot_id", "seq", "ts_unix", "demo", "connected", "busy", "locked",
                       "maintenance", "fault", "lease", "running", "last_command", "readings"}
    assert "devices" in st["readings"]
    # v1 is unchanged on the same port
    assert h.get("/api/describe")[1]["contract"] == 1


def test_commands_ids_duplicates_refusals_and_lease(ports):
    urls, _ = ports
    h = Http2(urls["stacking"])
    read = {"command_id": "brain-1", "action": "read_all", "params": {}}
    status, out = h.post("/stacking/api/command", read)
    assert status == 202 and out["command_id"] == "brain-1"
    assert record(h, "stacking", "brain-1")["result"] == "ok"
    status, out = h.post("/stacking/api/command", read)
    assert status == 200 and out["duplicate"] is True
    status, out = h.post("/stacking/api/command", {**read, "params": {"x": 1}})
    assert status == 409 and out["error"]["code"] == "id_reused"
    go = {"command_id": "brain-2", "action": "go_to_point", "params": {"point": "arm_right.park"}}
    status, out = h.post("/stacking/api/command", go)
    assert status == 409 and out["error"]["code"] == "lease_required"
    assert h.post("/stacking/api/lease", {})[0] == 200
    status, out = h.post("/stacking/api/lease", {}, client="someone-else")
    assert status == 409 and out["error"] == {"code": "lease_held", "message": out["error"]["message"],
                                              "retry": True}
    go["command_id"] = "brain-3"
    assert h.post("/stacking/api/command", go)[0] == 202
    assert record(h, "stacking", "brain-3")["result"] == "ok"
    for body, code in (({"command_id": "b-4", "action": "go_to_point", "point": "arm_right.park"}, "bad_params"),
                       ({"command_id": "b-5", "action": "nope", "params": {}}, "unknown_action"),
                       ({"action": "read_all", "params": {}}, "bad_request")):
        status, out = h.post("/stacking/api/command", body)
        assert status == 400 and out["error"]["code"] == code, body
    assert h.post("/stacking/api/command", read, client="")[1]["error"]["code"] == "bad_request"
    assert h.call("POST", "/stacking/api/command", read, ctype="text/plain")[0] == 415
    assert h.post("/stacking/api/lease", {"renew_only": True})[0] == 200
    assert h.call("DELETE", "/stacking/api/lease")[1]["holder"] is None
    status, out = h.post("/stacking/api/lease", {"renew_only": True})
    assert status == 409 and out["error"]["code"] == "lease_lost"
    assert h.get("/stacking/api/commands/nope")[0] == 404
    assert h.get("/stacking/api/nothing")[1]["error"]["code"] == "not_found"
    for _ in range(2):
        status, out = h.post("/stacking/api/stop", {"wait_s": 0}, client="anyone")
        assert status == 200 and out["stop"] == "confirmed"


def test_local_only_stays_refused_on_the_module_ports(ports):
    """A tunnel ends on loopback: the module ports never trust it (qumus-lab PR #7)."""
    urls, _ = ports
    h = Http2(urls["stacking"])
    h.post("/stacking/api/lease", {})
    body = {"command_id": "sw-1", "action": "set_switch", "params": {"device": "stage_vacuum", "on": True}}
    status, out = h.post("/stacking/api/command", body)
    assert status == 403 and out["error"]["code"] == "local_only"


def test_a_deadline_stops_a_slow_move_as_expired(ports, stack):
    urls, _ = ports
    h = Http2(urls["stacking"])
    action = stack.services["stacking"].module.actions["go_to_point"]

    def slow(ctx, params):  # a move that only ends when stopped
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            ctx.check()
            time.sleep(0.01)

    action.fn = slow
    h.post("/stacking/api/lease", {})
    body = {"command_id": "slow-1", "action": "go_to_point", "params": {"point": "arm_right.park"}, "deadline_s": 0.3}
    assert h.post("/stacking/api/command", body)[0] == 202
    assert h.get("/stacking/api/state")[1]["running"] == ["slow-1"]
    assert record(h, "stacking", "slow-1")["result"] == "expired"


def test_job_command_and_jobs_route(ports):
    urls, _ = ports
    h = Http2(urls["stacking"])
    h.post("/stacking/api/lease", {})
    body = {"command_id": "job-1", "action": "inspect_bench", "params": {}}
    assert h.post("/stacking/api/command", body)[0] == 202
    rec = record(h, "stacking", "job-1")
    assert rec["result"] == "ok"
    run_id = rec["data"]["run_id"]

    def finished():
        job = h.get(f"/stacking/api/jobs/{run_id}")[1]
        return job if job["state"] in ("succeeded", "failed", "cancelled") else None

    job = wait_for(finished, timeout=60)
    assert job["state"] == "succeeded" and job["action"] == "inspect_bench"
    assert h.get("/stacking/api/jobs/run_nope")[0] == 404


def _conformance():
    for folder in (os.environ.get("LAB_CONTRACT"), Path(__file__).resolve().parents[2] / "lab-contract"):
        if folder and (Path(folder) / "conformance" / "v2.py").is_file():
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            break
    return pytest.importorskip("conformance.v2")


def test_lab_contract_conformance(ports):
    v2 = _conformance()
    urls, _ = ports
    report = v2.run(urls["stacking"] + "/", demo=True, timeout_s=10.0)
    assert len(report.results) >= 16
    assert report.passed, [r for r in report.results if not r.ok]
