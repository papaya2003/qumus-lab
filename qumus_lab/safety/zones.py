"""Occupancy locks for shared zones (the middle board, where both arms reach).

An arm must hold a zone before ``go_to_point`` may send it to a point inside that zone. One holder at
a time, across both modules (both services run in this process and share one registry).

- ``acquire(zone, device)``: 409 if the other arm holds it.
- Entering: the precheck refuses a zone point without the lock (409). From the moment the move starts
  the arm counts as *inside* (also if the move is stopped or fails: its position is then unknown).
- Leaving: when a ``go_to_point`` to a point outside the zone finishes ``ok``, the arm is outside and the
  lock is released automatically.
- ``release(zone, device)``: refused while the arm is inside (move it out first).
- ``clear(zone)``: a person on the lab PC, after looking at the bench (local only).
- The holder is stored in qumus.db. After a restart a stored holder stays held and its arm counts as
  inside, because nobody knows where the arm is; a person clears it or the arm moves out.

Zones are configured in config/points.yaml (``zones:``) with point patterns such as ``arm_left.slot_S*``.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from collections.abc import Callable

from .gate import Rejected


class ZoneRegistry:
    def __init__(self, zones_cfg: dict | None, store=None, emit: Callable[[str, dict, str], None] | None = None):
        self._lock = threading.Lock()
        self.zones: dict[str, dict] = {}
        for name, z in (zones_cfg or {}).items():
            self.zones[name] = {"devices": list(z.get("devices", [])), "points": list(z.get("points", [])),
                                "description": z.get("description", "")}
        self.store = store
        self._emit = emit
        self.holder: dict[str, dict | None] = {name: None for name in self.zones}
        self.inside: dict[str, str] = {}  # device -> zone
        if store is not None:
            for name, row in store.zone_holders().items():
                if name in self.zones and row.get("holder"):
                    self.holder[name] = {"device": row["holder"], "since_unix": row.get("since_unix")}
                    self.inside[row["holder"]] = name
                    self.emit("zone held from before restart", {"zone": name, "device": row["holder"]}, "warning")

    def emit(self, message: str, fields: dict, level: str = "info") -> None:
        if self._emit is not None:
            self._emit(message, fields, level)

    # ------------------------------------------------------------------ lookup
    def zone_of_point(self, point: str) -> str | None:
        for name, z in self.zones.items():
            if any(fnmatch.fnmatchcase(point, pat) for pat in z["points"]):
                return name
        return None

    def zones_for_devices(self, device_ids) -> list[str]:
        ids = set(device_ids)
        return [n for n, z in self.zones.items() if ids & set(z["devices"])]

    def _check_zone(self, zone: str, device: str) -> None:
        if zone not in self.zones:
            raise Rejected(f"unknown zone {zone!r}; zones: {sorted(self.zones)}")
        if device not in self.zones[zone]["devices"]:
            raise Rejected(f"{device} does not use zone {zone}")

    def view(self, zones: list[str] | None = None) -> dict:
        with self._lock:
            out = {}
            for name in zones if zones is not None else self.zones:
                h = self.holder.get(name)
                out[name] = {"holder": h and h["device"], "since_unix": h and h["since_unix"],
                             "inside": sorted(d for d, z in self.inside.items() if z == name)}
            return out

    # ------------------------------------------------------------------ lock / unlock
    def _set(self, zone: str, holder: dict | None) -> None:
        self.holder[zone] = holder
        if self.store is not None:
            self.store.set_zone_holder(zone, holder and holder["device"], holder and holder["since_unix"])

    def acquire(self, zone: str, device: str) -> dict:
        self._check_zone(zone, device)
        with self._lock:
            h = self.holder[zone]
            if h is not None and h["device"] != device:
                raise Rejected(f"{zone} is held by {h['device']}", 409)
            if h is None:
                self._set(zone, {"device": device, "since_unix": round(time.time(), 3)})
                self.emit("zone acquired", {"zone": zone, "device": device})
        return self.view([zone])[zone]

    def check_release(self, zone: str, device: str) -> None:
        self._check_zone(zone, device)
        with self._lock:
            self._check_release_locked(zone, device)

    def _check_release_locked(self, zone: str, device: str) -> None:
        h = self.holder[zone]
        if h is not None and h["device"] != device:
            raise Rejected(f"{zone} is held by {h['device']}, not {device}", 409)
        if h is not None and self.inside.get(device) == zone:
            raise Rejected(f"{device} is (or may be) inside {zone}; move it to a point outside first", 409)

    def check_acquire(self, zone: str, device: str) -> None:
        self._check_zone(zone, device)
        with self._lock:
            h = self.holder[zone]
            if h is not None and h["device"] != device:
                raise Rejected(f"{zone} is held by {h['device']}", 409)

    def release(self, zone: str, device: str) -> dict:
        self._check_zone(zone, device)
        with self._lock:
            if self.holder[zone] is None:
                return self._view_unlocked(zone)
            self._check_release_locked(zone, device)
            self._set(zone, None)
        self.emit("zone released", {"zone": zone, "device": device})
        return self.view([zone])[zone]

    def clear(self, zone: str, by: str) -> dict:
        """A person checked the bench: no arm is in the zone. Local only."""
        if zone not in self.zones:
            raise Rejected(f"unknown zone {zone!r}; zones: {sorted(self.zones)}")
        with self._lock:
            was = self.holder[zone]
            self._set(zone, None)
            for d in [d for d, z in self.inside.items() if z == zone]:
                del self.inside[d]
        self.emit("zone cleared by a person", {"zone": zone, "by": by, "was": was and was["device"]}, "warning")
        return self.view([zone])[zone]

    def _view_unlocked(self, zone: str) -> dict:
        h = self.holder[zone]
        return {"holder": h and h["device"], "since_unix": h and h["since_unix"],
                "inside": sorted(d for d, z in self.inside.items() if z == zone)}

    # ------------------------------------------------------------------ motion hooks
    def check_enter(self, device: str, point: str) -> None:
        zone = self.zone_of_point(point)
        if zone is None:
            return
        with self._lock:
            h = self.holder[zone]
        if h is None or h["device"] != device:
            who = f"held by {h['device']}" if h else "free"
            raise Rejected(f"{point} is in {zone} ({who}); {device} must hold it first (zone_acquire)", 409)

    def move_started(self, device: str, point: str) -> None:
        zone = self.zone_of_point(point)
        if zone is not None:
            with self._lock:
                self.inside[device] = zone

    def move_finished(self, device: str, point: str, ok: bool) -> None:
        """An ok move to a point outside every zone means the arm left: release what it held there."""
        if not ok or self.zone_of_point(point) is not None:
            return
        with self._lock:
            zone = self.inside.pop(device, None)
            h = self.holder.get(zone) if zone else None
            if zone is None or h is None or h["device"] != device:
                return
            self._set(zone, None)
        self.emit("zone released (arm left)", {"zone": zone, "device": device, "at": point})
