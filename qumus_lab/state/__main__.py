"""Look at (and hand-edit samples in) runs/<lab>/qumus.db.

    python -m qumus_lab.state devices                  (Windows: scripts\\state.bat devices)
    python -m qumus_lab.state samples
    python -m qumus_lab.state events --since 0 --limit 50
    python -m qumus_lab.state zones
    python -m qumus_lab.state sample-set chip_001 blank_chip left_board.L3   (source: manual)

Add --demo for the demo lab's file (runs/demo/qumus.db). Safe while the services run (SQLite WAL).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from ..config import load_config
from .store import LabStore


def _t(ts: float | None) -> str:
    return "never" if ts is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qumus-state", description="Show the lab state tables in qumus.db")
    ap.add_argument("--demo", action="store_true", help="the demo lab's file (runs/demo/qumus.db)")
    ap.add_argument("--runs-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices")
    sub.add_parser("samples")
    ev = sub.add_parser("events")
    ev.add_argument("--since", type=int, default=0)
    ev.add_argument("--limit", type=int, default=50)
    ev.add_argument("--module")
    sub.add_parser("zones")
    ss = sub.add_parser("sample-set", help="a person records where an object is (source: manual)")
    ss.add_argument("id")
    ss.add_argument("kind")
    ss.add_argument("location")
    ss.add_argument("--status")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    cfg = load_config(runs_dir=args.runs_dir, demo=args.demo)
    store = LabStore.for_runs_dir(cfg.runs_dir, args.demo)
    print(f"{store.path} ({store.bench} lab)")
    try:
        if args.cmd == "devices":
            for d in store.list_devices():
                flag = "online " if d["online"] else "offline"
                print(f"  {d['id']:22s} {d['kind']:12s} {flag} read {_t(d['read_at'])}")
                if d["state"] is not None:
                    print(f"      {json.dumps(d['state'], ensure_ascii=False)[:300]}")
                if d["conflict"]:
                    print(f"      CONFLICT {json.dumps(d['conflict'], ensure_ascii=False)}")
        elif args.cmd == "samples":
            rows = store.list_samples()
            if not rows:
                print("  no objects recorded yet")
            for s in rows:
                held = f" held by {s['held_by']}" if s["held_by"] else ""
                print(f"  {s['id']:16s} {s['kind']:14s} at {s['location']} ({s['location_source'] or 'unknown'}, "
                      f"checked {_t(s['location_checked'])}){held} {s['status'] or ''}")
        elif args.cmd == "events":
            for e in store.list_events(args.since, args.limit, args.module):
                print(f"  {e['seq']:6d} {_t(e['ts_unix'])} {e['module'] or '':12s} {e['level']:7s} {e['message']} "
                      f"{json.dumps(e['fields'], ensure_ascii=False)[:200]}")
        elif args.cmd == "zones":
            zones = store.zone_holders()
            if not zones:
                print("  no zone was ever taken")
            for name, z in zones.items():
                print(f"  {name}: {'held by ' + z['holder'] if z['holder'] else 'free'} (since {_t(z['since_unix'])})")
        elif args.cmd == "sample-set":
            old = store.get_sample(args.id)
            store.upsert_sample(args.id, args.kind, location=args.location, status=args.status,
                                props=(old or {}).get("props"), location_source="manual",
                                location_checked=time.time())
            store.add_event(None, "info", "sample.manual", {"id": args.id, "old": old and old["location"],
                                                             "new": args.location})
            print(f"  {args.id} -> {args.location} (manual)")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
