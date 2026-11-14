"""Byte-for-byte comparison: what v1 wrote to each port vs what the new drivers write, same operation.

Each scenario runs the operation twice on fresh emulators: once through the unchanged v1 class
(tests/v1_harness.py, needs the v1 tree) and once through the new driver. The v1 bytes are also
stored in tests/golden/v1_bytes.json so the comparison still runs where v1 is absent (lab PC, CI).
Regenerate the golden file with QUMUS_REGEN_GOLDEN=1 (needs v1).

Deliberate differences (the v1 hazards) are tested separately at the bottom.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qumus_lab.drivers.focus_tmcl import TmclFocus
from qumus_lab.drivers.gcode_arm import GcodeArm
from qumus_lab.drivers.kz100 import KZ100Link, KZ100Stage
from qumus_lab.drivers.power_supply import TP3005P
from qumus_lab.drivers.printer_gantry import PrinterGantry
from qumus_lab.drivers.relay import RelaySwitch
from qumus_lab.drivers.tape_reels import TapeReels
from qumus_lab.drivers.thermometer import dracal_argv
from qumus_lab.drivers.turret import Turret
from qumus_lab.sim.emulators import (
    ArmEmulator,
    Clock,
    KZ100Emulator,
    PrinterEmulator,
    ReelsEmulator,
    RelayEmulator,
    TmclEmulator,
    TP3005PEmulator,
    TurretEmulator,
)
from qumus_lab.transport.fake_serial import FakeSerialFactory
from qumus_lab.transport.serial_port import PortSpec

from .v1_harness import PortRecorder, load_v1, patched_serial, v1_available, v1_missing

GOLDEN = Path(__file__).parent / "golden" / "v1_bytes.json"
FAST = Clock(1000.0)


def _emus():
    return {
        "COM7": ArmEmulator(FAST, start=(0.0, 220.0, 95.0)),
        "COM4": RelayEmulator(),
        "COM8": TP3005PEmulator(),
        "COM3": ReelsEmulator(),
        "COM22": PrinterEmulator(FAST),
        "COM9": KZ100Emulator(FAST, start_steps={"X": 12800, "Y": -6400, "L": 0}),
        "COM11": TmclEmulator(),
        "COM10": TurretEmulator(),
    }


def _arm(f):
    a = GcodeArm("arm_left", {"port": {"port": "COM7", "baudrate": 115200, "timeout": 0.1}, "open_settle_s": 0}, f)
    a.connect()
    a.move_to(0, 220, 95, feed=5000)  # sends the one-off G21/G90 so the scenario compares the op only
    return a


def _gantry(f):
    g = PrinterGantry("tape_gantry", {"port": {"port": "COM22", "baudrate": 115200, "timeout": 2}, "open_settle_s": 0}, f)
    g.connect()
    g.power(True)  # v1's constructor already enabled motors; compare the operation from the same state
    return g


def _stage(f):
    s = KZ100Stage("sample_stage", {"port": {"port": "COM9", "baudrate": 57600, "timeout": 3},
                                     "axes": {"x": {"channel": "X", "steps_per_mm": 12800},
                                              "y": {"channel": "Y", "steps_per_mm": 12800},
                                              "rail": {"channel": "L", "steps_per_mm": 3280}}}, f)
    s.connect()
    return s


# Each scenario: (port, v1 setup(mods) -> obj, v1 op(obj), new setup(factory) -> obj, new op(obj))
SCENARIOS = {
    "arm_move_to": ("COM7",
                    lambda m: m["robot_arm_controller"].RobotArmController("COM7"),
                    lambda o: o.move_to(10.0, 256.0, 80, speed=1000),
                    _arm, lambda a: a.move_to(10.0, 256.0, 80, feed=1000)),
    "arm_park_left": ("COM7",
                      lambda m: m["robot_arm_controller"].RobotArmController("COM7"),
                      lambda o: o.home_to_exfoliation_stage(),
                      _arm, lambda a: a.move_to(0, 220, 95, feed=5000)),
    "arm_pick": ("COM7",
                 lambda m: m["robot_arm_controller"].RobotArmController("COM7"),
                 lambda o: o.pick_up_item(),
                 _arm, lambda a: (a.set_vacuum(True), a.dwell(300), a.finish_moves())),
    "arm_place": ("COM7",
                  lambda m: m["robot_arm_controller"].RobotArmController("COM7"),
                  lambda o: o.place_item(),
                  _arm, lambda a: a.release_item()),
    # arm_rotate_nozzle: deliberate difference, see test_arm_rotate_returns_to_the_pump_module
    "arm_vacuum_on_off": ("COM7",
                          lambda m: m["robot_arm_controller"].RobotArmController("COM7"),
                          lambda o: (o.vacuum_on(), o.vacuum_off()),
                          _arm, lambda a: (a.set_vacuum(True), a.read(), a.set_vacuum(False), a.read())),
    "relay_on_off": ("COM4",
                     lambda m: m["relay_controller"].RelayController("COM4"),
                     lambda o: (o.on(), o.off()),
                     lambda f: _connected(RelaySwitch("v", {"port": {"port": "COM4"}, "open_settle_s": 0}, f)),
                     lambda r: (r.set(True), r.set(False))),
    "psu_heat_sequence": ("COM8",
                          lambda m: m["power_controller"].TP3005PController("COM8"),
                          lambda o: (o.set_output(27.0), o.turn_on(), o.set_output(6.5), o.turn_off()),
                          lambda f: _connected(TP3005P("psu", {"port": {"port": "COM8"}, "open_settle_s": 0,
                                                               "reply_window_s": 0.01}, f)),
                          lambda p: (p.set_voltage(27.0), p.output(True), p.set_voltage(6.5), p.output(False))),
    "reels_release_new_tape": ("COM3",
                               lambda m: _reels_v1(m),
                               lambda o: o.release_new_tape(30),
                               lambda f: _connected(TapeReels("reels", {"port": {"port": "COM3"}, "open_settle_s": 0}, f)),
                               lambda r: (r.power(1, True), r.power(2, False), r.rotate(1, False, 30.0), r.power(2, True))),
    "reels_adjust_negative": ("COM3",
                              lambda m: _reels_v1(m),
                              lambda o: o.adjust_mother_tape_position(-12.5),
                              lambda f: _connected(TapeReels("reels", {"port": {"port": "COM3"}, "open_settle_s": 0}, f)),
                              lambda r: (r.power(2, True), r.power(1, False), r.rotate(2, True, 12.5), r.power(1, True))),
    "gantry_apply_tape": ("COM22",
                          lambda m: _printer_v1(m),
                          lambda o: o.apply_tape(),
                          _gantry,
                          lambda g: (g.read(), g.set_relative(True), g.line(x=-140, feed=1000), g.finish(),
                                     g.arc_relative(end_x=-110, end_z=-160, centre_x=80, centre_z=-170, feed=1000,
                                                    clockwise=True), g.finish(), g.set_relative(False))),
    "gantry_peel_tape": ("COM22",
                         lambda m: _printer_v1(m),
                         lambda o: o.exfoliate_tape(500.0),
                         _gantry,
                         lambda g: (g.set_relative(True),
                                    g.arc_relative(end_x=110, end_z=160, centre_x=190, centre_z=-10, feed=500.0,
                                                   clockwise=False), g.finish(), g.line(x=140, feed=1000),
                                    g.finish(), g.set_relative(False))),
    "gantry_power": ("COM22",
                     lambda m: _printer_v1(m),
                     lambda o: (o.power_off(), o.power_on()),
                     _gantry, lambda g: (g.power(False), g.power(True))),
    "stage_move_axis_to": ("COM9",
                           lambda m: m["sample_PDV_controller"].SamplePDVController("COM9"),
                           lambda o: (o.move_axis_to("x", -12), o.move_axis_to("y", 8), o.move_L_axis_to_mm(80.0)),
                           _stage,
                           lambda s: (s.move_to("x", -12, wait=False), s.move_to("y", 8, wait=False),
                                      s.move_to("rail", 80.0, wait=False))),
    "stage_read_stop": ("COM9",
                        lambda m: m["sample_PDV_controller"].SamplePDVController("COM9"),
                        lambda o: (o.get_display("X"), o.get_status("M"), o.stop(), o.set_max_speed("X", 5000)),
                        _stage,
                        lambda s: (s.link.read_steps("X"), s.link.read_status("M"), s.link.stop_all(),
                                   s.set_max_speed("x", 5000))),
    "focus_moves_and_stop": ("COM11",
                             lambda m: m["focus_controller"].FocusController("COM11"),
                             lambda o: (o.move_relative_um(100), o.move_relative_um(-250.5), o.move_relative_mm(1.2),
                                        o.stop()),
                             lambda f: _connected(TmclFocus("focus", {"port": {"port": "COM11", "baudrate": 115200}}, f)),
                             lambda d: (d.move_by_um(100), d.move_by_um(-250.5), d.move_by_um(1200.0), d.stop())),
    "turret_rotate": ("COM10",
                      lambda m: m["turret_controller"].TurretController("COM10"),
                      lambda o: (o.rotate_to_position(3), o.rotate_to_position(1)),
                      lambda f: _connected(Turret("turret", {"port": {"port": "COM10"}, "settle_s": 0}, f)),
                      lambda t: (t.rotate_to(3), t.rotate_to(1))),
}


def _connected(dev):
    dev.connect()
    return dev


def _reels_v1(m):
    o = m["motor_controller"].DualMotorController("COM3")
    return o


def _printer_v1(m):
    o = m["printer_controller"].SharedPrinterController("COM22")
    return o


def _run_v1(name, monkeypatch) -> bytes:
    port, v1_setup, v1_op, _, _ = SCENARIOS[name]
    rec = PortRecorder(_emus())
    patched_serial(monkeypatch, rec)
    mods = load_v1()
    obj = v1_setup(mods)
    rec.clear()
    v1_op(obj)
    return bytes(rec.tx[port])


def _run_new(name) -> bytes:
    port, _, _, new_setup, new_op = SCENARIOS[name]
    factory = FakeSerialFactory(_emus())
    obj = new_setup(factory)
    fs = factory.opened[port]
    start = len(fs.tx)
    new_op(obj)
    return bytes(fs.tx[start:])


def _golden() -> dict:
    if GOLDEN.is_file():
        return json.loads(GOLDEN.read_text(encoding="utf-8"))
    return {}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_new_driver_matches_golden_v1_bytes(name):
    golden = _golden()
    assert name in golden, f"no golden bytes for {name}; run QUMUS_REGEN_GOLDEN=1 pytest (needs v1)"
    assert _run_new(name) == golden[name].encode("latin-1")


@pytest.mark.skipif(not v1_available(), reason=str(v1_missing()))
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_new_driver_matches_live_v1(name, monkeypatch):
    v1 = _run_v1(name, monkeypatch)
    new = _run_new(name)
    assert new == v1, f"\nv1 : {v1!r}\nnew: {new!r}"
    if os.environ.get("QUMUS_REGEN_GOLDEN"):
        data = _golden()
        data[name] = v1.decode("latin-1")
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


@pytest.mark.skipif(not v1_available(), reason=str(v1_missing()))
def test_dracal_command_line_matches_v1(monkeypatch):
    mods = load_v1()
    seen = {}

    def fake_check_output(argv, **kw):
        seen["argv"] = argv
        return "23.5"

    monkeypatch.setattr(mods["temp_controller"].subprocess, "check_output", fake_check_output)
    t = mods["temp_controller"].TemperatureController(exe_path="X.exe")
    t.read_temperature(stage="transfer")
    assert seen["argv"] == dracal_argv("X.exe", "E30753", "0", "C")


# ----------------------------------------------------------------------- deliberate differences (hazards)
@pytest.mark.skipif(not v1_available(), reason=str(v1_missing()))
def test_arm_connect_v1_powers_motors_new_only_queries(monkeypatch):
    rec = PortRecorder(_emus())
    patched_serial(monkeypatch, rec)
    load_v1()["robot_arm_controller"].RobotArmController("COM7")
    assert bytes(rec.tx["COM7"]) == b"G21\r\nG90\r\nM17\r\nM114\r\n"
    factory = FakeSerialFactory(_emus())
    GcodeArm("a", {"port": {"port": "COM7"}, "open_settle_s": 0}, factory).connect()
    assert bytes(factory.opened["COM7"].tx) == b"M114\r\n"


@pytest.mark.skipif(not v1_available(), reason=str(v1_missing()))
def test_reels_and_printer_connect_send_nothing_but_queries(monkeypatch):
    rec = PortRecorder(_emus())
    patched_serial(monkeypatch, rec)
    mods = load_v1()
    mods["motor_controller"].DualMotorController("COM3")
    mods["printer_controller"].SharedPrinterController("COM22")
    assert bytes(rec.tx["COM3"]) == b"MOTOR1_ON\nMOTOR2_ON\n"
    assert bytes(rec.tx["COM22"]) == b"M17 X Y Z\n"
    factory = FakeSerialFactory(_emus())
    TapeReels("r", {"port": {"port": "COM3"}, "open_settle_s": 0}, factory).connect()
    PrinterGantry("g", {"port": {"port": "COM22"}, "open_settle_s": 0}, factory).connect()
    assert bytes(factory.opened["COM3"].tx) == b""
    assert bytes(factory.opened["COM22"].tx) == b"M114\n"


def test_arm_first_move_sends_units_and_absolute_once():
    factory = FakeSerialFactory(_emus())
    a = GcodeArm("a", {"port": {"port": "COM7"}, "open_settle_s": 0}, factory)
    a.connect()
    a.move_to(1, 220, 95, feed=500)
    a.move_to(2, 220, 95, feed=500)
    lines = bytes(factory.opened["COM7"].tx).decode().split("\r\n")
    assert lines.count("G21") == 1 and lines.count("G90") == 1
    assert lines.index("G90") < lines.index("G0 X1 Y220 Z95 F500")


def test_arm_rotate_returns_to_the_pump_module():
    """v1 rotate_nose left the DexArm in front-end module P6 (rotary), so later moves used that module's tool offset
    and went to the wrong XY until the next pump command (M888 P2) shifted the arm. The new driver sends v1's bytes,
    then M888 P2 and M400 (wait for the switch), then reads the pose."""
    factory = FakeSerialFactory(_emus())
    a = GcodeArm("a", {"port": {"port": "COM7"}, "open_settle_s": 0}, factory)
    a.connect()
    factory.opened["COM7"].tx.clear()
    a.rotate_nozzle(50)
    assert _golden().get("arm_rotate_nozzle", "M888 P6\r\nM2100\r\nM2101 R50\r\nM114\r\n") == "M888 P6\r\nM2100\r\nM2101 R50\r\nM114\r\n"  # what v1 sent
    assert bytes(factory.opened["COM7"].tx).decode() == "M888 P6\r\nM2100\r\nM2101 R50\r\nM888 P2\r\nM400\r\nM114\r\n"
    assert a.nozzle_turned_deg == 50


def test_kz100_link_keeps_port_open_same_bytes():
    emu = KZ100Emulator(FAST)
    factory = FakeSerialFactory({"COM9": emu})
    link = KZ100Link(PortSpec("COM9", 57600, 0.5), factory)
    link.open()
    link.move_steps("X", 12800)
    assert emu.commands == ["DX12800;"]
    assert len(factory.opened) == 1
