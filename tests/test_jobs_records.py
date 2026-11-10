"""Job base and the brain <-> lab records (simlab docs/interface.md shapes)."""

from __future__ import annotations

import time

import pytest

from qumus_lab.launcher import build_stack
from qumus_lab.sim.bench import DEMO_SAMPLES, seed_demo_bench

from .conftest import wait_for


@pytest.fixture
def fast(cfg):
    s = build_stack(cfg, demo=True, demo_speed=400.0)
    yield s
    s.safe_state("test teardown")
    s.close()


def test_records_feed_has_head_lab_id_and_sample_rows(fast):
    seed_demo_bench(fast.store)
    page = fast.services["stacking"].records(0, 2000)
    assert page["lab_id"] and page["head"] >= page["next"] > 0
    keys = {c["key"] for c in page["changes"] if c["table"] == "sample"}
    assert {s[0] for s in DEMO_SAMPLES} <= keys
    nxt = fast.services["exfoliation"].records(page["next"], 10)
    assert nxt["changes"] == [] and nxt["next"] == page["next"]


def test_put_records_upserts_removes_and_takes_project_lists(fast):
    svc = fast.services["stacking"]
    svc.heartbeat("brain")
    out = svc.command({"action": "put_records", "project": [{"id": "prj_1", "name": "demo", "state": {"a": 1}}],
                       "samples": [{"id": "flake_x", "kind": "flake", "location": "chip_001", "status": "found",
                                    "props": {"area_um2": 10}}],
                       "reservations": [{"kind": "slot", "key": "mid_chip_3", "run_id": "r", "flow": "f"}]},
                      "brain", loopback=False)
    assert out["command_id"]
    wait_for(lambda: fast.store.get_sample("flake_x"))
    assert fast.store.get_project("prj_1")["state"]["a"] == 1
    assert "mid_chip_3" in fast.store.reservations("slot")
    svc.command({"action": "put_records", "removed_samples": ["flake_x"],
                 "reservations": [{"kind": "slot", "key": "mid_chip_3", "released": True}]}, "brain", loopback=False)
    wait_for(lambda: fast.store.get_sample("flake_x") is None)
    assert "mid_chip_3" not in fast.store.reservations("slot")
    rows = [c for c in fast.store.changes(0, 2000)["changes"] if c["key"] == "flake_x"]
    assert rows[-1]["row"] is None


def test_job_answers_run_id_at_once_and_the_run_is_in_the_feed(fast):
    seed_demo_bench(fast.store)
    svc = fast.services["stacking"]
    svc.heartbeat("brain")
    out = svc.command({"action": "cool_experiment_stages", "job": {"project_id": "prj_1"}}, "brain", loopback=False)
    rid = out["run_id"]
    assert out["result"] == {"run_id": rid, "flow": "cool_experiment_stages"}
    assert svc.last_command["data"]["run_id"] == rid
    assert any(c["table"] == "run" and c["key"] == rid for c in fast.store.changes(0, 5000)["changes"])

    def done():
        svc.heartbeat("brain")
        return fast.store.get_run(rid)["status"] in ("succeeded", "failed")
    wait_for(done, timeout=30)
    st = svc.command({"action": "job_status", "run_id": rid}, "brain", loopback=False)
    assert st["command_id"]
    status = wait_for(lambda: svc.last_command.get("action") == "job_status" and svc.last_command, 5)
    assert status["result"] == "ok"
    run = fast.store.get_run(rid)
    assert run["status"] == "succeeded" and run["project_id"] == "prj_1"
    steps = fast.store.get_steps(rid)
    assert steps and all(s["status"] == "done" for s in steps)


def test_job_names_match_the_simulated_lab(fast):
    jobs = {n: sorted(a.name for a in svc.module.actions.values() if a.job) for n, svc in fast.services.items()}
    assert jobs["exfoliation"] == ["cool_experiment_stages", "exfoliate_chips", "inspect_bench"]
    assert {"place_chip_on_transfer_stage", "search_chip", "revisit_chip", "stack_device",
            "cool_experiment_stages", "inspect_bench"} <= set(jobs["stacking"])


def test_stop_cancels_the_running_job(fast):
    seed_demo_bench(fast.store)
    svc = fast.services["exfoliation"]
    svc.heartbeat("brain")
    rid = svc.command({"action": "exfoliate_chips", "material": "hBN"}, "brain", loopback=False)["run_id"]
    wait_for(lambda: fast.store.get_run(rid)["status"] == "running", timeout=10)
    time.sleep(0.2)
    svc.stop(reason="test")
    wait_for(lambda: fast.store.get_run(rid)["status"] == "cancelled", timeout=20)
