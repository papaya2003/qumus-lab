"""L1-L2 probe for BOTH modules (exfoliation + stacking), run on the lab PC.

    qumus-probe ports                       L1: COM ports seen by Windows vs config/devices.yaml (opens nothing)
    qumus-probe read all|<device>           L1: connect (query only) and read
    qumus-probe l1                          L1: ports + read every device of both modules, one at a time
    qumus-probe jog <device> <axis> <mm>    L2: one small move (caps), asks Enter first
    qumus-probe switch <device> on|off      L2: relay, 'on' turns itself off after <= 10 s
    qumus-probe heat <device> <target_c> --timeout S   L2: first heat <= 40 C, <= 6.5 V
    qumus-probe l2                          L2: guided walk through one actuator at a time (Enter / s / q)

Add --demo to try any command on simulated devices. Every step is logged to
runs/real-lab/probe/<date>-probe.jsonl. Ctrl+C at any time -> safe_state (stop axes, heat off).
Commands go through the same module actions and SafetyGate as the services (client "probe", loopback).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from ..config import LabConfig, load_config
from ..launcher import Stack, build_stack
from ..service.core import ApiError, ContractService
from ..transport.serial_port import list_serial_ports

CLIENT = "probe"


class ProbeLog:
    def __init__(self, runs_dir: Path):
        self.dir = runs_dir / "probe"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{time.strftime('%Y%m%d')}-probe.jsonl"

    def write(self, step: str, **fields) -> None:
        rec = {"ts_unix": round(time.time(), 3), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "step": step, **fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")


class Probe:
    def __init__(self, config: LabConfig, demo: bool, out=print, ask=input):
        self.config = config
        self.demo = demo
        self.out = out
        self.ask = ask
        self.log = ProbeLog(config.runs_dir)
        self.stack: Stack = build_stack(config, demo=demo, connect=False)
        self._hb_stop = threading.Event()
        self._hb = threading.Thread(target=self._heartbeat, daemon=True)
        self._hb.start()

    # ------------------------------------------------------------------ plumbing
    def _heartbeat(self) -> None:
        while not self._hb_stop.wait(1.0):
            for svc in self.stack.services.values():
                try:
                    svc.heartbeat(CLIENT)
                except ApiError:
                    pass

    def close(self) -> None:
        self._hb_stop.set()
        self.stack.close()

    def safe_state(self, reason: str) -> None:
        self.out(f"\n>>> safe_state ({reason}): stopping axes, heat off")
        self.stack.safe_state(reason)
        self.log.write("safe_state", reason=reason)

    def svc_for(self, device_id: str) -> ContractService:
        mod = self.config.module_of(device_id)
        if mod is None or mod not in self.stack.services:
            raise SystemExit(f"unknown device {device_id!r}. Devices: {', '.join(self.config.all_device_ids())}")
        return self.stack.services[mod]

    def connect(self, device_id: str) -> bool:
        lab = self.stack.lab
        dev = lab.devices[device_id]
        if dev.kind == "heater" and not lab.shared["power_supply"].connected:
            self._connect_one("power_supply", lab.shared["power_supply"])
        return self._connect_one(device_id, dev)

    def _connect_one(self, device_id, dev) -> bool:
        if dev.connected:
            return True
        try:
            dev.connect()
            dev.last_error = None
            self.log.write("connect", device=device_id, ok=True)
            return True
        except Exception as exc:  # noqa: BLE001
            dev.connected = False
            dev.last_error = f"{type(exc).__name__}: {exc}"
            self.log.write("connect", device=device_id, ok=False, error=dev.last_error)
            self.out(f"  [FAIL] {device_id}: connect failed: {dev.last_error}")
            return False

    def run(self, device_id: str, body: dict, timeout_s: float = 120.0) -> dict:
        svc = self.svc_for(device_id)
        svc.heartbeat(CLIENT)
        try:
            r = svc.command(body, CLIENT, loopback=True)
        except ApiError as exc:
            self.log.write("command", body=body, result="rejected", status=exc.status, message=exc.message)
            self.out(f"  [REFUSED {exc.status}] {exc.message}")
            return {"result": "rejected", "message": exc.message}
        cid = r["command_id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            lc = svc.last_command
            if lc and lc.get("id") == cid:
                self.log.write("command", body=body, **{k: lc.get(k) for k in ("result", "message", "output")})
                tag = "OK" if lc["result"] == "ok" else lc["result"].upper()
                self.out(f"  [{tag}] {body['action']} {lc.get('message') or ''}".rstrip())
                if lc.get("output") is not None:
                    self.out("        " + json.dumps(lc["output"], default=str)[:400])
                if svc.locked:
                    self.out(f"  !!! {svc.name} is LOCKED: {svc.fault}. Check the instrument, then unlock on the debug page.")
                return lc
            time.sleep(0.05)
        self.out("  [TIMEOUT] no result; treat as unknown outcome")
        self.safe_state("probe timeout")
        return {"result": "timeout"}

    # ------------------------------------------------------------------ L1
    def ports(self) -> list[dict]:
        seen = {p["port"].upper(): p for p in list_serial_ports()}
        rows = []
        self.out("L1 ports: config/devices.yaml vs ports Windows reports (nothing is opened)")
        for dev_id, port, what in self.config.configured_ports():
            hit = seen.get(port.upper())
            rows.append({"device": dev_id, "port": port, "present": bool(hit), "description": hit and hit["description"]})
            mark = "present" if hit else "MISSING"
            self.out(f"  {dev_id:22s} {what:10s} {port:7s} {mark:8s} {hit['description'] if hit else ''}")
        extra = sorted(set(seen) - {r["port"].upper() for r in rows})
        for port in extra:
            self.out(f"  {'(not in config)':22s} {'':10s} {port:7s} {'':8s} {seen[port]['description']}")
        self.out("  Kinesis serials, cameras and the Dracal exe are checked by `read`.")
        self.log.write("ports", rows=rows, unconfigured=extra)
        return rows

    def read(self, target: str) -> dict:
        ids = self.config.all_device_ids() if target == "all" else ([] if target == "power_supply" else [target])
        results = {}
        for dev_id in ids:
            svc = self.svc_for(dev_id)
            self.out(f"- {dev_id} ({svc.name})")
            if not self.connect(dev_id):
                results[dev_id] = {"ok": False, "error": self.stack.lab.devices[dev_id].last_error}
                continue
            lc = self.run(dev_id, {"action": "read", "device": dev_id})
            results[dev_id] = {"ok": lc.get("result") == "ok", "output": lc.get("output"), "message": lc.get("message")}
        if target in ("all", "power_supply"):
            psu = self.stack.lab.shared["power_supply"]
            self.out("- power_supply (shared by both heaters)")
            if self._connect_one("power_supply", psu):
                try:
                    out = psu.read()
                    results["power_supply"] = {"ok": out.get("out_v") is not None, "output": out,
                                               "message": None if out.get("out_v") is not None else "no VOUT1? reply"}
                    self.out(f"  [{'OK' if results['power_supply']['ok'] else 'NO REPLY'}] {json.dumps(out)}")
                except Exception as exc:  # noqa: BLE001
                    results["power_supply"] = {"ok": False, "error": str(exc)}
            else:
                results["power_supply"] = {"ok": False, "error": psu.last_error}
            self.log.write("read", device="power_supply", **results["power_supply"])
        ok = sum(1 for r in results.values() if r["ok"])
        self.out(f"L1 read: {ok}/{len(results)} devices answered")
        return results

    def l1(self) -> dict:
        self.ports()
        res = self.read("all")
        path = self.config.runs_dir / "probe" / f"{time.strftime('%Y%m%d-%H%M%S')}-l1-report.json"
        path.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        self.out(f"L1 report: {path}")
        return res

    # ------------------------------------------------------------------ L2
    def confirm(self, text: str) -> str:
        ans = self.ask(f"  {text}  [Enter = do it, s = skip, q = quit] ").strip().lower()
        return {"": "go", "s": "skip", "q": "quit"}.get(ans, "skip")

    def jog(self, device_id: str, axis: str, mm: float, *, confirm: bool = True) -> dict:
        if not self.connect(device_id):
            return {"result": "not_connected"}
        dev = self.stack.lab.devices[device_id]
        before = dev.read()
        self.out(f"  {device_id} now: {json.dumps(before, default=str)}")
        if confirm and self.confirm(f"jog {device_id} {axis} by {mm} mm?") != "go":
            return {"result": "skipped"}
        return self.run(device_id, {"action": "jog", "device": device_id, "axis": axis, "mm": mm})

    def switch(self, device_id: str, on: bool, auto_off_s: float = 10.0, *, confirm: bool = True) -> dict:
        if not self.connect(device_id):
            return {"result": "not_connected"}
        if confirm and self.confirm(f"switch {device_id} {'ON (auto-off ' + str(auto_off_s) + ' s)' if on else 'OFF'}?") != "go":
            return {"result": "skipped"}
        body = {"action": "set_switch", "device": device_id, "on": on}
        if on:
            body["auto_off_s"] = auto_off_s
        return self.run(device_id, body)

    def heat(self, device_id: str, target_c: float, timeout_s: float, volts: float | None = None, *,
             confirm: bool = True, watch: bool = True) -> dict:
        if not self.connect(device_id):
            return {"result": "not_connected"}
        dev = self.stack.lab.devices[device_id]
        self.out(f"  {device_id} now {dev.read()['temp_c']} C")
        if confirm and self.confirm(f"heat {device_id} to {target_c} C (timeout {timeout_s} s)?") != "go":
            return {"result": "skipped"}
        body = {"action": "heat", "device": device_id, "target_c": target_c, "timeout_s": timeout_s}
        if volts is not None:
            body["volts"] = volts
        lc = self.run(device_id, body)
        if lc.get("result") != "ok" or not watch:
            return lc
        self.out("  watching (Ctrl+C = heat off) ...")
        while dev.heating:
            s = dev.snapshot()
            self.out(f"    {time.strftime('%H:%M:%S')}  {s['temp_c']} C  phase {s['phase']}  {s['set_v']} V")
            self.log.write("heat_watch", device=device_id, **s)
            time.sleep(max(1.0, float(dev.poll_s)))
        self.out(f"  heater off: {dev.last_off_reason}; now {dev.snapshot()['temp_c']} C")
        return lc

    def l2_plan(self) -> list[tuple[str, str, dict]]:
        """One actuator at a time, smallest possible step. Both modules, left side first."""
        return [
            ("exfoliation", "switch exfoliation vacuum on (auto-off)", {"kind": "switch", "device": "exfoliation_vacuum"}),
            ("exfoliation", "switch exfoliation fan on (auto-off)", {"kind": "switch", "device": "exfoliation_fan"}),
            ("exfoliation", "jog left arm z +0.5 mm", {"kind": "jog", "device": "arm_left", "axis": "z", "mm": 0.5}),
            ("exfoliation", "jog left arm z -0.5 mm (back)", {"kind": "jog", "device": "arm_left", "axis": "z", "mm": -0.5}),
            ("exfoliation", "jog tape gantry z +0.5 mm", {"kind": "jog", "device": "tape_gantry", "axis": "z", "mm": 0.5}),
            ("exfoliation", "jog tape gantry z -0.5 mm (back)", {"kind": "jog", "device": "tape_gantry", "axis": "z", "mm": -0.5}),
            ("exfoliation", "heat exfoliation stage to 30 C (6.5 V, 300 s)", {"kind": "heat", "device": "exfoliation_heater",
                                                                             "target_c": 30, "timeout_s": 300, "volts": 6.5}),
            ("stacking", "switch sample-stage vacuum on (auto-off)", {"kind": "switch", "device": "stage_vacuum"}),
            ("stacking", "jog right arm z +0.5 mm", {"kind": "jog", "device": "arm_right", "axis": "z", "mm": 0.5}),
            ("stacking", "jog right arm z -0.5 mm (back)", {"kind": "jog", "device": "arm_right", "axis": "z", "mm": -0.5}),
            ("stacking", "jog sample stage x +0.5 mm", {"kind": "jog", "device": "sample_stage", "axis": "x", "mm": 0.5}),
            ("stacking", "jog sample stage x -0.5 mm (back)", {"kind": "jog", "device": "sample_stage", "axis": "x", "mm": -0.5}),
            ("stacking", "jog stamp z +0.1 mm (up, away from the chip)", {"kind": "jog", "device": "stamp", "axis": "z", "mm": 0.1}),
            ("stacking", "jog stamp z -0.1 mm (back)", {"kind": "jog", "device": "stamp", "axis": "z", "mm": -0.1}),
            ("stacking", "jog focus +0.02 mm (20 um)", {"kind": "jog", "device": "focus", "axis": "z", "mm": 0.02}),
            ("stacking", "jog focus -0.02 mm (back)", {"kind": "jog", "device": "focus", "axis": "z", "mm": -0.02}),
            ("stacking", "capture microscope camera", {"kind": "capture", "device": "microscope_camera"}),
            ("stacking", "heat transfer stage to 30 C (6.5 V, 300 s)", {"kind": "heat", "device": "transfer_heater",
                                                                       "target_c": 30, "timeout_s": 300, "volts": 6.5}),
        ]

    def l2(self, *, confirm: bool = True) -> list[dict]:
        self.out("L2 guided: one actuator at a time. Watch the instrument before pressing Enter.")
        done = []
        for module, text, step in self.l2_plan():
            if module not in self.stack.services:
                continue
            self.out(f"\n[{module}] {text}")
            if confirm:
                ans = self.confirm("ready?")
                if ans == "quit":
                    break
                if ans == "skip":
                    done.append({"step": text, "result": "skipped"})
                    continue
            kind, dev = step["kind"], step["device"]
            if kind == "switch":
                r = self.switch(dev, True, 10.0, confirm=False)
            elif kind == "jog":
                r = self.jog(dev, step["axis"], step["mm"], confirm=False)
            elif kind == "heat":
                r = self.heat(dev, step["target_c"], step["timeout_s"], step.get("volts"), confirm=False, watch=confirm)
                if not confirm:
                    self.run(dev, {"action": "heat_off", "device": dev})
            else:
                if not self.connect(dev):
                    r = {"result": "not_connected"}
                else:
                    r = self.run(dev, {"action": "capture", "camera": dev})
            done.append({"step": text, "result": r.get("result")})
            if confirm and self.ask("  did the instrument do exactly that? [y/n] ").strip().lower() != "y":
                self.log.write("l2_observation", step=text, observed="NOT as expected")
                self.out("  noted as NOT as expected (see the probe log)")
        self.log.write("l2_summary", steps=done)
        return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qumus-probe", description="Qumus lab L1-L2 probe (both modules)")
    ap.add_argument("--demo", action="store_true", help="simulated devices")
    ap.add_argument("--config-dir")
    ap.add_argument("--runs-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports")
    r = sub.add_parser("read")
    r.add_argument("device", help="device id or 'all'")
    sub.add_parser("l1")
    j = sub.add_parser("jog")
    j.add_argument("device")
    j.add_argument("axis")
    j.add_argument("mm", type=float)
    s = sub.add_parser("switch")
    s.add_argument("device")
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("--auto-off", type=float, default=10.0)
    h = sub.add_parser("heat")
    h.add_argument("device")
    h.add_argument("target_c", type=float)
    h.add_argument("--timeout", type=float, required=True, help="seconds; mandatory")
    h.add_argument("--volts", type=float, default=None)
    sub.add_parser("l2")
    args = ap.parse_args(argv)

    config = load_config(args.config_dir, args.runs_dir)
    probe = Probe(config, demo=args.demo)
    probe.out(f"qumus-probe {args.cmd} ({'DEMO, simulated devices' if args.demo else 'REAL instruments'}); "
              f"log: {probe.log.path}")
    probe.log.write("start", cmd=args.cmd, argv=sys.argv[1:], demo=args.demo)
    try:
        if args.cmd == "ports":
            probe.ports()
        elif args.cmd == "read":
            probe.read(args.device)
        elif args.cmd == "l1":
            probe.l1()
        elif args.cmd == "jog":
            probe.jog(args.device, args.axis, args.mm)
        elif args.cmd == "switch":
            probe.switch(args.device, args.state == "on", args.auto_off)
            if args.state == "on":
                probe.out(f"  waiting {args.auto_off} s for auto-off ...")
                time.sleep(args.auto_off + 0.5)
        elif args.cmd == "heat":
            probe.heat(args.device, args.target_c, args.timeout, args.volts)
        elif args.cmd == "l2":
            probe.l2()
    except KeyboardInterrupt:
        probe.safe_state("Ctrl+C")
        return 130
    finally:
        if args.cmd in ("jog", "switch", "heat", "l2"):
            probe.safe_state("probe finished")
        probe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
