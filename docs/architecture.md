# Architecture

## Layers

```
 agent (qumus-agent)        debug page (8780)        probe (L1-L2 CLI)
        |  HTTP contract v1       |  /m/<module>/api/*      |  in-process
        v                         v                         v
 +------------------------ ContractService (one per module) ------------------------+
 | describe / state / command / stop / heartbeat / renew / release / events / unlock |
 | lease (3 s), busy, locked + fault, event journal, heater orphan watchdog          |
 +-----------------------------------+----------------------------------------------+
                                     | action name + params
 +-----------------------------------v----------------------------------------------+
 | Module actions: read_all, read, home_query, go_to_point, jog, set_switch, heat,   |
 | heat_off, capture, arm_suction, rotate_nozzle; exfoliation: apply_tape,           |
 | peel_tape, advance_tape, gantry_power; stacking: set_objective, focus_by          |
 |        every action -> SafetyGate (limits.yaml, points.yaml, demo/real)           |
 +-----------------------------------+----------------------------------------------+
                                     | generic operations
 +-----------------------------------v----------------------------------------------+
 | Drivers: GcodeArm.move_to/read/set_vacuum/release_item/rotate_nozzle,             |
 | RelaySwitch.set, TP3005P.set_voltage/output, DracalThermometer.read,              |
 | Heater.heat/off, TapeReels.power/rotate, PrinterGantry.line/arc_relative/power,   |
 | KZ100Stage.move_to/move_by, TmclFocus.move_by_um, Turret.rotate_to,               |
 | StampManipulator.move_to (Kinesis + rail), Camera.capture                          |
 +-----------------------------------+----------------------------------------------+
                                     | bytes
                 real serial / .NET Kinesis / OpenCV / Uvcham / Dracal exe
                 or, with --demo, byte-level emulators (qumus_lab/sim)
```

## Rules that shaped the code (Jialei, 11-08)

- **Drivers are separate from interfaces and only offer generic operations.** The arm has one
  `move_to(x, y, z, feed)`; there is no function per place. v1 had `home_to_exfoliation_stage`,
  `place_silicon_chip_on_exfoliation_stage`, ...; those coordinates are now named points in
  `config/points.yaml` and the sequences are composed by actions.
- **Ports in `devices.yaml`, coordinates in `points.yaml`, limits in `limits.yaml`.** Every v1 value
  says `source: v1` and cites `file:line`; `tests/test_config_cites.py` re-reads each cited line.
- **Every command goes through the SafetyGate.** Synchronous checks (caps, ranges, measured points,
  heater limits) run before the command is accepted, so a refusal is an immediate `400`/`409`.
- **v1 hazards are not copied** (list in [v1-mapping.md](v1-mapping.md)).
- **Windows lab PC:** pathlib, UTF-8 file I/O, no fcntl or POSIX-only signals, Windows SDKs
  (Kinesis .NET, Uvcham, Dracal exe) imported lazily with their paths in config, `.bat` wrappers.

## One process, two services

Both module services run in one process (`qumus_lab.launcher`). Reasons: the two heaters share one
TP3005P supply on COM8 (the supply has an owner: one heater at a time), each heater must be able to
switch the other heater's relay off, and a Windows COM port can be open in one process only.
Each module still has its own port, lease, lock, event log and describe, so the agent sees two
independent windows exactly like the reactor services.

## Concurrency

- One executor per module: motion, switch and heat commands run one at a time (`busy`).
- Reads, `capture` and `heat_off` are `concurrent`: they run beside a busy executor.
- Stop: sets the cancel flag (checked between driver steps), sends each motion device's stop
  (`PA;` to the KZ-100s, TMCL stop to the focus, `StopImmediate` to the K-Cubes, `M410` to the gantry).
  The arms have no stop command in v1, so stopping a moving arm returns `unconfirmed` and locks the
  module until a person checks it.
- Heaters run their own control thread (poll the thermometer, boost then hold, overshoot cut-off,
  mandatory timeout). They ignore the 3 s lease; the service switches them off after
  `orphan_timeout_s` (600 s) without any lease holder.

## Demo mode

`--demo` keeps everything above the bytes identical. `qumus_lab/sim/world.py` builds one emulator per
configured port (G-code arm, Marlin gantry, AT relays, TP3005P, Arduino reels, KZ-100 with per-character
echo and timed motion, TMCL focus, turret), fake K-Cubes, a first-order thermal model behind the fake
Dracal CLI, and synthetic camera frames. The same emulators drive the tests.

## Lab state

`runs/real-lab/qumus.db` (demo: `runs/demo/qumus.db`) holds the device, sample, event and zone tables;
the services write it after every command. The middle-board occupancy lock lets one arm at a time into the
middle board. Details: [lab-state.md](lab-state.md).

## Not done tonight (next steps)

- Restart reconciliation of the stored state, and sample writes from L4 actions.
- L3+ actions (pick chip, place chip, stamp load, massage) composed from the generic operations once
  points are measured.
- Recognition and the two-camera check.
