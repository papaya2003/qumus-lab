"""SafetyGate: every command of every module is checked here before any driver is called.

The gate knows the limits (config/limits.yaml), the named points (config/points.yaml) and whether the
module runs in --demo. It returns the *resolved* plan values (e.g. which heat mode and voltage) so the
action cannot drift from what was checked.

Rules (contract v1 + brief B):
- local_only actions (jog, move_axis, raw switch) only from loopback; caps: arm/stage/gantry 0.5 mm, stamp
  0.1 mm, focus 50 um, switches auto-off <= 10 s, first heat <= 40 C.
- move_axis (type a target on the debug page): one axis, at most `*_target_max_*` away from the position just
  read (stamp x/y 1 mm, focus 500 um; a cap set to null in limits.yaml means no distance cap, which is how arm,
  stage, gantry and stamp rail are configured since 2026-11-13). Axis ranges still apply. Stamp z and arm z *down*
  keep the jog cap while the arm's min_z is not in limits.yaml: a crash there cannot be stopped in software.
- real mode: go_to_point needs a `source: measured` point (v1 numbers are starting guesses only);
  heating a heater whose limits are not measured is capped like the first heat.
- feeds never above the arm's max feed; axis targets inside their ranges.

Development mode (`dev_mode: true` in limits.yaml, operator decision 2026-11-13): the limits that only slow
development work are off: jog and typed-target distance caps, the switch auto-off cap, the first-heat caps,
`require_measured_for_motion`, and the debug page uses `dev_mode_feed_mm_min` instead of the slow jog / travel
feeds (never above the arm's max feed). Kept in development mode: heater max_c / overshoot cut-off / mandatory
timeout / orphan timeout, STOP ALL and stop on lease lapse, axis ranges, arm min_z, the stamp z floor of the
stacking ops, the middle-board zone lock, local-only actions, and the arm's max feed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import LabConfig


class Rejected(Exception):
    """Command refused by the gate. ``status`` is the HTTP status the service returns."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class GateContext:
    client: str
    loopback: bool
    demo: bool


class SafetyGate:
    def __init__(self, config: LabConfig, module: str, demo: bool):
        self.config = config
        self.module = module
        self.demo = demo
        self.limits = config.limits
        self.probe = self.limits.get("probe", {})
        self.points = config.points.get("points", {})
        self.dev_mode = bool(self.limits.get("dev_mode", False))

    # ------------------------------------------------------------------ helpers
    @property
    def real(self) -> bool:
        return not self.demo

    def _need_measured(self) -> bool:
        return self.real and not self.dev_mode and bool(self.limits.get("require_measured_for_motion", True))

    def check_local(self, local_only: bool, ctx: GateContext) -> None:
        if local_only and not ctx.loopback:
            raise Rejected("this action is local-only (debug page / probe on the lab PC)", 403)

    def point(self, device: str, name: str) -> dict:
        pts = self.points.get(device, {})
        if name not in pts:
            raise Rejected(f"unknown point {device}.{name}; known: {sorted(pts)}")
        return pts[name]

    def axis_range(self, device: str, axis: str) -> tuple[float | None, float | None, str]:
        a = self.limits.get("axes", {}).get(device, {}).get(axis, {})
        return a.get("min_mm"), a.get("max_mm"), a.get("source", "none")

    # ------------------------------------------------------------------ checks
    def check_jog(self, kind: str, device: str, axis: str, amount: float) -> float:
        if not math.isfinite(amount) or amount == 0:
            raise Rejected("jog amount must be a non-zero number")
        caps = {
            "arm": ("arm_jog_max_mm", 0.5),
            "stage": ("stage_jog_max_mm", 0.5),
            "manipulator": ("stamp_jog_max_mm", 0.1),
            "gantry": ("gantry_jog_max_mm", 0.5),
            "focus": ("focus_jog_max_um", 50),
            "tape_reels": ("reel_jog_max_deg", 10),
        }
        if kind not in caps:
            raise Rejected(f"{device} ({kind}) cannot be jogged")
        if self.dev_mode:
            return math.inf
        key, default = caps[kind]
        cap = float(self.probe.get(key, default))
        if kind == "arm" and axis == "z":
            arm_limits = self.limits.get("arms", {}).get(device, {})
            cap = float(arm_limits.get("probe_z_jog_max_mm", cap))
        if kind == "gantry" and axis in ("x", "z"):
            cap = float(self.probe.get(f"gantry_{axis}_jog_max_mm", cap))
        if kind == "manipulator" and axis == "z":
            cap = float(self.probe.get("stamp_z_jog_max_mm", cap))
        if not math.isfinite(cap) or cap <= 0:
            raise Rejected(f"invalid jog cap for {device}.{axis}")
        if abs(amount) > cap:
            unit = "um" if kind == "focus" else ("deg" if kind == "tape_reels" else "mm")
            raise Rejected(f"jog {amount} {unit} is above the cap {cap} {unit} for {device}")
        return cap

    def _target_cap(self, key: str, default: float) -> float:
        """A move_axis distance cap from limits.yaml; null there means no distance cap (math.inf)."""
        if key in self.probe and self.probe[key] is None:
            return math.inf
        cap = float(self.probe.get(key, default))
        if not math.isfinite(cap) or cap <= 0:
            raise Rejected(f"invalid move_axis cap {key}: {cap}")
        return cap

    def check_target_move(self, kind: str, device: str, axis: str, current: float, target: float) -> float:
        """move_axis: distance from the position just read must be within the per-kind cap. Returns the cap."""
        if not (math.isfinite(current) and math.isfinite(target)):
            raise Rejected("current position and target must be numbers")
        dist = target - current
        caps = {
            "arm": ("arm_target_max_mm", 10.0),
            "stage": ("stage_target_max_mm", 10.0),
            "manipulator": ("stamp_target_max_mm", 1.0),
            "gantry": ("gantry_target_max_mm", 10.0),
            "focus": ("focus_target_max_um", 500.0),
        }
        if kind not in caps:
            raise Rejected(f"{device} ({kind}) cannot be moved to a typed target")
        if self.dev_mode:
            return math.inf
        key, default = caps[kind]
        cap = self._target_cap(key, default)
        why = ""
        if kind == "manipulator" and axis == "rail":
            cap = self._target_cap("stamp_rail_target_max_mm", 10.0)
        elif kind == "manipulator" and axis == "z":
            cap, why = min(cap, float(self.probe.get("stamp_jog_max_mm", 0.1))), " (stamp z: jog cap, to protect the stamp)"
        elif kind == "arm" and axis == "z" and dist < 0:
            min_z = self.limits.get("arms", {}).get(device, {}).get("min_z_mm")
            if min_z is None:
                cap = min(cap, float(self.probe.get("arm_jog_max_mm", 0.5)))
                why = " (arm z down: jog cap until min_z_mm is measured in limits.yaml)"
        unit = "um" if kind == "focus" else "mm"
        if abs(dist) > cap + 1e-9:
            raise Rejected(f"{device}.{axis}: {current:.4g} -> {target:.4g} {unit} is {abs(dist):.4g} {unit}, "
                           f"above the cap {cap:g} {unit}{why}")
        return cap

    def check_axis_target(self, device: str, axis: str, target: float, *, for_point: bool) -> None:
        lo, hi, source = self.axis_range(device, axis)
        if lo is not None and hi is not None and not float(lo) <= target <= float(hi):
            raise Rejected(f"{device}.{axis} target {target} outside [{lo}, {hi}] ({source})")
        if for_point and self._need_measured() and (lo is None or hi is None) and axis != "rail":
            raise Rejected(f"{device}.{axis} has no measured range; go_to_point is refused in real mode")

    def check_point(self, device: str, name: str) -> dict:
        pt = self.point(device, name)
        if self._need_measured() and pt.get("source") != "measured":
            raise Rejected(
                f"point {device}.{name} is source: {pt.get('source')}; real motion to a point needs source: measured "
                "(measure it at L3, see docs/open-questions.md)", 409)
        return pt

    def arm_feed(self, device: str, feed: float | None = None) -> float:
        lim = self.limits.get("arms", {}).get(device, {})
        max_feed = float(lim.get("max_feed_mm_min", 1000))
        f = float(feed if feed is not None else lim.get("travel_feed_mm_min", 1000))
        if not 0 < f <= max_feed:
            raise Rejected(f"feed {f} mm/min outside (0, {max_feed}] for {device}")
        return f

    def debug_feed(self, device: str, kind: str, slow: float) -> float:
        """Feed for debug-page moves (jog, move_axis, go_to_point). Development mode: dev_mode_feed_mm_min."""
        if not self.dev_mode:
            return slow
        f = float(self.probe.get("dev_mode_feed_mm_min", 3000))
        if kind == "arm":
            f = min(f, float(self.limits.get("arms", {}).get(device, {}).get("max_feed_mm_min", 1000)))
        if not math.isfinite(f) or f <= 0:
            raise Rejected(f"invalid dev_mode_feed_mm_min: {f}")
        return f

    def check_arm_z(self, device: str, z: float) -> None:
        mz = self.limits.get("arms", {}).get(device, {}).get("min_z_mm")
        if mz is not None and z < float(mz):
            raise Rejected(f"{device} z {z} below min_z {mz}")

    def switch_auto_off(self, local_only_call: bool, auto_off_s: float | None) -> float | None:
        cap = float(self.probe.get("switch_auto_off_max_s", 10))
        default = float(self.limits.get("switches", {}).get("auto_off_default_s", cap))
        if local_only_call and self.dev_mode:
            if auto_off_s is None:
                return None
            val = float(auto_off_s)
            if not math.isfinite(val) or val <= 0:
                raise Rejected("auto_off_s must be a positive number")
            return val
        if local_only_call:
            val = default if auto_off_s is None else float(auto_off_s)
            if not 0 < val <= cap:
                raise Rejected(f"auto_off_s must be in (0, {cap}] for the probe/debug switch")
            return val
        return None if auto_off_s is None else float(auto_off_s)

    def check_heat(self, heater_dev, device: str, target_c: float, timeout_s: float, volts: float | None) -> dict:
        lim = self.limits.get("heaters", {}).get(device, {})
        max_c = float(lim.get("max_c", 60))
        max_timeout = float(lim.get("max_timeout_s", 1800))
        if not math.isfinite(target_c) or target_c <= 0:
            raise Rejected("target_c must be a positive number")
        if target_c > max_c:
            raise Rejected(f"{device}: target {target_c} C above max {max_c} C")
        if not 0 < timeout_s <= max_timeout:
            raise Rejected(f"{device}: timeout_s is mandatory and must be in (0, {max_timeout}]")
        first_heat = self.real and not self.dev_mode and lim.get("source") != "measured"
        default_v = float(self.probe.get("heat_max_volts", 6.5))
        max_v = float(lim.get("probe_max_volts", default_v))
        if not math.isfinite(max_v) or max_v <= 0:
            raise Rejected(f"{device}: invalid heater voltage cap")
        if first_heat:
            cap_c = float(self.probe.get("first_heat_max_c", 40))
            if target_c > cap_c:
                raise Rejected(f"{device}: heater not measured yet; first heats are capped at {cap_c} C")
            if timeout_s > float(self.probe.get("heat_timeout_max_s", 600)):
                raise Rejected(f"{device}: first heats need timeout_s <= {self.probe.get('heat_timeout_max_s', 600)}")
            v = min(default_v, max_v) if volts is None else float(volts)
            if not 0 < v <= max_v:
                raise Rejected(f"{device}: first heats use at most {max_v} V")
            return {"mode": "until", "volts": v}
        if volts is not None:
            if not 0 < float(volts) <= max_v:
                raise Rejected(f"{device}: explicit volts must be in (0, {max_v}]")
            return {"mode": "until", "volts": float(volts)}
        try:
            heater_dev.hold_voltage_for(target_c)
        except Exception as exc:  # noqa: BLE001
            raise Rejected(str(exc)) from exc
        return {"mode": "boost_hold", "volts": None}
