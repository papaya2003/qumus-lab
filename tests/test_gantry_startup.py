"""Gantry startup and reply ordering regressions found against the working local v1."""

import time

import pytest

from qumus_lab.drivers.base import Interrupted
from qumus_lab.drivers.printer_gantry import PrinterGantry
from qumus_lab.sim.emulators import Clock, PrinterEmulator
from qumus_lab.transport.fake_serial import FakeSerialFactory


class MotorGatedPrinter(PrinterEmulator):
    def __init__(self):
        super().__init__(Clock(1000))
        self.enabled = False
        self.ignored_moves = 0

    def on_line(self, line):
        if line.startswith("M17"):
            self.enabled = True
        elif line.startswith("M18"):
            self.enabled = False
        elif line.startswith("G1 ") and not self.enabled:
            self.ignored_moves += 1
            self.ok()
            return
        super().on_line(line)


def _gantry(emulator):
    factory = FakeSerialFactory({"COM22": emulator})
    gantry = PrinterGantry("tape_gantry", {"port": {"port": "COM22", "timeout": 0.2},
                                          "open_settle_s": 0, "command_settle_s": 0}, factory)
    gantry.connect()
    return gantry, factory.opened["COM22"]


def test_first_move_enables_motors_but_connect_only_queries():
    emulator = MotorGatedPrinter()
    gantry, port = _gantry(emulator)
    try:
        assert bytes(port.tx) == b"M114\n"
        assert not emulator.enabled
        gantry.move(x=1, feed=300, relative=True)
        assert gantry.read()["x_mm"] == 1.0
        assert emulator.ignored_moves == 0
        lines = bytes(port.tx).decode().splitlines()
        assert lines.index("M17 X Y Z") < lines.index("G1 X1 F300")
        gantry.power(False)
        gantry.move(x=-1, feed=300, relative=True)
        assert gantry.read()["x_mm"] == 0.0
        assert bytes(port.tx).count(b"M17 X Y Z\n") == 2
    finally:
        gantry.close()


def test_stop_before_first_move_does_not_enable_or_move():
    emulator = MotorGatedPrinter()
    gantry, port = _gantry(emulator)
    try:
        gantry.stop()
        with pytest.raises(Interrupted):
            gantry.move(x=1, feed=300, relative=True)
        assert not emulator.enabled
        assert b"M17" not in port.tx and b"G1 X" not in port.tx
    finally:
        gantry.close()


def test_position_query_consumes_its_delayed_acknowledgement():
    class DelayedAckPrinter(PrinterEmulator):
        def on_line(self, line):
            if line == "M114":
                self.emit("X:0 Y:0 Z:0\n")
                self.emit("ok\n", delay=0.1)
            else:
                super().on_line(line)

    start = time.monotonic()
    gantry, port = _gantry(DelayedAckPrinter(Clock(1000)))
    try:
        assert time.monotonic() - start >= 0.08
        assert port.in_waiting == 0
    finally:
        gantry.close()


def test_stop_of_an_idle_gantry_sends_no_quick_stop():
    """Bench 2026-11-13: after M410 the Prusa acknowledged moves but its motors no longer turned until it was
    restarted. A finished move (M400 acknowledged) leaves nothing to stop, so no M410 is sent."""
    emulator = MotorGatedPrinter()
    gantry, port = _gantry(emulator)
    try:
        assert gantry.stop() is None  # connected, never moved
        gantry.clear_stop()
        gantry.move(x=-10, feed=300, relative=True)  # move() ends with M400
        assert gantry.motion_pending is False
        assert gantry.stop() is None
        assert b"M410" not in bytes(port.tx)
        assert gantry.quick_stopped is False
    finally:
        gantry.close()


def test_stop_while_a_move_may_run_still_sends_quick_stop():
    emulator = MotorGatedPrinter()
    gantry, port = _gantry(emulator)
    try:
        gantry.set_relative(True)
        gantry.line(x=-10, feed=300)  # queued, no M400 yet: the gantry may be moving
        assert gantry.motion_pending is True
        assert gantry.stop() is True
        assert bytes(port.tx).endswith(b"M410\n")
        assert gantry.quick_stopped is True
        assert "restart the Prusa" in gantry.last_error
    finally:
        gantry.close()
