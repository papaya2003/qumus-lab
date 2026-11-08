from __future__ import annotations

import json

from qumus_lab import selfcheck


def test_selfcheck_passes_here_and_writes_a_report(tmp_path, capsys):
    runs = tmp_path / "runs"
    code = selfcheck.main(["--runs-dir", str(runs)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[FAIL]" not in out
    for line in ("demo debug page", "demo exfoliation", "demo stacking", "real-mode build", "runs folder"):
        assert f"[PASS] {line}" in out
    report = next((runs / "selfcheck").glob("*-selfcheck.json"))
    data = json.loads(report.read_text(encoding="utf-8"))
    assert {r["status"] for r in data["results"]} <= {"PASS", "WARN", "SKIP"}


def test_selfcheck_bad_config_fails(tmp_path, capsys):
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "devices.yaml").write_text("modules: [unclosed\n", encoding="utf-8")
    code = selfcheck.main(["--config-dir", str(cdir), "--runs-dir", str(tmp_path / "runs"), "--no-demo"])
    out = capsys.readouterr().out
    assert code == 1 and "[FAIL] config" in out


def test_selfcheck_opens_no_instrument_port(tmp_path, monkeypatch):
    import qumus_lab.transport.serial_port as sp

    def refuse(self, spec):
        raise AssertionError(f"self-check opened {spec}")

    monkeypatch.setattr(sp.RealSerialFactory, "open", refuse)
    results = selfcheck.run_all(runs_dir=tmp_path / "runs")
    assert not [r for r in results if r.status == "FAIL"], results
