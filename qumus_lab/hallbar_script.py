"""L6: a scripted Hall bar (no AI). Talks to the two module windows over contract v1 only, so it runs the
same against qumus-lab (real or --demo) and qumus-simlab, on this computer or on another one: only the URLs
change (e.g. the ends of SSH tunnels on 127.0.0.1:18781 / 18782).

    python -m qumus_lab.hallbar_script --exfoliation http://127.0.0.1:8781 --stacking http://127.0.0.1:8782

Steps (the fixed v1 order, decisions by simple rules instead of the agent):
  1. exfoliate_chips hBN (1 chip), then Gr (1 chip)
  2. search_chip on each exfoliated chip
  3. choose: hBN = largest catalogued flake labelled 10-20 / 20-30 nm (else any hBN);
             Gr  = largest catalogued monolayer smaller than half the hBN (else the smallest monolayer)
  4. stack_device [hBN, Gr] onto the electrode chip (props.electrodes), v1 Hall-device order
  5. cool_experiment_stages
The lease is renewed every second on both windows while the script runs (a lost lease stops the job).
Progress is followed through GET /api/records (run rows), never through files.

Standard library only: this file can be copied to the brain computer and run there.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

CLIENT = "l6-hallbar-script"
TERMINAL = {"succeeded", "failed", "cancelled", "parked", "waiting_human", "paused"}


class Station:
    def __init__(self, name: str, url: str, client: str = CLIENT, timeout: float = 30.0):
        self.name, self.url, self.client, self.timeout = name, url.rstrip("/"), client, timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-Qumus-Client": self.client, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": str(e)}

    def describe(self) -> dict:
        return self._req("GET", "/api/describe")[1]

    def heartbeat(self) -> dict:
        return self._req("POST", "/api/heartbeat", {})[1]

    def records(self, since: int, limit: int = 2000) -> dict:
        return self._req("GET", f"/api/records?since={since}&limit={limit}")[1]

    def command(self, action: str, **params) -> dict:
        status, out = self._req("POST", "/api/command", dict(params, action=action))
        if status >= 400:
            raise RuntimeError(f"{self.name} {action}: HTTP {status} {out.get('error') or out}")
        return out


class Ledger:
    """The lab's records, mirrored from GET /api/records (what the brain does with its own store)."""

    def __init__(self, station: Station):
        self.st, self.since, self.lab_id = station, 0, None
        self.samples: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.steps: dict[str, dict] = {}

    def pull(self) -> None:
        while True:
            page = self.st.records(self.since)
            if self.lab_id is not None and page.get("lab_id") != self.lab_id:
                self.__init__(self.st)          # the lab started a new store: start again
                continue
            self.lab_id = page.get("lab_id")
            for ch in page.get("changes", []):
                table = {"sample": self.samples, "run": self.runs, "step": self.steps}.get(ch["table"])
                if table is None:
                    continue
                if ch["row"] is None:
                    table.pop(ch["key"], None)
                else:
                    table[ch["key"]] = ch["row"]
            nxt = page.get("next", self.since)
            if nxt == self.since or not page.get("changes"):
                return
            self.since = nxt

    def kind(self, kind: str) -> list[dict]:
        return [s for s in self.samples.values() if s.get("kind") == kind]


def _material(s: dict) -> str:
    m = str((s.get("props") or {}).get("material") or "").lower()
    return "hBN" if m.startswith("h") or "bn" in m else ("Gr" if m.startswith("g") else m)


def _area(s: dict) -> float:
    p = s.get("props") or {}
    for k in ("area_um2_20x", "area_um2"):
        if isinstance(p.get(k), (int, float)):
            return float(p[k])
    return 0.0


def _label(s: dict) -> str:
    p = s.get("props") or {}
    labels = p.get("labels") or {}
    return str(labels.get("10x") or labels.get("5x") or p.get("thickness_label") or "")


def choose_flakes(flakes: list[dict]) -> tuple[dict, dict]:
    usable = [f for f in flakes if f.get("status") in ("catalogued", "candidate")]
    hbn = [f for f in usable if _material(f) == "hBN"]
    gr = [f for f in usable if _material(f) == "Gr"]
    if not hbn or not gr:
        raise RuntimeError(f"need hBN and Gr flakes, have {len(hbn)} hBN and {len(gr)} Gr")
    good = [f for f in hbn if _label(f) in ("10-20", "20-30")] or hbn
    h = max(good, key=_area)
    mono = [f for f in gr if _label(f) == "monolayer"] or gr
    fit = [f for f in mono if _area(f) <= 0.5 * _area(h)]
    g = max(fit, key=_area) if fit else min(mono, key=_area)
    return h, g


class Script:
    def __init__(self, exf: Station, stk: Station, log=print, job_timeout_s: float = 7200.0):
        self.exf, self.stk, self.log = exf, stk, log
        self.ledger = Ledger(stk)
        self.job_timeout_s = job_timeout_s
        self._stop = threading.Event()
        self.report: dict = {"steps": []}

    def _beat(self) -> None:
        while not self._stop.wait(1.0):
            for s in (self.exf, self.stk):
                try:
                    s.heartbeat()
                except Exception as exc:  # noqa: BLE001 - one missed beat is not fatal (lease 3 s)
                    self.log(f"heartbeat {s.name} failed: {exc}")

    def job(self, station: Station, flow: str, **params) -> dict:
        t0 = time.time()
        out = station.command(flow, job={"requested_by": CLIENT}, **params)
        run_id = out.get("run_id") or (out.get("result") or {}).get("run_id")
        if not run_id:
            raise RuntimeError(f"{flow}: no run_id in {out}")
        self.log(f"-> {station.name} {flow} {json.dumps(params)} run {run_id}")
        while True:
            self.ledger.pull()
            run = self.ledger.runs.get(run_id)
            if run and run.get("status") in TERMINAL:
                break
            if time.time() - t0 > self.job_timeout_s:
                raise RuntimeError(f"{flow} {run_id}: still {run and run.get('status')} after {self.job_timeout_s} s")
            time.sleep(0.5)
        dt = round(time.time() - t0, 1)
        self.report["steps"].append({"flow": flow, "run_id": run_id, "status": run["status"], "seconds": dt,
                                     "params": params})
        self.log(f"<- {flow} {run['status']} in {dt} s")
        if run["status"] != "succeeded":
            raise RuntimeError(f"{flow} {run_id} ended {run['status']}: {run.get('error')}")
        return run

    def run(self) -> dict:
        beat = threading.Thread(target=self._beat, daemon=True)
        for s in (self.exf, self.stk):
            s.heartbeat()
        beat.start()
        t0 = time.time()
        try:
            self.ledger.pull()
            before = {s["id"] for s in self.ledger.kind("chip") if s.get("status") == "exfoliated"}
            self.job(self.exf, "exfoliate_chips", material="hBN", num_chips=1)
            self.job(self.exf, "exfoliate_chips", material="Gr", num_chips=1)
            self.ledger.pull()
            new = [s for s in self.ledger.kind("chip") if s.get("status") == "exfoliated" and s["id"] not in before]
            chips = {_material(s): s["id"] for s in new}
            if set(chips) < {"hBN", "Gr"}:
                raise RuntimeError(f"exfoliated chips found: {chips}")
            for m in ("hBN", "Gr"):
                self.job(self.stk, "search_chip", chip_id=chips[m])
            self.ledger.pull()
            flakes = [f for f in self.ledger.kind("flake") if f.get("location") in chips.values()]
            h, g = choose_flakes(flakes)
            self.log(f"chosen hBN {h['id']} ({_label(h)}, {_area(h):.0f} um2), "
                     f"Gr {g['id']} ({_label(g)}, {_area(g):.0f} um2)")
            targets = [s for s in self.ledger.kind("chip") if (s.get("props") or {}).get("electrodes")
                       and s.get("status") not in ("device",)]
            if not targets:
                raise RuntimeError("no electrode chip in the ledger")
            run = self.job(self.stk, "stack_device", flake_ids=[h["id"], g["id"]], target_chip_id=targets[0]["id"])
            self.job(self.stk, "cool_experiment_stages")
            self.ledger.pull()
            self.report.update(ok=True, device_chip=targets[0]["id"], stack=[h["id"], g["id"]],
                               stack_result=run.get("result"), lab_id=self.ledger.lab_id)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            self.report.update(ok=False, error=str(exc))
            self.log(f"FAILED: {exc}")
        finally:
            self._stop.set()
            self.report["seconds"] = round(time.time() - t0, 1)
            self.report["stations"] = {"exfoliation": self.exf.url, "stacking": self.stk.url}
        return self.report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--exfoliation", default="http://127.0.0.1:8781")
    ap.add_argument("--stacking", default="http://127.0.0.1:8782")
    ap.add_argument("--client", default=CLIENT)
    ap.add_argument("--report", help="write the JSON report here")
    ap.add_argument("--job-timeout-s", type=float, default=7200.0)
    a = ap.parse_args(argv)
    exf, stk = Station("exfoliation", a.exfoliation, a.client), Station("stacking", a.stacking, a.client)
    rep = Script(exf, stk, job_timeout_s=a.job_timeout_s).run()
    text = json.dumps(rep, indent=2, default=str)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text if not rep.get("ok") else json.dumps({k: rep[k] for k in ("ok", "device_chip", "stack", "seconds")}))
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
