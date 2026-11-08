/* Qumus lab bench debug page. Talks to the module services through /m/<module>/api/... (contract v1). */
"use strict";

const CLIENT = "debug-page-" + Math.random().toString(36).slice(2, 6);
const MODULE_TITLES = { exfoliation: "Exfoliation (left)", stacking: "Stacking (right)" };
const modules = {};   // name -> {describe, state, el, rows, evNext, holder}
const events = [];
let control = false;

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
  m.flash = { text, cls, until: Date.now() + 8000 };
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
    const step = h("select", { "aria-label": "jog step" }, jog.steps.map((s) => h("option", { value: s }, d.kind === "focus" ? `${Math.round(s * 1000)} um` : `${s} mm`)));
    ctl.append(h("label", { class: "muted" }, "step ", step));
    for (const axis of jog.axes) {
      ctl.append(h("span", { class: "jog" },
        guarded(mod, "jog", "-", () => ({ device: d.id, axis, mm: -Number(step.value) })),
        h("b", {}, axis),
        guarded(mod, "jog", "+", () => ({ device: d.id, axis, mm: Number(step.value) }))));
    }
  }
  if (d.kind === "arm") {
    ctl.append(guarded(mod, "arm_suction", "Suction on", () => ({ device: d.id, on: true })),
               guarded(mod, "arm_suction", "Suction off", () => ({ device: d.id, on: false })));
  }
  if (d.kind === "turret") {
    for (const mag of [5, 10, 20, 100]) ctl.append(guarded(mod, "set_objective", `${mag}x`, () => ({ objective: mag })));
  }
  if (d.kind === "switch") {
    const auto = numInput(10, 1, 10, 1, "auto-off seconds");
    ctl.append(guarded(mod, "set_switch", "On", () => ({ device: d.id, on: true, auto_off_s: Number(auto.value) })),
               h("label", { class: "muted" }, "auto-off ", auto, " s"),
               guarded(mod, "set_switch", "Off", () => ({ device: d.id, on: false })));
  }
  if (d.kind === "heater") {
    const t = numInput(30, 1, 40, 1, "target C"), to = numInput(300, 10, 600, 10, "timeout s"), v = numInput(6.5, 0.5, 6.5, 0.5, "volts");
    ctl.append(h("label", { class: "muted" }, "target ", t, " C"), h("label", { class: "muted" }, "timeout ", to, " s"),
               h("label", { class: "muted" }, "at ", v, " V"),
               guarded(mod, "heat", "Heat", () => ({ device: d.id, target_c: Number(t.value), timeout_s: Number(to.value), volts: Number(v.value) }), "primary"),
               guarded(mod, "heat_off", "Heat off", () => ({ device: d.id })));
  }
  if (d.kind === "camera") {
    ctl.append(guarded(mod, "capture", "Capture", () => ({ camera: d.id })));
    row.append(h("div", { class: "ctl" }, h("img", { class: "thumb", alt: `last picture from ${d.id}`, hidden: "" })));
  }
  if (d.kind === "tape_reels" && m.describe.commands.advance_tape) {
    const deg = numInput(5, 1, 10, 1, "degrees");
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
    const img = $("img.thumb", row);
    const src = `/m/${mod}/api/images/${encodeURIComponent(st.last_image_id)}`;
    if (img && img.dataset.src !== src) { img.dataset.src = src; img.src = src; img.hidden = false; }
  }
}

function refreshGuards(m) {
  for (const b of m.el.querySelectorAll("button[data-action]")) {
    const g = canRun(m, b.dataset.action);
    b.disabled = !g.ok;
    b.title = g.ok ? "" : g.why;
  }
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
  if (m.flash && m.flash.until > Date.now()) parts.push(h("span", { class: "last " + m.flash.cls }, m.flash.text));
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

/* ------------------------------------------------------------------ boot */
async function boot() {
  const list = await (await fetch("/modules.json")).json();
  const bench = $("#bench");
  let anyDemo = false;
  for (const name of list.modules) {
    const d = (await api(name, "GET", "/api/describe")).data;
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
      if (confirm(`Move to ${p}? Watch the instrument; STOP ALL is at the bottom.`)) send(name, "go_to_point", { point: p });
    });
    if (!pts.length) $(".goto", el).hidden = true;
    for (const dev of d.devices) $(".devices", el).append(buildRow(name, dev));
    bench.append(el);
    anyDemo = anyDemo || d.demo;
  }
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
