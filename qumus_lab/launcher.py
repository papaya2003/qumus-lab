"""Start the module services (exfoliation 8781, stacking 8782) and the debug page (8780) in one process.

    python -m qumus_lab.launcher --demo        # simulated devices, safe anywhere
    python -m qumus_lab.launcher               # lab PC: real ports from config/devices.yaml

One process on purpose: both heaters share one TP3005P supply (COM8), and Windows COM ports can be
opened by one process only. Loopback only by default. Ctrl+C -> safe_state (stop axes, heat off).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from dataclasses import dataclass

from .config import LabConfig, load_config
from .lab import Lab, build_lab, demo_backends, real_backends
from .modules.base import Module
from .modules.exfoliation import build_exfoliation
from .modules.stacking import build_stacking
from .safety.zones import ZoneRegistry
from .service.core import ContractService
from .service.http import make_server, serve_in_thread
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
    zone_events: list[tuple] = []
    zones = ZoneRegistry(config.points.get("zones"), store, lambda *ev: zone_events.append(ev))
    for name in modules:
        mod = Module(name, config, lab.module_devices(name), demo, lab.shared, zones)
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
    args = ap.parse_args(argv)
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
    for name, svc in stack.services.items():
        bad = {d: dev.last_error for d, dev in svc.module.devices.items() if not dev.connected}
        if bad:
            logging.warning("%s: not connected: %s", name, bad)

    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, on_signal)
    try:
        while not stop.wait(0.5):
            pass
    finally:
        logging.info("shutting down: safe_state (stop axes, heat off)")
        stack.safe_state("launcher shutdown")
        for srv in servers:
            srv.shutdown()
        stack.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
