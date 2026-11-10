"""Start the module services (exfoliation 8781, stacking 8782) and the debug page (8780) in one process.

    python -m qumus_lab.launcher --demo        # simulated devices, safe anywhere
    python -m qumus_lab.launcher               # lab PC: real ports from config/devices.yaml

One process on purpose: both heaters share one TP3005P supply (COM8), and Windows COM ports can be
opened by one process only. Ctrl+C -> safe_state (stop axes, heat off).

Network: the module services and the debug page (control) listen on loopback only. The view-only page
(cameras and readings, GET only, needs the access key) listens on 0.0.0.0:8790 so the bench can be
watched from another computer on the university network; ``--viewer-host 127.0.0.1`` or ``--no-viewer``
keeps it on this PC.

``scripts/update.bat`` restarts a running launcher through ``<runs_dir>/shutdown.request``; the launcher
refuses (and writes ``shutdown.refused``) while a module is busy, locked, heating, has a switch on, or
someone holds control.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import LabConfig, load_config
from .lab import Lab, build_lab, demo_backends, real_backends
from .modules.base import Module
from .modules.exfoliation import build_exfoliation
from .modules.stacking import build_stacking
from .safety.zones import ZoneRegistry
from .service.core import ContractService
from .service.http import make_server, serve_in_thread
from .service.viewer import lan_address, load_or_create_key, make_viewer
from .state.store import LabStore

BUILDERS = {"exfoliation": build_exfoliation, "stacking": build_stacking}


@dataclass
class Stack:
    config: LabConfig
    lab: Lab
    services: dict[str, ContractService]
    store: LabStore | None = None
    zones: ZoneRegistry | None = None

    def safe_state(self, reason: str) -> None:
        for svc in self.services.values():
            try:
                svc.safe_state(reason)
            except Exception as exc:  # noqa: BLE001
                logging.error("safe_state %s failed: %s", svc.name, exc)

    def unsafe_to_stop(self) -> list[str]:
        """Why a restart now would interrupt something (empty = safe to stop)."""
        out = []
        for name, svc in self.services.items():
            st = svc.state()
            if st["busy"]:
                out.append(f"{name}: busy with {st['busy_action']}")
            if st["locked"]:
                out.append(f"{name}: locked ({(st['fault'] or {}).get('code')}); a person must check and unlock first")
            if st["lease"]:
                out.append(f"{name}: control held by {st['lease']['holder']}")
            for dev in svc.module.devices.values():
                if dev.kind == "heater" and (getattr(dev, "heating", False)
                                             or getattr(dev, "phase", "off") not in ("off", "reached")):
                    out.append(f"{dev.id}: heater {dev.phase}")
            for dev_id, timer in list(svc.module._switch_timers.items()):
                if timer.is_alive():
                    out.append(f"{dev_id}: switched on")
        return out

    def close(self) -> None:
        for svc in self.services.values():
            svc.close()
        self.lab.close_all()
        if self.store is not None:
            self.store.close()


def build_stack(config: LabConfig, *, demo: bool, modules: list[str] | None = None, connect: bool = True,
                demo_speed: float = 5.0, use_store: bool = True) -> Stack:
    modules = modules or config.module_names
    backends = demo_backends(config, demo_speed) if demo else real_backends()
    lab = build_lab(config, backends, modules)
    if connect:
        lab.connect_all()
    services = {}
    journal = config.runs_dir / "events"
    store = LabStore.for_runs_dir(config.runs_dir, demo) if use_store else None
    world = getattr(backends, "world", None)
    if world is not None and getattr(world, "optics", None) is not None:
        world.optics.ledger = lambda: store   # demo microscope learns which chip is on the stage
    zone_events: list[tuple] = []
    zones = ZoneRegistry(config.points.get("zones"), store, lambda *ev: zone_events.append(ev))
    for name in modules:
        mod = Module(name, config, lab.module_devices(name), demo, lab.shared, zones)
        mod.lab_store = store
        mod.time_scale = float(demo_speed) if demo else 1.0   # ops waits (v1 sleeps) follow the demo clock
        mod.world = world if demo else None
        BUILDERS[name](mod)
        svc = ContractService(mod, journal_dir=journal, store=store)
        for dev_id, dev in mod.devices.items():
            if dev.last_error:
                svc.emit("device not connected", {"device": dev_id, "error": dev.last_error}, "warning")
        svc.emit("service started", {"demo": demo, "devices": sorted(mod.devices)})
        svc.record_devices(list(mod.devices), initial=True)
        services[name] = svc
    _route_zone_events(zones, services, zone_events)
    return Stack(config, lab, services, store, zones)


def _route_zone_events(zones: ZoneRegistry, services: dict[str, ContractService], early: list[tuple]) -> None:
    """Zone events go to the journal of every module that uses the zone."""
    def emit(message: str, fields: dict, level: str = "info") -> None:
        for svc in services.values():
            if fields.get("zone") in svc.module.zone_names():
                svc.emit(message, fields, level)

    zones._emit = emit
    for ev in early:
        emit(*ev)


SHUTDOWN_REQUEST = "shutdown.request"
SHUTDOWN_REFUSED = "shutdown.refused"
LAUNCHER_INFO = "launcher.json"


def handle_shutdown_request(stack: Stack, runs_dir: Path) -> bool:
    """True when a shutdown was requested and it is safe; otherwise writes the refusal reasons."""
    req = Path(runs_dir) / SHUTDOWN_REQUEST
    if not req.exists():
        return False
    try:
        info = json.loads(req.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        info = {}
    try:
        req.unlink()
    except OSError:
        pass
    reasons = stack.unsafe_to_stop()
    if reasons:
        (Path(runs_dir) / SHUTDOWN_REFUSED).write_text(
            json.dumps({"at_unix": time.time(), "request": info, "reasons": reasons}, indent=2), encoding="utf-8")
        logging.warning("restart refused: %s", "; ".join(reasons))
        return False
    logging.info("shutdown requested by %s", info.get("by", "?"))
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qumus-lab", description=__doc__.splitlines()[0])
    ap.add_argument("--demo", action="store_true", help="simulated devices (no hardware)")
    ap.add_argument("--modules", default="exfoliation,stacking")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (keep loopback; VPN is the access boundary)")
    ap.add_argument("--web-port", type=int, default=8780)
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--runs-dir", default=None)
    ap.add_argument("--demo-speed", type=float, default=5.0, help="--demo time factor for moves and heating")
    ap.add_argument("--viewer-host", default="0.0.0.0",
                    help="view-only camera page (GET only, access key); 127.0.0.1 keeps it on this PC")
    ap.add_argument("--viewer-port", type=int, default=8790)
    ap.add_argument("--no-viewer", action="store_true")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(raw_argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config_dir, args.runs_dir, demo=args.demo)
    modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    stack = build_stack(config, demo=args.demo, modules=modules, demo_speed=args.demo_speed)
    servers = []
    for name, svc in stack.services.items():
        port = int(config.module(name).get("port", 0))
        srv = make_server(args.host, port, service=svc)
        serve_in_thread(srv)
        servers.append(srv)
        logging.info("%s service on http://%s:%d  (%s)", name, args.host, port, "DEMO" if args.demo else "REAL")
    if not args.no_web:
        web = make_server(args.host, args.web_port, services=stack.services, web=True)
        serve_in_thread(web)
        servers.append(web)
        logging.info("debug page on http://%s:%d/", args.host, args.web_port)
    viewer_url = None
    if not args.no_viewer:
        key = load_or_create_key(config.runs_dir)
        viewer = make_viewer(args.viewer_host, args.viewer_port, stack.services, key)
        serve_in_thread(viewer)
        servers.append(viewer)
        shown = lan_address() if args.viewer_host in ("0.0.0.0", "") else args.viewer_host
        viewer_url = f"http://{shown}:{args.viewer_port}/?key={key}"
        (config.runs_dir / "viewer-url.txt").write_text(viewer_url + "\n", encoding="utf-8")
        logging.info("view-only page (cameras, readings; no control) on %s", viewer_url)
    for name, svc in stack.services.items():
        bad = {d: dev.last_error for d, dev in svc.module.devices.items() if not dev.connected}
        if bad:
            logging.warning("%s: not connected: %s", name, bad)

    info_path = config.runs_dir / LAUNCHER_INFO
    for stale in (SHUTDOWN_REQUEST, SHUTDOWN_REFUSED):
        try:
            (config.runs_dir / stale).unlink()
        except OSError:
            pass
    info_path.write_text(json.dumps({
        "pid": os.getpid(), "demo": args.demo, "argv": raw_argv, "started_unix": time.time(),
        "ports": {name: int(config.module(name).get("port", 0)) for name in stack.services},
        "web_port": None if args.no_web else args.web_port, "viewer_url": viewer_url,
    }, indent=2), encoding="utf-8")

    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, on_signal)
    try:
        while not stop.wait(0.5):
            if handle_shutdown_request(stack, config.runs_dir):
                break
    finally:
        logging.info("shutting down: safe_state (stop axes, heat off)")
        stack.safe_state("launcher shutdown")
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        stack.close()
        try:
            if json.loads(info_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                info_path.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
