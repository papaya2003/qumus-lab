# Instructions for coding agents (Codex, Claude) working in qumus-lab

**中文摘要（给 Jialei）**：这份文件是给在实验室电脑上帮忙开发的 AI 助手（Codex 等）读的规则。
最重要的几条：只有人站在仪器旁边时才能让真实仪器动；安全上限、`measured` 坐标、`qumus_lab/safety/`
的改动必须先给人看；每次改代码先在 `--demo` 模拟仪器上跑通测试；遇到 FAIL / locked / 结果不对就停下来
问人，不要自己“修好”再继续；代码只推到私有仓库 papaya2003/qumus-lab，用新分支 + PR，由 Jialei 合并。

This code drives real instruments (robot arms, stages, heaters, vacuum) on the Windows lab PC of the
Sanfeng Wu lab. A mistake can break a sample, a stamp, or an instrument. Read this whole file first.

## What this repo is

- Read `README.md`, then `docs/architecture.md`, `docs/contract.md`, `docs/lab-state.md`.
- Bench test levels: L1 read only, L2 one actuator at a time with small caps, L3 measure points,
  L4+ real sequences. Step-by-step guide in Chinese: `docs/l1-l2-guide.zh.md`.
- Unknowns that only the bench can answer: `docs/open-questions.md`. Update it when the bench answers one.

## Commands (Windows, from the repo folder)

```bat
scripts\setup.bat                           :: once: .venv + packages
scripts\check.bat                           :: self-check, opens no instrument port; must end with 0 FAIL
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q       :: fake serial only, never opens a real port
scripts\start-demo.bat                      :: debug page on simulated instruments, http://127.0.0.1:8780/
scripts\probe.bat --demo l1                 :: any probe command on simulated instruments
```

Real instruments: `scripts\probe.bat ...` and `scripts\start-lab.bat` (see the guide).

## Safety rules (never break these)

1. **A person at the bench for anything that can move, switch or heat.** Run real L2+ commands only
   when the person says they are standing at the instruments, one step at a time, and let the person
   press Enter. Never loop or batch real actuation commands. L1 (read only) may run without asking.
2. **Do not weaken the safety layer without the person's explicit OK in this conversation:**
   `config/limits.yaml`, the probe caps, `qumus_lab/safety/`, the heater limits and timeouts, lease/stop/
   lock logic in `qumus_lab/service/core.py`. Show the diff and explain the risk first.
3. **`source: measured` only for values a person measured on the bench**, with `measured_by` and
   `date`. Never mark a v1 value or a guess as measured; the gate trusts that flag.
4. **Stop and ask on anything unexpected:** a FAIL line, a module that reports `locked`, an
   `unconfirmed` stop, a move in the wrong direction, a reading that does not make sense. Do not
   unlock, retry, or "fix and continue" on your own. An unknown outcome is never retried.
5. **Ctrl+C is the stop** in the probe and the launcher (stops axes, switches heaters off). If software
   cannot stop something, tell the person to switch the instrument's power off.
6. **Close Qumus v1 first.** A COM port can only be open in one program.
7. **Tests:** never skip, delete or weaken a test to get green. Every change to drivers, gate, service
   or heater code needs a test on the fake serial / `--demo` emulators. A fault-injection test must
   first prove its fault fires (it must fail on the old code). Tests must never open a real port.

## How to change code

- Work on a new branch, small steps; run `ruff check .` and `pytest -q` before every commit.
  GitHub Actions has no minutes, so local checks are the only CI.
- Try every change on `--demo` first; then on the bench at the lowest level that shows it works.
- Commit messages and code comments in English; talk to Jialei in Chinese, short and concrete.
- Push only to `papaya2003/qumus-lab` (private) and open a pull request into `main`; Jialei merges.
  Never push to `main` directly, never force-push, never publish this code anywhere else.

## Design rules (Jialei's decisions)

- Drivers expose generic operations only (an arm has one `move_to`, not one function per place).
  Coordinates live in `config/points.yaml`, ports in `config/devices.yaml`, limits in `config/limits.yaml`.
- From Qumus v1 take only data and wire formats (bytes sent, measured numbers), with a `cite:` to the
  v1 file and line. Do not copy v1 code structure. Do not copy code from qumus-v2.
- Every command goes through the SafetyGate; nothing moves outside an action.
- `qumus.db` is touched only by `qumus_lab/state/store.py`. NULL means "not known"; never invent values.

## Never commit

Lab data, run folders (`runs/`), photos of samples, GDS files, model weights, vendor DLLs or SDKs,
keys, `.env`, passwords. Vendor software stays where `config/devices.yaml` points.

## Recording what the bench showed

- Fix COM ports, serials, paths in `config/devices.yaml`; measured points in `config/points.yaml`
  with `source: measured`, `measured_by`, `date`.
- Close the matching item in `docs/open-questions.md` (what the bench showed, date).
- Probe logs stay in `runs\real-lab\probe\` (not in git); summarise findings in the PR description.
