"""SQLite store for the lab state: what each instrument is doing now, where each object is now, what happened.

    device  one row per instrument: last readings (state_json), when they were read, online, conflict
    sample  one row per object (chip, tape, stamp, flake): location, who says so, when it was checked,
            which instrument holds it
    event   append-only history (the services' events)
    zone    who holds each shared zone (the middle board), kept across restarts
    run     one row per whole job (L5 flow), in the brain's RunStore shape
    step    the steps (checkpoints) of each run
    reservation  slots / samples a job has booked (kind, key)
    project the brain's project state pushed with put_records (flows read it)
    change  ordered, gap-free change feed for GET /api/records (sample, reservation, run, step, event)

Rules (same as the approved lab-state design, docs/lab-state.md):
- NULL means "not known". Nothing is guessed: a device row gets readings only from the instrument.
- One file per lab. The file records its bench ("real" or "demo"); opening it as the other is refused,
  so simulated and real records never mix.
- Every read and write goes through this class (no SQL elsewhere).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
LOCATION_SOURCES = ("camera", "inferred", "manual", "config")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  module        TEXT,
  state_json    TEXT,
  read_at       REAL,
  online        INTEGER NOT NULL DEFAULT 0,
  conflict_json TEXT,
  note          TEXT,
  updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sample (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  location         TEXT,
  status           TEXT,
  props_json       TEXT NOT NULL DEFAULT '{}',
  location_source  TEXT,
  location_checked REAL,
  held_by          TEXT,
  updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_unix     REAL NOT NULL,
  module      TEXT,
  level       TEXT NOT NULL,
  message     TEXT NOT NULL,
  fields_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS event_ts ON event (ts_unix);
CREATE TABLE IF NOT EXISTS zone (
  name        TEXT PRIMARY KEY,
  holder      TEXT,
  since_unix  REAL,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run (
  id           TEXT PRIMARY KEY,
  project_id   TEXT,
  flow         TEXT NOT NULL,
  module       TEXT,
  params_json  TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL,
  result_json  TEXT,
  error_json   TEXT,
  requested_by TEXT,
  meta_json    TEXT NOT NULL DEFAULT '{}',
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS step (
  run_id            TEXT NOT NULL,
  seq               INTEGER NOT NULL,
  name              TEXT NOT NULL,
  status            TEXT NOT NULL,
  safe_to_resume    INTEGER NOT NULL DEFAULT 1,
  measurements_json TEXT,
  note              TEXT,
  started_at        REAL,
  finished_at       REAL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS reservation (
  kind       TEXT NOT NULL,
  key        TEXT NOT NULL,
  row_json   TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS project (
  id         TEXT PRIMARY KEY,
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS change (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl      TEXT NOT NULL,
  key      TEXT NOT NULL,
  row_json TEXT,
  ts_unix  REAL NOT NULL
);
"""

RUN_STATUSES = ("queued", "running", "waiting_human", "parked", "paused", "succeeded", "failed", "cancelled")
TERMINAL_RUN = ("succeeded", "failed", "cancelled")
STEP_STATUSES = ("in_progress", "done", "failed", "skipped")
FEED_TABLES = ("sample", "reservation", "run", "step", "event")
#: store events (by message) that go into the change feed as brain events; everything else stays local
FEED_EVENT_PREFIXES = ("job.", "step.", "sample.", "safety.", "lab.")


class StoreError(Exception):
    pass


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False, sort_keys=True)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class LabStore:
    def __init__(self, path: str | Path, bench: str):
        if bench not in ("real", "demo"):
            raise StoreError(f"bench must be 'real' or 'demo', not {bench!r}")
        self.path = Path(path)
        self.bench = bench
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            row = self._db.execute("SELECT value FROM meta WHERE key='bench'").fetchone()
            if row is None:
                self._db.execute("INSERT INTO meta VALUES ('bench', ?)", (bench,))
                self._db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            if self._db.execute("SELECT value FROM meta WHERE key='lab_id'").fetchone() is None:
                import uuid
                self._db.execute("INSERT INTO meta VALUES ('lab_id', ?)", (f"qumus-lab-{bench}-{uuid.uuid4().hex[:12]}",))
            self.lab_id = self._db.execute("SELECT value FROM meta WHERE key='lab_id'").fetchone()["value"]
            stored = bench if row is None else row["value"]
        if stored != bench:
            self._db.close()
            raise StoreError(f"{self.path} belongs to the {stored!r} lab; refusing to open it as {bench!r}")

    @classmethod
    def for_runs_dir(cls, runs_dir: str | Path, demo: bool) -> LabStore:
        return cls(Path(runs_dir) / "qumus.db", "demo" if demo else "real")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ device
    def upsert_device(self, device_id: str, kind: str, *, module: str | None = None, state: dict | None = None,
                      read_at: float | None = None, online: bool | None = None, note: str | None = None) -> None:
        """Write what an instrument reported. ``state=None`` keeps the stored readings (and their read time):
        a device that was not read in this session keeps its last known values."""
        now = time.time()
        with self._lock, self._db:
            old = self._db.execute("SELECT * FROM device WHERE id=?", (device_id,)).fetchone()
            if old is None:
                self._db.execute(
                    "INSERT INTO device (id, kind, module, state_json, read_at, online, note, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (device_id, kind, module, None if state is None else _dumps(state),
                     read_at if state is not None else None, int(bool(online)), note, now))
                return
            self._db.execute(
                "UPDATE device SET kind=?, module=COALESCE(?, module), state_json=?, read_at=?, online=?, "
                "note=COALESCE(?, note), updated_at=? WHERE id=?",
                (kind, module, old["state_json"] if state is None else _dumps(state),
                 old["read_at"] if state is None else read_at,
                 old["online"] if online is None else int(bool(online)), note, now, device_id))

    def set_device_conflict(self, device_id: str, conflict: dict | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE device SET conflict_json=?, updated_at=? WHERE id=?",
                             (None if conflict is None else _dumps(conflict), time.time(), device_id))

    def get_device(self, device_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM device WHERE id=?", (device_id,)).fetchone()
        return None if row is None else self._device_dict(row)

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM device ORDER BY module, id").fetchall()
        return [self._device_dict(r) for r in rows]

    @staticmethod
    def _device_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "kind": row["kind"], "module": row["module"], "state": _loads(row["state_json"]),
                "read_at": row["read_at"], "online": bool(row["online"]), "conflict": _loads(row["conflict_json"]),
                "note": row["note"], "updated_at": row["updated_at"]}

    # ------------------------------------------------------------------ sample
    def upsert_sample(self, sample_id: str, kind: str, *, location: str | None = None, status: str | None = None,
                      props: dict | None = None, location_source: str | None = None,
                      location_checked: float | None = None, held_by: str | None = None) -> None:
        if location_source is not None and location_source not in LOCATION_SOURCES:
            raise StoreError(f"location_source must be one of {LOCATION_SOURCES}")
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sample (id, kind, location, status, props_json, location_source, location_checked, "
                "held_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, location=excluded.location, "
                "status=excluded.status, props_json=excluded.props_json, location_source=excluded.location_source, "
                "location_checked=excluded.location_checked, held_by=excluded.held_by, updated_at=excluded.updated_at",
                (sample_id, kind, location, status, _dumps(props or {}), location_source, location_checked,
                 held_by, now))
            self._feed_sample(sample_id)

    def set_sample_location(self, sample_id: str, location: str | None, source: str, *,
                            checked: float | None = None, held_by: str | None = None) -> None:
        if source not in LOCATION_SOURCES:
            raise StoreError(f"location source must be one of {LOCATION_SOURCES}")
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE sample SET location=?, location_source=?, location_checked=COALESCE(?, location_checked), "
                "held_by=?, updated_at=? WHERE id=?", (location, source, checked, held_by, time.time(), sample_id))
            if cur.rowcount == 0:
                raise StoreError(f"no sample {sample_id!r}")
            self._feed_sample(sample_id)

    def get_sample(self, sample_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM sample WHERE id=?", (sample_id,)).fetchone()
        return None if row is None else self._sample_dict(row)

    def list_samples(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sample ORDER BY id").fetchall()
        return [self._sample_dict(r) for r in rows]

    @staticmethod
    def _sample_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "kind": row["kind"], "location": row["location"], "status": row["status"],
                "props": _loads(row["props_json"]), "location_source": row["location_source"],
                "location_checked": row["location_checked"], "held_by": row["held_by"],
                "updated_at": row["updated_at"]}

    # ------------------------------------------------------------------ event
    def add_event(self, module: str | None, level: str, message: str, fields: dict | None = None,
                  ts_unix: float | None = None) -> int:
        ts = ts_unix if ts_unix is not None else time.time()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO event (ts_unix, module, level, message, fields_json) VALUES (?, ?, ?, ?, ?)",
                (ts, module, level, message, _dumps(fields or {})))
            seq = int(cur.lastrowid)
            if message.startswith(FEED_EVENT_PREFIXES):
                f = dict(fields or {})
                row = {"id": seq, "ts": ts, "kind": message, "run_id": f.pop("run_id", None),
                       "agent": module, "payload": dict(f, level=level)}
                self._change("event", str(seq), row)
            return seq

    def list_events(self, since: int = 0, limit: int = 200, module: str | None = None) -> list[dict]:
        sql = "SELECT * FROM event WHERE seq > ?"
        args: list[Any] = [since]
        if module:
            sql += " AND module = ?"
            args.append(module)
        sql += " ORDER BY seq LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{"seq": r["seq"], "ts_unix": r["ts_unix"], "module": r["module"], "level": r["level"],
                 "message": r["message"], "fields": _loads(r["fields_json"])} for r in rows]

    # ------------------------------------------------------------------ zone
    def set_zone_holder(self, zone: str, holder: str | None, since_unix: float | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO zone (name, holder, since_unix, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, since_unix=excluded.since_unix, "
                "updated_at=excluded.updated_at", (zone, holder, since_unix, time.time()))

    def zone_holders(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM zone").fetchall()
        return {r["name"]: {"holder": r["holder"], "since_unix": r["since_unix"]} for r in rows}

    # ------------------------------------------------------------------ change feed (GET /api/records)
    def _change(self, tbl: str, key: str, row: dict | None) -> None:
        """Append one change. Caller holds the lock inside a transaction."""
        self._db.execute("INSERT INTO change (tbl, key, row_json, ts_unix) VALUES (?, ?, ?, ?)",
                         (tbl, key, None if row is None else _dumps(row), time.time()))

    def _feed_sample(self, sample_id: str) -> None:
        row = self._db.execute("SELECT * FROM sample WHERE id=?", (sample_id,)).fetchone()
        self._change("sample", sample_id, None if row is None else self._sample_dict(row))

    def changes(self, since: int = 0, limit: int = 500) -> dict:
        """{changes: [{seq, table, key, row}], next, lab_id}. ``since`` = the last ``next`` (0 = from the start)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM change WHERE seq > ? ORDER BY seq LIMIT ?",
                                    (int(since), int(limit))).fetchall()
            head = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM change").fetchone()[0]
        out = [{"seq": r["seq"], "table": r["tbl"], "key": r["key"], "row": _loads(r["row_json"])} for r in rows]
        return {"changes": out, "next": out[-1]["seq"] if out else max(int(since), 0), "head": int(head),
                "lab_id": self.lab_id}

    def remove_sample(self, sample_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sample WHERE id=?", (sample_id,))
            self._change("sample", sample_id, None)

    def merge_sample(self, row: dict) -> None:
        """put_records: upsert a sample row as the brain sent it (last write wins). Unknown keys go to props."""
        sid, kind = row.get("id"), row.get("kind")
        if not isinstance(sid, str) or not sid or not isinstance(kind, str) or not kind:
            raise StoreError("sample row needs string 'id' and 'kind'")
        props = row.get("props") if isinstance(row.get("props"), dict) else {}
        old = self.get_sample(sid)
        self.upsert_sample(sid, kind, location=row.get("location"), status=row.get("status"), props=props,
                           location_source=(old or {}).get("location_source") if row.get("location") ==
                           (old or {}).get("location") else "manual",
                           location_checked=(old or {}).get("location_checked"), held_by=(old or {}).get("held_by"))

    # ------------------------------------------------------------------ runs and steps (brain RunStore shape)
    @staticmethod
    def _run_dict(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "project_id": r["project_id"], "flow": r["flow"], "params": _loads(r["params_json"]),
                "status": r["status"], "result": _loads(r["result_json"]), "error": _loads(r["error_json"]),
                "requested_by": r["requested_by"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                "bench": None, "module": r["module"], "meta": _loads(r["meta_json"]) or {}}

    def _feed_run(self, run_id: str) -> None:
        r = self._db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        if r is not None:
            d = self._run_dict(r)
            d["bench"] = self.bench
            self._change("run", run_id, d)

    def create_run(self, run_id: str, flow: str, module: str, params: dict, *, project_id: str | None,
                   requested_by: str | None, meta: dict | None = None) -> dict:
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO run (id, project_id, flow, module, params_json, status, requested_by, meta_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                (run_id, project_id, flow, module, _dumps(params), requested_by, _dumps(meta or {}), now, now))
            self._feed_run(run_id)
        return self.get_run(run_id)

    def update_run(self, run_id: str, *, status: str | None = None, result: Any = None, error: Any = None,
                   meta: dict | None = None) -> None:
        if status is not None and status not in RUN_STATUSES:
            raise StoreError(f"bad run status {status!r}")
        with self._lock, self._db:
            r = self._db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
            if r is None:
                raise StoreError(f"no run {run_id!r}")
            new_meta = dict(_loads(r["meta_json"]) or {}, **(meta or {}))
            self._db.execute(
                "UPDATE run SET status=?, result_json=?, error_json=?, meta_json=?, updated_at=? WHERE id=?",
                (status or r["status"], r["result_json"] if result is None else _dumps(result),
                 r["error_json"] if error is None else _dumps(error), _dumps(new_meta), time.time(), run_id))
            self._feed_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        if r is None:
            return None
        d = self._run_dict(r)
        d["bench"] = self.bench
        return d

    def list_runs(self, status: str | None = None) -> list[dict]:
        with self._lock:
            if status:
                rows = self._db.execute("SELECT id FROM run WHERE status=? ORDER BY created_at", (status,)).fetchall()
            else:
                rows = self._db.execute("SELECT id FROM run ORDER BY created_at").fetchall()
        return [r for row in rows if (r := self.get_run(row["id"])) is not None]

    @staticmethod
    def _step_dict(r: sqlite3.Row) -> dict:
        return {"run_id": r["run_id"], "seq": r["seq"], "name": r["name"], "status": r["status"],
                "safe_to_resume": bool(r["safe_to_resume"]), "measurements": _loads(r["measurements_json"]),
                "note": r["note"], "started_at": r["started_at"], "finished_at": r["finished_at"]}

    def _feed_step(self, run_id: str, seq: int) -> None:
        r = self._db.execute("SELECT * FROM step WHERE run_id=? AND seq=?", (run_id, seq)).fetchone()
        if r is not None:
            self._change("step", f"{run_id}:{seq}", self._step_dict(r))

    def begin_step(self, run_id: str, seq: int, name: str, *, safe_to_resume: bool = True) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO step (run_id, seq, name, status, safe_to_resume, started_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, seq) DO UPDATE SET status='in_progress', started_at=excluded.started_at",
                (run_id, seq, name, "in_progress", int(safe_to_resume), time.time()))
            self._feed_step(run_id, seq)

    def finish_step(self, run_id: str, seq: int, status: str, *, measurements: Any = None, note: str = "") -> None:
        if status not in STEP_STATUSES:
            raise StoreError(f"bad step status {status!r}")
        with self._lock, self._db:
            self._db.execute("UPDATE step SET status=?, measurements_json=?, note=?, finished_at=? "
                             "WHERE run_id=? AND seq=?",
                             (status, None if measurements is None else _dumps(measurements), note, time.time(),
                              run_id, seq))
            self._feed_step(run_id, seq)

    def get_steps(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM step WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [self._step_dict(r) for r in rows]

    # ------------------------------------------------------------------ reservations and project state
    def put_reservation(self, row: dict) -> None:
        kind, key = row.get("kind"), row.get("key")
        if not isinstance(kind, str) or not isinstance(key, str):
            raise StoreError("reservation row needs string 'kind' and 'key'")
        with self._lock, self._db:
            self._db.execute("INSERT INTO reservation (kind, key, row_json, updated_at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(kind, key) DO UPDATE SET row_json=excluded.row_json, "
                             "updated_at=excluded.updated_at", (kind, key, _dumps(row), time.time()))
            self._change("reservation", f"{kind}:{key}", row)

    def release_reservation(self, kind: str, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM reservation WHERE kind=? AND key=?", (kind, key))
            self._change("reservation", f"{kind}:{key}", None)

    def reservations(self, kind: str | None = None) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM reservation").fetchall()
        return {r["key"]: _loads(r["row_json"]) for r in rows if kind is None or r["kind"] == kind}

    def put_project(self, project_id: str, state: dict) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO project (id, state_json, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, "
                             "updated_at=excluded.updated_at", (project_id, _dumps(state or {}), time.time()))

    def get_project(self, project_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
        return None if r is None else {"id": r["id"], "state": _loads(r["state_json"]) or {}}
