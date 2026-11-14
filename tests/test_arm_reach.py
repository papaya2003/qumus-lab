"""The DexArm refuses an unreachable target with 'XY beyond limit.....' and then still answers 'ok'.

Bench, 2026-11-14: from stage_chip (X10 Y256 Z80), G0 X0 Y220 Z95 (v1 park) got that reply and the arm stayed put,
while the debug page reported the move as ok. At X0 Z95 the arm accepts Y223 and refuses Y222.
"""

from __future__ import annotations

import pytest

from qumus_lab.drivers.base import FirmwareError
from qumus_lab.drivers.gcode_arm import GcodeArm
from qumus_lab.sim.emulators import ArmEmulator, Clock
from qumus_lab.transport.fake_serial import FakeSerialFactory


def _arm(start=(10.0, 256.0, 80.0)):
    emu = ArmEmulator(Clock(1000.0), start=start)
    emu.reachable = lambda x, y, z: not (z >= 95 and (x * x + y * y) ** 0.5 < 223)  # the bench boundary at Z95
    a = GcodeArm("arm_left", {"port": {"port": "COM7"}, "open_settle_s": 0}, FakeSerialFactory({"COM7": emu}))
    a.connect()
    return a, emu


def test_unreachable_target_is_an_error_not_ok():
    a, _ = _arm()
    with pytest.raises(FirmwareError, match=r"X0 Y220 Z95 is outside the arm's reach.*did not move"):
        a.move_to(0, 220, 95, feed=3000)
    assert (a.pose["x"], a.pose["y"], a.pose["z"]) == (10.0, 256.0, 80.0)
    assert a.moving is False
    assert a.pose_trusted is True  # nothing moved: the reported pose is still right


def test_reachable_target_still_moves_after_a_refusal():
    a, _ = _arm()
    with pytest.raises(FirmwareError):
        a.move_to(0, 220, 95, feed=3000)
    snap = a.move_to(0, 230, 95, feed=3000)
    assert (snap["x_mm"], snap["y_mm"], snap["z_mm"]) == (0.0, 230.0, 95.0)

