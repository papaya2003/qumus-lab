"""known_state: on Take control the debug page puts every unknown on/off into its safe state (2026-11-13).

Relays, the arm pump and the printer's motor enable cannot be queried, so after connecting they read null.
"""

from __future__ import annotations

from .conftest import wait_done


def _cmd(svc, body, client="page"):
    svc.heartbeat(client)
    r = svc.command(body, client, loopback=True)
    return wait_done(svc, r["command_id"])


def _dev(svc, dev_id):
    return svc.state()["devices"][dev_id]


def test_unknown_switches_become_safe_and_known(stack):
    exf, stk = stack.services["exfoliation"], stack.services["stacking"]
    assert _dev(exf, "exfoliation_fan")["on"] is None and _dev(exf, "arm_left")["vacuum_on"] is None
    lc = _cmd(exf, {"action": "known_state"})
    assert lc["result"] == "ok", lc
    assert _dev(exf, "exfoliation_fan")["on"] is False and _dev(exf, "exfoliation_vacuum")["on"] is False
    assert _dev(exf, "arm_left")["vacuum_on"] is False
    assert _dev(exf, "tape_gantry")["motors_on"] is False
    assert _cmd(stk, {"action": "known_state"})["result"] == "ok"
    assert _dev(stk, "stamp_vacuum")["on"] is True   # safe state on: holds the stamp slide
    assert _dev(stk, "stage_vacuum")["on"] is False


def test_known_switches_are_left_alone(stack):
    exf = stack.services["exfoliation"]
    assert _cmd(exf, {"action": "set_switch", "device": "exfoliation_fan", "on": True, "auto_off_s": 10})["result"] == "ok"
    assert _cmd(exf, {"action": "known_state"})["result"] == "ok"
    assert _dev(exf, "exfoliation_fan")["on"] is True


def test_known_state_is_local_and_needs_control(stack):
    d = stack.services["exfoliation"].describe()["commands"]["known_state"]
    assert d["local_only"] is True and d["any_client"] is False and d["motion"] is False
