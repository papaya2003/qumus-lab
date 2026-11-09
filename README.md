# qumus-lab

Software for the Qumus lab PC (Windows, Sanfeng Wu lab): drivers for every instrument of the
**exfoliation** module (left side of the bench) and the **stacking** module (right side), a SafetyGate
that checks every command, and one HTTP service per module that speaks the shared
*instrument service contract v1* (the same rules as the labmate's reactor services). The Qumus agent
(papaya2003/qumus-agent) will be the only commander and talks to these services through that
contract. This repo also runs on its own, without any AI: a debug page and L1-L2 probe scripts.

Rewritten from scratch. From Qumus v1 only the bytes sent to each instrument and the measured numbers
were taken (each value cites its v1 file and line); the structure is new. Nothing was copied from
qumus-v2. Lab-private repository: no lab data, GDS, model weights or vendor DLLs in git.

| Module | Service | Devices |
| --- | --- | --- |
| exfoliation | `127.0.0.1:8781` | left arm, exfoliation vacuum, fan, exfoliation heater, tape reels, tape gantry, left desk camera |
| stacking | `127.0.0.1:8782` | right arm, sample stage (x, y, rail), stage vacuum, turret, focus, microscope camera, stamp holder (Kinesis x/y/z, goniometer, rail), stamp vacuum, transfer heater, right desk camera |
| debug page | `127.0.0.1:8780` | both modules |

## Try it without hardware (any PC)

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/python -m qumus_lab.launcher --demo
# open http://127.0.0.1:8780/  (tick "Take control" to move, switch or heat simulated devices)
.venv/bin/python -m qumus_lab.probe.cli --demo l1
.venv/bin/python -m pytest -q
```

`--demo` runs the *same drivers* against byte-level instrument emulators, so the demo exercises the
code that will talk to the real instruments.

## On the lab PC (Windows, Python 3.13)

1. `scripts\setup.bat` (creates `.venv`, installs the lab extras: OpenCV, numpy, pythonnet).
2. Check `config\devices.yaml` (COM ports, Kinesis serials, Dracal exe path, Uvcham SDK folder).
   Then `scripts\check.bat`: self-check of Python, packages, config, vendor paths, the COM ports
   Windows sees, and a demo start of both services (opens no instrument port). Fix every FAIL line.
3. **L1, read only** (opens ports and queries; nothing moves, nothing is switched or heated):

   ```bat
   scripts\probe.bat ports          :: COM ports Windows sees vs devices.yaml (opens nothing)
   scripts\probe.bat read arm_left  :: one device
   scripts\probe.bat l1             :: every device of both modules, report in runs\real-lab\probe\
   ```

4. **L2, one actuator at a time** (asks Enter before each step; caps: arm/stage/gantry 0.5 mm, stamp
   0.1 mm, focus 50 um, switches turn themselves off after <= 10 s, first heats <= 40 C at <= 6.5 V
   with a mandatory timeout):

   ```bat
   scripts\probe.bat switch exfoliation_vacuum on
   scripts\probe.bat jog arm_left z 0.5
   scripts\probe.bat heat exfoliation_heater 30 --timeout 300
   scripts\probe.bat l2             :: guided walk through both modules
   ```

   Ctrl+C at any time = stop every axis and switch heaters off. Every step is logged to
   `runs\real-lab\probe\<date>-probe.jsonl`.

5. Debug page: `scripts\start-lab.bat` (real) or `scripts\start-demo.bat`, then
   http://127.0.0.1:8780/. STOP ALL is the red button at the bottom and needs no control.

Real motion to a named point (`go_to_point`) stays refused until that point is measured on the bench
(`source: measured` in `config/points.yaml`, test level L3). v1 coordinates are only starting guesses.

## Remote work

- **Watch** from another computer on the university network or VPN: the launcher also serves a
  view-only page on port 8790 (three live camera pictures and the readings; GET only, no command,
  stop, lease or unlock route; needs the access key). The link with the key is printed at start and
  saved in `runs\real-lab\viewer-url.txt`. `--viewer-host 127.0.0.1` or `--no-viewer` keeps it local.
- **Control** only on the lab PC itself (e.g. through AnyDesk): the debug page and the module services stay on
  127.0.0.1. The debug page also shows the live camera pictures.
- **Update** with `scripts\update.bat`: git pull main, self-check, restart the running services in the
  same mode. It refuses (exit code 2) while a module is busy, locked, heating, has a switch on, or
  someone holds control, and leaves the services alone if the pull or the self-check fails.

- **No admin rights needed**: the venv lives in the repo, loopback traffic is not filtered by the Windows
  firewall. Only reaching port 8790 from another computer needs an inbound firewall rule (admin); without
  it, watch the cameras on the debug page through the remote desktop session.

Chinese guide: [docs/remote.zh.md](docs/remote.zh.md).

## Layout

```
config/            devices.yaml (ports), points.yaml (coordinates), limits.yaml (gate limits)
qumus_lab/
  transport/       real serial (pyserial, lazy) and fake serial ports
  drivers/         one file per instrument kind, generic operations only
  safety/gate.py   SafetyGate
  modules/         actions per module (compose driver operations + named points)
  service/         contract v1 core and HTTP layer
  probe/cli.py     L1-L2 probe
  selfcheck.py     lab PC self-check (scripts\check.bat)
  state/           qumus.db tables (device / sample / event / zone); scripts\state.bat to look
  safety/zones.py  middle-board occupancy lock
  sim/             instrument emulators and the simulated world for --demo
  web/             debug page
  launcher.py      starts both services + debug page in one process
scripts/           Windows .bat wrappers
tests/             byte-for-byte v1 comparison, contract behaviour, probe, config cites
docs/              architecture, contract, v1 mapping, open questions, debug page design
```

**Lab PC step by step (Chinese): [docs/l1-l2-guide.zh.md](docs/l1-l2-guide.zh.md).**

Docs: [architecture](docs/architecture.md), [contract](docs/contract.md),
[v1 mapping and hazards](docs/v1-mapping.md), [open questions](docs/open-questions.md),
[debug page design](docs/debug-page-design.md), [lab state and middle-board lock](docs/lab-state.md).
