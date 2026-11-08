# Instrument service contract v1 (qumus-lab implementation)

The two module services follow the shared *instrument service contract v1* (the rules written for the
labmate's reactor services, ADR 0001). This page says what the shared contract requires, what this repo
adds, and the actions each module offers. The agent (papaya2003/qumus-agent) should treat each module as
one independent service.

| Module | Base URL |
| --- | --- |
| exfoliation | `http://127.0.0.1:8781/api/` |
| stacking | `http://127.0.0.1:8782/api/` |
| debug page (both, same API) | `http://127.0.0.1:8780/m/<module>/api/` |

## Rules

- **One commander at a time.** A client takes the lease with `POST heartbeat` and keeps it by
  heartbeating at least every `lease_seconds` (3 s). Without the lease, only actions marked
  `any_client` (reads, capture, heat_off) and `stop` are accepted.
- **Lease expiry stops motion.** When the holder stops heartbeating, the service stops every motion
  device of the module. Heaters keep running under their own mandatory timeout, and they are
  switched off after `orphan_timeout_s` (600 s) with no lease holder at all.
- **Stop works for anyone.** `POST stop` needs no lease and is never refused.
- **Unknown outcome locks.** If a command can not tell whether the instrument did what was asked
  (no reply, timeout during motion, a stop that can not be confirmed), the module is `locked` with a
  `fault`, the command is never retried, and every motion/switch/heat command is refused with `409`
  until a person unlocks it on the lab PC.
- **Client name.** Every POST carries `X-Qumus-Client: <name>` (1 to 128 printable ASCII).
- **JSON only.** POST bodies are JSON objects with `Content-Type: application/json`.

## Endpoints

| Method | Path | Body | Reply |
| --- | --- | --- | --- |
| GET | `describe` | | service, version, `contract: 1`, module, demo, client_header, lease_seconds, on_lease_expiry, zones, devices, commands |
| GET | `state` | | see below |
| GET | `events?since=<seq>` | | `{events: [{seq, ts_unix, level, message, fields}], next}` |
| POST | `heartbeat` | `{}` | takes or keeps the lease; `409` if another client holds it |
| POST | `renew` | `{}` | keeps the lease; `409` if the caller does not hold it |
| POST | `release` | `{}` | gives the lease back (motion is not stopped) |
| POST | `command` | `{"action": "...", ...params, "command_id"?: "..."}` | `202 {command_id, state}` |
| POST | `stop` | `{"wait_s": 0..15, "heat_off"?: bool}` | `{stop: confirmed / pending / unconfirmed, state}` |
| POST | `unlock` | `{}` | local only: clears `locked` and `fault` |
| GET | `images/<image_id>` | | [Q] the JPEG/PNG of a `capture` |

`state`:

```json
{
  "seq": 812, "ts_unix": 1794100000.12, "demo": true, "connected": true,
  "busy": false, "busy_action": null,
  "locked": false, "fault": null,
  "lease": {"holder": "qumus-agent", "remaining_s": 2.4},
  "last_command": {"id": "c-...", "action": "jog", "client": "qumus-agent",
                   "result": "ok", "message": "", "finished_at": 1794099999.8, "output": {}},
  "devices": {"arm_left": {"connected": true, "x_mm": 0.0, "y_mm": 220.0, "z_mm": 95.0, "...": "..."}},
  "zones": {"middle_board": {"holder": "arm_left", "since_unix": 1794100000.0, "inside": ["arm_left"]}}
}
```

`last_command.result` is `ok`, `failed`, `rejected` or `stopped`. A command is accepted (`202`) before it
runs; the agent polls `state` (or `events`) for the result. Checks that can be done without moving
(parameter ranges, caps, unmeasured points, heater limits, lease, busy, locked) run before `202`, so a
refusal comes back at once.

## Errors

| Status | When |
| --- | --- |
| 400 | bad JSON, unknown action, missing or out-of-range parameter, a SafetyGate cap |
| 403 | a `local_only` action or `unlock` from a non-loopback address |
| 404 | no such route or image |
| 409 | no lease, lease held by someone else, busy, locked, unmeasured point in real mode, power supply owned by the other heater, zone held by the other arm or not taken |
| 415 | POST without `Content-Type: application/json` |
| 503 | device not connected |

The brief mentioned `422` for bad parameters; the shared ADR says `400`, and we follow the ADR.

## Actions

Flags: **M** motion (stopped on lease expiry and by `stop`), **A** any client (no lease needed),
**L** local only (loopback clients: the probe and the debug page on the lab PC).

Both modules:

| Action | Params | Flags | What it does |
| --- | --- | --- | --- |
| `read_all` | | A | query every device once |
| `read` | `device` | A | query one device |
| `home_query` | | A | which devices know their position (never homes) |
| `capture` | `camera` | A | one picture, returns `image_id` |
| `go_to_point` | `point` (`<device>.<name>` from points.yaml) | M | move to a named point; real mode needs `source: measured` |
| `jog` | `device, axis, mm` | M L | small relative move, capped (arm/stage/gantry 0.5 mm, stamp 0.1 mm, focus 0.05 mm) |
| `arm_suction` | `device, on` | | arm nozzle suction (v1 M888 P2 + M1000 / M1003) |
| `rotate_nozzle` | `device, deg` | M | arm nozzle rotation (v1 M2101) |
| `set_switch` | `device, on, auto_off_s?` | L | relay on/off; `on` turns itself off after <= 10 s |
| `heat` | `device, target_c, timeout_s, volts?` | | heat with a mandatory timeout; unmeasured heaters in real mode: <= 40 C at <= 6.5 V |
| `heat_off` | `device` | A | heater output and relay off |

Zones (both modules; the middle board is shared by the two arms, see [lab-state.md](lab-state.md)):

| Action | Params | Flags | What it does |
| --- | --- | --- | --- |
| `zone_acquire` | `zone, device` | | take the zone for an arm; 409 if the other arm holds it |
| `zone_release` | `zone, device` | | give it back; 409 while the arm is inside (a move to a point outside releases it automatically) |
| `zone_clear` | `zone` | A L | a person checked the bench: no arm is in the zone |

`go_to_point` to a point inside a zone answers 409 unless the arm holds that zone.

Exfoliation only: `apply_tape` (M), `peel_tape {speed}` (M, 50 to 500 mm/min), `advance_tape {deg}`
(M, real-mode cap 10 deg before L3), `gantry_power {on}`.

Stacking only: `set_objective {objective: 5|10|20|100}` (M), `focus_by {um}` (M, real-mode cap 50 um
before L3).

The exact parameter schemas (JSON Schema per action) are in `GET describe` under `commands`.

## Extensions marked [Q]

These are additions beyond the shared contract, kept so that the shared spec can adopt or reject them:

- `stop` accepts `heat_off: true` to also switch the module's heaters off (STOP ALL on the debug page
  uses it).
- `GET images/<image_id>` returns a captured picture, so a client does not need file access.
- `unlock` is a `local_only` endpoint: only a person on the lab PC can clear a lock.
- `describe` adds `module`, and each command adds `motion`, `any_client`, `local_only`, `description`.
  `describe.zones` is the list of zone names (as the shared contract), and `state.zones` adds each
  zone's holder.
- Each finished command is written to the device table of qumus.db before `last_command` shows it.
- `set_switch` and `jog` are `local_only` probe tools. The agent's switching (vacuum held for minutes)
  and long moves will be separate L3 actions once the bench is measured.
