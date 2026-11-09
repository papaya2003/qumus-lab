"""Update the lab PC in one step (``scripts\\update.bat``): git pull main -> self-check -> restart services.

    python -m qumus_lab.update                # pull, check, restart a running launcher in the same mode
    python -m qumus_lab.update --no-restart   # pull and check only
    python -m qumus_lab.update --no-pull      # restart only (after a safe check)

Nothing is restarted while it would interrupt something: a module busy, locked, a heater on, or someone
holding control (the launcher checks again itself and refuses; see launcher.handle_shutdown_request).
A lock is never cleared by a restart. If the pull or the self-check fails, the running services are left
alone. Exit code: 0 done, 1 failed, 2 refused (nothing was restarted).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT, load_config
from .launcher import LAUNCHER_INFO, SHUTDOWN_REFUSED, SHUTDOWN_REQUEST

OK, FAILED, REFUSED = 0, 1, 2


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr).strip()


def _http_get_json(url: str, timeout_s: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _spawn(cmd: list[str], cwd: Path) -> None:
    env = dict(os.environ, PYTHONUTF8="1")
    if os.name == "nt":  # its own console window, like start-lab.bat; it outlives this script
        subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class Running:
    runs_dir: Path
    info: dict

    @property
    def ports(self) -> dict[str, int]:
        return {k: int(v) for k, v in (self.info.get("ports") or {}).items()}


@dataclass
class Updater:
    repo: Path = REPO_ROOT
    runs_dirs: list[Path] = field(default_factory=list)
    python: str = sys.executable
    run: Callable[[list[str], Path], tuple[int, str]] = _run
    get_json: Callable[[str], dict | None] = _http_get_json
    spawn: Callable[[list[str], Path], None] = _spawn
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    out: Callable[[str], None] = print
    stop_timeout_s: float = 30.0
    start_timeout_s: float = 120.0

    # ------------------------------------------------------------------ running launcher
    def find_running(self) -> Running | None:
        for d in self.runs_dirs:
            p = Path(d) / LAUNCHER_INFO
            try:
                info = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            r = Running(Path(d), info)
            if any(self.state(port) is not None for port in r.ports.values()):
                return r
        return None

    def state(self, port: int) -> dict | None:
        return self.get_json(f"http://127.0.0.1:{port}/api/state")

    def reasons(self, running: Running) -> list[str]:
        """Same rules as the launcher's own check, read over the services' state (GET only)."""
        out = []
        for name, port in running.ports.items():
            st = self.state(port)
            if st is None:
                out.append(f"{name}: service on port {port} does not answer")
                continue
            if st.get("busy"):
                out.append(f"{name}: busy with {st.get('busy_action')}")
            if st.get("locked"):
                out.append(f"{name}: locked ({(st.get('fault') or {}).get('code')}); a person must check and unlock first")
            if st.get("lease"):
                out.append(f"{name}: control held by {st['lease'].get('holder')} (untick 'Take control' / stop the agent)")
            for dev_id, d in (st.get("devices") or {}).items():
                if d.get("kind") == "heater" and d.get("phase") not in (None, "off", "reached"):
                    out.append(f"{dev_id}: heater {d.get('phase')}")
        return out

    # ------------------------------------------------------------------ steps
    def git_pull(self) -> bool:
        rc, branch = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], self.repo)
        if rc != 0:
            self.out(f"[FAIL] git: {branch}")
            return False
        if branch.strip() != "main":
            self.out(f"[FAIL] the repo is on branch {branch.strip()!r}, not main. Finish or put away that work "
                     "first (git switch main), then update.")
            return False
        rc, dirty = self.run(["git", "status", "--porcelain", "--untracked-files=no"], self.repo)
        if rc != 0 or dirty.strip():
            self.out("[FAIL] files changed on this PC and not committed:\n" + dirty +
                     "\nCommit them on a branch or undo them, then update.")
            return False
        _, old = self.run(["git", "rev-parse", "HEAD"], self.repo)
        rc, msg = self.run(["git", "pull", "--ff-only", "origin", "main"], self.repo)
        if rc != 0:
            self.out(f"[FAIL] git pull: {msg}")
            return False
        _, new = self.run(["git", "rev-parse", "HEAD"], self.repo)
        if old.strip() == new.strip():
            self.out(f"[OK] already up to date ({new.strip()[:7]})")
            return True
        _, log = self.run(["git", "log", "--oneline", f"{old.strip()}..{new.strip()}"], self.repo)
        self.out(f"[OK] updated {old.strip()[:7]} -> {new.strip()[:7]}\n{log}")
        _, changed = self.run(["git", "diff", "--name-only", old.strip(), new.strip()], self.repo)
        if "pyproject.toml" in changed.split():
            self.out("pyproject.toml changed: reinstalling packages")
            rc, msg = self.run([self.python, "-m", "pip", "install", "-e", ".[lab,dev]"], self.repo)
            if rc != 0:
                self.out(f"[FAIL] pip install: {msg[-2000:]}")
                return False
        return True

    def selfcheck(self) -> bool:
        rc, msg = self.run([self.python, "-m", "qumus_lab.selfcheck"], self.repo)
        tail = "\n".join(msg.splitlines()[-25:])
        self.out(tail)
        if rc != 0:
            self.out("[FAIL] self-check has FAIL lines; the running services were left as they are")
            return False
        self.out("[OK] self-check")
        return True

    def restart(self, running: Running) -> int:
        reasons = self.reasons(running)
        if reasons:
            self.out("[REFUSED] not restarting now:\n  " + "\n  ".join(reasons))
            return REFUSED
        req = running.runs_dir / SHUTDOWN_REQUEST
        refused = running.runs_dir / SHUTDOWN_REFUSED
        try:
            refused.unlink()
        except OSError:
            pass
        req.write_text(json.dumps({"by": "update.bat", "at_unix": time.time()}), encoding="utf-8")
        self.out("asked the running services to stop (stop axes, heat off, then exit)")
        t0 = self.clock()
        while self.clock() - t0 < self.stop_timeout_s:
            if refused.exists():
                try:
                    why = json.loads(refused.read_text(encoding="utf-8")).get("reasons", [])
                except (OSError, ValueError):
                    why = ["(unreadable)"]
                self.out("[REFUSED] the services refused to stop:\n  " + "\n  ".join(why))
                return REFUSED
            if not (running.runs_dir / LAUNCHER_INFO).exists() and all(
                    self.state(p) is None for p in running.ports.values()):
                break
            self.sleep(0.5)
        else:
            try:
                req.unlink()
            except OSError:
                pass
            self.out("[FAIL] the services did not stop in time; nothing was started. Check their window.")
            return FAILED
        argv = [str(a) for a in running.info.get("argv") or []]
        self.spawn([self.python, "-m", "qumus_lab.launcher", *argv], self.repo)
        mode = "DEMO" if running.info.get("demo") else "REAL instruments"
        self.out(f"starting the services again ({mode}) in a new window")
        t0 = self.clock()
        while self.clock() - t0 < self.start_timeout_s:
            if all(self.state(p) is not None for p in running.ports.values()):
                self.out("[OK] services are back: debug page http://127.0.0.1:8780/")
                url = running.runs_dir / "viewer-url.txt"
                if url.exists():
                    self.out("view-only page: " + url.read_text(encoding="utf-8").strip())
                return OK
            self.sleep(1.0)
        self.out("[FAIL] the services did not answer after the restart; look at their window")
        return FAILED

    def update(self, *, pull: bool = True, restart: bool = True) -> int:
        running = self.find_running()
        if running is not None and restart:
            reasons = self.reasons(running)
            if reasons:  # say so before pulling, so nothing half-happens
                self.out("[REFUSED] the services are in use; nothing was changed:\n  " + "\n  ".join(reasons))
                return REFUSED
        if pull and not self.git_pull():
            return FAILED
        if not self.selfcheck():
            return FAILED
        if not restart:
            return OK
        if running is None:
            self.out("services are not running; start them with scripts\\start-lab.bat (or start-demo.bat)")
            return OK
        return self.restart(running)


def default_runs_dirs(runs_dir: str | None) -> list[Path]:
    if runs_dir:
        return [Path(runs_dir)]
    dirs = [load_config(demo=False).runs_dir, load_config(demo=True).runs_dir]
    return list(dict.fromkeys(dirs))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qumus-update", description=__doc__.splitlines()[0])
    ap.add_argument("--no-pull", action="store_true", help="skip git pull")
    ap.add_argument("--no-restart", action="store_true", help="pull and check only")
    ap.add_argument("--runs-dir", default=None)
    args = ap.parse_args(argv)
    up = Updater(runs_dirs=default_runs_dirs(args.runs_dir))
    return up.update(pull=not args.no_pull, restart=not args.no_restart)


if __name__ == "__main__":
    sys.exit(main())
