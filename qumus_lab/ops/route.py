"""Arm travel routes: lift straight up, go round the travel ring, go down straight (operator decision 2026-11-14).

The left arm can not pass over the tape (it stands tall beside the exfoliation stage), so it travels on a ring of
named points close to its base (``routes:`` in config/points.yaml): high (ring z) and at about the shortest radius
the arm reaches. Every named point is served by one ring point (left board -> travel_L, stage -> park, middle board
-> travel_R). A route from where the arm is to a target point is:

1. leave: straight up at the current x/y to the leave height of where the arm is (its point's approach z; slowly
   where the config says so, e.g. off the stage);
2. go to the ring point that serves the start, walk the ring point by point to the ring point that serves the
   target (never a shortcut);
3. go to above the target (its approach z), then straight down (slowly where the config says so).

Start and target served by the same ring point (slot -> slot on one board, stage_chip -> stage_carrier): lift, move
across at the higher of the two approach heights, go down; no ring.

v1 had no ring: every sequence went through its park point X0 Y220 Z95, which this arm can not reach (PR #21), so
on the bench the arm swung straight through the tape. This module only plans; ops/common.py and the go_to_point
action execute the steps with the usual gate checks.
"""

from __future__ import annotations

import fnmatch
import math
from typing import NamedTuple

from ..safety.gate import Rejected

NEAR_MM = 60.0  # a pose within this x/y distance of a named point counts as "at" that point


class Step(NamedTuple):
    x: float
    y: float
    z: float
    slow: bool    # straight up / down at the slow feed
    point: str    # the named point this move belongs to ("" = none: a lift where the arm already is)
    kind: str     # "lift" | "ring" | "above" | "down" | "across"


def config(points_cfg: dict, arm_id: str) -> dict | None:
    """The arm's route config (points.yaml ``routes.<arm>``), or None when the arm has no ring."""
    return ((points_cfg or {}).get("routes") or {}).get(arm_id)


def _xyz(pt: dict) -> tuple[float, float, float]:
    x, y, z = (float(v) for v in pt["xyz"])
    return x, y, z


def ring_point_for(cfg: dict, name: str) -> str:
    """The ring point that serves a named point (a ring point serves itself)."""
    if name in cfg["ring"]:
        return name
    for ring_pt, patterns in (cfg.get("serve") or {}).items():
        if any(fnmatch.fnmatchcase(name, p) for p in patterns):
            return ring_pt
    raise Rejected(f"no travel route to {name}: add it to routes.serve in config/points.yaml")


def _is_slow(cfg: dict, name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in cfg.get("slow", []))


def where(cfg: dict, points: dict, pose: dict) -> tuple[str, str]:
    """(kind, name) of where the arm is: ("ring", ring point) when it is at a ring point, else ("point", the
    nearest served named point). Rejected when the pose is unknown or far from every known point."""
    if pose.get("x") is None or pose.get("y") is None or pose.get("z") is None:
        raise Rejected("the arm position is unknown: press Read first")
    px, py = float(pose["x"]), float(pose["y"])
    best = None
    for name, pt in points.items():
        if "xyz" not in pt:
            continue
        x, y, _ = _xyz(pt)
        d = math.hypot(px - x, py - y)
        if best is None or d < best[0]:
            best = (d, name)
    if best is None or best[0] > NEAR_MM:
        raise Rejected(f"the arm (x {px:g}, y {py:g}) is far from every named point: move it near one with the "
                       "position boxes first")
    name = best[1]
    if name in cfg["ring"]:
        return "ring", name
    ring_point_for(cfg, name)  # raises when the point has no route
    return "point", name


def _ring_walk(cfg: dict, a: str, b: str) -> list[str]:
    ring = list(cfg["ring"])
    i, j = ring.index(a), ring.index(b)
    return ring[i:j + 1] if i <= j else list(reversed(ring[j:i + 1]))


def plan(points_cfg: dict, arm_id: str, pose: dict, target: str, *, dx: float = 0.0, dy: float = 0.0,
         stop_above: bool = False) -> list[Step]:
    """Steps from ``pose`` to the named point ``target`` (+ dx, dy). ``stop_above`` ends at the target's approach
    height. Raises Rejected when the arm has no route config, the pose is unknown or a point has no route."""
    cfg = config(points_cfg, arm_id)
    if cfg is None:
        raise Rejected(f"{arm_id} has no travel route (routes in config/points.yaml)")
    points = points_cfg["points"][arm_id]
    ring_z = float(cfg["ring_z"])
    if target not in points:
        raise Rejected(f"unknown point {arm_id}.{target}")
    tx, ty, tz = _xyz(points[target])
    tx, ty = tx + dx, ty + dy
    t_ring = ring_point_for(cfg, target)
    t_is_ring = target in cfg["ring"]
    t_az = float(points[target].get("approach_z", ring_z if not t_is_ring else tz))
    t_slow = _is_slow(cfg, target)

    kind, here = where(cfg, points, pose)
    px, py, pz = float(pose["x"]), float(pose["y"]), float(pose["z"])
    steps: list[Step] = []

    if kind == "point":
        h_pt = points[here]
        leave_z = float(h_pt.get("approach_z", ring_z))
        s_ring = ring_point_for(cfg, here)
        h_slow = _is_slow(cfg, here)
        if s_ring == t_ring and not t_is_ring:
            # same area (one board, or the stage): lift, across at the higher approach height, down
            across = max(leave_z, t_az)
            if pz < across - 1e-6:
                steps.append(Step(px, py, across, h_slow, "", "lift"))
            steps.append(Step(tx, ty, across, False, target, "across"))
            if not stop_above and abs(tz - across) > 1e-6:
                steps.append(Step(tx, ty, tz, t_slow, target, "down"))
            return steps
        if pz < leave_z - 1e-6:
            steps.append(Step(px, py, leave_z, h_slow, "", "lift"))
    else:
        s_ring = here
        if pz < ring_z - 1e-6:
            steps.append(Step(px, py, ring_z, False, "", "lift"))

    for name in _ring_walk(cfg, s_ring, t_ring):
        x, y, _ = _xyz(points[name])
        steps.append(Step(x, y, ring_z, False, name, "ring"))
    if t_is_ring:
        if abs(tz - ring_z) > 1e-6:
            steps.append(Step(tx, ty, tz, False, target, "down"))
        return steps
    steps.append(Step(tx, ty, t_az, False, target, "above"))
    if not stop_above and abs(tz - t_az) > 1e-6:
        steps.append(Step(tx, ty, tz, t_slow, target, "down"))
    return steps
