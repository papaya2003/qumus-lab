"""Fault injection for the safety paths (self-review 11-08). Each test first proves its fault fired.

Heaters: an unexpected error in the control thread, a dead control thread, a heater off that cannot be
confirmed, a supply found on at start, heat_off while the module is locked.
Switches: an 'on' whose reply is lost, an auto-off that cannot be confirmed.
Stop: a stop that lands between the last cancel check and the next motion command (stage, Kinesis),
and a stop in the middle of the gantry's 100-segment arc.
"""

from __future__ import annotations

import threading
import time

import pytest

from qumus_lab.drivers.base import Interrupted
from qumus_lab.launcher import build_stack

from .conftest import wait_done, wait_for


def _heat(svc, device):
    svc.heartbeat("agent")
    r = svc.command({"action": "heat", "device": device, "target_c": 30, "timeout_s": 300, "volts": 6.5},
                    "agent", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "ok"


# ---------------------------------------------------------------- heaters


def test_unexpected_error_in_heater_thread_switches_the_supply_off(stack):
    """OSError from the Dracal exe (e.g. WinError 5) is not a DriverError: the thread used to die with
    the supply on, and nothing else would ever switch it off."""
    heater = stack.lab.devices["exfoliation_heater"]
    world = stack.lab.backends.world
    fired = []

    def broken_runner(argv, timeout_s):
        fired.append(argv)
        raise PermissionError(5, "Access is denied")

    _heat(stack.services["exfoliation"], "exfoliation_heater")
    assert world.psu.output is True
    heater.thermo.runner = broken_runner
    wait_for(lambda: fired, timeout=5)
    wait_for(lambda: world.psu.output is False and heater.phase == "off", timeout=5)
    assert "PermissionError" in (heater.last_off_reason or "")


def test_watchdog_switches_off_a_heater_whose_thread_died(stack):
    heater = stack.lab.devices["exfoliation_heater"]
    world = stack.lab.backends.world
    called = []

    def dead_loop(*args):  # the control thread ends at once without switching anything off
        called.append(args)

    heater._loop = dead_loop
    _heat(stack.services["exfoliation"], "exfoliation_heater")
    wait_for(lambda: called and not heater.heating, timeout=5)  # fault fired: the control thread is gone
    wait_for(lambda: world.psu.output is False and heater.phase == "off", timeout=5)
    assert "control thread" in heater.last_off_reason
    assert any(e["message"] == "heater control thread stopped" for e in stack.services["exfoliation"].get_events(0)["events"])


def test_unconfirmed_heater_off_locks_and_safe_state_still_finishes(stack):
    svc = stack.services["exfoliation"]
    heater = stack.lab.devices["exfoliation_heater"]
    world = stack.lab.backends.world
    _heat(svc, "exfoliation_heater")
    r = svc.command({"action": "set_switch", "device": "exfoliation_fan", "on": True}, "agent", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "ok"
    raised = []

    def broken(*a, **k):
        raised.append(a)
        raise OSError("port gone")

    heater.psu.output = broken
    heater.relay.set = broken
    out = svc.safe_state("test")  # must not raise, and must still switch the fan off
    assert raised
    assert out["stop"] == "unconfirmed"
    assert svc.locked and svc.fault["code"] == "heat_off_unconfirmed"
    assert world.by_device["exfoliation_fan"].on is False


def test_supply_found_on_at_start_is_switched_off(cfg):
    stack = build_stack(cfg, demo=True, demo_speed=50.0, connect=False)
    try:
        psu = stack.lab.backends.world.psu
        psu.voltage, psu.output = 27.0, True  # a previous run died while heating
        stack.lab.connect_all()
        assert psu.output is False
        reasons = [stack.lab.devices[h].last_off_reason or "" for h in ("exfoliation_heater", "transfer_heater")]
        assert any("found on" in r for r in reasons), reasons
    finally:
        stack.close()


def test_heat_off_is_allowed_while_the_module_is_locked(stack):
    svc = stack.services["exfoliation"]
    world = stack.lab.backends.world
    _heat(svc, "exfoliation_heater")
    world.by_device["exfoliation_vacuum"].reply = False
    r = svc.command({"action": "set_switch", "device": "exfoliation_vacuum", "on": True}, "agent", loopback=True)
    wait_done(svc, r["command_id"])
    assert svc.locked  # fault fired
    r = svc.command({"action": "heat_off", "device": "exfoliation_heater"}, "agent", loopback=False)
    assert wait_done(svc, r["command_id"])["result"] == "ok"
    assert world.psu.output is False


def test_switch_whose_on_reply_is_lost_still_turns_itself_off(stack):
    """The auto-off timer used to be armed only after a confirmed 'on': a lost reply left the relay on."""
    svc = stack.services["exfoliation"]
    relay = stack.lab.backends.world.by_device["exfoliation_vacuum"]
    relay.reply = False
    svc.heartbeat("agent")
    r = svc.command({"action": "set_switch", "device": "exfoliation_vacuum", "on": True, "auto_off_s": 2.0},
                    "agent", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "failed"
    assert svc.locked and relay.history == [True]  # fault fired: the relay is on, unconfirmed
    wait_for(lambda: relay.on is False, timeout=5)


def test_failed_switch_auto_off_locks_the_module(stack):
    svc = stack.services["exfoliation"]
    relay = stack.lab.backends.world.by_device["exfoliation_vacuum"]
    svc.heartbeat("agent")
    r = svc.command({"action": "set_switch", "device": "exfoliation_vacuum", "on": True, "auto_off_s": 0.5},
                    "agent", loopback=True)
    assert wait_done(svc, r["command_id"])["result"] == "ok"
    assert not svc.locked
    relay.reply = False  # the off is sent but never confirmed
    wait_for(lambda: any(e["message"] == "switch_auto_off_failed" for e in svc.get_events(0)["events"]),
             timeout=3)  # fault fired
    wait_for(lambda: svc.locked, timeout=2)
    assert svc.fault["code"] == "switch_auto_off_failed"


# ---------------------------------------------------------------- stop races


def test_stop_between_check_and_stage_move_is_not_lost(cfg):
    stack = build_stack(cfg, demo=True, demo_speed=1.0, modules=["stacking"])
    try:
        stage = stack.lab.devices["sample_stage"]
        link = stage.link
        orig = link.read_steps
        fired = []

        def read_then_stop(channel):  # move_to reads the position, then sends D...; stop lands in between
            v = orig(channel)
            if not fired:
                fired.append(channel)
                stage.stop()
            return v

        link.read_steps = read_then_stop
        stage.clear_stop()
        with pytest.raises(Interrupted):
            stage.move_to("rail", 80.0)
        assert fired
        time.sleep(0.5)
        link.read_steps = orig
        assert abs(stack.lab.backends.world.by_device["sample_stage"].steps("L")) < 3280  # under 1 mm
    finally:
        stack.close()


def test_stop_while_a_kinesis_move_is_being_sent_still_stops_it(cfg):
    stack = build_stack(cfg, demo=True, demo_speed=1.0, modules=["stacking"])
    try:
        stamp = stack.lab.devices["stamp"]
        cube = stamp.axes["z"].backend
        orig = cube.move_to
        fired = []

        def stop_then_move(mm, timeout_ms):  # StopImmediate arrives just before the move command
            fired.append(mm)
            stamp.stop()
            orig(mm, timeout_ms)

        cube.move_to = stop_then_move
        stamp.clear_stop()
        try:
            stamp.move_to("z", 1.0)
        except Interrupted:
            pass
        assert fired
        time.sleep(0.5)
        assert not cube.is_moving()
        assert abs(cube.position() - 6.5) < 0.1  # 5.5 mm move at 0.5 mm/s: it was stopped at once
    finally:
        stack.close()


def test_stop_during_the_gantry_arc_stops_sending_segments(cfg):
    stack = build_stack(cfg, demo=True, demo_speed=50.0, modules=["exfoliation"])
    try:
        svc = stack.services["exfoliation"]
        gantry = stack.lab.devices["tape_gantry"]
        orig = gantry.send
        sent_after_stop, stopped = [], threading.Event()
        segments = []

        def send(cmd, **kw):
            if cmd.startswith("G1 X") and "Z" in cmd:
                segments.append(cmd)
                if stopped.is_set():
                    sent_after_stop.append(cmd)
                if len(segments) == 10:
                    stopped.set()
                    svc.stop(wait_s=0)
            return orig(cmd, **kw)

        gantry.send = send
        svc.heartbeat("agent")
        r = svc.command({"action": "apply_tape"}, "agent", loopback=True)
        lc = wait_done(svc, r["command_id"], timeout=30)
        assert stopped.is_set()  # fault fired
        assert sent_after_stop == []
        assert lc["result"] == "stopped"
    finally:
        stack.safe_state("t")
        stack.close()
