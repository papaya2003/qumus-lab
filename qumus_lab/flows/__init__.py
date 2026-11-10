"""L5: whole flows as job actions, with the job names and parameters of the simulated lab (qumus-simlab) so
the brain submits the same job to either. A job answers at once with a run_id; the flow then runs in the
module's job thread and writes run / step rows (GET /api/records).

Mapping to the brain's flows (qumus-agent qumus2/flows/library, simlab FLOW_MODULE):
  exfoliation: exfoliate_chips
  stacking:    place_chip_on_transfer_stage, search_chip, revisit_chip, stack_device,
               cool_experiment_stages, inspect_bench
Parameters the lab does not use yet are accepted and echoed in the result under ``ignored`` (e.g.
press_depth_mm: v1 pressed to a fixed z), so a brain request never fails on them.
"""

from __future__ import annotations

from typing import Any

from ..jobs.runner import StepFailed
from ..modules.base import Action, Module
from ..ops import common as c
from ..safety.gate import Rejected

MAX_TAPE_USES = 5           # qumus-agent exfoliation.py MAX_TAPE_USES (v1 decided from the camera)
FINAL_BELOW_C = 40.0        # qumus-agent stage_cooling.py FINAL_BELOW_C
MATERIALS = {"gr": "Gr", "graphene": "Gr", "graphite": "Gr", "hbn": "hBN", "bn": "hBN"}


def norm_material(m: str | None) -> str | None:
    if not m:
        return None
    return MATERIALS.get(str(m).strip().lower(), str(m).strip())


def _store(ctx):
    st = c._store(ctx)
    if st is None:
        raise Rejected("this module has no state store (qumus.db)")
    return st


def _job(desc: str) -> str:
    return "Job (L5): answers at once with run_id; follow it in GET /api/records. " + desc


def _ignored(p: dict, used: set[str]) -> dict:
    return {k: v for k, v in p.items() if k not in used and v not in (None, [], {})}


# ============================================================================== exfoliation
def exfoliate_chips(ctx, p: dict) -> dict:
    from ..ops import exfoliation as ox

    st = _store(ctx)
    material = norm_material(p.get("material"))
    if not material:
        raise Rejected("material is required")
    n = int(p.get("num_chips") or 1)
    samples = st.list_samples()
    # carrier: given slot, else the left-board carrier whose material matches
    carrier_slot = p.get("carrier_slot")
    if not carrier_slot:
        cands = [s for s in samples if s["kind"] == "carrier"
                 and norm_material((s.get("props") or {}).get("material")) == material
                 and str(s.get("location", "")).startswith("left_slide")]
        if p.get("carrier_id"):
            cands = [s for s in cands if s["id"] == p["carrier_id"]]
        carrier_slot = cands[0]["location"] if cands else None
    # chips: given ids, else the first bare chips on the left board
    if p.get("chip_ids"):
        chips = [st.get_sample(cid) for cid in p["chip_ids"]]
        if any(ch is None for ch in chips):
            raise Rejected(f"unknown chip in {p['chip_ids']}")
    else:
        chips = [s for s in samples if s["kind"] == "chip" and str(s.get("location", "")).startswith("left_chip")
                 and s.get("status") in (None, "", "bare", "new")][:n]
    if len(chips) < n:
        raise Rejected(f"only {len(chips)} bare chips on the left board, {n} asked")
    homes = [ch["location"] for ch in chips]
    stores = list(p.get("store_slots") or [])
    if len(stores) < n:
        free = [f for f in c.free_places(ctx, "arm_left", station="middle_storage") if f not in stores]
        stores += free[: n - len(stores)]
    if len(stores) < n:
        raise Rejected("not enough free middle-board slots")
    tape = st.get_sample("tape") or {}
    tprops = tape.get("props") or {}
    loaded = tape.get("status") == "loaded" and norm_material(tprops.get("material")) == material
    uses = int(tprops.get("uses") or 0)
    mode = p.get("prepare_mother_tape") or "auto"
    need = mode == "always" or (mode == "auto" and (not loaded or uses + n > MAX_TAPE_USES))
    if mode == "never" and not loaded:
        raise Rejected(f"prepare_mother_tape=never but the tape holds no {material}")
    if need and not carrier_slot:
        raise Rejected(f"no {material} carrier on the left board (carrier_slot)")
    if need:
        ox.prepare_mother_tape(ctx, {"carrier_place": carrier_slot, "peel_speed": p.get("peel_speed"),
                                     "rounds": p.get("num_massages")})
        uses = 0
    done = []
    for ch, home, dst in zip(chips, homes, stores):
        out = ox.exfoliate_chip(ctx, {"chip_place": home, "store_place": dst, "material": material,
                                      "target_c": float(p.get("target_temp_c") or 22),
                                      "dwell_s": float(p.get("dwell_s") or 10),
                                      "peel_speed": float(p.get("peel_speed") or 500),
                                      "rounds": int(p.get("num_massages") or 1)})
        uses += 1
        c.update_sample(ctx, "tape", props={"uses": uses})
        done.append({"chip_id": out.get("chip") or ch["id"], "from": home, "to": dst})
    used = {"material", "num_chips", "chip_ids", "prepare_mother_tape", "target_temp_c", "dwell_s",
            "num_massages", "peel_speed", "carrier_id", "carrier_slot", "home_slots", "store_slots"}
    return {"material": material, "chips": done, "mother_tape_prepared": need, "tape_uses": uses,
            "ignored": _ignored(p, used)}


# ============================================================================== stacking
def _chip_row(ctx, chip_id: str) -> dict:
    row = _store(ctx).get_sample(chip_id)
    if row is None or row["kind"] not in ("chip", "electrode_chip"):
        raise Rejected(f"unknown chip {chip_id}")
    return row


def _bring_chip(ctx, chip_id: str) -> str:
    """Chip onto the transfer stage (no-op if it is there). Returns where it came from."""
    from ..ops import stacking as so

    row = _chip_row(ctx, chip_id)
    if row["location"] == so.STATION:
        return (row.get("props") or {}).get("home") or ""
    on = c.sample_at(ctx, so.STATION)
    if on is not None:
        raise StepFailed(f"{on['id']} is on the transfer stage; store it first")
    home = row["location"]
    if not (c.places(ctx).get(home) or {}).get("arm_right"):
        raise Rejected(f"{chip_id} is at {home}, which arm_right can not reach (exfoliate_chips stores chips "
                       "on the middle board)")
    ctx.step(f"chip {chip_id} to transfer stage", lambda: so.place_chip_on_transfer_stage(ctx, {"place": home}))
    c.update_sample(ctx, chip_id, props={"home": home})
    return home


def _return_chip(ctx, chip_id: str, home: str | None) -> str:
    from ..ops import stacking as so

    dst = home or (_chip_row(ctx, chip_id).get("props") or {}).get("home")
    if not dst or c.sample_at(ctx, dst) is not None:
        free = c.free_places(ctx, "arm_right", station="middle_storage")
        if not free:
            raise StepFailed("no free middle-board slot to store the chip")
        dst = free[0]
    ctx.step(f"store chip {chip_id}", lambda: so.store_chip(ctx, {"place": dst}))
    return dst


def place_chip_on_transfer_stage(ctx, p: dict) -> dict:
    home = _bring_chip(ctx, p["chip_id"])
    return {"chip_id": p["chip_id"], "from": home, "ignored": _ignored(p, {"chip_id"})}


def search_chip(ctx, p: dict) -> dict:
    from ..ops import stacking as so

    st = _store(ctx)
    chip_id = p.get("chip_id")
    if not chip_id:  # simlab: None = most recent exfoliated chip
        ex = [s for s in st.list_samples() if s["kind"] == "chip" and s.get("status") == "exfoliated"]
        if not ex:
            raise Rejected("no exfoliated chip in the ledger")
        chip_id = max(ex, key=lambda s: s.get("updated_at") or 0)["id"]
    home = _bring_chip(ctx, chip_id)
    material = norm_material(p.get("material") or (_chip_row(ctx, chip_id).get("props") or {}).get("material"))
    scan = ctx.step("scan chip", lambda: so.scan_chip(ctx, {"material": material,
                                                            "min_area_um2": p.get("min_area_um2") or 50.0}))
    revisits = []
    for fid in scan["flakes"][: int(p.get("max_revisits", 20))]:
        revisits.append(ctx.step(f"revisit {fid}", lambda fid=fid: so.revisit_flake(ctx, {"flake_id": fid})))
    stored = _return_chip(ctx, chip_id, p.get("home_slot") or home)
    flakes = [st.get_sample(f) for f in scan["flakes"]]
    c.update_sample(ctx, chip_id, status="searched")
    return {"chip_id": chip_id, "stored_at": stored, "tiles": scan["tiles"],
            "flakes": [{"id": f["id"], "status": f["status"], **{k: (f.get("props") or {}).get(k) for k in
                                                                 ("thickness_label", "area_um2", "stage_xy")}}
                       for f in flakes if f],
            "ignored": _ignored(p, {"chip_id", "material", "min_area_um2", "max_revisits", "home_slot"})}


def revisit_chip(ctx, p: dict) -> dict:
    from ..ops import stacking as so

    st = _store(ctx)
    ids = list(p.get("flake_ids") or [])
    if not ids:
        raise Rejected("flake_ids is required")
    rows = [st.get_sample(f) for f in ids]
    if any(r is None for r in rows):
        raise Rejected(f"unknown flake in {ids}")
    chips = {r["location"] for r in rows}
    if len(chips) != 1:
        raise Rejected(f"flakes are on more than one chip: {sorted(chips)}")
    chip_id = chips.pop()
    home = _bring_chip(ctx, chip_id)
    out = [ctx.step(f"revisit {f}", lambda f=f: so.revisit_flake(ctx, {"flake_id": f})) for f in ids]
    stored = _return_chip(ctx, chip_id, p.get("home_slot") or home)
    return {"chip_id": chip_id, "stored_at": stored, "revisits": out}


def stack_device(ctx, p: dict) -> dict:
    """v1 assembly_quantum_hall_device order: initial temperature, load stamp, stamp to safe position; per
    flake: chip on stage, re-find flake, initialize stamp, (first flake only) adjust touching point, pick
    up, remove stamp, [Qumus: check the pick-up], store chip; then the target chip: drop point, initialize
    stamp, drop down, remove stamp, record, store; recycle the stamp."""
    from ..ops import stacking as so

    st = _store(ctx)
    for key, v1 in (("pickup_temp_c", so.INITIAL_TEMP_C), ("drop_temp_c", so.DROP_DOWN_TEMP_C),
                    ("melt_temp_c", so.MELT_TEMP_C)):
        if p.get(key) is not None and float(p[key]) != v1:
            raise Rejected(f"{key}={p[key]}: the lab runs the v1 value {v1} only (approved hold voltages)")
    flakes = [st.get_sample(f) for f in p["flake_ids"]]
    if any(f is None or f["kind"] != "flake" for f in flakes):
        raise Rejected(f"unknown flake in {p['flake_ids']}")
    target = _chip_row(ctx, p["target_chip_id"])
    stamp_id = p.get("stamp_id")
    stamps = [s for s in st.list_samples() if s["kind"] == "stamp" and s.get("status") in (None, "", "new")
              and str(s.get("location", "")).startswith("right_stamp")]
    if stamp_id:
        stamps = [s for s in stamps if s["id"] == stamp_id]
    holder = c.sample_at(ctx, so.HOLDER)
    if holder is None and not stamps:
        raise Rejected("no new stamp on the right board")
    ctx.step("set initial temperature", lambda: so.set_initial_temp(ctx, {}))
    if holder is None:
        ctx.step("load stamp", lambda: so.load_stamp(ctx, {"place": stamps[0]["location"]}))
        ctx.step("stamp to safe position", lambda: so.remove_stamp(ctx, {"focus": False}))
    stamp = c.sample_at(ctx, so.HOLDER)
    picks = []
    for i, f in enumerate(flakes):
        chip_id = f["location"]
        home = _bring_chip(ctx, chip_id)
        ctx.step(f"re-find {f['id']}", lambda f=f: so.go_to_flake(ctx, {"flake_id": f["id"]}))
        ctx.step("initialize stamp", lambda: so.initialize_stamp(ctx, {}))
        if i == 0:
            ctx.step("adjust touching point", lambda: so.adjust_touching_point(ctx, {}))
        pk = ctx.step(f"pick up {f['id']}", lambda: so.pick_up(ctx, {}), safe_to_resume=False)
        ctx.step("remove stamp", lambda: so.remove_stamp(ctx, {}))
        chk = ctx.step(f"check pick-up {f['id']}", lambda f=f: so.check_pickup(ctx, {"flake_id": f["id"]}))
        _return_chip(ctx, chip_id, home)
        picks.append({"flake_id": f["id"], "picked": chk["picked"], "pick_up": pk})
        if not chk["picked"]:
            raise StepFailed(f"{f['id']} is still on {chip_id} after the pick-up (camera)")
    home = _bring_chip(ctx, target["id"])
    ctx.step("go to drop point", lambda: so.go_to_target(ctx, {"target_xy_mm": p.get("target_xy_mm")}))
    ctx.step("initialize stamp", lambda: so.initialize_stamp(ctx, {}))
    drop = ctx.step("drop down", lambda: so.drop_down(ctx, {}), safe_to_resume=False)
    ctx.step("remove stamp", lambda: so.remove_stamp(ctx, {}))
    rec = ctx.step("record device", lambda: so.record_device(ctx, {}))
    for f in flakes:
        st.set_sample_location(f["id"], target["id"], "inferred")
        c.update_sample(ctx, f["id"], status="stacked")
    c.update_sample(ctx, target["id"], status="device", props={"stack": [f["id"] for f in flakes],
                                                             "stamp": stamp["id"] if stamp else None})
    stored = _return_chip(ctx, target["id"], home)
    ctx.step("recycle stamp", lambda: so.recycle_stamp(ctx, {}))
    used = {"flake_ids", "target_chip_id", "target_xy_mm", "pickup_temp_c", "drop_temp_c", "melt_temp_c",
            "stamp_id"}
    return {"device_chip_id": target["id"], "stored_at": stored, "stack": [f["id"] for f in flakes],
            "picks": picks, "drop": drop, "images": rec.get("images"), "ignored": _ignored(p, used)}


def cool_experiment_stages(ctx, p: dict) -> dict:
    """Heaters of THIS module off (fan on where there is one) until strictly below 40 C, fan off."""
    out = {}
    for dev_id, dev in ctx.module.devices.items():
        if dev.kind != "heater" or not dev.connected:
            continue
        fan = "exfoliation_fan" if dev_id == "exfoliation_heater" else None
        dev.off("cool_experiment_stages")
        if fan and fan in ctx.module.devices:
            ctx.device(fan, ("switch",)).set(True)

        def cold(dev=dev):
            t = dev.read().get("temp_c")
            return t is not None and t < FINAL_BELOW_C and t
        t = ctx.step(f"cool {dev_id}", lambda cold=cold: c.wait_until(ctx, cold, 3600, 5.0,
                                                                      f"{dev_id} below {FINAL_BELOW_C} C"))
        if fan and fan in ctx.module.devices:
            ctx.device(fan, ("switch",)).set(False)
        out[dev_id] = {"temperature_c": t}
    return {"below_c": FINAL_BELOW_C, "readings": out, "module": ctx.module.name}


def inspect_bench(ctx, p: dict) -> dict:
    """Photos from this module's desk camera(s). No detection yet (the brain gets the images)."""
    shots = {}
    for dev_id, dev in ctx.module.devices.items():
        if dev.kind == "camera" and dev_id.startswith("desk_camera") and dev.connected:
            info = dict(dev.capture(), camera=dev_id)
            ctx.image(info, "inspect_bench")
            shots[dev_id] = info["image_id"]
    return {"regions": p.get("regions"), "images": shots, "detections": None,
            "note": "no bench detector in qumus-lab yet; regions are not checked"}


# ============================================================================== registration
_JOB = {"job": {"type": "object", "description": "job bookkeeping: project_id, requested_by"}}


def register_exfoliation(m: Module) -> None:
    if not {"arm_left", "tape_gantry", "tape_reels", "exfoliation_heater"} <= set(m.devices):
        return
    m.add(Action("exfoliate_chips", exfoliate_chips, dict(_JOB, **{
        "material": {"type": "string"}, "num_chips": {"type": "integer", "minimum": 1, "maximum": 6},
        "chip_ids": {"type": "array"}, "prepare_mother_tape": {"type": "string", "enum": ["auto", "always", "never"]},
        "target_temp_c": {"type": "number", "enum": [22, 30, 50, 70, 90]},
        "dwell_s": {"type": "number", "minimum": 10, "maximum": 90},
        "num_massages": {"type": "integer", "minimum": 1, "maximum": 5},
        "press_depth_mm": {"type": "number"}, "peel_speed": {"type": "number", "minimum": 50, "maximum": 500},
        "peel_angle_deg": {"type": "number"}, "carrier_id": {"type": "string"}, "carrier_slot": {"type": "string"},
        "home_slots": {"type": "array"}, "store_slots": {"type": "array"}}), ("material",), motion=True, job=True,
        description=_job("Exfoliate bare chips with a crystal (mother tape when needed); chips go to the "
                         "middle board.")))
    _common_jobs(m)


def register_stacking(m: Module) -> None:
    from ..modules.stacking import STACKING_L4_DEVICES

    if STACKING_L4_DEVICES <= set(m.devices):
        m.add(Action("place_chip_on_transfer_stage", place_chip_on_transfer_stage,
                     dict(_JOB, chip_id={"type": "string"}, handover_slot={"type": "string"}), ("chip_id",),
                     motion=True, job=True, description=_job("Chip from its slot onto the transfer stage.")))
        m.add(Action("search_chip", search_chip, dict(_JOB, **{
            "chip_id": {"type": "string"}, "material": {"type": "string"},
            "min_area_um2": {"type": "number", "minimum": 0}, "max_revisits": {"type": "integer", "minimum": 0,
                                                                                "maximum": 50},
            "revisit_quota": {"type": "array"}, "home_slot": {"type": "string"},
            "thickness_labels": {"type": "array"}}), motion=True, job=True,
            description=_job("Chip under the microscope, scan, revisit candidates at 20x, store it back; flakes "
                             "go to the ledger.")))
        m.add(Action("revisit_chip", revisit_chip, dict(_JOB, **{
            "flake_ids": {"type": "array"}, "chip_id": {"type": "string"}, "material": {"type": "string"},
            "home_slot": {"type": "string"}, "max_revisits": {"type": "integer"}}), ("flake_ids",),
            motion=True, job=True, description=_job("Look again at given flakes at 20x.")))
        m.add(Action("stack_device", stack_device, dict(_JOB, **{
            "flake_ids": {"type": "array"}, "target_chip_id": {"type": "string"},
            "target_xy_mm": {"type": "array"}, "pickup_temp_c": {"type": "number"},
            "drop_temp_c": {"type": "number"}, "melt_temp_c": {"type": "number"}, "stamp_id": {"type": "string"},
            "design_path": {"type": "string"}}), ("flake_ids", "target_chip_id"), motion=True, job=True,
            description=_job("Pick the flakes in order with one stamp (top first) and drop the stack on the "
                             "target chip (v1 Hall-device order).")))
    _common_jobs(m)


def _common_jobs(m: Module) -> None:
    if any(d.kind == "heater" for d in m.devices.values()):
        m.add(Action("cool_experiment_stages", cool_experiment_stages, dict(_JOB), motion=True, job=True,
                     description=_job("This module's heaters off until strictly below 40 C.")))
    if any(d.kind == "camera" and k.startswith("desk_camera") for k, d in m.devices.items()):
        m.add(Action("inspect_bench", inspect_bench, dict(_JOB, regions={"type": "array"},
                                                         camera={"type": "string"}), motion=True, job=True,
                     description=_job("Photos from this module's desk camera.")))


def describe_jobs(m: Module) -> list[str]:
    return [a.name for a in m.actions.values() if getattr(a, "job", False)]


__all__ = ["register_exfoliation", "register_stacking", "describe_jobs", "norm_material", "Any"]
