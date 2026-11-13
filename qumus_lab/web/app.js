/* Qumus lab bench debug page. Talks to the module services through /m/<module>/api/... (contract v1). */
"use strict";

const CLIENT = "debug-page-" + Math.random().toString(36).slice(2, 6);
const MODULE_TITLES = { exfoliation: "Exfoliation (left)", stacking: "Stacking (right)" };
const modules = {};   // name -> {describe, state, el, nav, evNext}
const events = [];
let control = false;
let devMode = false;  // limits.yaml dev_mode: speed caps off (the service still checks ranges, heat, zones)
let current = null;   // module shown in the main area
const HOLD_DELAY_MS = 350;   // a press longer than this keeps stepping
const HOLD_MAX_STEPS = 20;   // a hold stops by itself after this many steps (e.g. a lost mouse-up over remote desktop)
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

const $ = (sel, root = document) => root.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};

async function api(mod, method, path, body) {
  const opt = { method, headers: {} };
  if (method === "POST") {
    opt.headers["Content-Type"] = "application/json";
    opt.headers["X-Qumus-Client"] = CLIENT;
    opt.body = JSON.stringify(body || {});
  }
  const r = await fetch(`/m/${mod}${path}`, opt);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  return { status: r.status, data };
}

function fmt(v, digits = 3) {
  if (v === null || v === undefined) return "n/a";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(digits);
  return String(v);
}

/* ------------------------------------------------------------------ commands */
function iHoldLease(m) { return m.state && m.state.lease && m.state.lease.holder === CLIENT; }

function canRun(m, action) {
  const c = m.describe.commands[action];
  if (!c) return { ok: false, why: "not available" };
  if (m.state && m.state.locked) return { ok: false, why: "module locked" };
  if (!c.any_client && !iHoldLease(m)) return { ok: false, why: "take control first" };
  return { ok: true };
}

async function send(mod, action, params = {}) {
  const m = modules[mod];
  const r = await api(mod, "POST", "/api/command", { action, ...params });
  if (r.status !== 202) flash(mod, `${action} refused (${r.status}): ${r.data && r.data.error}`, "failed");
  return r;
}

function flash(mod, text, cls) {
  const m = modules[mod];
  m.flash = { text, cls, at: Date.now(), until: Date.now() + 8000 };
  renderStatus(m);
}

/* ------------------------------------------------------------------ device rows */
const READ_KEYS = {
  arm: [["x_mm", "x", "mm"], ["y_mm", "y", "mm"], ["z_mm", "z", "mm"], ["nozzle_deg", "nozzle", "deg"], ["vacuum_on", "suction", ""], ["pose_trusted", "position trusted", ""]],
  stage: [["x_mm", "x", "mm"], ["y_mm", "y", "mm"], ["rail_mm", "rail", "mm"], ["moving", "moving", ""]],
  manipulator: [["x_mm", "x", "mm"], ["y_mm", "y", "mm"], ["z_mm", "z", "mm"], ["rail_mm", "rail", "mm"], ["goni_deg", "goniometer", "deg"]],
  gantry: [["x_mm", "x", "mm"], ["z_mm", "z", "mm"], ["motors_on", "motors on", ""]],
  focus: [["z_um", "z (estimate)", "um"]],
  turret: [["objective", "objective", "x"], ["position", "position", ""], ["last_reply", "reply", ""]],
  switch: [["on", "on", ""], ["confirmed", "confirmed", ""], ["raw_reply", "reply", ""]],
  heater: [["temp_c", "", "C"], ["target_c", "target", "C"], ["set_v", "set", "V"], ["phase", "phase", ""], ["last_off_reason", "last off", ""]],
  camera: [["last_image_id", "last image", ""]],
  tape_reels: [["motor1_on", "reel 1 on", ""], ["motor2_on", "reel 2 on", ""], ["last_command", "last", ""]],
};
const JOG = {
  arm: { axes: ["x", "y", "z"], steps: [0.1, 0.5] },
  stage: { axes: ["x", "y", "rail"], steps: [0.1, 0.5] },
  manipulator: { axes: ["x", "y", "z", "rail"], steps: [0.01, 0.05, 0.1] },
  gantry: { axes: ["x", "z"], steps: [0.1, 0.5] },
  focus: { axes: ["z"], steps: [0.005, 0.02, 0.05] },
};
// Development mode: larger jog steps (mm; focus in mm too, shown as um).
const DEV_STEPS = {
  arm: [0.1, 0.5, 1, 5, 10], stage: [0.1, 0.5, 1, 5, 10], manipulator: [0.01, 0.1, 0.5, 1],
  gantry: [0.1, 0.5, 1, 5, 10], focus: [0.005, 0.05, 0.5, 1],
};

const axisKey = (kind, axis) => (kind === "focus" ? "z_um" : `${axis}_mm`);
const axisUnit = (kind) => (kind === "focus" ? "um" : "mm");

/* Wait for a command to finish (last_command carries its id). Returns last_command, or null on timeout. */
async function waitDone(mod, id, timeoutMs = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const r = await api(mod, "GET", "/api/state").catch(() => null);
    if (r && r.status === 200) {
      modules[mod].state = r.data;
      if (r.data.last_command && r.data.last_command.id === id) return r.data.last_command;
    }
    await sleep(60);
  }
  return null;
}

/* Hold-to-move: every step is an ordinary capped jog that the gate checks; the next one is sent only after the
   last one finished ok and while the button is still held. Letting go sends nothing more (the step that is
   running finishes; an arm has no stop command, so no stop is sent). */
let activeHold = null;
function releaseHold() { if (activeHold) { activeHold.held = false; activeHold = null; } }
window.addEventListener("blur", releaseHold);
document.addEventListener("visibilitychange", () => { if (document.hidden) releaseHold(); });

function holdButton(mod, label, paramsFn, tip) {
  const b = h("button", { type: "button", class: "hold", "data-action": "jog", "data-tip": tip, title: tip, "aria-label": tip }, label);
  const run = async (hold) => {
    const m = modules[mod];
    if (m.stepping) { releaseHold(); return; }  // one stepping loop per module
    m.stepping = true;
    const t0 = Date.now();
    let steps = 0;
    b.classList.add("holding");
    try {
      while (true) {
        const r = await send(mod, "jog", paramsFn());
        if (r.status !== 202) break;
        steps += 1;
        const lc = await waitDone(mod, r.data.command_id);
        if (!lc || lc.result !== "ok" || !hold.held) break;
        const wait = HOLD_DELAY_MS - (Date.now() - t0);
        if (wait > 0) await sleep(wait);
        if (!hold.held) break;
        if (steps >= HOLD_MAX_STEPS) {
          flash(mod, `stopped after ${steps} steps: let go and press again to keep moving`, "stopped");
          break;
        }
      }
    } finally {
      m.stepping = false;
      b.classList.remove("holding");
      if (activeHold === hold) activeHold = null;
      refreshGuards(modules[mod]);
    }
  };
  b.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || b.disabled || activeHold) return;
    e.preventDefault();
    try { b.setPointerCapture(e.pointerId); } catch { /* old browser */ }
    activeHold = { held: true };
    run(activeHold);
  });
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) b.addEventListener(ev, releaseHold);
  b.addEventListener("click", (e) => {  // keyboard (Enter / Space): exactly one step
    if (e.detail !== 0 || b.disabled || activeHold) return;
    activeHold = { held: false };
    run(activeHold);
  });
  return b;
}

function guarded(mod, action, label, paramsFn, cls = "") {
  const b = h("button", { type: "button", class: cls, "data-action": action }, label);
  b.addEventListener("click", async () => {
    const p = paramsFn ? paramsFn() : {};
    if (p === null) return;
    b.disabled = true;
    await send(mod, action, p);
    setTimeout(() => refreshGuards(modules[mod]), 200);
  });
  return b;
}

function numInput(value, min, max, step, title) {
  return h("input", { type: "number", value, min, max, step, title, "aria-label": title });
}

function buildRow(mod, d) {
  const m = modules[mod];
  const row = h("div", { class: "dev", id: `dev-${mod}-${d.id}` });
  const name = h("div", {}, h("div", { class: "devname" }, h("span", { class: "dot" }), d.id), h("div", { class: "devkind" }, d.kind + (d.source ? `, values from ${d.source}` : "")));
  const read = h("div", { class: "read" });
  const err = h("div", { class: "err" });
  const ctl = h("div", { class: "ctl" });
  row.append(name, read, err, ctl);

  const jog = JOG[d.kind];
  if (jog && m.describe.commands.jog) {
    const step = h("select", { "aria-label": "jog step" }, (devMode ? DEV_STEPS[d.kind] : jog.steps).map((s) => h("option", { value: s }, d.kind === "focus" ? `${Math.round(s * 1000)} um` : `${s} mm`)));
    ctl.append(h("label", { class: "muted" }, "step ", step));
    for (const axis of jog.axes) {
      ctl.append(h("span", { class: "jog" },
        holdButton(mod, "-", () => ({ device: d.id, axis, mm: -Number(step.value) }), `${d.id} ${axis} minus: click = one step, hold = keep stepping`),
        h("b", {}, axis),
        holdButton(mod, "+", () => ({ device: d.id, axis, mm: Number(step.value) }), `${d.id} ${axis} plus: click = one step, hold = keep stepping`)));
    }
  }
  if (jog && m.describe.commands.move_axis) ctl.append(moveTo(mod, d, jog.axes));
  if (d.kind === "arm") {
    ctl.append(guarded(mod, "arm_suction", "Suction on", () => ({ device: d.id, on: true })),
               guarded(mod, "arm_suction", "Suction off", () => ({ device: d.id, on: false })));
    if (m.describe.commands.rotate_nozzle) {
      // v1 M2101 R<integer deg>; the arm does not report its nozzle angle, the page shows the last command
      const deg = numInput(0, -180, 180, 1, "nozzle degrees");
      ctl.append(h("label", { class: "muted" }, "nozzle ", deg, " deg"),
                 guarded(mod, "rotate_nozzle", "Rotate nozzle", () => {
                   const v = Math.round(Number(deg.value));
                   if (!Number.isFinite(v) || v < -180 || v > 180) return null;
                   return { device: d.id, deg: v };
                 }));
    }
  }
  if (d.kind === "turret") {
    for (const mag of [5, 10, 20, 100]) ctl.append(guarded(mod, "set_objective", `${mag}x`, () => ({ objective: mag })));
  }
  if (d.kind === "switch") {
    // Development mode: no auto-off cap; leave the box empty to keep the switch on until Off.
    const auto = devMode ? numInput("", 1, 86400, 1, "auto-off seconds (empty = stays on)") : numInput(10, 1, 10, 1, "auto-off seconds");
    ctl.append(guarded(mod, "set_switch", "On", () => (auto.value === "" ? { device: d.id, on: true } : { device: d.id, on: true, auto_off_s: Number(auto.value) })),
               h("label", { class: "muted" }, "auto-off ", auto, " s"),
               guarded(mod, "set_switch", "Off", () => ({ device: d.id, on: false })));
  }
  if (d.kind === "heater") {
    // Development mode: no first-heat caps on the page either; the service still refuses targets above the
    // heater's max_c and timeouts above its max. Empty volts = the heater's own boost/hold voltages.
    const t = devMode ? numInput(30, 1, 175, 1, "target C") : numInput(30, 1, 40, 1, "target C");
    const to = devMode ? numInput(300, 10, 7200, 10, "timeout s") : numInput(300, 10, 600, 10, "timeout s");
    const v = devMode ? numInput("", 0.5, 30, 0.5, "volts (empty = heater's own boost/hold)") : numInput(6.5, 0.5, 6.5, 0.5, "volts");
    ctl.append(h("label", { class: "muted" }, "target ", t, " C"), h("label", { class: "muted" }, "timeout ", to, " s"),
               h("label", { class: "muted" }, "at ", v, " V"),
               guarded(mod, "heat", "Heat", () => {
                 const p = { device: d.id, target_c: Number(t.value), timeout_s: Number(to.value) };
                 if (v.value !== "") p.volts = Number(v.value);
                 return p;
               }, "primary"),
               guarded(mod, "heat_off", "Heat off", () => ({ device: d.id })));
  }
  if (d.kind === "camera") {
    row.classList.add("cam");
    const live = cameraLive(mod, d.id);
    ctl.append(live.toggle, guarded(mod, "capture", "Capture", () => ({ camera: d.id })));
    const last = h("a", { class: "lastcap", target: "_blank", rel: "noopener", hidden: "" },
      h("img", { class: "thumb", alt: `last captured picture from ${d.id}` }), h("span", { class: "muted" }, "last capture"));
    row.append(h("div", { class: "liveview" }, live.figure, last));
  }
  if (d.kind === "tape_reels" && m.describe.commands.advance_tape) {
    const deg = devMode ? numInput(5, 0.1, 3600, 1, "degrees") : numInput(5, 1, 10, 1, "degrees");
    ctl.append(h("label", { class: "muted" }, "feed tape ", deg, " deg"), guarded(mod, "advance_tape", "Advance", () => ({ deg: Number(deg.value) })));
  }
  if (d.kind === "gantry" && m.describe.commands.apply_tape) {
    const sp = numInput(500, 50, 500, 50, "peel speed");
    ctl.append(guarded(mod, "gantry_power", "Motors on", () => ({ on: true })), guarded(mod, "gantry_power", "Motors off", () => ({ on: false })),
               guarded(mod, "apply_tape", "Apply tape"), h("label", { class: "muted" }, "peel at ", sp, " mm/min"),
               guarded(mod, "peel_tape", "Peel tape", () => ({ speed: Number(sp.value) })));
  }
  if (m.describe.commands.read) ctl.append(guarded(mod, "read", "Read", () => ({ device: d.id })));
  return row;
}

function renderRow(m, mod, id, st) {
  const row = document.getElementById(`dev-${mod}-${id}`);
  if (!row || !st) return;
  $(".dot", row).classList.toggle("on", !!st.connected);
  const keys = READ_KEYS[st.kind] || Object.keys(st).filter((k) => !["kind", "connected", "error"].includes(k)).map((k) => [k, k, ""]);
  const read = $(".read", row);
  read.replaceChildren(...keys.filter(([k]) => k in st).map(([k, label, unit]) => {
    const big = st.kind === "heater" && k === "temp_c";
    let val = fmt(st[k], st.kind === "focus" || unit === "C" ? 1 : 3);
    if (k === "last_image_id" && st[k]) val = st[k].split("-").slice(-2).join("-");  // time + short id
    return h("span", {}, label ? h("span", { class: "k" }, label) : null, h("span", { class: "v" + (big ? " big" : "") }, val), unit ? h("span", { class: "u" }, unit) : null);
  }));
  if (st.kind === "heater" && st.deadline_unix && st.phase !== "off") {
    read.append(h("span", {}, h("span", { class: "k" }, "off in"), h("span", { class: "v" }, Math.max(0, Math.round(st.deadline_unix - Date.now() / 1000))), h("span", { class: "u" }, "s")));
  }
  $(".err", row).textContent = st.error ? st.error : "";
  if (st.kind === "camera" && st.last_image_id) {
    const a = $("a.lastcap", row);
    const src = `/m/${mod}/api/images/${encodeURIComponent(st.last_image_id)}`;
    if (a && a.dataset.src !== src) { a.dataset.src = src; a.href = src; $("img", a).src = src; a.hidden = false; }
  }
  const mv = $(".moveto", row);
  if (mv) {
    const inp = $("input", mv);
    const cur = st[axisKey(st.kind, $("select", mv).value)];
    $(".now", mv).textContent = `now ${fmt(cur, st.kind === "focus" ? 1 : 3)}`;
    if (inp.value === "" && typeof cur === "number" && document.activeElement !== inp) inp.value = String(Number(cur.toFixed(4)));
  }
}

function refreshGuards(m) {
  for (const b of m.el.querySelectorAll("button[data-action]")) {
    if (b.classList.contains("holding")) continue;
    const g = canRun(m, b.dataset.action);
    b.disabled = !g.ok;
    b.title = g.ok ? (b.dataset.tip || "") : g.why;
  }
}

/* Typed target for one axis. Arm, stage, gantry and stamp rail have no distance cap (stamp x/y 1 mm, focus
   500 um); stamp z and arm z down keep the jog cap. The service reads the position again before it moves. */
function moveTo(mod, d, axes) {
  const unit = axisUnit(d.kind);
  const axis = h("select", { "aria-label": `${d.id} axis to move` }, axes.map((a) => h("option", { value: a }, a)));
  const target = h("input", { type: "number", step: d.kind === "focus" ? "1" : "0.01", "aria-label": `${d.id} target (${unit})` });
  const now = h("span", { class: "now muted" }, "");
  axis.addEventListener("change", () => { target.value = ""; renderRow(modules[mod], mod, d.id, modules[mod].state && modules[mod].state.devices[d.id]); });
  const go = guarded(mod, "move_axis", "Move", () => {
    const st = modules[mod].state && modules[mod].state.devices[d.id];
    const cur = st ? st[axisKey(d.kind, axis.value)] : null;
    const t = Number(target.value);
    if (target.value === "" || !Number.isFinite(t)) { flash(mod, "type a target position first", "failed"); return null; }
    if (!devMode && !confirm(`Move ${d.id} ${axis.value} from ${fmt(cur)} to ${t} ${unit}? Watch the instrument; STOP ALL is at the bottom.`)) return null;
    return { device: d.id, axis: axis.value, target: t };
  });
  go.dataset.tip = devMode ? "Move one axis to the typed position (development mode: no distance cap, axis ranges still apply)" : `Move one axis to the typed position${d.kind === "focus" ? " (at most 500 um away)" : d.kind === "manipulator" ? " (stamp x/y at most 1 mm, z 0.1 mm away)" : " (no distance cap; arm z down 0.5 mm until min_z is measured)"}`;
  return h("span", { class: "moveto" }, h("span", { class: "muted" }, "move "), axis, h("span", { class: "muted" }, " to "), target,
    h("span", { class: "muted" }, ` ${unit} `), go, now);
}

function renderStatus(m) {
  const st = m.state;
  const box = $(".status", m.el);
  if (!st) { box.textContent = "service not reachable"; return; }
  const parts = [];
  parts.push(st.locked ? h("span", { class: "pill locked" }, "Locked") : h("span", { class: "pill ok" }, st.busy ? `Busy: ${st.busy_action}` : "Ready"));
  if (st.locked && st.fault) parts.push(h("span", {}, `${st.fault.code}: ${st.fault.message}`));
  if (st.locked) parts.push(h("button", { type: "button", class: "primary", onclick: () => unlock(m.name) }, "Unlock"));
  const lease = st.lease ? (st.lease.holder === CLIENT ? "you have control" : `controlled by ${st.lease.holder}`) : "nobody has control";
  parts.push(h("span", { class: "muted" }, lease));
  for (const [zone, z] of Object.entries(st.zones || {})) parts.push(...zoneParts(m, zone, z));
  const lc = st.last_command;
  const flashNewer = m.flash && m.flash.until > Date.now() && !(lc && lc.finished_at * 1000 > m.flash.at);
  if (flashNewer) parts.push(h("span", { class: "last " + m.flash.cls }, m.flash.text));
  else if (lc) parts.push(h("span", { class: "last " + lc.result }, `${lc.action}: ${lc.result}${lc.message ? ", " + lc.message : ""}`));
  box.replaceChildren(...parts);
}

/* Middle board occupancy lock: shared by both modules (one arm at a time). */
function zoneParts(m, zone, z) {
  const label = zone.replace(/_/g, " ");
  const c = m.describe.commands.zone_acquire;
  const arms = c ? c.params.properties.device.enum : [];
  const out = [h("span", { class: "zone" + (z.holder ? " held" : "") },
    z.holder ? `${label}: held by ${z.holder}${z.inside.length ? " (inside)" : ""}` : `${label}: free`)];
  const ctl = canRun(m, "zone_acquire");
  for (const arm of arms) {
    if (!z.holder) {
      out.push(h("button", { type: "button", disabled: ctl.ok ? undefined : "", title: ctl.ok ? "" : ctl.why,
        onclick: () => send(m.name, "zone_acquire", { zone, device: arm }) }, `Take for ${arm}`));
    } else if (z.holder === arm) {
      out.push(h("button", { type: "button", disabled: ctl.ok && !z.inside.length ? undefined : "",
        title: z.inside.length ? "move the arm out first" : (ctl.ok ? "" : ctl.why),
        onclick: () => send(m.name, "zone_release", { zone, device: arm }) }, "Release"));
    }
  }
  if (z.holder) {
    out.push(h("button", { type: "button", onclick: () => {
      if (confirm(`Clear ${label}? Only after you checked by eye that no arm is in it.`)) send(m.name, "zone_clear", { zone });
    } }, "Clear"));
  }
  return out;
}

async function unlock(mod) {
  if (!confirm(`Unlock ${mod}? Only after you checked the instrument by eye.`)) return;
  const r = await api(mod, "POST", "/api/unlock", {});
  if (r.status !== 200) flash(mod, `unlock refused: ${r.data && r.data.error}`, "failed");
}

/* ------------------------------------------------------------------ loops */
async function pollState() {
  for (const [name, m] of Object.entries(modules)) {
    try {
      const r = await api(name, "GET", "/api/state");
      m.state = r.status === 200 ? r.data : null;
    } catch { m.state = null; }
    if (m.state) for (const [id, st] of Object.entries(m.state.devices)) renderRow(m, name, id, st);
    renderStatus(m);
    renderNav(m);
    refreshGuards(m);
  }
  const holders = Object.values(modules).map((m) => m.state && m.state.lease && m.state.lease.holder).filter(Boolean);
  const other = holders.find((x) => x !== CLIENT);
  $("#leaseinfo").textContent = control ? (other ? `Another client (${other}) holds control of a module.` : "You have control of both modules.")
    : (other ? `Controlled by ${other}. Reading only.` : "Reading only. Moves, switches and heating need control.");
}

async function heartbeat() {
  if (!control) return;
  for (const name of Object.keys(modules)) {
    const r = await api(name, "POST", "/api/heartbeat", {});
    if (r.status === 409) flash(name, `control refused: ${r.data.error}`, "failed");
  }
}

async function pollEvents() {
  let added = false;
  for (const [name, m] of Object.entries(modules)) {
    try {
      const r = await api(name, "GET", `/api/events?since=${m.evNext || 0}`);
      if (r.status !== 200) continue;
      for (const e of r.data.events) { events.push({ ...e, module: name }); added = true; }
      m.evNext = r.data.next;
    } catch { /* service down */ }
  }
  if (added) renderEvents();
}

function renderEvents() {
  events.sort((a, b) => b.ts_unix - a.ts_unix);
  if (events.length > 400) events.length = 400;
  const onlyWarn = $("#evwarn").checked;
  $("#events").replaceChildren(...events.filter((e) => !onlyWarn || e.level !== "info").slice(0, 200).map((e) => {
    const t = new Date(e.ts_unix * 1000).toLocaleTimeString();
    const f = Object.keys(e.fields || {}).length ? JSON.stringify(e.fields) : "";
    return h("li", { class: e.level }, h("span", {}, t), h("span", { class: "src" }, e.module), h("span", {}, e.message, " ", h("span", { class: "f" }, f)));
  }));
}

async function stopAll() {
  const out = [];
  await Promise.all(Object.keys(modules).map(async (name) => {
    try {
      const r = await api(name, "POST", "/api/stop", { wait_s: 2, heat_off: true });
      out.push(`${name}: ${r.data ? r.data.stop : r.status}`);
    } catch (e) { out.push(`${name}: not reachable`); }
  }));
  $("#stopresult").textContent = "Stop " + out.join(", ");
}

/* ------------------------------------------------------------------ cameras (live preview, read only) */
/* Each camera row shows its live picture (about 5 per second, nothing saved) while its module is on screen,
   the page is visible and "Live" is ticked. The service grabs frames only while someone asks for them. */
const camLoops = {};

function cameraLive(mod, id) {
  const img = h("img", { alt: `Live picture from ${id}` });
  const info = h("span", { class: "muted" }, "off");
  const box = h("input", { type: "checkbox" });
  let saved = null;
  try { saved = localStorage.getItem(`qumus-camlive-${id}`); } catch { saved = null; }
  box.checked = saved !== "off";
  box.addEventListener("change", () => {
    try { localStorage.setItem(`qumus-camlive-${id}`, box.checked ? "on" : "off"); } catch { /* private window */ }
    startCameras();
  });
  camLoops[id] = { mod, img, info, box, running: false };
  return { toggle: h("label", { class: "muted camtoggle" }, box, " Live"),
           figure: h("figure", { class: "live" }, img, h("figcaption", {}, info)) };
}

const camWanted = (c) => c.box.checked && c.mod === current && !document.hidden;

function startCameras() {
  for (const id of Object.keys(camLoops)) if (camWanted(camLoops[id])) camLoop(id);
}

async function camLoop(id) {
  const c = camLoops[id];
  if (c.running) return;
  c.running = true;
  c.info.textContent = "loading";
  while (camWanted(c)) {
    const t0 = Date.now();
    try {
      const r = await fetch(`/cam/${id}?t=${t0}`, { cache: "no-store" });
      if (r.ok) {
        const old = c.img.src;
        c.img.src = URL.createObjectURL(await r.blob());
        if (old.startsWith("blob:")) URL.revokeObjectURL(old);
        c.info.textContent = `live, ${new Date().toLocaleTimeString()}`;
        c.info.className = "muted";
      } else {
        const body = await r.json().catch(() => ({}));
        c.info.textContent = body.error || `error ${r.status}`;
        c.info.className = "warn";
      }
    } catch (e) {
      c.info.textContent = "not reachable";
      c.info.className = "warn";
    }
    await sleep(Math.max(80, 200 - (Date.now() - t0)));
  }
  c.info.textContent = c.box.checked ? "paused" : "off";
  c.info.className = "muted";
  c.running = false;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) startCameras(); });

/* ------------------------------------------------------------------ module switcher (left column) */
function navItem(name, title, service) {
  const state = h("span", { class: "navstate" }, "connecting");
  const b = h("button", { type: "button", onclick: () => select(name) },
    h("span", { class: "navname" }, title), h("span", { class: "navport muted" }, service), state);
  return { li: h("li", {}, b), button: b, state };
}

function renderNav(m) {
  const st = m.state;
  const s = m.nav.state;
  if (!st) { s.textContent = "not reachable"; s.className = "navstate locked"; return; }
  if (st.locked) { s.textContent = "Locked"; s.className = "navstate locked"; return; }
  s.textContent = st.busy ? `Busy: ${st.busy_action}` : "Ready";
  s.className = "navstate " + (st.busy ? "busy" : "ok");
}

function select(name) {
  if (!modules[name]) name = Object.keys(modules)[0];
  current = name;
  for (const [n, m] of Object.entries(modules)) {
    m.el.hidden = n !== name;
    if (n === name) m.nav.button.setAttribute("aria-current", "page"); else m.nav.button.removeAttribute("aria-current");
  }
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  startCameras();
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  const list = await (await fetch("/modules.json")).json();
  const bench = $("#bench");
  let anyDemo = false;
  for (const name of list.modules) {
    const d = (await api(name, "GET", "/api/describe")).data;
    devMode = devMode || Boolean(d.dev_mode);
    const el = $("#tpl-module").content.firstElementChild.cloneNode(true);
    $("h2", el).textContent = MODULE_TITLES[name] || name;
    $(".port", el).textContent = d.service;
    const m = { name, describe: d, el, state: null, evNext: 0 };
    modules[name] = m;
    $(".readall", el).dataset.action = "read_all";
    $(".readall", el).addEventListener("click", () => send(name, "read_all"));
    const pts = d.commands.go_to_point ? d.commands.go_to_point.params.properties.point.enum : [];
    $(".points", el).replaceChildren(...pts.map((p) => h("option", { value: p }, p)));
    const go = $(".go", el);
    go.dataset.action = "go_to_point";
    go.addEventListener("click", () => {
      const p = $(".points", el).value;
      if (devMode || confirm(`Move to ${p}? Watch the instrument; STOP ALL is at the bottom.`)) send(name, "go_to_point", { point: p });
    });
    if (!pts.length) $(".goto", el).hidden = true;
    for (const dev of d.devices) $(".devices", el).append(buildRow(name, dev));
    if (!d.commands.jog) $(".howto", el).hidden = true;
    m.nav = navItem(name, MODULE_TITLES[name] || name, d.service);
    $("#modnav").append(m.nav.li);
    bench.append(el);
    anyDemo = anyDemo || d.demo;
  }
  if (devMode) $("#devmode").hidden = false;
  select(location.hash.slice(1));
  window.addEventListener("hashchange", () => select(location.hash.slice(1)));
  const mode = $("#mode");
  mode.textContent = anyDemo ? "Demo: simulated devices" : "Real instruments";
  mode.className = "mode " + (anyDemo ? "demo" : "real");
  $("#take").addEventListener("change", async (e) => {
    control = e.target.checked;
    if (control) await heartbeat();
    else for (const name of Object.keys(modules)) await api(name, "POST", "/api/release", {});
    pollState();
  });
  $("#evwarn").addEventListener("change", renderEvents);
  $("#stopall").addEventListener("click", stopAll);
  window.addEventListener("beforeunload", () => {
    if (control) for (const name of Object.keys(modules)) navigator.sendBeacon && fetch(`/m/${name}/api/release`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Qumus-Client": CLIENT }, body: "{}" });
  });
  await pollState();
  setInterval(pollState, 500);
  setInterval(heartbeat, 1000);
  setInterval(pollEvents, 1000);
  pollEvents();
}

boot().catch((e) => { document.body.prepend(h("p", { class: "err" }, `Debug page failed to start: ${e}`)); });
