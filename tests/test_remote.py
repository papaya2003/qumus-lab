"""Remote work: live camera pictures (debug page + view-only page) and scripts\\update.bat (qumus_lab.update)."""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from qumus_lab.drivers.cameras import LivePreview
from qumus_lab.launcher import LAUNCHER_INFO, SHUTDOWN_REFUSED, handle_shutdown_request
from qumus_lab.service.http import make_server, serve_in_thread
from qumus_lab.service.viewer import load_or_create_key, make_viewer
from qumus_lab.update import FAILED, OK, REFUSED, Updater

from .conftest import wait_done, wait_for

CAMS = {"desk_camera_left", "desk_camera_right", "microscope_camera"}


def _req(port, method, path, *, cookie=None, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    if body is not None:
        h.setdefault("Content-Type", "application/json")
        h.setdefault("X-Qumus-Client", "outsider")
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = conn.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    conn.close()
    return out


@pytest.fixture
def viewer(stack, cfg):
    key = load_or_create_key(cfg.runs_dir)
    srv = make_viewer("127.0.0.1", 0, stack.services, key)
    serve_in_thread(srv)
    yield srv.server_address[1], key
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- view-only page


def test_viewer_needs_the_key_and_keeps_it_in_a_cookie(viewer, cfg):
    port, key = viewer
    assert len(key) >= 16 and (cfg.runs_dir / "viewer-key.txt").read_text(encoding="utf-8").strip() == key
    assert load_or_create_key(cfg.runs_dir) == key  # stable across restarts
    assert _req(port, "GET", "/")[0] == 403
    assert _req(port, "GET", "/?key=wrong-key-wrong-key")[0] == 403
    assert _req(port, "GET", "/cameras.json")[0] == 403
    status, headers, _ = _req(port, "GET", f"/?key={key}")
    assert status == 303 and headers["Location"] == "/"  # the key leaves the address bar
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"]
    status, _, page = _req(port, "GET", "/", cookie=cookie)
    assert status == 200 and b"View only" in page and b"STOP ALL" not in page


def test_viewer_shows_three_cameras_and_readings(viewer):
    port, key = viewer
    cookie = f"qumus_view={key}"
    cams = json.loads(_req(port, "GET", "/cameras.json", cookie=cookie)[2])["cameras"]
    assert {c["id"] for c in cams} == CAMS
    for cid in CAMS:
        status, headers, data = _req(port, "GET", f"/cam/{cid}", cookie=cookie)
        assert status == 200 and headers["Content-Type"] == "image/png" and data.startswith(b"\x89PNG")
    status_doc = json.loads(_req(port, "GET", "/status.json", cookie=cookie)[2])
    assert set(status_doc["modules"]) == {"exfoliation", "stacking"}
    assert "arm_left" in status_doc["modules"]["exfoliation"]["devices"]


def test_viewer_has_no_control_route(viewer, stack):
    """Even with the key: no POST of any kind, and the module APIs do not exist on this port."""
    port, key = viewer
    cookie = f"qumus_view={key}"
    svc = stack.services["exfoliation"]
    events_before = svc.get_events(0)["next"]
    for path in ("/api/command", "/api/stop", "/api/heartbeat", "/api/unlock", "/m/exfoliation/api/command",
                 "/m/exfoliation/api/stop", "/"):
        body = {"action": "heat", "device": "exfoliation_heater", "target_c": 30, "timeout_s": 60}
        assert _req(port, "POST", path, cookie=cookie, body=body)[0] == 405, path
    for method in ("PUT", "DELETE", "PATCH"):
        assert _req(port, method, "/m/exfoliation/api/command", cookie=cookie, body={})[0] == 405
    assert _req(port, "GET", "/m/exfoliation/api/state", cookie=cookie)[0] == 404
    assert _req(port, "GET", "/api/describe", cookie=cookie)[0] == 404
    assert svc.lease is None and svc.last_command is None
    assert svc.get_events(events_before)["events"] == []  # nothing reached the service


def test_debug_page_shows_the_cameras(stack):
    srv = make_server("127.0.0.1", 0, services=stack.services, web=True)
    serve_in_thread(srv)
    try:
        port = srv.server_address[1]
        cams = json.loads(_req(port, "GET", "/cameras.json")[2])["cameras"]
        assert {c["id"] for c in cams} == CAMS
        status, _, data = _req(port, "GET", "/cam/microscope_camera")
        assert status == 200 and data.startswith(b"\x89PNG")
        assert _req(port, "GET", "/cam/nope")[0] == 404
        page = _req(port, "GET", "/")[2]
        assert b'id="camgrid"' in page
    finally:
        srv.shutdown()
        srv.server_close()


def test_live_preview_runs_only_while_watched_and_capture_still_works(stack):
    cam = stack.lab.devices["desk_camera_left"]
    live = LivePreview(cam, fps=10, idle_s=0.3)
    got = live.latest()
    assert got["frame"].startswith(b"\x89PNG") and live.running
    r = cam.capture()  # shares the camera lock with the preview grabber
    assert (cam.capture_dir / f"{r['image_id']}.png").is_file()
    wait_for(lambda: not live.running, timeout=3)  # nobody asked for 0.3 s: grabbing stopped
    assert live.latest()["frame"] is not None and live.running  # a new viewer starts it again
    live.stop()


def test_preview_of_a_broken_camera_reports_the_error(stack):
    cam = stack.lab.devices["desk_camera_right"]

    def broken(*a, **k):
        raise OSError("camera unplugged")

    cam.backend.grab = broken
    live = LivePreview(cam, fps=10, idle_s=1)
    got = live.latest()
    assert got["frame"] is None and "camera unplugged" in got["error"]
    live.stop()


# ---------------------------------------------------------------- launcher restart rules


def _request(cfg):
    (cfg.runs_dir / "shutdown.request").write_text('{"by": "test"}', encoding="utf-8")


def test_launcher_stops_on_request_only_when_idle(stack, cfg):
    assert handle_shutdown_request(stack, cfg.runs_dir) is False  # no request
    _request(cfg)
    assert handle_shutdown_request(stack, cfg.runs_dir) is True
    assert not (cfg.runs_dir / "shutdown.request").exists()


@pytest.mark.parametrize("situation", ["control", "heating", "locked", "switch_on"])
def test_launcher_refuses_to_stop_while_in_use(stack, cfg, situation):
    svc = stack.services["exfoliation"]
    svc.heartbeat("agent")
    if situation == "heating":
        r = svc.command({"action": "heat", "device": "exfoliation_heater", "target_c": 30, "timeout_s": 300,
                         "volts": 6.5}, "agent", loopback=True)
        assert wait_done(svc, r["command_id"])["result"] == "ok"
    elif situation == "locked":
        stack.lab.backends.world.by_device["exfoliation_vacuum"].reply = False
        r = svc.command({"action": "set_switch", "device": "exfoliation_vacuum", "on": True}, "agent", loopback=True)
        wait_done(svc, r["command_id"])
    elif situation == "switch_on":
        r = svc.command({"action": "set_switch", "device": "exfoliation_fan", "on": True}, "agent", loopback=True)
        assert wait_done(svc, r["command_id"])["result"] == "ok"
    if situation != "control":
        svc.release("agent")
    _request(cfg)
    assert handle_shutdown_request(stack, cfg.runs_dir) is False
    reasons = " ".join(json.loads((cfg.runs_dir / SHUTDOWN_REFUSED).read_text(encoding="utf-8"))["reasons"])
    expected = {"control": "control held by agent", "heating": "exfoliation_heater: heater",
                "locked": "locked", "switch_on": "exfoliation_fan: switched on"}[situation]
    assert expected in reasons, reasons


# ---------------------------------------------------------------- update.bat (qumus_lab.update)


class FakeGit:
    def __init__(self, branch="main", dirty="", pull_rc=0, selfcheck_rc=0, changed=""):
        self.branch, self.dirty, self.pull_rc, self.selfcheck_rc, self.changed = branch, dirty, pull_rc, selfcheck_rc, changed
        self.calls: list[list[str]] = []
        self.head = "a" * 40

    def __call__(self, cmd, cwd):
        self.calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, self.branch
        if cmd[:2] == ["git", "status"]:
            return 0, self.dirty
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, self.head
        if cmd[:2] == ["git", "pull"]:
            if self.pull_rc == 0:
                self.head = "b" * 40
            return self.pull_rc, "pulled"
        if cmd[:2] == ["git", "log"]:
            return 0, "bbbbbbb new commit"
        if cmd[:2] == ["git", "diff"]:
            return 0, self.changed
        if cmd[1:3] == ["-m", "qumus_lab.selfcheck"]:
            return self.selfcheck_rc, "0 FAIL" if self.selfcheck_rc == 0 else "[FAIL] config"
        if cmd[1:3] == ["-m", "pip"]:
            return 0, "installed"
        raise AssertionError(cmd)

    def ran(self, word):
        return any(word in " ".join(c) for c in self.calls)


@pytest.fixture
def served(stack, cfg):
    """The demo stack behind real HTTP module services, plus the launcher's info file and stop loop."""
    servers = {}
    for name, svc in stack.services.items():
        srv = make_server("127.0.0.1", 0, service=svc)
        serve_in_thread(srv)
        servers[name] = srv
    info = {"pid": 1, "demo": True, "argv": ["--demo", "--demo-speed", "50"],
            "ports": {n: s.server_address[1] for n, s in servers.items()}}
    (cfg.runs_dir / LAUNCHER_INFO).write_text(json.dumps(info), encoding="utf-8")
    stopped = threading.Event()

    def launcher_loop():
        while not stopped.is_set():
            if handle_shutdown_request(stack, cfg.runs_dir):
                for srv in servers.values():
                    srv.shutdown()
                    srv.server_close()
                (cfg.runs_dir / LAUNCHER_INFO).unlink()
                stopped.set()
                return
            time.sleep(0.05)

    t = threading.Thread(target=launcher_loop, daemon=True)
    t.start()
    yield info, stopped
    if not stopped.is_set():
        stopped.set()
        t.join(1)
        for srv in servers.values():
            srv.shutdown()
            srv.server_close()


def _updater(cfg, git, log, spawned=None, get_json=None):
    kw = {}
    if get_json is not None:
        kw["get_json"] = get_json
    return Updater(runs_dirs=[cfg.runs_dir], python="py", run=git, out=log.append,
                   spawn=lambda cmd, cwd: spawned.append(cmd) if spawned is not None else None,
                   sleep=lambda s: time.sleep(min(s, 0.05)), stop_timeout_s=5, start_timeout_s=2, **kw)


def test_update_pulls_checks_and_restarts_in_the_same_mode(cfg, served):
    info, stopped = served
    git, log, spawned = FakeGit(), [], []
    real_get = Updater().get_json

    def get_json(url):  # after the restart the "new" launcher answers
        return {"busy": False} if spawned else real_get(url)

    rc = _updater(cfg, git, log, spawned, get_json).update()
    assert rc == OK, log
    assert stopped.is_set()  # the old launcher stopped through its own safe path
    assert git.ran("pull --ff-only origin main") and git.ran("qumus_lab.selfcheck")
    assert spawned == [["py", "-m", "qumus_lab.launcher", "--demo", "--demo-speed", "50"]]


def test_update_refuses_before_pulling_while_someone_holds_control(cfg, served, stack):
    stack.services["stacking"].heartbeat("qumus-agent")
    git, log = FakeGit(), []
    assert _updater(cfg, git, log).update() == REFUSED
    assert not git.ran("pull") and not served[1].is_set()
    assert "control held by qumus-agent" in "\n".join(log)


def test_update_respects_the_launchers_own_refusal(cfg, served, stack):
    """The state can change between the script's check and the stop: the launcher checks again."""
    git, log, spawned = FakeGit(), [], []
    up = _updater(cfg, git, log, spawned)
    stack.services["exfoliation"].heartbeat("agent")
    up.reasons = lambda running: []  # pretend the script's own check saw nothing
    assert up.update() == REFUSED
    assert spawned == [] and not served[1].is_set()
    assert "control held by agent" in "\n".join(log)


@pytest.mark.parametrize("git,why", [
    (FakeGit(branch="codex/fix-ports"), "not main"),
    (FakeGit(dirty=" M config/devices.yaml"), "not committed"),
    (FakeGit(pull_rc=1), "git pull"),
    (FakeGit(selfcheck_rc=1), "self-check has FAIL"),
])
def test_update_stops_early_and_leaves_services_alone(cfg, served, git, why):
    log, spawned = [], []
    assert _updater(cfg, git, log, spawned).update() == FAILED
    assert why in "\n".join(log)
    assert spawned == [] and not served[1].is_set()
    assert not (cfg.runs_dir / "shutdown.request").exists()


def test_update_reinstalls_when_dependencies_changed(cfg):
    git, log = FakeGit(changed="pyproject.toml\nqumus_lab/update.py"), []
    assert _updater(cfg, git, log).update() == OK  # nothing running: pull + check only
    assert git.ran("pip install -e .[lab,dev]")
    assert "not running" in "\n".join(log)
