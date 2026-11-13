"""Contract 2 (papaya2003/lab-contract spec/contract-2.md) next to contract v1. Stdlib only.

The modules of one qumus-lab process (exfoliation, stacking) are one contract 2 **station**. Every
module port answers the station entry and every module's routes:

    GET  /api/station                       -> services exfoliation/, stacking/
    GET  /<module>/api/describe | state | events | commands/<id> | jobs/<run_id>
    POST /<module>/api/lease  {renew_only?}  DELETE /<module>/api/lease
    POST /<module>/api/command {command_id, action, params, deadline_s?}
    POST /<module>/api/stop    {wait_s, heat_off?}

The v1 routes (/api/...) stay as they are until the brain no longer needs them. This is an adapter:
the module's ContractService still does the work (lease, busy, locked, gate, jobs, stop) and the
adapter adds what contract 2 requires: client command ids with records, duplicates and id_reused,
nested params, error codes, boot_id, deadlines and readings under `readings`.

Who is local is decided exactly as on the v1 routes: on the module ports a loopback address proves
nothing (SSH tunnels end there), so `local_only` commands stay refused there; only the debug page
(8780) is local.

Notes:
- Units are added to params from their names (`*_c` -> C, `*_s` -> s, `*_mm`/`mm` -> mm, `*_um`/`um`
  -> um, `deg` -> deg, `volts` -> V).
- `heat` and `set_initial_temp` heat: contract 2 lists them as motion with hazard heat (lease,
  deadline). A heater keeps its own mandatory timeout and the orphan timeout (`orphan_timeout_s`).
- Jobs (`job: true`) finish their command when queued; `deadline_s` (default = maximum = 48 h) then
  bounds the job: when it passes, the job is cancelled.
- Records and put_records stay on the v1 feed (/api/records) until the contract 2 records profile.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from .core import ApiError, ContractService

CONTRACT = 2
CONTRACT_MINOR = 0
CLIENT_HEADER = "X-Lab-Client"
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
CLIENT_RE = re.compile(r"^[\x21-\x7e][\x20-\x7e]{0,127}$")
RETRYABLE = {"busy", "lease_held", "device_offline"}
STATUS = {
    "bad_request": 400, "unknown_action": 400, "bad_params": 400, "local_only": 403, "forbidden": 403,
    "not_found": 404, "busy": 409, "locked": 409, "maintenance": 409, "lease_held": 409,
    "lease_lost": 409, "lease_required": 409, "id_reused": 409, "bad_content_type": 415,
    "device_offline": 503, "internal": 500,
}
UNIT_SUFFIXES = (("_c", "C"), ("_s", "s"), ("_mm", "mm"), ("mm", "mm"), ("_um", "um"), ("um", "um"),
                 ("deg", "deg"), ("volts", "V"))
HEATS = frozenset({"heat", "set_initial_temp"})
JOB_DEADLINE_S = (48 * 3600.0, 48 * 3600.0)
MOTION_DEADLINE_S = (600.0, 3600.0)
HEAT_DEADLINE_S = (3600.0, 3600.0)
JOB_STATE = {"queued": "queued", "running": "running", "succeeded": "succeeded", "failed": "failed",
             "cancelled": "cancelled", "paused": "held", "parked": "held", "waiting_human": "held"}
KEEP = 500


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "retry": self.code in RETRYABLE}}


def v1_code(e: ApiError) -> str:
    """A contract v1 refusal (status + text) as a contract 2 error code."""
    text = e.message.lower()
    if e.status == 403:
        return "local_only"
    if e.status == 404:
        return "not_found"
    if e.status == 415:
        return "bad_content_type"
    if e.status == 503:
        return "device_offline"
    if e.status == 400:
        return "unknown_action" if text.startswith("unknown action") else "bad_params"
    if e.status == 409:
        if "locked" in text:
            return "locked"
        if "needs the lease" in text:
            return "lease_required"
        if "lease held" in text:
            return "lease_held"
        if "does not hold the lease" in text:
            return "lease_lost"
        return "busy"
    return "internal"


def _unit(name: str) -> str | None:
    for suffix, unit in UNIT_SUFFIXES:
        if name.endswith(suffix):
            return unit
    return None


class Window:
    """Contract 2 face of one module's ContractService."""

    def __init__(self, name: str, service: ContractService, station: Station) -> None:
        self.name, self.service, self.station = name, service, station
        self.lock = threading.RLock()
        self.records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.bodies: dict[str, str] = {}

    # -- describe ----------------------------------------------------------------------------------
    def command_spec(self, action: Any) -> dict[str, Any]:
        params = json.loads(json.dumps(action.schema()))
        params["additionalProperties"] = False
        for pname, prop in params.get("properties", {}).items():
            unit = _unit(pname)
            if unit and isinstance(prop, dict) and prop.get("type") in ("number", "integer"):
                prop.setdefault("unit", unit)
        heats = action.name in HEATS
        motion = bool(action.motion or heats or action.job)
        spec: dict[str, Any] = {
            "summary": action.description or action.name,
            "params": params,
            "lease": not action.any_client,
            "motion": motion,
            "hazard": ["heat"] if heats else ["motion"] if motion else [],
            "local_only": action.local_only,
            "allowed_when_locked": action.allowed_when_locked,
            "job": action.job,
        }
        if motion:
            default, maximum = (JOB_DEADLINE_S if action.job else
                                HEAT_DEADLINE_S if heats else MOTION_DEADLINE_S)
            spec["deadline_s"] = {"default": default, "maximum": maximum}
        return spec

    def describe(self) -> dict[str, Any]:
        svc = self.service
        lease: dict[str, Any] = {"duration_s": svc.lease_s, "on_expiry": "stop_motion"}
        if svc.orphan_timeout_s:
            lease["orphan_timeout_s"] = max(svc.orphan_timeout_s.values())
        return {"service": self.name, "station": self.station.name, "contract": CONTRACT,
                "contract_minor": CONTRACT_MINOR, "version": f"qumus-lab {svc.version}",
                "demo": svc.module.demo, "boot_id": self.station.boot_id, "lease": lease,
                "profiles": ["jobs"] if svc.store is not None else [],
                "commands": {n: self.command_spec(a) for n, a in svc.module.actions.items()}}

    # -- state -------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        v1 = self.service.state()
        self.sync()
        with self.lock:
            last = next(reversed(self.records.values()), None)
            running = [cid for cid, r in self.records.items() if r["result"] is None]
        fault = v1.get("fault")
        lease = v1.get("lease") or {"holder": None, "remaining_s": 0.0}
        return {
            "service": self.name, "boot_id": self.station.boot_id, "seq": v1["seq"],
            "ts_unix": v1["ts_unix"], "demo": v1["demo"], "connected": v1["connected"],
            "busy": v1["busy"], "locked": v1["locked"], "maintenance": None,
            "fault": None if not fault else {"code": str(fault.get("code") or "fault"),
                                             "message": str(fault.get("message") or ""),
                                             "since_unix": v1["ts_unix"]},
            "lease": {"holder": lease["holder"], "remaining_s": lease["remaining_s"]},
            "running": running,
            "last_command": (None if last is None else
                             {"id": last["id"], "action": last["action"], "result": last["result"]}),
            "readings": {"devices": v1.get("devices", {}), "zones": v1.get("zones", {}),
                         "jobs": v1.get("jobs", {}), "busy_action": v1.get("busy_action")},
        }

    # -- events ------------------------------------------------------------------------------------
    @staticmethod
    def _kind(e: dict[str, Any]) -> str:
        msg = e["message"]
        if "lease" in msg:
            return "lease"
        if msg.startswith(("command", "stop")):
            return "command"
        if msg in ("locked", "unlocked"):
            return "fault"
        if msg.startswith("job") or "run_id" in (e.get("fields") or {}):
            return "job"
        return "log"

    def events(self, since: int, limit: int) -> dict[str, Any]:
        page = self.service.get_events(since)
        evs = page["events"][:limit]
        out = [{"seq": e["seq"], "ts_unix": e["ts_unix"], "level": e["level"], "kind": self._kind(e),
                "message": e["message"], "fields": e.get("fields") or {}} for e in evs]
        return {"boot_id": self.station.boot_id, "events": out,
                "next": out[-1]["seq"] if out else max(since, 0)}

    # -- lease -------------------------------------------------------------------------------------
    def lease(self, client: str, renew_only: bool) -> dict[str, Any]:
        try:
            out = self.service.renew(client) if renew_only else self.service.heartbeat(client)
        except ApiError as e:
            raise Refusal("lease_lost" if renew_only else v1_code(e), e.message) from None
        view = out["lease"] or {"holder": None, "remaining_s": 0.0}
        return {"holder": view["holder"], "remaining_s": view["remaining_s"],
                "duration_s": self.service.lease_s}

    def release(self, client: str) -> dict[str, Any]:
        lease = self.service.lease_view()
        if lease is None:
            raise Refusal("lease_lost", "you do not hold the lease")
        if lease["holder"] != client:
            raise Refusal("lease_held", f"lease held by {lease['holder']}")
        self.service.release(client)
        return {"holder": None, "remaining_s": 0.0, "duration_s": self.service.lease_s}

    # -- commands ----------------------------------------------------------------------------------
    def command(self, client: str, body: dict[str, Any], loopback: bool) -> tuple[int, dict]:
        extra = sorted(set(body) - {"command_id", "action", "params", "deadline_s"})
        if extra:
            raise Refusal("bad_params", f"unknown fields {extra}; params go under 'params'")
        cid, action, params = body.get("command_id"), body.get("action"), body.get("params")
        if not isinstance(cid, str) or not ID_RE.match(cid):
            raise Refusal("bad_request", "command_id is required: 1-64 chars of [A-Za-z0-9._:-]")
        if not isinstance(action, str):
            raise Refusal("bad_request", "action is required")
        if not isinstance(params, dict):
            raise Refusal("bad_params", "params must be an object ({} when none)")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        # check everything first (no side effects), then look up and reserve the id in one lock
        # section, so two requests with the same new command_id never both run
        refusal, deadline_s, spec = None, body.get("deadline_s"), {}
        act = self.service.module.actions.get(action)
        if act is None:
            refusal = Refusal("unknown_action", f"no command {action!r}")
        else:
            spec = self.command_spec(act)
            unknown = sorted(set(params) - set(spec["params"].get("properties", {})))
            limit = spec.get("deadline_s", {}).get("maximum")
            if unknown:
                refusal = Refusal("bad_params", f"unknown params {unknown}")
            elif deadline_s is not None and (
                    not isinstance(deadline_s, int | float) or isinstance(deadline_s, bool)
                    or deadline_s <= 0 or (limit is not None and deadline_s > limit)):
                refusal = Refusal("bad_params", f"deadline_s must be > 0 and <= {limit}")
            elif deadline_s is None and "deadline_s" in spec:
                deadline_s = spec["deadline_s"]["default"]
        now = time.time()
        with self.service.lock:
            since = self.service.seq
        record = {"id": cid, "action": str(action), "client": client, "params": params,
                  "result": None, "message": "", "accepted_unix": now, "finished_unix": None,
                  "data": {}, "boot_id": self.station.boot_id, "_since": since}
        with self.lock:
            if cid in self.bodies:
                if self.bodies[cid] != digest:
                    raise Refusal("id_reused", f"command_id {cid} was used with another body")
                rec = self.records[cid]
                return 200, {"command_id": cid, "accepted_unix": rec["accepted_unix"],
                             "seq": self.service.seq, "duplicate": True}
            if refusal is not None:
                record.update(result="rejected", message=refusal.message, finished_unix=now,
                              _since=0)
            self._remember(cid, digest, record)
        if refusal is not None:
            raise refusal
        try:
            out = self.service.command({"action": action, "command_id": cid, **params}, client, loopback)
        except ApiError as e:
            self._finish(record, "rejected", e.message)
            raise Refusal(v1_code(e), e.message) from None
        if act.job:
            data = out.get("result") or {}
            self._finish(record, "ok", "job accepted", data)
            run_id = data.get("run_id")
            if run_id and deadline_s:
                self._job_deadline(run_id, float(deadline_s))
        elif deadline_s:
            self._deadline(record, float(deadline_s))
        return 202, {"command_id": cid, "accepted_unix": now, "seq": self.service.seq}

    def _remember(self, cid: str, digest: str, record: dict[str, Any]) -> None:
        self.records[cid] = record
        self.bodies[cid] = digest
        while len(self.records) > KEEP:
            old, _ = self.records.popitem(last=False)
            self.bodies.pop(old, None)

    def _finish(self, record: dict[str, Any], result: str, message: str,
                data: dict[str, Any] | None = None) -> None:
        with self.lock:
            if record["result"] is None:
                record.update(result=result, message=message or "", data=data or {},
                              finished_unix=time.time())

    def _deadline(self, record: dict[str, Any], seconds: float) -> None:
        def fire() -> None:
            self.sync()
            if record["result"] is None:
                record["_expiring"] = True
                self.service.stop(wait_s=2.0, reason=f"deadline of {record['id']} passed")
                self.sync()
        t = threading.Timer(seconds, fire)
        t.daemon = True
        t.start()

    def _job_deadline(self, run_id: str, seconds: float) -> None:
        def fire() -> None:
            run = self.service.store.get_run(run_id) if self.service.store is not None else None
            if run is not None and run["status"] in ("queued", "running"):
                with self.service.lock:
                    self.service.jobs.cancel_locked(run_id, "contract 2 deadline passed")
        t = threading.Timer(seconds, fire)
        t.daemon = True
        t.start()

    def sync(self) -> None:
        """Finish open records from the module's v1 events ('command done/stopped/failed/rejected')."""
        with self.lock:
            open_recs = {cid: r for cid, r in self.records.items() if r["result"] is None}
        if not open_recs:
            return
        since = min(r["_since"] for r in open_recs.values())
        for e in self.service.get_events(since)["events"]:
            f = e.get("fields") or {}
            rec = open_recs.get(f.get("id"))
            if rec is None or not e["message"].startswith("command ") or "result" not in f:
                continue
            result = {"ok": "ok", "stopped": "stopped", "failed": "failed",
                      "rejected": "failed"}.get(f["result"], "failed")
            message = str(f.get("message") or "")
            if result == "stopped" and rec.get("_expiring"):
                result = "expired"
            if result == "failed" and message.startswith("unknown outcome"):
                result = "unknown"
            data = None
            last = self.service.last_command or {}
            if last.get("id") == rec["id"] and isinstance(last.get("data"), dict):
                data = last["data"]
            self._finish(rec, result, message, data)

    def command_record(self, cid: str) -> dict[str, Any]:
        self.sync()
        with self.lock:
            rec = self.records.get(cid)
            if rec is None:
                raise Refusal("not_found", f"no command {cid} in this boot")
            return {k: v for k, v in rec.items() if not k.startswith("_")}

    def stop(self, client: str, wait_s: float, heat_off: bool) -> dict[str, Any]:
        out = self.service.stop(wait_s=wait_s, reason=f"api_stop by {client}", heat_off=heat_off)
        return {"stop": out["stop"], "seq": self.service.seq}

    # -- jobs profile ------------------------------------------------------------------------------
    def job(self, run_id: str) -> dict[str, Any]:
        store = self.service.store
        run = store.get_run(run_id) if store is not None else None
        if run is None:
            raise Refusal("not_found", f"no run {run_id}")
        steps = store.get_steps(run_id) or []
        done = [s for s in steps if s.get("status") in ("done", "skipped")]
        current = steps[len(done)] if len(done) < len(steps) else (steps[-1] if steps else {})
        state = JOB_STATE.get(run["status"], "held")
        error = run.get("error")
        message = (str(error.get("message") or error) if isinstance(error, dict) else str(error or ""))
        return {"run_id": run_id, "action": run["flow"], "state": state, "step": len(done),
                "steps_total": len(steps), "step_name": str(current.get("name") or ""),
                "message": message, "safe_to_resume": state == "held",
                "started_unix": run.get("created_at"),
                "finished_unix": (run.get("updated_at")
                                  if state in ("succeeded", "failed", "cancelled") else None),
                "sample_ids": [], "image_ids": [],
                "result": run.get("result") if isinstance(run.get("result"), dict) else None}


class Station:
    """The modules of one qumus-lab process as one contract 2 station."""

    def __init__(self, services: dict[str, ContractService], *, name: str = "qumus-lab") -> None:
        self.name = name
        self.boot_id = time.strftime("b-%Y%m%dT%H%M%S-") + secrets.token_hex(3)
        self.windows = {n: Window(n, svc, self) for n, svc in services.items()}

    def entry(self) -> dict[str, Any]:
        demo = all(w.service.module.demo for w in self.windows.values())
        return {"station": self.name, "contract": CONTRACT, "contract_minor": CONTRACT_MINOR,
                "demo": demo, "boot_id": self.boot_id,
                "services": [{"name": n, "path": f"{n}/", "kind": "device"} for n in self.windows],
                "auth": "none", "maintenance": None}

    def owns(self, path: str) -> bool:
        """True for the contract 2 routes (the station entry and /<module>/api/...)."""
        if path == "/api/station":
            return True
        parts = path.split("/")
        return len(parts) > 2 and parts[1] in self.windows and parts[2] == "api"

    def handle(self, method: str, path: str, query: dict, headers: Any, body: bytes,
               loopback: bool) -> tuple[int, dict]:
        """One contract 2 request -> (status, JSON body). Never raises."""
        try:
            return self._route(method, path, query, headers, body, loopback)
        except Refusal as r:
            return r.status, r.body()
        except ApiError as e:
            r = Refusal(v1_code(e), e.message)
            return r.status, r.body()
        except Exception as e:  # noqa: BLE001 - always a contract 2 error body
            r = Refusal("internal", f"{type(e).__name__}: {e}")
            return r.status, r.body()

    def _route(self, method: str, path: str, query: dict, headers: Any, body: bytes,
               loopback: bool) -> tuple[int, dict]:
        if path == "/api/station" and method == "GET":
            return 200, self.entry()
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3 or parts[0] not in self.windows or parts[1] != "api":
            raise Refusal("not_found", f"no route {method} {path}")
        w, route = self.windows[parts[0]], parts[2:]
        if method == "GET":
            if route == ["describe"]:
                return 200, w.describe()
            if route == ["state"]:
                return 200, w.state()
            if route == ["events"]:
                try:
                    since = max(0, int((query.get("since") or ["0"])[0]))
                    limit = min(max(1, int((query.get("limit") or ["500"])[0])), 2000)
                except ValueError:
                    raise Refusal("bad_request", "since and limit must be integers") from None
                return 200, w.events(since, limit)
            if len(route) == 2 and route[0] == "commands":
                return 200, w.command_record(route[1])
            if len(route) == 2 and route[0] == "jobs":
                return 200, w.job(route[1])
            raise Refusal("not_found", f"no route GET {path}")
        if method not in ("POST", "DELETE"):
            raise Refusal("not_found", f"no route {method} {path}")
        client = headers.get(CLIENT_HEADER) or ""
        if not CLIENT_RE.match(client):
            raise Refusal("bad_request", f"{CLIENT_HEADER} header is required (1-128 printable ASCII)")
        if method == "DELETE":
            if route == ["lease"]:
                return 200, w.release(client)
            raise Refusal("not_found", f"no route DELETE {path}")
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Refusal("bad_content_type", "POST bodies are application/json")
        try:
            payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        except (UnicodeDecodeError, ValueError):
            raise Refusal("bad_request", "body is not JSON") from None
        if not isinstance(payload, dict):
            raise Refusal("bad_request", "body must be a JSON object")
        if route == ["lease"]:
            return 200, w.lease(client, bool(payload.get("renew_only")))
        if route == ["command"]:
            return w.command(client, payload, loopback)
        if route == ["stop"]:
            # Contract 2 section 15.1: stop always works. A wait_s that is not a number in 0..15 is treated as 0
            # (or clamped) instead of refusing the stop, which used to leave everything moving.
            wait_s = payload.get("wait_s", 0)
            if not isinstance(wait_s, int | float) or isinstance(wait_s, bool) or wait_s != wait_s:
                wait_s = 0
            return 200, w.stop(client, min(max(float(wait_s), 0.0), 15.0), bool(payload.get("heat_off", False)))
        raise Refusal("not_found", f"no route POST {path}")


__all__ = ["CLIENT_HEADER", "CONTRACT", "Station", "Window"]
