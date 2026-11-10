"""SSH tunnels make remote requests arrive from 127.0.0.1.

So the module ports (8781/8782, the ones the brain reaches through a tunnel) must not trust a loopback
address: local_only actions and unlock are refused there even from 127.0.0.1. The debug page server (8780,
never tunnelled) still lets a person on the lab PC use them.
"""

from __future__ import annotations

import pytest

from qumus_lab.service.http import make_server, serve_in_thread

from .conftest import Http, wait_done

LOCAL_ONLY = [
    {"action": "jog", "device": "arm_left", "axis": "x", "mm": 0.1},
    {"action": "set_switch", "device": "exfoliation_vacuum", "on": True},
]


@pytest.fixture
def ports(stack):
    module_srv = make_server("127.0.0.1", 0, service=stack.services["exfoliation"])
    web_srv = make_server("127.0.0.1", 0, services=stack.services, web=True)
    for s in (module_srv, web_srv):
        serve_in_thread(s)
    yield (Http(f"http://127.0.0.1:{module_srv.server_address[1]}", client="probe"),
           Http(f"http://127.0.0.1:{web_srv.server_address[1]}/m/exfoliation", client="probe"))
    for s in (module_srv, web_srv):
        s.shutdown()
        s.server_close()


def test_module_port_refuses_local_only_from_loopback(ports):
    module, _ = ports
    for body in LOCAL_ONLY:
        status, r = module.post("/api/command", body)
        assert status == 403, (body, r)
        assert "local-only" in r["error"]


def test_module_port_refuses_unlock_from_loopback(ports, stack):
    module, _ = ports
    stack.services["exfoliation"].locked = True
    status, r = module.post("/api/unlock", {})
    assert status == 403
    assert stack.services["exfoliation"].locked is True


def test_module_port_still_serves_the_agent(ports):
    module, _ = ports
    assert module.get("/api/describe")[0] == 200
    status, r = module.post("/api/command", {"action": "read_all"})
    assert status == 202
    assert module.post("/api/stop", {})[0] == 200


def test_debug_page_keeps_local_tools(ports, stack):
    _, page = ports
    svc = stack.services["exfoliation"]
    assert page.post("/api/heartbeat", {})[0] == 200
    status, r = page.post("/api/command", {"action": "set_switch", "device": "exfoliation_vacuum", "on": True})
    assert status == 202, r
    wait_done(svc, r["command_id"])
    svc.locked = True
    status, _ = page.post("/api/unlock", {})
    assert status == 200 and svc.locked is False


def test_trust_loopback_can_be_set_explicitly(stack):
    srv = make_server("127.0.0.1", 0, service=stack.services["exfoliation"], trust_loopback=True)
    try:
        assert srv.RequestHandlerClass.trust_loopback is True
    finally:
        srv.server_close()
    srv = make_server("127.0.0.1", 0, services=stack.services, web=True, trust_loopback=False)
    try:
        assert srv.RequestHandlerClass.trust_loopback is False
    finally:
        srv.server_close()
