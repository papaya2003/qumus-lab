# Lab state: qumus.db and the middle-board lock

Follows the lab-state design Jialei approved on 2026-11-06. qumus-v2's `docs/lab-state.md` and
`docs/lab-state-table.md` were read as reference; no code was copied. This page describes what qumus-lab
does.

**中文摘要**：每个实验室一个 `qumus.db`（真实：`runs\real-lab\qumus.db`；演示：`runs\demo\qumus.db`，
两者不会混用）。表格包括：`device`（每台仪器一行，记录最后一次从仪器读到的值和读取时间；没读过就是空，
程序不会编造），`sample`（每个物体一行，记录在哪、谁说的、什么时候确认过、被谁拿着），`event`（历史），
`zone`（中间板现在被哪只机械臂占用）。机械臂必须先占用中间板（`zone_acquire`）才能去中间板的槽位，
同一时间只有一只机械臂能占用。机械臂移到中间板外的点后自动释放。查看命令：`scripts\state.bat devices`。

## Files

| Lab | File | Opened by |
| --- | --- | --- |
| real | `runs/real-lab/qumus.db` | launcher, probe (real) |
| demo | `runs/demo/qumus.db` | launcher, probe with `--demo` |

The file records its bench (`meta.bench`). Opening a real file as demo, or the other way round, is refused,
so simulated and real records never mix. `--runs-dir` / `QUMUS_LAB_RUNS` override the folder.
SQLite runs in WAL mode, so `scripts\state.bat` can read while the services run.

## Tables

| Table | One row = | Written by |
| --- | --- | --- |
| `device` | one instrument | the service after every command that names it (`device`, `camera`, `point`; all of the module's devices for module-wide commands such as `read_all`), before the result is published; the heater and the shared supply every 10 s while heating; at start only `kind` / `module` / `online` |
| `sample` | one object (chip, tape, stamp, flake) | `scripts\state.bat sample-set` (a person, source `manual`); later the L4 actions (pick, place, search) |
| `event` | one event | every service event (`module`, `level`, `message`, `fields`) |
| `zone` | one shared zone | the zone lock (holder and since when) |

`device` columns: `id`, `kind`, `module` (`shared` for the supply), `state_json` (the driver's last values,
fields with units), `read_at` (last time the instrument answered a query; NULL = never), `online`,
`conflict_json` (for restart reconciliation, later), `note`, `updated_at`.

`sample` columns: `id`, `kind`, `location`, `status`, `props_json`, `location_source`
(`camera` / `inferred` / `manual` / `config`, NULL = unknown), `location_checked` (last camera or person
confirmation), `held_by` (device id while on a nozzle or holder), `updated_at`.

Rules:
- NULL means not known. A row only carries what a driver reported; nothing is guessed. A device not
  read in this session keeps its last stored values and their `read_at`.
- Only `qumus_lab/state/store.py` touches the file (no SQL elsewhere).
- Devices whose instrument has no query (turret, tape reels) store their last commanded values, and their
  `read_at` stays NULL.

## Middle-board occupancy lock

Configured in `config/points.yaml`:

```yaml
zones:
  middle_board:
    devices: [arm_left, arm_right]
    points: ["arm_left.slot_S*", "arm_right.slot_S*"]
```

| Step | Rule |
| --- | --- |
| `zone_acquire {zone, device}` | 409 if the other arm holds it (also across the two modules) |
| `go_to_point` to a zone point | 409 before anything moves unless that arm holds the zone |
| move starts | the arm counts as inside the zone, also if the move is then stopped or fails (position unknown) |
| `go_to_point` to a point outside finishes ok | the arm is outside; the zone is released automatically |
| `zone_release {zone, device}` | 409 while the arm is (or may be) inside |
| `zone_clear {zone}` | local only, no lease: a person checked by eye that no arm is in it |
| restart | a stored holder stays held and its arm counts as inside until it moves out or a person clears it |

`describe.zones` lists the zones a module uses; `state.zones` shows `{holder, since_unix, inside}`.
The debug page shows the zone in each module's status line with Take, Release and Clear.

Limit: `jog` does not know zone geometry (no measured outline yet). Jogs are capped at 0.5 mm and local only.

## Not done yet

- Restart reconciliation (compare stored and read values, fill `conflict_json`, park until cleared).
- Writes to `sample` from L4 actions (pick sets `held_by`, place sets `inferred`, camera check upgrades to
  `camera`), and the freshness rule (camera check older than 10 minutes counts as stale).
