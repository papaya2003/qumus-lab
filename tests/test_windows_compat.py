"""Pretend-Windows checks, run on any OS: no POSIX-only calls, explicit text encodings, file names and
paths that Windows accepts, CRLF .bat wrappers, and the Windows branches of the self-check."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path, PureWindowsPath

import yaml

from qumus_lab import selfcheck
from qumus_lab.config import REPO_ROOT, load_config
from qumus_lab.drivers.thermometer import dracal_argv
from qumus_lab.probe.cli import Probe

SRC = sorted((REPO_ROOT / "qumus_lab").rglob("*.py"))
POSIX_MODULES = {"fcntl", "termios", "pwd", "grp", "resource", "pty", "tty", "curses", "posix"}
POSIX_ATTRS = {
    ("os", "fork"), ("os", "getuid"), ("os", "geteuid"), ("os", "setsid"), ("os", "killpg"),
    ("os", "getpgid"), ("signal", "SIGALRM"), ("signal", "SIGHUP"), ("signal", "SIGKILL"),
    ("signal", "SIGUSR1"), ("signal", "SIGUSR2"), ("signal", "alarm"), ("signal", "SIGQUIT"),
}
WIN_BAD_CHARS = set('<>:"|?*')
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _trees():
    for path in SRC:
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_posix_only_modules_or_calls():
    bad = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [f"{path.name}: import {a.name}" for a in node.names if a.name.split(".")[0] in POSIX_MODULES]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in POSIX_MODULES:
                bad.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if (node.value.id, node.attr) in POSIX_ATTRS:
                    bad.append(f"{path.name}:{node.lineno} {node.value.id}.{node.attr}")
    assert not bad, bad


def _kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def test_text_io_names_its_encoding():
    """Windows defaults to the ANSI code page (cp936 on a Chinese PC); every text open says UTF-8."""
    bad = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id == "open":
                mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else "r"
                if "b" not in str(mode) and not _kw(node, "encoding"):
                    bad.append(f"{path.name}:{node.lineno} open() without encoding")
            elif isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text") and not _kw(node, "encoding"):
                bad.append(f"{path.name}:{node.lineno} {f.attr}() without encoding")
            elif isinstance(f, ast.Attribute) and f.attr in ("check_output", "run", "Popen") and _kw(node, "text"):
                if not (_kw(node, "encoding") or _kw(node, "errors")):
                    bad.append(f"{path.name}:{node.lineno} subprocess text without encoding/errors")
    assert not bad, bad


def test_no_glibc_only_strftime_flags():
    bad = [p.name for p in SRC if re.search(r"%-[a-zA-Z]", p.read_text(encoding="utf-8"))]
    assert not bad, bad  # %-d etc. raise ValueError on Windows


def _windows_ok(name: str) -> bool:
    stem = name.split(".")[0].upper()
    return (not (set(name) & WIN_BAD_CHARS) and not name.endswith((" ", ".")) and stem not in WIN_RESERVED
            and all(ord(c) >= 32 for c in name))


def test_files_written_in_a_session_have_windows_names(tmp_path):
    runs = tmp_path / "实验 runs" / "real-lab"  # a space and Chinese characters, like a Windows user folder
    cfg = load_config(runs_dir=runs)
    probe = Probe(cfg, demo=True, out=lambda *a: None, ask=lambda *a: "")
    try:
        probe.stack.lab.connect_all()
        probe.l1()
        for cam in ("desk_camera_left", "microscope_camera"):
            probe.stack.lab.devices[cam].capture()
    finally:
        probe.close()
    assert selfcheck.main(["--runs-dir", str(runs), "--no-demo"]) in (0, 1)
    written = [p for p in runs.rglob("*")]
    assert any(p.suffix == ".jsonl" for p in written) and any("captures" in p.parts for p in written)
    bad = [str(p.relative_to(runs)) for p in written if not all(_windows_ok(part) for part in p.relative_to(runs).parts)]
    assert not bad, bad
    longest = max(len(str(p.relative_to(runs))) for p in written)
    assert longest < 120  # leaves room under MAX_PATH (260) for C:\Users\<name>\...\runs\real-lab\
    for p in written:
        if p.suffix in (".json", ".jsonl"):
            p.read_text(encoding="utf-8")  # valid UTF-8


def test_windows_paths_in_config_stay_untouched():
    cfg = load_config()
    paths = selfcheck.vendor_paths(cfg)
    assert {label.split(" (")[0] for label, _ in paths} == {"Dracal exe", "Uvcham SDK folder", "Kinesis folder"}
    for _, p in paths:
        assert PureWindowsPath(p).is_absolute() and PureWindowsPath(p).drive == "C:"
    exe = cfg.devices["shared"]["dracal"]["exe_path"]
    assert dracal_argv(exe, "E30559")[0] == exe  # passed to Windows as written, no POSIX Path() rewrite


def test_bat_wrappers():
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.bat\s+text\s+eol=crlf", attrs, re.M)  # checked out with CRLF on Windows
    bats = sorted((REPO_ROOT / "scripts").glob("*.bat"))
    assert {b.name for b in bats} >= {"setup.bat", "probe.bat", "check.bat", "start-lab.bat", "start-demo.bat"}
    for b in bats:
        text = b.read_bytes().decode("ascii")  # cmd.exe reads .bat in the OEM code page: keep them ASCII
        assert text.lstrip().lower().startswith("@echo off"), b.name
        assert 'cd /d "%~dp0\\.."' in text, b.name  # works from any current folder, paths with spaces
        if b.name != "setup.bat":
            assert "PYTHONUTF8=1" in text, b.name


# ---------------------------------------------------------------- self-check Windows branches


def test_selfcheck_windows_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = load_config(runs_dir=tmp_path / "runs")

    def importer(name):
        if name == "clr":
            raise ImportError(name)
        return __import__("json")

    pk = {r.name: r.status for r in selfcheck.check_packages(importer)}
    assert pk["package pythonnet"] == "WARN" and pk["package pyserial"] == "PASS"

    exe = tmp_path / "dracal-usb-get.exe"
    exe.write_text("", encoding="utf-8")
    cfg.devices["shared"]["dracal"]["exe_path"] = str(exe)
    cfg.devices["modules"]["stacking"]["devices"]["stamp"]["kinesis_dir"] = str(tmp_path / "missing-kinesis")
    vp = {r.name: r.status for r in selfcheck.check_vendor_paths(cfg)}
    assert vp["Dracal exe"] == "PASS" and vp["Kinesis folder (stamp)"] == "WARN"

    all_ports = [{"port": p.lower()} for _, p, _ in cfg.configured_ports()]  # Windows may report com7
    assert selfcheck.check_com_ports(cfg, lister=lambda: all_ports)[0].status == "PASS"
    some = selfcheck.check_com_ports(cfg, lister=lambda: all_ports[1:])[0]
    assert some.status == "WARN" and "power_supply COM8" in some.detail


def test_selfcheck_duplicate_port_fails(tmp_path):
    cfg = load_config(runs_dir=tmp_path / "runs")
    cdir = tmp_path / "config"
    cdir.mkdir()
    devices = cfg.devices
    devices["modules"]["stacking"]["devices"]["turret"]["port"]["port"] = "COM7"  # COM7 is arm_left
    for name, data in (("devices", devices), ("points", cfg.points), ("limits", cfg.limits)):
        (cdir / f"{name}.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    _, res = selfcheck.check_config(Path(cdir), tmp_path / "runs")
    by = {r.name: r for r in res}
    assert by["config"].status == "PASS"
    assert by["config ports"].status == "FAIL" and "COM7" in by["config ports"].detail
