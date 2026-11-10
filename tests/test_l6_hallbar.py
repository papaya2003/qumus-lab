"""L6 scripted Hall bar against qumus-lab --demo over HTTP only: once with the module ports directly (same
computer), once through a TCP forwarder that stands in for the SSH tunnel (other computer, brain-side ends
18781 / 18782 in the framework plan; ephemeral ports here). Both runs must give the same result."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")

from qumus_lab.config import load_config  # noqa: E402
from qumus_lab.hallbar_script import Script, Station  # noqa: E402
from qumus_lab.launcher import build_stack  # noqa: E402
from qumus_lab.service.http import make_server, serve_in_thread  # noqa: E402
from qumus_lab.sim.bench import seed_demo_bench  # noqa: E402
from qumus_lab.tunnel_sim import Forwarder  # noqa: E402


@pytest.fixture
def lab(tmp_path):
    cfg = load_config(runs_dir=tmp_path / "runs", demo=True)
    st = build_stack(cfg, demo=True, demo_speed=400.0)
    seed_demo_bench(st.store)
    servers = {}
    for name, svc in st.services.items():
        srv = make_server("127.0.0.1", 0, service=svc)
        serve_in_thread(srv)
        servers[name] = srv
    yield st, {n: f"http://127.0.0.1:{s.server_address[1]}" for n, s in servers.items()}
    for s in servers.values():
        s.shutdown()
        s.server_close()
    st.safe_state("test end")
    st.close()


def _check_device(st, rep):
    assert rep["ok"], rep.get("error")
    assert [s["flow"] for s in rep["steps"]] == ["exfoliate_chips", "exfoliate_chips", "search_chip", "search_chip",
                                                 "stack_device", "cool_experiment_stages"]
    dev = st.store.get_sample(rep["device_chip"])
    assert dev["status"] == "device" and dev["props"]["stack"] == rep["stack"]
    for fid in rep["stack"]:
        f = st.store.get_sample(fid)
        assert f["status"] == "stacked" and f["location"] == rep["device_chip"]
    optics = st.services["stacking"].module.world.optics
    landed = optics.chips[rep["device_chip"]].dropped
    assert [f.material for f in landed] == ["hBN", "graphene"]
    for f in landed:  # electrode channel at the chip centre; 5x centring is ~3 um per pixel
        assert math.hypot(*f.centre) < 0.03, f.centre


def test_l6_hallbar_same_computer(lab):
    st, urls = lab
    rep = Script(Station("exfoliation", urls["exfoliation"]), Station("stacking", urls["stacking"]),
                 log=lambda *_: None, job_timeout_s=600).run()
    _check_device(st, rep)


def test_l6_hallbar_through_a_tunnel(lab):
    st, urls = lab
    fwd = {n: Forwarder(0, "127.0.0.1", int(u.rsplit(":", 1)[1])).start() for n, u in urls.items()}
    try:
        tun = {n: f"http://127.0.0.1:{f.port}" for n, f in fwd.items()}
        # the tunnel end is loopback, but local-only actions stay refused there (PR #7)
        req = urllib.request.Request(tun["stacking"] + "/api/command", method="POST",
                                     data=json.dumps({"action": "jog", "device": "stamp", "axis": "z",
                                                      "mm": 0.01}).encode(),
                                     headers={"X-Qumus-Client": "brain", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 403
        rep = Script(Station("exfoliation", tun["exfoliation"]), Station("stacking", tun["stacking"]),
                     log=lambda *_: None, job_timeout_s=600).run()
        _check_device(st, rep)
        assert all(f.connections > 0 for f in fwd.values())
    finally:
        for f in fwd.values():
            f.close()
