"""Contract v1 service core (transport-independent; the HTTP layer and the probe both use it).

Wire format: notes/night-20261108/contract-v1-qumus.md "WIRE FORMAT" (follows reactor ADR 0001):
describe.commands {name: {params, any_client, motion, local_only}}, flat command body, state.last_command
{id, action, client, result: ok|failed|rejected|stopped, message, finished_at}, state.lease
{holder, remaining_s}, events {events: [{seq, ts_unix, level, message, fields}], next}, stop {wait_s} ->
{stop: confirmed|pending|unconfirmed, state}, heartbeat / renew / release, unlock [Q].
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ..drivers.base import DriverError, OutcomeUnknown, Refused
from ..modules.base import ActionContext, Cancelled, Module
from ..safety.gate import GateContext, Rejected
from ..state.store import LabStore

CONTRACT = 1
LEASE_S = 3.0
CLIENT_HEADER = "X-Qumus-Client"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Lease:
    holder: str
    expires: float


class ContractService:
    def __init__(self, module: Module, *, version: str = "0.1.0", journal_dir: Path | None = None,
                 lease_s: float = LEASE_S, store: LabStore | None = None):
        self.module = module
        self.store = store
        self._heater_written: dict[str, float] = {}
        self.name = module.name
        self.version = version
        self.lease_s = lease_s
        self.lock = threading.RLock()
        self.seq = 0
        self.events: deque[dict] = deque(maxlen=2000)
        self.lease: Lease | None = None
        self.locked = False
        self.fault: dict | None = None
        self.last_command: dict | None = None
        self.busy_command: dict | None = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._no_lease_since: float | None = time.monotonic()
        self.orphan_timeout_s = {k: float(v.get("orphan_timeout_s", 600))
                                 for k, v in module.config.limits.get("heaters", {}).items()}
        self.journal_path = None
        if journal_dir is not None:
            journal_dir.mkdir(parents=True, exist_ok=True)
            self.journal_path = journal_dir / f"{self.name}-{time.strftime('%Y%m%d')}.jsonl"
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, daemon=True, name=f"watchdog-{self.name}")
        self._watchdog.start()

    # ------------------------------------------------------------------ events
    def emit(self, message: str, fields: dict | None = None, level: str = "info") -> dict:
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "ts_unix": round(time.time(), 3), "level": level, "message": message,
                  "fields": fields or {}}
            self.events.append(ev)
        if self.journal_path is not None:
            try:
                with open(self.journal_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(dict(ev, service=self.name), default=str) + "\n")
            except OSError:
                pass
        if self.store is not None:
            try:
                self.store.add_event(self.name, level, message, ev["fields"], ev["ts_unix"])
            except Exception:  # noqa: BLE001 - the in-memory journal still has it
                pass
        return ev

    # ------------------------------------------------------------------ device table (qumus.db)
    def touched_devices(self, params: dict) -> list[str]:
        """Devices a command names (device, camera, point); all of the module's devices otherwise."""
        ids = [params[k] for k in ("device", "camera") if params.get(k) in self.module.devices]
        point = params.get("point")
        if isinstance(point, str) and point.split(".", 1)[0] in self.module.devices:
            ids.append(point.split(".", 1)[0])
        return ids or list(self.module.devices)

    def record_devices(self, device_ids: list[str], *, initial: bool = False) -> None:
        """Write the touched devices' last readings to the device table. Values only come from the drivers
        (null = not known). ``initial``: only kind / module / online, keeping the stored readings."""
        if self.store is None:
            return
        try:
            for dev_id in device_ids:
                dev = self.module.devices[dev_id]
                online = bool(dev.connected and not dev.last_error)
                state = None if initial else _jsonable(dev.snapshot())
                self.store.upsert_device(dev_id, dev.kind, module=self.name, state=state,
                                         read_at=dev.last_read_unix, online=online)
                psu = getattr(dev, "psu", None)
                if psu is not None:  # the shared supply follows the heater that uses it
                    self.store.upsert_device(psu.id, psu.kind, module="shared",
                                             state=None if initial else _jsonable(psu.snapshot()),
                                             read_at=psu.last_read_unix, online=bool(psu.connected))
        except Exception as exc:  # noqa: BLE001 - never fail a command because of the state file
            self.emit("state table write failed", {"error": f"{type(exc).__name__}: {exc}"}, "warning")

    def get_events(self, since: int = 0) -> dict:
        with self.lock:
            evs = [e for e in self.events if e["seq"] > since]
            nxt = evs[-1]["seq"] if evs else max(since, 0)
            return {"events": evs, "next": nxt}

    # ------------------------------------------------------------------ describe / state
    def describe(self) -> dict:
        return {
            "service": f"qumus-lab.{self.name}", "version": self.version, "contract": CONTRACT, "module": self.name,
            "demo": self.module.demo, "client_header": CLIENT_HEADER, "lease_seconds": self.lease_s,
            "on_lease_expiry": "stop_motion (heaters keep their own timeout and orphan timeout)",
            "zones": self.module.zone_names(), "devices": self.module.describe_devices(),
            "commands": {n: a.describe() for n, a in self.module.actions.items()},
        }

    def lease_view(self) -> dict | None:
        with self.lock:
            if self.lease is None:
                return None
            return {"holder": self.lease.holder, "remaining_s": round(max(0.0, self.lease.expires - time.monotonic()), 2)}

    def state(self) -> dict:
        devices = {}
        for dev_id, dev in self.module.devices.items():
            try:
                devices[dev_id] = dev.status()
            except Exception as exc:  # noqa: BLE001
                devices[dev_id] = {"kind": dev.kind, "connected": dev.connected, "error": str(exc)}
        with self.lock:
            return {
                "service": f"qumus-lab.{self.name}", "seq": self.seq, "ts_unix": round(time.time(), 3),
                "demo": self.module.demo, "connected": all(d.connected for d in self.module.devices.values()),
                "busy": self.busy_command is not None, "busy_action": (self.busy_command or {}).get("action"),
                "locked": self.locked, "fault": self.fault, "lease": self.lease_view(),
                "last_command": self.last_command, "devices": devices,
                "zones": self.module.zones.view(self.module.zone_names()) if self.module.zones else {},
            }

    # ------------------------------------------------------------------ lease
    def _expire_lease_locked(self) -> None:
        if self.lease is not None and time.monotonic() > self.lease.expires:
            holder = self.lease.holder
            self.lease = None
            self._no_lease_since = time.monotonic()
            busy = self.busy_command
            self.emit("lease expired", {"holder": holder})
            if busy is not None and self.module.actions[busy["action"]].motion:
                threading.Thread(target=self.stop, kwargs={"reason": "lease_expired", "wait_s": 0}, daemon=True).start()

    def heartbeat(self, client: str) -> dict:
        with self.lock:
            self._expire_lease_locked()
            if self.lease is not None and self.lease.holder != client:
                raise ApiError(409, f"lease held by {self.lease.holder}")
            new = self.lease is None
            self.lease = Lease(client, time.monotonic() + self.lease_s)
            self._no_lease_since = None
            if new:
                self.emit("lease acquired", {"holder": client})
            return {"lease": self.lease_view()}

    def renew(self, client: str) -> dict:
        with self.lock:
            self._expire_lease_locked()
            if self.lease is None or self.lease.holder != client:
                raise ApiError(409, "caller does not hold the lease")
            self.lease.expires = time.monotonic() + self.lease_s
            return {"lease": self.lease_view()}

    def release(self, client: str) -> dict:
        with self.lock:
            if self.lease is not None and self.lease.holder == client:
                self.lease = None
                self._no_lease_since = time.monotonic()
                self.emit("lease released", {"holder": client})
            elif self.lease is not None:
                raise ApiError(409, "caller does not hold the lease")
            return {"lease": None}

    # ------------------------------------------------------------------ commands
    def command(self, body: dict, client: str, loopback: bool) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            raise ApiError(400, "body must be a JSON object with a string 'action'")
        body = dict(body)
        name = body.pop("action")
        command_id = str(body.pop("command_id", None) or uuid.uuid4().hex[:12])
        action = self.module.actions.get(name)
        if action is None:
            raise ApiError(400, f"unknown action {name!r}")
        gctx = GateContext(client=client, loopback=loopback, demo=self.module.demo)
        try:
            self.module.gate.check_local(action.local_only, gctx)
            params = action.validate(body)
            if action.precheck is not None:
                action.precheck(self.module, params)
        except Rejected as exc:
            self._finish_rejected(command_id, name, client, str(exc))
            raise ApiError(exc.status, str(exc)) from exc
        with self.lock:
            self._expire_lease_locked()
            if self.locked:
                raise ApiError(409, "service is locked (unknown outcome); a person must unlock it on the lab PC")
            if not action.any_client and (self.lease is None or self.lease.holder != client):
                holder = self.lease.holder if self.lease else None
                raise ApiError(409, f"{name} needs the lease (holder: {holder}); POST api/heartbeat first")
            if not action.concurrent:
                if self.busy_command is not None:
                    raise ApiError(409, f"busy with {self.busy_command['action']}")
                self.busy_command = {"id": command_id, "action": name, "client": client}
                self._cancel.clear()
            self.emit("command accepted", {"id": command_id, "action": name, "client": client, "params": params})
        t = threading.Thread(target=self._run, args=(action, params, command_id, client, gctx), daemon=True,
                             name=f"{self.name}-{name}")
        if not action.concurrent:
            self._worker = t
        t.start()
        return {"command_id": command_id, "state": self.state()}

    def _finish_rejected(self, command_id: str, name: str, client: str, message: str) -> None:
        with self.lock:
            self.last_command = {"id": command_id, "action": name, "client": client, "result": "rejected",
                                 "message": message, "finished_at": round(time.time(), 3)}
        self.emit("command rejected", {"id": command_id, "action": name, "message": message}, "warning")

    def _run(self, action, params, command_id, client, gctx) -> None:
        cancel = self._cancel if not action.concurrent else threading.Event()
        ctx = ActionContext(self.module, self.module.gate, gctx, cancel, lambda m, f: self.emit(m, f))
        self.emit("command started", {"id": command_id, "action": action.name})
        result, message, output, level = "ok", "", None, "info"
        try:
            output = action.fn(ctx, params)
        except Cancelled:
            result, message, level = "stopped", "stopped before completion", "warning"
        except Rejected as exc:
            result, message, level = "rejected", str(exc), "warning"
        except Refused as exc:
            result, message, level = "rejected", str(exc), "warning"
        except OutcomeUnknown as exc:
            result, message, level = "failed", f"unknown outcome: {exc}", "error"
            self._lock_service("outcome_unknown", str(exc), command_id)
        except DriverError as exc:
            result, message, level = "failed", f"{type(exc).__name__}: {exc}", "error"
        except Exception as exc:  # noqa: BLE001 - never kill the service thread
            result, message, level = "failed", f"internal error {type(exc).__name__}: {exc}", "error"
            if action.motion:
                self._lock_service("internal_error", message, command_id)
        if result == "ok" and cancel.is_set() and action.motion:
            result, message = "stopped", "stop requested during the action"
        self.record_devices(self.touched_devices(params))  # before last_command: a finished command is on record
        with self.lock:
            self.last_command = {"id": command_id, "action": action.name, "client": client, "result": result,
                                 "message": message, "finished_at": round(time.time(), 3),
                                 "output": _jsonable(output)}
            if not action.concurrent and self.busy_command and self.busy_command["id"] == command_id:
                self.busy_command = None
        self.emit(f"command {'done' if result == 'ok' else result}",
                  {"id": command_id, "action": action.name, "result": result, "message": message}, level)

    def _lock_service(self, code: str, message: str, command_id: str | None = None) -> None:
        with self.lock:
            self.locked = True
            self.fault = {"code": code, "message": message}
        self.emit("locked", {"code": code, "message": message, "command_id": command_id}, "error")

    def wait_idle(self, timeout_s: float) -> bool:
        t = self._worker
        if t is not None:
            t.join(timeout=max(0.0, timeout_s))
        return self.busy_command is None

    # ------------------------------------------------------------------ stop / unlock
    def stop(self, wait_s: float = 0.0, reason: str = "api_stop", heat_off: bool = False) -> dict:
        """Any client, no lease, idempotent. Stops motion; ``heat_off`` [Q] also switches heaters off."""
        wait_s = min(max(float(wait_s or 0.0), 0.0), 15.0)
        self._cancel.set()
        unconfirmed = []
        for dev in self.module.motion_devices():
            try:
                r = dev.stop()
            except Exception as exc:  # noqa: BLE001
                r = False
                dev.last_error = str(exc)
            if r is False:
                unconfirmed.append(dev.id)
        if heat_off:
            for dev in self.module.devices.values():
                if dev.kind == "heater" and dev.connected:
                    dev.off("stop")
        idle = self.wait_idle(wait_s) if wait_s > 0 else self.busy_command is None
        if unconfirmed:
            verdict = "unconfirmed"
            self._lock_service("stop_unconfirmed", f"could not confirm stop of {', '.join(unconfirmed)}")
        elif idle:
            verdict = "confirmed"
        else:
            verdict = "pending"
        self.emit("stop", {"reason": reason, "stop": verdict, "unconfirmed": unconfirmed, "heat_off": heat_off},
                  "warning")
        return {"stop": verdict, "state": self.state()}

    def unlock(self, client: str, loopback: bool) -> dict:
        if not loopback:
            raise ApiError(403, "unlock is local-only: a person clears the lock on the lab PC")
        with self.lock:
            was = self.fault
            self.locked = False
            self.fault = None
        self.emit("unlocked", {"by": client, "cleared": was}, "warning")
        return {"state": self.state()}

    def safe_state(self, reason: str = "safe_state") -> dict:
        """Stop axes and heat off (probe Ctrl+C, launcher shutdown)."""
        out = self.stop(wait_s=2.0, reason=reason, heat_off=True)
        for dev_id in list(self.module._switch_timers):
            self.module.cancel_auto_off(dev_id)
            dev = self.module.devices.get(dev_id)
            try:
                if dev is not None and dev.connected:
                    dev.set(False)  # a probe/debug switch never stays on after safe_state
            except Exception as exc:  # noqa: BLE001
                self.emit("switch off failed in safe_state", {"device": dev_id, "error": str(exc)}, "error")
        return out

    # ------------------------------------------------------------------ watchdog
    def _watch(self) -> None:
        while not self._watchdog_stop.wait(0.25):
            with self.lock:
                self._expire_lease_locked()
                since = self._no_lease_since
            if since is None:
                self._record_heaters()
                continue
            idle_s = time.monotonic() - since
            self._record_heaters()
            for dev in self.module.devices.values():
                if dev.kind == "heater" and getattr(dev, "heating", False):
                    limit = self.orphan_timeout_s.get(dev.id, 600.0)
                    if idle_s > limit:
                        dev.off("orphan timeout (no lease holder)")
                        self.emit("heater orphan timeout", {"device": dev.id, "after_s": round(idle_s)}, "warning")

    def _record_heaters(self, every_s: float = 10.0) -> None:
        """While a heater has a target, write its row every 10 s (its control thread reads the thermometer
        anyway; nothing extra is sent to the instrument)."""
        if self.store is None:
            return
        now = time.monotonic()
        for dev in self.module.devices.values():
            if dev.kind != "heater":
                continue
            heating = getattr(dev, "heating", False)
            last = self._heater_written.get(dev.id)
            if heating and (last is None or now - last >= every_s):
                self._heater_written[dev.id] = now
                self.record_devices([dev.id])
            elif not heating and last is not None:
                self._heater_written.pop(dev.id, None)
                self.record_devices([dev.id])  # final row after it switched off

    def close(self) -> None:
        self._watchdog_stop.set()


def _jsonable(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))
