"""Shared pieces of the L4 operations: arm moves at v1 feeds, pick/place, places and the sample ledger."""

from __future__ import annotations

import fnmatch
import math
import time

from ..modules.base import ActionContext
from ..safety.gate import Rejected

# v1 feeds (common/hardware/robot_arm_controller.py): travel G0 F5000, final approach F1000, massage G1 F500
TRAVEL_F = 5000.0   # robot_arm_controller.py:208
SLOW_F = 1000.0     # robot_arm_controller.py:219
MASSAGE_F = 500.0   # robot_arm_controller.py:405
PICK_DWELL_MS = 300  # robot_arm_controller.py:189 (G4 P300)


# ------------------------------------------------------------------------------------------- places
def places(ctx: ActionContext) -> dict:
    return (ctx.module.config.places or {}).get("places", {})


def place_point(ctx: ActionContext, place: str, arm: str) -> tuple[str, dict]:
    """(point name, point) of a storage place for this arm; Rejected if the arm can not reach it."""
    p = places(ctx).get(place)
    if p is None:
        raise Rejected(f"unknown place {place!r}; known: {sorted(places(ctx))}")
    name = p.get(arm)
    if not name:
        raise Rejected(f"{arm} can not reach {place} (config/places.yaml)")
    return name, ctx.gate.check_point(arm, name)


def places_for(ctx: ActionContext, arm: str, *, station: str | None = None, holds: str | None = None) -> list[str]:
    return [k for k, v in places(ctx).items() if v.get(arm) and (station is None or v.get("station") == station)
            and (holds is None or v.get("holds") == holds)]


# ------------------------------------------------------------------------------------------- arm moves
def arm(ctx: ActionContext, arm_id: str):
    return ctx.device(arm_id, ("arm",))


def move(ctx: ActionContext, arm_id: str, x=None, y=None, z=None, feed: float = TRAVEL_F, *, linear: bool = False,
         point: str | None = None) -> dict:
    """One G0 (or G1) move, gate-checked. ``point`` = the named point the move belongs to: a point inside a
    zone takes the zone lock first; an ok move to a point outside releases it. Moves inside a zone always
    name their point; a move with no point changes nothing in the zone registry."""
    ctx.check()
    f = ctx.gate.arm_feed(arm_id, feed)
    if z is not None:
        ctx.gate.check_arm_z(arm_id, float(z))
    dev = arm(ctx, arm_id)
    zones = ctx.module.zones
    qualified = f"{arm_id}.{point}" if point else None
    if zones is not None and qualified and zones.zone_of_point(qualified):
        _ensure_zone(ctx, arm_id, zones.zone_of_point(qualified))
        zones.check_enter(arm_id, qualified)
        zones.move_started(arm_id, qualified)
        ok = False
        try:
            out = dev.move_to(x, y, z, f, linear=linear)
            ok = True
            return out
        finally:
            zones.move_finished(arm_id, qualified, ok)
    out = dev.move_to(x, y, z, f, linear=linear)
    if zones is not None and qualified is not None:
        zones.move_finished(arm_id, qualified, True)  # ok move to a point outside every zone: the arm left
    return out


def _ensure_zone(ctx: ActionContext, arm_id: str, zone: str) -> None:
    holder = ctx.module.zones.view([zone]).get(zone, {}).get("holder")
    if holder != arm_id:
        ctx.module.zones.acquire(zone, arm_id)  # 409 Rejected if the other arm holds it


def move_point(ctx: ActionContext, arm_id: str, point: str, *, z: float | None = None, feed: float = TRAVEL_F,
               dx: float = 0.0, dy: float = 0.0) -> dict:
    """Move to a named point (x, y from the point + offset; z = the point's z unless given)."""
    pt = ctx.gate.check_point(arm_id, point)
    x, y, pz = (float(v) for v in pt["xyz"])
    return move(ctx, arm_id, x + dx, y + dy, pz if z is None else z, feed, point=point)


def pick_item(ctx: ActionContext, arm_id: str) -> None:
    """v1 pick_up_item (robot_arm_controller.py:186-190): M888 P2, M1000, G4 P300, M400."""
    dev = arm(ctx, arm_id)
    dev.set_vacuum(True)
    dev.dwell(PICK_DWELL_MS)
    dev.finish_moves(expected_s=1.0)


def place_item(ctx: ActionContext, arm_id: str) -> None:
    """v1 place_item (robot_arm_controller.py:192-199): M888 P2, M1002, M1001, G4 P300, M1003, M400."""
    arm(ctx, arm_id).release_item()


def rotate(ctx: ActionContext, arm_id: str, deg: float) -> None:
    """v1 rotate_nose: M888 P6, M2100, M2101 R<int>: turns the nozzle BY deg (relative), as v1 did."""
    ctx.check()
    arm(ctx, arm_id).rotate_nozzle(float(deg))


def park(ctx: ActionContext, arm_id: str) -> dict:
    """v1 home_to_exfoliation_stage (X0 Y220 Z95) / home_to_search_stage (X0 Y260 Z140), G0 F5000."""
    return move_point(ctx, arm_id, "park")


def slot_angle(pt: dict, key: str = "stage_nozzle_deg") -> float | None:
    """A slot's measured nozzle angle a (points.yaml header): how far to turn the nozzle (relative, M2101 R)
    after picking from this slot so the object sits square on the work stage; going back turns -a. None = not
    measured (v1 behaviour)."""
    v = pt.get(key)
    return None if v is None else float(v)


def store_offset(pt: dict, dx: float, dy: float) -> tuple[float, float]:
    """v1 stored chips a little beside the slot centre it picked from (e.g. x+10, y-10). A measured slot point
    is where the object sits, so it gets no offset."""
    return (0.0, 0.0) if pt.get("source") == "measured" else (dx, dy)


def store_rotation(x: float, y: float, sx: float, sy: float) -> float:
    """v1 nose angle for storing (robot_arm_controller.py:286-299): |90 - angle between (x, y) and (sx, sy)|."""
    n1, n2 = math.hypot(x, y), math.hypot(sx, sy)
    if n1 == 0 or n2 == 0:
        raise Rejected(f"cannot compute nozzle rotation from ({x}, {y}) and ({sx}, {sy})")
    cosang = max(-1.0, min(1.0, (x * sx + y * sy) / (n1 * n2)))
    return abs(90.0 - math.degrees(math.acos(cosang)))


# ------------------------------------------------------------------------------------------- sample ledger
def sample_at(ctx: ActionContext, place: str) -> dict | None:
    store = getattr(ctx.module, "store", None) or _store(ctx)
    if store is None:
        return None
    for s in store.list_samples():
        if s["location"] == place:
            return s
    return None


def _store(ctx: ActionContext):
    svc = getattr(ctx, "service", None)
    if svc is not None:
        return svc.store
    return getattr(ctx.module, "lab_store", None)


def move_sample(ctx: ActionContext, from_place: str, to_place: str, *, kind: str = "chip",
                held_by: str | None = None, sample_id: str | None = None) -> str | None:
    """The ledger follows the object (source ``inferred``: the arm moved it, no camera check yet)."""
    store = _store(ctx)
    if store is None:
        return None
    s = store.get_sample(sample_id) if sample_id else sample_at(ctx, from_place)
    if s is None:
        return None
    store.set_sample_location(s["id"], to_place, "inferred", checked=time.time(), held_by=held_by)
    return s["id"]


def update_sample(ctx: ActionContext, sample_id: str, *, status: str | None = None, props: dict | None = None,
                  kind: str | None = None, location: str | None = None) -> None:
    store = _store(ctx)
    if store is None or not sample_id:
        return
    old = store.get_sample(sample_id) or {}
    store.upsert_sample(sample_id, kind or old.get("kind") or "chip",
                        location=location if location is not None else old.get("location"),
                        status=status if status is not None else old.get("status"),
                        props=dict(old.get("props") or {}, **(props or {})),
                        location_source="inferred" if location is not None else old.get("location_source"),
                        location_checked=old.get("location_checked"), held_by=old.get("held_by"))


def free_places(ctx: ActionContext, arm_id: str, *, station: str, holds: str = "chip") -> list[str]:
    store = _store(ctx)
    used = {s["location"] for s in store.list_samples()} if store is not None else set()
    booked = set(store.reservations("slot")) if store is not None else set()
    return [p for p in places_for(ctx, arm_id, station=station, holds=holds) if p not in used and p not in booked]


def match(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# ------------------------------------------------------------------------------------------- waiting
def sleep(ctx: ActionContext, seconds: float) -> None:
    """Wait, waking at once on stop (works for single commands and jobs)."""
    from ..modules.base import Cancelled

    scale = float(getattr(ctx.module, "time_scale", 1.0) or 1.0)
    if ctx.cancel.wait(max(0.0, float(seconds)) / scale):
        raise Cancelled("stopped")


def wait_until(ctx: ActionContext, pred, timeout_s: float, poll_s: float = 0.5, what: str = ""):
    from ..jobs.runner import StepFailed

    deadline = time.monotonic() + float(timeout_s)
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            raise StepFailed(f"timed out after {timeout_s:.0f} s waiting for {what or 'a condition'}")
        sleep(ctx, poll_s)


def step(ctx: ActionContext, name: str, fn):
    """A checkpoint inside a job; a plain call inside a single command."""
    if hasattr(ctx, "step") and getattr(ctx, "run_id", ""):
        return ctx.step(name, fn)
    ctx.check()
    out = fn()
    ctx.log("op step done", {"step": name})
    return out
