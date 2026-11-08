from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from qumus_lab.config import load_config
from qumus_lab.launcher import build_stack


@pytest.fixture
def cfg(tmp_path):
    return load_config(runs_dir=tmp_path / "runs")


@pytest.fixture
def stack(cfg):
    s = build_stack(cfg, demo=True, demo_speed=50.0)
    yield s
    s.safe_state("test teardown")
    s.close()


def wait_for(pred, timeout=5.0, step=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


def wait_done(svc, command_id, timeout=10.0):
    return wait_for(lambda: (svc.last_command or {}).get("id") == command_id and svc.last_command, timeout)


class Http:
    def __init__(self, base: str, client: str = "pytest"):
        self.base, self.client = base, client

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, json.loads(r.read())

    def post(self, path, body=None, *, client=None, ctype="application/json"):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        if ctype:
            req.add_header("Content-Type", ctype)
        if client is not False:
            req.add_header("X-Qumus-Client", client or self.client)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
