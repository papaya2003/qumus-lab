"""Lab PC self-check: is this PC ready for L1? Opens no instrument port, moves nothing.

    python -m qumus_lab.selfcheck            (Windows: scripts\\check.bat)

Checks Python, the packages, the config, the vendor paths in devices.yaml, which COM ports Windows
sees, that runs/ is writable, that the service ports are free, and that the demo stack starts and the
debug page and both module APIs answer (on temporary ports, with simulated devices).
Each line is PASS, WARN (works, but look at it) or FAIL (fix before L1). Exit code 1 on any FAIL.
A JSON copy goes to runs/real-lab/selfcheck/.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import socket
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
SERVICE_PORTS = {"debug page": 8780}


@dataclass
class Result:
    name: str
    status: str
    detail: str


def _is_windows() -> bool:
    return sys.platform == "win32"


def check_python() -> list[Result]:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 11):
        out = [Result("python", FAIL, f"Python {ver}; need 3.11 or newer (lab PC: 3.13 from python.org)")]
    elif (v.major, v.minor) != (3, 13):
        out = [Result("python", WARN, f"Python {ver}; the lab PC target is 3.13")]
    else:
        out = [Result("python", PASS, f"Python {ver}")]
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    out.append(Result("venv", PASS if in_venv else WARN,
                      sys.prefix if in_venv else "not in a virtual environment (run scripts\\setup.bat)"))
    osname = f"{platform.system()} {platform.release()}"
    out.append(Result("os", PASS if _is_windows() else WARN,
                      osname if _is_windows() else f"{osname}: not Windows, fine for --demo only"))
    return out


def _version(mod) -> str:
    return str(getattr(mod, "__version__", getattr(mod, "VERSION", "")) or "installed")


def check_packages(importer: Callable[[str], object] = importlib.import_module) -> list[Result]:
    out = []
    for mod, pip_name in (("serial", "pyserial"), ("yaml", "pyyaml")):
        try:
            out.append(Result(f"package {pip_name}", PASS, _version(importer(mod))))
        except ImportError:
            out.append(Result(f"package {pip_name}", FAIL, "missing: run scripts\\setup.bat"))
    lab = [("cv2", "opencv-python", "desk cameras"), ("numpy", "numpy", "cameras")]
    if _is_windows():
        lab.append(("clr", "pythonnet", "Kinesis stamp axes"))
    for mod, pip_name, why in lab:
        try:
            out.append(Result(f"package {pip_name}", PASS, _version(importer(mod))))
        except ImportError:
            out.append(Result(f"package {pip_name}", WARN, f"missing (needed for {why}; --demo works without)"))
    return out


def check_config(config_dir: Path | None, runs_dir: Path | None):
    from .config import load_config

    try:
        cfg = load_config(config_dir, runs_dir)
    except Exception as exc:  # noqa: BLE001 - any parse error is the answer
        return None, [Result("config", FAIL, f"{type(exc).__name__}: {exc}")]
    n = len(cfg.all_device_ids())
    out = [Result("config", PASS, f"{cfg.config_dir}: {len(cfg.module_names)} modules, {n} devices")]
    by_port: dict[str, list[str]] = {}
    for dev, port, _ in cfg.configured_ports():
        by_port.setdefault(port.upper(), []).append(dev)
    dup = {p: d for p, d in by_port.items() if len(d) > 1}
    out.append(Result("config ports", FAIL if dup else PASS,
                      f"same COM port twice: {dup}" if dup else f"{len(by_port)} COM ports, no duplicates"))
    try:
        from .launcher import build_stack

        stack = build_stack(cfg, demo=False, connect=False)
        stack.close()
        out.append(Result("real-mode build", PASS, "both services build from the config (no port opened)"))
    except Exception as exc:  # noqa: BLE001
        out.append(Result("real-mode build", FAIL, f"{type(exc).__name__}: {exc}"))
    return cfg, out


def vendor_paths(cfg) -> list[tuple[str, str]]:
    paths = []
    exe = cfg.devices.get("shared", {}).get("dracal", {}).get("exe_path")
    if exe:
        paths.append(("Dracal exe", exe))
    for dev_id in cfg.all_device_ids():
        dcfg = cfg.device_cfg(dev_id)
        for key, label in (("sdk_dir", "Uvcham SDK folder"), ("kinesis_dir", "Kinesis folder")):
            if dcfg.get(key):
                paths.append((f"{label} ({dev_id})", dcfg[key]))
    return paths


def check_vendor_paths(cfg) -> list[Result]:
    out = []
    for label, p in vendor_paths(cfg):
        if not _is_windows():
            out.append(Result(label, SKIP, f"{p} (checked on Windows only)"))
        elif Path(p).exists():
            out.append(Result(label, PASS, p))
        else:
            out.append(Result(label, WARN, f"{p} not found: fix devices.yaml before L1 of that device"))
    return out


def check_com_ports(cfg, lister=None) -> list[Result]:
    from .transport.serial_port import list_serial_ports

    seen = {p["port"].upper() for p in (lister or list_serial_ports)()}
    missing = [f"{dev} {port}" for dev, port, _ in cfg.configured_ports() if port.upper() not in seen]
    total = len(cfg.configured_ports())
    if not _is_windows():
        return [Result("COM ports", SKIP, "not Windows (probe ports lists them on the lab PC)")]
    if missing:
        return [Result("COM ports", WARN, f"{total - len(missing)}/{total} configured ports visible; "
                                          f"missing: {', '.join(missing)} (powered on? USB plugged?)")]
    return [Result("COM ports", PASS, f"all {total} configured ports visible (none opened)")]


def check_runs_dir(cfg) -> list[Result]:
    d = Path(cfg.runs_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".selfcheck-{int(time.time() * 1000)}.tmp"
        probe.write_text("ok 中文\n", encoding="utf-8")
        ok = probe.read_text(encoding="utf-8") == "ok 中文\n"
        probe.unlink()
    except OSError as exc:
        return [Result("runs folder", FAIL, f"{d}: {exc}")]
    return [Result("runs folder", PASS if ok else FAIL, f"{d} writable (UTF-8)")]


def check_service_ports(cfg) -> list[Result]:
    ports = dict(SERVICE_PORTS)
    for m in cfg.module_names:
        ports[f"{m} service"] = int(cfg.module(m).get("port", 0))
    busy = []
    for label, port in ports.items():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            busy.append(f"{label} {port}")
        finally:
            s.close()
    if busy:
        return [Result("service ports", WARN, f"in use: {', '.join(busy)} (qumus-lab already running? "
                                              "close it before starting again)")]
    return [Result("service ports", PASS, ", ".join(f"{p}" for p in ports.values()) + " free")]


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Qumus-Client": "selfcheck"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _wait_result(state_url: str, command_id: str, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        last = json.loads(_get(state_url)[1]).get("last_command") or {}
        if last.get("id") == command_id and last.get("result"):
            return f"{last['result']}" + (f" ({last.get('message')})" if last["result"] != "ok" else "")
        time.sleep(0.05)
    return "no result in time"


def check_demo(cfg) -> list[Result]:
    """Start the demo stack in this process on temporary ports and talk to it over HTTP."""
    from .launcher import build_stack
    from .service.http import make_server, serve_in_thread

    tmp = tempfile.TemporaryDirectory(prefix="qumus-selfcheck-")
    demo_cfg = cfg.copy()
    demo_cfg.runs_dir = Path(tmp.name)
    stack = srv = None
    try:
        stack = build_stack(demo_cfg, demo=True, demo_speed=50.0)
        srv = make_server("127.0.0.1", 0, services=stack.services, web=True)
        serve_in_thread(srv)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        status, page = _get(base + "/")
        if status != 200 or b"STOP ALL" not in page:
            return [Result("demo", FAIL, f"debug page answered {status} without the STOP ALL button")]
        mods = json.loads(_get(base + "/modules.json")[1])
        names = mods.get("modules", [])
        out = []
        for m in cfg.module_names:
            d = json.loads(_get(f"{base}/m/{m}/api/describe")[1])
            st = json.loads(_get(f"{base}/m/{m}/api/state")[1])
            off = [k for k, v in st["devices"].items() if not v.get("connected", True)]
            read = _post(f"{base}/m/{m}/api/command", {"action": "read_all"})
            last = _wait_result(f"{base}/m/{m}/api/state", read["command_id"])
            if d.get("contract") != 1 or off or last != "ok":
                out.append(Result(f"demo {m}", FAIL, f"contract {d.get('contract')}, not connected: {off}, "
                                                     f"read_all: {last}"))
            else:
                out.append(Result(f"demo {m}", PASS, f"{len(st['devices'])} simulated devices, read_all ok"))
        out.insert(0, Result("demo debug page", PASS, f"page and {len(names)} module APIs answer"))
        return out
    except Exception as exc:  # noqa: BLE001
        return [Result("demo", FAIL, f"{type(exc).__name__}: {exc}")]
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        if stack is not None:
            stack.safe_state("selfcheck done")
            stack.close()
        try:
            tmp.cleanup()
        except OSError:  # Windows may still hold a file for a moment; the folder is in %TEMP%
            pass


def run_all(config_dir: Path | None = None, runs_dir: Path | None = None, demo: bool = True) -> list[Result]:
    results = check_python() + check_packages()
    cfg, res = check_config(config_dir, runs_dir)
    results += res
    if cfg is not None:
        results += check_vendor_paths(cfg) + check_com_ports(cfg) + check_runs_dir(cfg) + check_service_ports(cfg)
        if demo:
            results += check_demo(cfg)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qumus-selfcheck", description=__doc__.splitlines()[0])
    ap.add_argument("--config-dir", type=Path)
    ap.add_argument("--runs-dir", type=Path)
    ap.add_argument("--no-demo", action="store_true", help="skip starting the demo stack")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console: keep non-ASCII paths printable
    except (AttributeError, ValueError):
        pass
    results = run_all(args.config_dir, args.runs_dir, demo=not args.no_demo)
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"[{r.status}] {r.name:<{width}}  {r.detail}")
    fails = [r for r in results if r.status == FAIL]
    warns = [r for r in results if r.status == WARN]
    print()
    print(f"{len(results)} checks: {len(fails)} FAIL, {len(warns)} WARN.",
          "Fix the FAIL lines before L1." if fails else "Ready for L1 (read the WARN lines).")
    try:
        from .config import load_config

        out_dir = Path(load_config(args.config_dir, args.runs_dir).runs_dir) / "selfcheck"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-selfcheck.json"
        path.write_text(json.dumps({"ts_unix": time.time(), "results": [asdict(r) for r in results]},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Report: {path}")
    except Exception as exc:  # noqa: BLE001 - the report is a convenience
        print(f"(report not written: {exc})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
