"""Vision port (v1 thresholds), demo microscope physics, heater manual hold, stacking safety caps."""

from __future__ import annotations

import threading

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from qumus_lab import vision  # noqa: E402
from qumus_lab.jobs.runner import StepFailed  # noqa: E402
from qumus_lab.launcher import build_stack  # noqa: E402
from qumus_lab.modules.base import ActionContext  # noqa: E402
from qumus_lab.ops import stacking as so  # noqa: E402
from qumus_lab.safety.gate import Rejected  # noqa: E402
from qumus_lab.sim.optics import TrueFlake, coverage, make_flakes  # noqa: E402


def _frame(w=960, h=540):
    return np.full((h, w, 3), (150, 92, 122), np.uint8)


def test_pixel_sizes_are_v1_code_values():
    assert vision.um_per_px(5) == pytest.approx(0.730)
    assert vision.um_per_px(20) == pytest.approx(0.730 / 4)
    assert vision.um_per_px(100) == pytest.approx(0.730 / 20)
    assert vision.um_per_px(5, 960) == pytest.approx(0.730 * 4)  # quarter-size demo frame


def test_first_touch_finds_a_round_change_and_ignores_none():
    ref = _frame()
    assert vision.first_touch(ref, ref.copy()) is None
    cur = ref.copy()
    cv2.circle(cur, (600, 200), 80, (180, 140, 128), -1)
    cx, cy = vision.first_touch(ref, cur)
    assert abs(cx - 600) <= 2 and abs(cy - 200) <= 2
    thin = ref.copy()
    cv2.rectangle(thin, (100, 100), (700, 130), (180, 140, 128), -1)  # long stripe: aspect outside 0.8-1.25
    assert vision.first_touch(ref, thin) is None


def test_contact_contour_inside_distance():
    ref = _frame()
    cur = ref.copy()
    cv2.circle(cur, (480, 200), 150, (180, 140, 128), -1)
    contour = vision.contact_contour(ref, cur)
    assert vision.inside_distance((480, 275), contour) > 60      # covered (SAFE_DIST 240 px on 3840 = 60 here)
    assert vision.inside_distance((900, 500), contour) == 0      # outside


def test_placeholder_detector_labels_like_the_sim_lab():
    img = _frame()
    cv2.circle(img, (300, 300), 20, (150 - 11, 92 - 11, 122 - 11), -1)
    found = vision.find_flakes(img, objective=5, material="Gr")
    assert found and found[0].label == "monolayer"
    assert vision.contrast_label(15, "hBN") == "10-20" and vision.contrast_label(-60, "Gr") == "manylayer"


def test_graphene_lifts_only_when_hbn_covers_80_percent():
    gr = TrueFlake("g", "graphene", (0.0, 0.0), [(-10, -10), (10, -10), (10, 10), (-10, 10)], (0, 0, 0), "mono")
    big = TrueFlake("h", "hBN", (0.0, 0.0), [(-30, -30), (30, -30), (30, 30), (-30, 30)], (0, 0, 0), "medium")
    half = TrueFlake("h", "hBN", (0.0, 0.0), [(0, -30), (30, -30), (30, 30), (0, 30)], (0, 0, 0), "medium")
    assert coverage(gr, [big], (0.0, 0.0)) == pytest.approx(1.0)
    assert coverage(gr, [half], (0.0, 0.0)) < 0.8


def test_demo_flakes_are_deterministic_and_inside_the_chip():
    a, b = make_flakes("chip_001", "hBN"), make_flakes("chip_001", "hBN")
    assert [f.centre for f in a] == [f.centre for f in b] and len(a) >= 4
    assert all(abs(f.centre[0]) < 3.8 and abs(f.centre[1]) < 2.8 for f in a)


@pytest.fixture
def stk(cfg):
    s = build_stack(cfg, demo=True, demo_speed=400.0)
    m = s.services["stacking"].module
    yield s, m, ActionContext(m, m.gate, None, threading.Event(), lambda *a: None)
    s.safe_state("teardown")
    s.close()


def test_drop_down_heater_keeps_boost_until_hold_now(stk):
    s, m, ctx = stk
    h = m.devices["transfer_heater"]
    h.heat(170, 600, auto_hold=False)
    import time
    t0 = time.monotonic()
    while h.read()["temp_c"] < 172 and time.monotonic() - t0 < 10:
        time.sleep(0.01)
    assert h.phase == "boost" and h.psu.set_v == 27.0       # past 170 C, still 27 V (v1)
    h.hold_now()
    # 16 V now; the fast demo heater may already be past target + overshoot_c, in which case the
    # control loop cuts the output (overshoot_off) right after the switch. Both are correct.
    assert h.psu.set_v == 16.0 and h.phase in ("hold", "overshoot_off")
    h.off("test")


def test_set_initial_temp_uses_v1_20_volts(stk):
    s, m, ctx = stk
    h = m.devices["transfer_heater"]
    h.heat(90, 600)
    assert h.psu.set_v == 20.0
    h.off("test")


def test_stamp_z_floor_stops_the_descent(stk):
    s, m, ctx = stk
    so._device_point(ctx, "stamp", "load")
    with pytest.raises(StepFailed, match="floor"):
        so._stamp_to(ctx, "z", 4.8)
    so._stamp_to(ctx, "z", 4.9)  # the floor itself (v1 drop_down z) is allowed


def test_real_mode_refuses_computed_stage_moves_and_sketch_points(cfg):
    from qumus_lab.safety.gate import SafetyGate

    gate = SafetyGate(cfg, "stacking", demo=False)
    with pytest.raises(Rejected):
        gate.check_point("arm_right", "right_stamp_1")      # source: sketch

    class M:  # minimal module stand-in: only the gate is consulted before any device
        pass
    m = M()
    m.gate = gate
    ctx = ActionContext(m, gate, None, threading.Event(), lambda *a: None)
    with pytest.raises(Rejected, match="measured range"):
        so._stage_xy(ctx, -12.0, 8.0)
