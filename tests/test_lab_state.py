"""qumus.db state tables (device / sample / event / zone) and the middle-board occupancy lock."""

from __future__ import annotations

import pytest

from qumus_lab.config import DEFAULT_DEMO_RUNS_DIR, DEFAULT_RUNS_DIR, load_config
from qumus_lab.launcher import build_stack
from qumus_lab.service.core import ApiError
from qumus_lab.state.store import LabStore, StoreError

from .conftest import wait_done


def _cmd(svc, body, client="agent", loopback=True):
    r = svc.command(body, client, loopback=loopback)
    return wait_done(svc, r["command_id"], timeout=20)


# ---------------------------------------------------------------- store


def test_store_refuses_the_other_bench(tmp_path):
    LabStore(tmp_path / "qumus.db", "real").close()
    with pytest.raises(StoreError):
        LabStore(tmp_path / "qumus.db", "demo")


def test_demo_records_go_to_their_own_folder(monkeypatch):
    monkeypatch.delenv("QUMUS_LAB_RUNS", raising=False)
    assert load_config(demo=True).runs_dir == DEFAULT_DEMO_RUNS_DIR
    assert load_config().runs_dir == DEFAULT_RUNS_DIR


def test_device_rows_keep_last_known_values_and_never_invent(tmp_path):
    st = LabStore(tmp_path / "qumus.db", "real")
    st.upsert_device("arm_left", "arm", module="exfoliation", online=False)
    row = st.get_device("arm_left")
    assert row["state"] is None and row["read_at"] is None and row["online"] is False
    st.upsert_device("arm_left", "arm", state={"x_mm": 1.0}, read_at=100.0, online=True)
    st.upsert_device("arm_left", "arm", online=False)  # restart: not read yet
    row = st.get_device("arm_left")
    assert row["state"] == {"x_mm": 1.0} and row["read_at"] == 100.0 and row["online"] is False
    st.set_device_conflict("arm_left", {"stored": 1, "read": 2})
    assert st.get_device("arm_left")["conflict"] == {"stored": 1, "read": 2}


def test_sample_rows(tmp_path):
    st = LabStore(tmp_path / "qumus.db", "real")
    st.upsert_sample("chip_001", "blank_chip", location="left_board.L3", location_source="manual",
                     props={"note": "中文 ok"})
    st.set_sample_location("chip_001", "arm_left", "inferred", held_by="arm_left")
    row = st.get_sample("chip_001")
    assert row["location"] == "arm_left" and row["held_by"] == "arm_left" and row["location_source"] == "inferred"
    assert row["location_checked"] is None and row["props"]["note"] == "中文 ok"
    with pytest.raises(StoreError):
        st.set_sample_location("chip_001", "x", "guess")
    with pytest.raises(StoreError):
        st.set_sample_location("nope", "x", "manual")


# ---------------------------------------------------------------- services write the tables


def test_services_write_device_and_event_rows(stack):
    store = stack.store
    rows = {r["id"]: r for r in store.list_devices()}
    assert set(stack.lab.devices) <= set(rows)
    assert all(r["state"] is None and r["read_at"] is None for r in rows.values())  # nothing read yet
    svc = stack.services["exfoliation"]
    assert _cmd(svc, {"action": "read_all"})["result"] == "ok"
    arm = store.get_device("arm_left")
    assert arm["online"] and arm["read_at"] and arm["state"]["y_mm"] == 220.0
    assert store.get_device("arm_right")["state"] is None  # other module not touched
    msgs = [e["message"] for e in store.list_events(module="exfoliation")]
    assert "service started" in msgs and "command done" in msgs


def test_heat_writes_heater_and_supply_rows(stack):
    svc = stack.services["exfoliation"]
    svc.heartbeat("agent")
    lc = _cmd(svc, {"action": "heat", "device": "exfoliation_heater", "target_c": 30, "timeout_s": 120, "volts": 6.5})
    assert lc["result"] == "ok"
    heater = stack.store.get_device("exfoliation_heater")
    assert heater["state"]["temp_c"] is not None
    assert stack.store.get_device("power_supply")["module"] == "shared"


# ---------------------------------------------------------------- middle-board lock


def test_middle_board_lock_across_modules(stack):
    left, right = stack.services["exfoliation"], stack.services["stacking"]
    assert left.describe()["zones"] == ["middle_board"] == right.describe()["zones"]
    left.heartbeat("agent")
    right.heartbeat("agent")
    with pytest.raises(ApiError) as e:  # entering without the lock
        left.command({"action": "go_to_point", "point": "arm_left.slot_S1"}, "agent", loopback=True)
    assert e.value.status == 409 and "zone_acquire" in e.value.message

    assert _cmd(left, {"action": "zone_acquire", "zone": "middle_board", "device": "arm_left"})["result"] == "ok"
    with pytest.raises(ApiError) as e:  # the other arm, through the other module
        right.command({"action": "zone_acquire", "zone": "middle_board", "device": "arm_right"}, "agent", loopback=True)
    assert e.value.status == 409 and "arm_left" in e.value.message
    with pytest.raises(ApiError):
        right.command({"action": "go_to_point", "point": "arm_right.slot_S1"}, "agent", loopback=True)

    assert _cmd(left, {"action": "go_to_point", "point": "arm_left.slot_S1"})["result"] == "ok"
    assert right.state()["zones"]["middle_board"] == {
        "holder": "arm_left", "since_unix": left.state()["zones"]["middle_board"]["since_unix"], "inside": ["arm_left"]}
    with pytest.raises(ApiError) as e:  # cannot give it back while inside
        left.command({"action": "zone_release", "zone": "middle_board", "device": "arm_left"}, "agent", loopback=True)
    assert e.value.status == 409 and "inside" in e.value.message

    assert _cmd(left, {"action": "go_to_point", "point": "arm_left.park"})["result"] == "ok"
    assert left.state()["zones"]["middle_board"]["holder"] is None  # left the zone: released
    assert any(ev["message"] == "zone released (arm left)" for ev in right.get_events(0)["events"])
    assert _cmd(right, {"action": "zone_acquire", "zone": "middle_board", "device": "arm_right"})["result"] == "ok"
    assert stack.store.zone_holders()["middle_board"]["holder"] == "arm_right"


def test_stopped_move_into_the_zone_keeps_the_arm_inside(cfg):
    stack = build_stack(cfg, demo=True, demo_speed=8.0, modules=["exfoliation"])
    try:
        svc = stack.services["exfoliation"]
        svc.heartbeat("agent")
        _cmd(svc, {"action": "zone_acquire", "zone": "middle_board", "device": "arm_left"})
        svc.command({"action": "go_to_point", "point": "arm_left.slot_S7"}, "agent", loopback=True)
        svc.stop(wait_s=10)
        assert svc.state()["zones"]["middle_board"]["inside"] == ["arm_left"]  # position unknown: still inside
    finally:
        stack.safe_state("t")
        stack.close()


def test_zone_holder_survives_a_restart_until_a_person_clears_it(cfg):
    s1 = build_stack(cfg, demo=True, demo_speed=50.0)
    try:
        svc = s1.services["exfoliation"]
        svc.heartbeat("agent")
        _cmd(svc, {"action": "zone_acquire", "zone": "middle_board", "device": "arm_left"})
        assert _cmd(svc, {"action": "go_to_point", "point": "arm_left.slot_S2"})["result"] == "ok"
    finally:
        s1.safe_state("t")
        s1.close()

    s2 = build_stack(cfg, demo=True, demo_speed=50.0)
    try:
        left, right = s2.services["exfoliation"], s2.services["stacking"]
        assert left.state()["zones"]["middle_board"] == {
            "holder": "arm_left", "since_unix": left.state()["zones"]["middle_board"]["since_unix"],
            "inside": ["arm_left"]}
        assert any(e["message"] == "zone held from before restart" for e in right.get_events(0)["events"])
        right.heartbeat("agent")
        with pytest.raises(ApiError):
            right.command({"action": "zone_acquire", "zone": "middle_board", "device": "arm_right"}, "agent",
                          loopback=True)
        with pytest.raises(ApiError) as e:
            right.command({"action": "zone_clear", "zone": "middle_board"}, "agent", loopback=False)
        assert e.value.status == 403
        assert _cmd(right, {"action": "zone_clear", "zone": "middle_board"}, client="person")["result"] == "ok"
        assert _cmd(right, {"action": "zone_acquire", "zone": "middle_board", "device": "arm_right"})["result"] == "ok"
    finally:
        s2.safe_state("t")
        s2.close()


def test_state_cli(tmp_path, capsys):
    from qumus_lab.state.__main__ import main as state_main

    runs = tmp_path / "runs"
    s = build_stack(load_config(runs_dir=runs), demo=True, demo_speed=50.0)
    try:
        _cmd(s.services["stacking"], {"action": "read", "device": "turret"})
    finally:
        s.safe_state("t")
        s.close()
    assert state_main(["--demo", "--runs-dir", str(runs), "devices"]) == 0
    assert state_main(["--demo", "--runs-dir", str(runs), "sample-set", "chip_001", "blank_chip", "left_board.L3"]) == 0
    assert state_main(["--demo", "--runs-dir", str(runs), "samples"]) == 0
    assert state_main(["--demo", "--runs-dir", str(runs), "events", "--limit", "500"]) == 0
    out = capsys.readouterr().out
    assert "turret" in out and "left_board.L3 (manual" in out and "sample.manual" in out
    with pytest.raises(StoreError):  # the demo file is never opened as the real lab
        state_main(["--runs-dir", str(runs), "devices"])
