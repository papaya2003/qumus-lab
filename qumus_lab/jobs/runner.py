"""Job runner: a job action (L5 flow) is accepted at once with a run_id; the flow then runs in this module's
worker thread, writing run and step rows (the brain's RunStore shapes) that GET /api/records feeds out.

Rules (same as single commands):
- one motion job or command at a time per module; further jobs wait in a FIFO queue (run status ``queued``);
- ``stop`` (any client) and lease expiry stop the running job at its next check; it ends ``cancelled`` and the
  queued jobs of this module are cancelled too, so nothing starts moving by itself after a stop;
- an unknown outcome (a device that did not confirm) locks the module, the run ends ``failed``;
- every step is a checkpoint: ``in_progress`` -> ``done`` | ``failed`` | ``skipped``.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..drivers.base import DriverError, Interrupted, OutcomeUnknown, Refused
from ..modules.base import ActionContext, Cancelled
from ..safety.gate import GateContext, Rejected


class StepFailed(Exception):
    """A step's check failed (e.g. nothing found, pickup not confirmed). The run ends ``failed``; the module
    is not locked (the outcome is known)."""


@dataclass
class JobContext(ActionContext):
    run_id: str = ""
    flow: str = ""
    project_id: str | None = None
    service: Any = None
    images: list = field(default_factory=list)
    _seq: int = 0

    @property
    def store(self):
        return self.service.store

    # -- steps -------------------------------------------------------------------------------
    def step(self, name: str, fn: Callable[[], Any] | None = None, *, safe_to_resume: bool = True) -> Any:
        """Run one checkpointed step. ``fn`` returns measurements (any JSON value) or None."""
        self.check()
        self._seq += 1
        seq = self._seq
        if self.store is not None:
            self.store.begin_step(self.run_id, seq, name, safe_to_resume=safe_to_resume)
        self.event("step.started", {"seq": seq, "name": name})
        try:
            out = fn() if fn is not None else None
        except Cancelled:
            self._finish(seq, name, "failed", None, "stopped")
            raise
        except Exception as exc:
            self._finish(seq, name, "failed", None, f"{type(exc).__name__}: {exc}")
            raise
        self._finish(seq, name, "done", out, "")
        return out

    def skip(self, name: str, note: str) -> None:
        self._seq += 1
        if self.store is not None:
            self.store.begin_step(self.run_id, self._seq, name)
            self.store.finish_step(self.run_id, self._seq, "skipped", note=note)

    def _finish(self, seq: int, name: str, status: str, measurements: Any, note: str) -> None:
        if self.store is not None:
            self.store.finish_step(self.run_id, seq, status, measurements=_jsonable(measurements), note=note)
        self.event("step.finished", {"seq": seq, "name": name, "status": status, "note": note})

    # -- helpers -----------------------------------------------------------------------------
    def event(self, kind: str, payload: dict | None = None, level: str = "info") -> None:
        self.service.emit(kind, dict(payload or {}, run_id=self.run_id), level)

    def sleep(self, seconds: float) -> None:
        """Wait, but wake at once on stop."""
        if self.cancel.wait(max(0.0, float(seconds))):
            raise Cancelled("stopped")

    def wait_until(self, pred: Callable[[], Any], timeout_s: float, poll_s: float = 0.5, what: str = "") -> Any:
        deadline = time.monotonic() + timeout_s
        while True:
            v = pred()
            if v:
                return v
            if time.monotonic() > deadline:
                raise StepFailed(f"timed out after {timeout_s:.0f} s waiting for {what or 'condition'}")
            self.sleep(poll_s)

    def image(self, capture: dict, role: str) -> dict:
        """Remember a captured picture for the run result (brain fetches it by GET /api/images/<id>)."""
        ref = {"image_id": capture.get("image_id"), "role": role, "camera": capture.get("camera")}
        self.images.append(ref)
        return ref

    def project_state(self) -> dict:
        if self.store is None or not self.project_id:
            return {}
        p = self.store.get_project(self.project_id)
        return (p or {}).get("state") or {}


@dataclass
class _Job:
    run_id: str
    flow: str
    params: dict
    client: str
    project_id: str | None
    fn: Callable[[JobContext, dict], Any]
    gctx: GateContext


class JobRunner:
    def __init__(self, service):
        self.svc = service
        self.queue: deque[_Job] = deque()
        self.current: _Job | None = None
        self._cv = threading.Condition(service.lock)
        self._thread: threading.Thread | None = None

    # called with service.lock held (from ContractService.command)
    def submit_locked(self, action, params: dict, client: str, gctx: GateContext) -> dict:
        job_meta = params.pop("job", None) or {}
        if not isinstance(job_meta, dict):
            raise Rejected("'job' must be an object {project_id, requested_by}")
        run_id = f"run_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        project_id = job_meta.get("project_id")
        requested_by = job_meta.get("requested_by") or client
        if self.svc.store is not None:
            self.svc.store.create_run(run_id, action.name, self.svc.name, params, project_id=project_id,
                                      requested_by=requested_by, meta={"module": self.svc.name})
        job = _Job(run_id, action.name, params, client, project_id, action.fn, gctx)
        self.queue.append(job)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name=f"jobs-{self.svc.name}")
            self._thread.start()
        return {"run_id": run_id, "flow": action.name, "queued_behind": len(self.queue) - 1 + (1 if self.current else 0)}

    def cancel_locked(self, run_id: str, reason: str) -> str:
        for job in list(self.queue):
            if job.run_id == run_id:
                self.queue.remove(job)
                self._end(job.run_id, "cancelled", None, {"reason": reason})
                return "cancelled"
        if self.current is not None and self.current.run_id == run_id:
            self.svc._cancel.set()
            for dev in self.svc.module.motion_devices():
                try:
                    dev.stop()
                except Exception:  # noqa: BLE001 - the job sees the cancel flag anyway
                    pass
            return "stopping"
        raise Rejected(f"job {run_id} is not queued or running on {self.svc.name}", 404)

    def on_stop_locked(self, reason: str) -> list[str]:
        """Stop pressed: the running job stops at its next check; queued jobs are cancelled."""
        dropped = []
        while self.queue:
            job = self.queue.popleft()
            self._end(job.run_id, "cancelled", None, {"reason": f"{reason} (was queued)"})
            dropped.append(job.run_id)
        return dropped

    def running_run(self) -> str | None:
        return self.current.run_id if self.current else None

    def _end(self, run_id: str, status: str, result: Any, error: Any) -> None:
        if self.svc.store is not None:
            self.svc.store.update_run(run_id, status=status, result=_jsonable(result), error=_jsonable(error))

    # worker -------------------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self.svc.lock:
                if not self.queue:
                    self.current = None
                    return
                if self.svc.locked:  # queued jobs never start by themselves on a locked module
                    while self.queue:
                        self._end(self.queue.popleft().run_id, "cancelled", None, {"reason": "module locked"})
                    self.current = None
                    return
                job = None
                if self.svc.busy_command is None:
                    job = self.queue.popleft()
                    self.current = job
                    self.svc.busy_command = {"id": job.run_id, "action": job.flow, "client": job.client,
                                             "job": True}
                    self.svc._cancel.clear()
                    for dev in self.svc.module.motion_devices():
                        dev.clear_stop()
            if job is None:  # a single command is still running: wait for it
                time.sleep(0.05)
                continue
            try:
                self._run(job)
            finally:
                with self.svc.lock:
                    if self.svc.busy_command and self.svc.busy_command.get("id") == job.run_id:
                        self.svc.busy_command = None
                    self.current = None

    def _run(self, job: _Job) -> None:
        svc = self.svc
        ctx = JobContext(svc.module, svc.module.gate, job.gctx, svc._cancel, lambda m, f: svc.emit(m, f),
                         run_id=job.run_id, flow=job.flow, project_id=job.project_id, service=svc)
        if svc.store is not None:
            svc.store.update_run(job.run_id, status="running", meta={"started_at": time.time()})
        ctx.event("job.started", {"flow": job.flow, "params": job.params})
        status, result, error, level = "succeeded", None, None, "info"
        try:
            result = job.fn(ctx, dict(job.params)) or {}
            if isinstance(result, dict):
                result = dict(result, images=list(ctx.images)) if ctx.images and "images" not in result else result
        except (Cancelled, Interrupted) as exc:
            status, error, level = "cancelled", {"reason": "stopped", "detail": str(exc)}, "warning"
        except (StepFailed, Rejected, Refused) as exc:
            status, error, level = "failed", {"reason": type(exc).__name__, "message": str(exc)}, "warning"
        except OutcomeUnknown as exc:
            status, error, level = "failed", {"reason": "outcome_unknown", "message": str(exc)}, "error"
            svc._lock_service("outcome_unknown", str(exc), job.run_id)
        except DriverError as exc:
            status, error, level = "failed", {"reason": type(exc).__name__, "message": str(exc)}, "error"
        except Exception as exc:  # noqa: BLE001 - never kill the worker
            status, level = "failed", "error"
            error = {"reason": "internal_error", "message": f"{type(exc).__name__}: {exc}",
                     "trace": traceback.format_exc(limit=4)}
            svc._lock_service("internal_error", error["message"], job.run_id)
        if status == "succeeded" and svc._cancel.is_set():
            status, error, level = "cancelled", {"reason": "stop requested during the job"}, "warning"
        svc.record_devices(list(svc.module.devices))
        self._end(job.run_id, status, result, error)
        ctx.event(f"job.{status}", {"flow": job.flow, "error": error}, level)


def _jsonable(obj):
    import json

    if obj is None:
        return None
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=str))
