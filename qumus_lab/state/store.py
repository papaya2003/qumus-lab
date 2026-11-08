"""SQLite store for the lab state: what each instrument is doing now, where each object is now, what happened.

    device  one row per instrument: last readings (state_json), when they were read, online, conflict
    sample  one row per object (chip, tape, stamp, flake): location, who says so, when it was checked,
            which instrument holds it
    event   append-only history (the services' events)
    zone    who holds each shared zone (the middle board), kept across restarts

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

SCHEMA_VERSION = 1
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
"""


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
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO event (ts_unix, module, level, message, fields_json) VALUES (?, ?, ?, ?, ?)",
                (ts_unix if ts_unix is not None else time.time(), module, level, message, _dumps(fields or {})))
            return int(cur.lastrowid)

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
