/* Qumus lab bench debug page. Talks to the module services through /m/<module>/api/... (contract v1).
   Layout (2026-11-13 redesign, Hearth design system): cameras on top, then one block per physical unit (UNITS:
   arm / tape / exfoliation stage; arm / sample stage / stamp / microscope). Position boxes show the live value
   and take a typed target; suction, switches and gantry motors are one toggle each. */
"use strict";

const CLIENT = "debug-page-" + Math.random().toString(36).slice(2, 6);
const MODULE_TITLES = { exfoliation: "Exfoliation", stacking: "Stacking" };
const modules = {};   // name -> {describe, state, el, tab, evNext, cards}
const events = [];
let control = false;
let devMode = false;  // limits.yaml dev_mode: speed caps off (the service still checks ranges, heat, zones)
let current = null;   // module shown
const HOLD_DELAY_MS = 350;   // a press longer than this keeps stepping
const HOLD_MAX_STEPS = 20;   // a hold stops by itself after this many steps (e.g. a lost mouse-up over remote desktop)
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

const $ = (sel, root = document) => root.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v);
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
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/* Replace an element's children only when what they show changed, so text on the page stays selectable
   between the 0.5 s refreshes. */
function setKids(el, nodes) {
  const box = h("div", {}, nodes);
  if (el.dataset.sig === box.innerHTML) return;
  el.dataset.sig = box.innerHTML;
  el.replaceChildren(...box.childNodes);
}

/* The connection dot in a card's head. */
function setDot(el, st, busy = false) {
  $(".dot", el).className = "dot " + (!st.connected ? "bad" : busy ? "busy" : "ok");
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
  const r = await api(mod, "POST", "/api/command", { action, ...params });
  if (r.status !== 202) flash(mod, `${action} refused (${r.status}): ${r.data && r.data.error}`, "failed");
  return r;
}

function flash(mod, text, cls) {
  const m = modules[mod];
  m.flash = { text, cls, at: Date.now(), until: Date.now() + 8000 };
  renderStatus(m);
}

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

/* ------------------------------------------------------------------ device tables */
const MOTION = { arm: ["x", "y", "z"], stage: ["x", "y", "rail"], manipulator: ["x", "y", "z", "rail"], gantry: ["x", "z"], focus: ["z"] };
const STEPS = {
  arm: [0.1, 0.5], stage: [0.1, 0.5], manipulator: [0.01, 0.05, 0.1], gantry: [0.1, 0.5], focus: [0.005, 0.02, 0.05],
};
// Development mode: larger jog steps (mm; focus in mm too, shown as um).
const DEV_STEPS = {
  arm: [0.1, 0.5, 1, 5, 10], stage: [0.1, 0.5, 1, 5, 10], manipulator: [0.01, 0.1, 0.5, 1],
  gantry: [0.1, 0.5, 1, 5, 10], focus: [0.005, 0.05, 0.5, 1],
};
// Small facts under the position boxes (read-only fields of the device).
const FACTS = {
  arm: [["pose_trusted", "position trusted"], ["moving", "moving"]],
  stage: [["moving", "moving"]],
  manipulator: [["goni_deg", "goniometer", "deg"]],
  gantry: [],
  focus: [],
};
const axisKey = (kind, axis) => (kind === "focus" ? "z_um" : `${axis}_mm`);
const axisUnit = (kind) => (kind === "focus" ? "um" : "mm");

/* One block per physical unit, in bench order. A device not listed here goes to its kind's block (arm) or to
   "Other devices", so a new device in devices.yaml still shows up. */
const UNITS = {
  exfoliation: [["Arm", ["arm_left"]], ["Tape", ["tape_gantry", "tape_reels"]],
                ["Exfoliation stage", ["exfoliation_heater", "exfoliation_vacuum", "exfoliation_fan"]]],
  stacking: [["Arm", ["arm_right"]], ["Sample stage", ["sample_stage", "stage_vacuum", "transfer_heater"]],
             ["Stamp", ["stamp", "stamp_vacuum"]], ["Microscope", ["focus", "turret"]]],
};
// Short names inside a block (the device id stays beside it, small, because the L3 sheet and points.yaml use it).
const LABELS = {
  tape_gantry: "Gantry", tape_reels: "Reels", exfoliation_heater: "Heater", exfoliation_vacuum: "Vacuum",
  exfoliation_fan: "Fan", sample_stage: "Stage", stage_vacuum: "Vacuum", transfer_heater: "Heater",
  stamp: "Holder", stamp_vacuum: "Vacuum", focus: "Focus", turret: "Objectives",
};

/* ------------------------------------------------------------------ buttons */
/* Hold-to-move: every step is an ordinary jog that the gate checks; the next one is sent only after the last one
   finished ok and while the button is still held. Letting go sends nothing more (the step that is running
   finishes; an arm has no stop command, so no stop is sent). */
let activeHold = null;
function releaseHold() { if (activeHold) { activeHold.held = false; activeHold = null; } }
window.addEventListener("blur", releaseHold);
document.addEventListener("visibilitychange", () => { if (document.hidden) releaseHold(); });

function holdButton(mod, label, paramsFn, tip) {
  const b = h("button", { type: "button", class: "step", "data-action": "jog", "data-tip": tip, title: tip, "aria-label": tip }, label);
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

/* One switch-style button that shows the state and flips it: [label (track) On]. When the state is unknown
   (never switched since connecting) a click sends OFF, so turning something off is always one click.
   ``paramsFn(on)`` returns the command parameters (null = cancel). */
function toggleButton(mod, action, label, paramsFn) {
  const word = h("span", { class: "toggle__word" }, "?");
  const b = h("button", { type: "button", class: "toggle", role: "switch", "aria-checked": "false", "data-action": action },
    h("span", { class: "toggle__label" }, label), h("span", { class: "toggle__track", "aria-hidden": "true" }, h("i")), word);
  b.on = null;
  b.addEventListener("click", async () => {
    const p = paramsFn(b.on === false);
    if (p === null) return;
    b.disabled = true;
    await send(mod, action, p);
    setTimeout(() => refreshGuards(modules[mod]), 200);
  });
  b.set = (on, note) => {
    b.on = on === null || on === undefined ? null : Boolean(on);
    b.classList.toggle("is-on", b.on === true);
    b.setAttribute("aria-checked", String(b.on === true));
    word.textContent = b.on === null ? "unknown" : b.on ? "On" : "Off";
    b.dataset.tip = `${label}: ${word.textContent.toLowerCase()}${note ? ` (${note})` : ""}. Click to turn ${b.on === false ? "on" : "off"}.`;
  };
  b.set(null);
  return b;
}

function numInput(value, min, max, step, title) {
  return h("input", { type: "number", value, min, max, step, title, "aria-label": title });
}

function refreshGuards(m) {
  for (const b of m.el.querySelectorAll("button[data-action]")) {
    if (b.classList.contains("holding")) continue;
    if (b.dataset.busy === "1") { b.disabled = true; continue; }
    const g = canRun(m, b.dataset.action);
    b.disabled = !g.ok;
    b.title = g.ok ? (b.dataset.tip || "") : g.why;
  }
}

/* ------------------------------------------------------------------ cards */
function card(mod, d, extraClass = "") {
  const el = h("article", { class: `card ${extraClass}`, id: `dev-${mod}-${d.id}` });
  const label = LABELS[d.id];
  const head = h("div", { class: "card__head" }, h("span", { class: "dot" }), h("span", { class: "card__name" }, label || d.id),
    h("span", { class: "card__id", title: d.kind + (d.source ? ` · values from ${d.source}` : "") }, label ? d.id : d.kind));
  if (modules[mod].describe.commands.read) {
    head.append(h("button", { type: "button", class: "icon", "data-action": "read", "data-tip": `read ${d.id} again`, "aria-label": `read ${d.id} again`,
      onclick: () => send(mod, "read", { device: d.id }) }, "↻"));
  }
  el.append(head);
  return el;
}

/* One moving device: a position box per axis (live value until you type), steps, go to point. */
function motionCard(mod, d) {
  const m = modules[mod];
  const el = card(mod, d);
  const unit = axisUnit(d.kind);
  const axes = MOTION[d.kind];
  const steps = devMode ? DEV_STEPS[d.kind] : STEPS[d.kind];
  const step = h("select", { "aria-label": `${d.id} step` }, steps.map((s) => h("option", { value: s }, d.kind === "focus" ? `${Math.round(s * 1000)} um` : `${s} mm`)));
  const help = "Type a value and press Enter (or Move) to go there; Esc puts the live value back. − / + steps once, hold to keep stepping."
    + (d.kind === "arm" ? " Several edited axes move one after another: z up first, z down last." : " Several edited axes move one after another, top to bottom.");
  const head = h("div", { class: "pos__head" }, h("span", { class: "muted small hint", title: help }, `Position (${unit}) `, h("span", { class: "hint__i", "aria-hidden": "true" }, "?")));
  if (m.describe.commands.jog) head.append(h("label", {}, "step ", step));
  el.append(head);
  const rows = {};
  const ui = { el, rows, kind: d.kind, id: d.id };
  for (const axis of axes) {
    // a text box (not type=number) so the live value can be refreshed without losing a select-all; arrow keys
    // change the typed value by the chosen step (nothing moves until Enter)
    const inp = h("input", { type: "text", inputmode: "decimal", autocomplete: "off", spellcheck: "false", "aria-label": `${d.id} ${axis} (${unit})`, class: "mono num" });
    const now = h("div", { class: "axis__now" });
    const edited = () => { inp.classList.add("dirty"); renderMotion(m, mod, ui, m.state && m.state.devices[d.id]); };
    inp.addEventListener("input", edited);
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); moveEdited(mod, ui); }
      if (e.key === "Escape") { inp.classList.remove("dirty"); inp.value = ""; renderMotion(m, mod, ui, m.state && m.state.devices[d.id]); inp.blur(); }
      if ((e.key === "ArrowUp" || e.key === "ArrowDown") && Number.isFinite(Number(inp.value)) && inp.value !== "") {
        e.preventDefault();
        const st = Number(step.value) * (d.kind === "focus" ? 1000 : 1);
        const digits = d.kind === "focus" ? 1 : 3;
        inp.value = (Number(inp.value) + (e.key === "ArrowUp" ? st : -st)).toFixed(digits);
        edited();
      }
    });
    const row = h("div", { class: "axis" }, h("span", { class: "axis__name" }, axis, h("span", { class: "axis__unit" }, unit)), inp);
    if (m.describe.commands.jog) {
      row.append(holdButton(mod, "−", () => ({ device: d.id, axis, mm: -Number(step.value) }), `${d.id} ${axis} minus: click = one step, hold = keep stepping`),
                 holdButton(mod, "+", () => ({ device: d.id, axis, mm: Number(step.value) }), `${d.id} ${axis} plus: click = one step, hold = keep stepping`));
    } else row.append(h("span"), h("span"));
    row.append(now);
    rows[axis] = { inp, now };
    el.append(row);
  }
  if (d.kind === "arm" && m.describe.commands.rotate_nozzle) {
    // v1 rotate_nose: M2100 then M2101 R<integer deg> turns the nozzle BY that much from where it is (as in v1;
    // the arm does not report its angle). The box keeps what you typed until the turn was accepted.
    const inp = h("input", { type: "number", step: "1", min: "-180", max: "180", "aria-label": `${d.id} nozzle: turn by (deg)`, class: "mono", placeholder: "turn by" });
    const now = h("div", { class: "axis__now", title: "The nozzle turns BY the typed angle from where it is (v1 M2101 R). Since pick resets when suction turns on: it is the slot angle when you measure one." }, "turns by this much from where it is now");
    const rot = h("button", { type: "button", class: "sm", "data-action": "rotate_nozzle", "data-tip": "turn the nozzle by the typed angle (v1 M2101 R)" }, "Turn");
    const turn = async () => {
      if (!canRun(m, "rotate_nozzle").ok || rot.dataset.busy === "1") return;
      const v = Math.round(Number(inp.value));
      if (inp.value === "" || !Number.isFinite(v) || v < -180 || v > 180) { flash(mod, "nozzle turn must be -180 to 180 deg", "failed"); return; }
      rot.dataset.busy = "1"; rot.disabled = true;
      try {
        const r = await send(mod, "rotate_nozzle", { device: d.id, deg: v });
        if (r.status !== 202) return;
        inp.value = ""; inp.classList.remove("dirty");
        const lc = await waitDone(mod, r.data.command_id);  // Turn stays off until this turn finished
        if (!lc || lc.result !== "ok") flash(mod, `nozzle turn: ${lc ? lc.result + (lc.message ? ", " + lc.message : "") : "no answer"}`, "failed");
      } finally { delete rot.dataset.busy; refreshGuards(m); }
    };
    rot.addEventListener("click", turn);
    inp.addEventListener("input", () => inp.classList.toggle("dirty", inp.value !== ""));
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); turn(); } });
    el.append(h("div", { class: "axis axis--note" }, h("span", { class: "axis__name" }, "nozzle", h("span", { class: "axis__unit" }, "deg")), inp, rot, h("span"), now));
    rows.nozzle = { inp, now, nozzle: true };
  }
  if (m.describe.commands.move_axis) {
    const go = h("button", { type: "button", class: "primary sm", "data-action": "move_axis",
      "data-tip": devMode ? "Move the edited axes (no distance cap in development mode; axis ranges still apply)" : "Move the edited axes" }, "Move");
    go.addEventListener("click", () => moveEdited(mod, ui));
    const reset = h("button", { type: "button", class: "sm", onclick: () => resetEdits(m, mod, ui) }, "Reset");
    ui.go = go;
    el.append(h("div", { class: "pos__go" }, go, reset));
  }
  const facts = h("div", { class: "facts" });
  el.append(facts);
  ui.facts = facts;
  if (d.points && d.points.length && m.describe.commands.go_to_point) {
    const sel = h("select", { "aria-label": `${d.id} point` }, d.points.map((p) => h("option", { value: `${d.id}.${p}` }, p)));
    const go = guarded(mod, "go_to_point", "Go", () => {
      const p = sel.value;
      if (!devMode && !confirm(`Move to ${p}? Watch the instrument; STOP ALL is at the bottom.`)) return null;
      return { point: p };
    }, "sm");
    el.append(h("div", { class: "goto" }, h("span", { class: "muted small" }, "Go to point"), sel, go));
  }
  const act = h("div", { class: "actions" });
  if (d.kind === "arm" && m.describe.commands.arm_suction) {
    ui.toggle = { key: "vacuum_on", b: toggleButton(mod, "arm_suction", "Suction", (on) => ({ device: d.id, on })) };
  }
  if (d.kind === "gantry" && m.describe.commands.gantry_power) {
    ui.toggle = { key: "motors_on", b: toggleButton(mod, "gantry_power", "Motors", (on) => ({ on })) };
  }
  if (ui.toggle) act.append(ui.toggle.b);
  if (d.kind === "gantry" && m.describe.commands.apply_tape) {
    const sp = numInput(500, 50, 500, 50, "peel speed");
    act.append(guarded(mod, "apply_tape", "Apply tape", null, "sm"),
               guarded(mod, "peel_tape", "Peel tape", () => ({ speed: Number(sp.value) }), "sm"), h("label", {}, "at ", sp, " mm/min"));
  }
  if (act.childNodes.length) el.append(h("div", { class: "sep" }), act);
  el.append(h("div", { class: "err" }));
  m.cards[d.id] = { ...ui, render: (st) => renderMotion(m, mod, ui, st) };
  return el;
}

function resetEdits(m, mod, ui) {
  for (const r of Object.values(ui.rows)) { r.inp.classList.remove("dirty"); r.inp.value = ""; }
  renderMotion(m, mod, ui, m.state && m.state.devices[ui.id]);
}

/* Move every edited axis, one after another. Arm: z up first, other axes, z down last (no sweep while low; a
   bigger arm z is higher). Other devices: which way their z points is not established, so z is moved on its
   own; edited x / y / rail move in box order. */
async function moveEdited(mod, ui) {
  const m = modules[mod];
  if (ui.moving) return;  // a second Enter while the first moves does nothing
  const g = canRun(m, "move_axis");
  if (!g.ok) { flash(mod, `move: ${g.why}`, "failed"); return; }
  const st = m.state && m.state.devices[ui.id];
  const plan = [];
  for (const [axis, r] of Object.entries(ui.rows)) {
    if (r.nozzle || !r.inp.classList.contains("dirty")) continue;
    const t = Number(r.inp.value);
    if (r.inp.value === "" || !Number.isFinite(t)) { flash(mod, `${ui.id} ${axis}: type a number`, "failed"); return; }
    const cur = st ? st[axisKey(ui.kind, axis)] : null;
    plan.push({ axis, target: t, cur });
  }
  if (!plan.length) { flash(mod, "type a new value in a position box first", "failed"); return; }
  if (ui.kind === "arm") {
    const rank = (p) => (p.axis !== "z" ? 1 : (typeof p.cur === "number" && p.target >= p.cur ? 0 : 2));
    plan.sort((a, b) => rank(a) - rank(b));
  } else if (plan.length > 1 && plan.some((p) => p.axis === "z")) {
    flash(mod, `${ui.id}: move z on its own (which way is up is not established for this device)`, "failed");
    return;
  }
  const unit = axisUnit(ui.kind);
  const text = plan.map((p) => `${p.axis} ${fmt(p.cur)} → ${p.target} ${unit}`).join(", ");
  if (!devMode && !confirm(`Move ${ui.id}: ${text}? Watch the instrument; STOP ALL is at the bottom.`)) return;
  ui.moving = true;
  if (ui.go) { ui.go.dataset.busy = "1"; ui.go.disabled = true; }
  try {
    for (const p of plan) {
      const r = await send(mod, "move_axis", { device: ui.id, axis: p.axis, target: p.target });
      if (r.status !== 202) return;
      const lc = await waitDone(mod, r.data.command_id, 180000);
      if (!lc || lc.result !== "ok") { flash(mod, `${ui.id} ${p.axis}: ${lc ? lc.result + (lc.message ? ", " + lc.message : "") : "no answer"}`, "failed"); return; }
      const row = ui.rows[p.axis];
      row.inp.classList.remove("dirty");
      row.inp.value = "";
    }
  } finally {
    ui.moving = false;
    if (ui.go) delete ui.go.dataset.busy;
    refreshGuards(m);
    renderMotion(m, mod, ui, m.state && m.state.devices[ui.id]);
  }
}

function renderMotion(m, mod, ui, st) {
  if (!st) return;
  setDot(ui.el, st);
  for (const [axis, r] of Object.entries(ui.rows)) {
    if (r.nozzle) {
      // the arm cannot report its angle: the driver counts the turns (since the last pick = a slot's angle)
      const pick = st.nozzle_since_pick_deg, total = st.nozzle_turned_deg;
      r.now.textContent = typeof pick === "number" ? `turned since pick ${pick} deg · since connect ${total} deg` : "turns by this much from where it is now";
      continue;
    }
    const cur = st[axisKey(ui.kind, axis)];
    const digits = ui.kind === "focus" ? 1 : 3;
    if (r.inp.classList.contains("dirty")) {
      r.now.textContent = `now ${fmt(cur, digits)}`;
    } else {
      // Not edited: the box always follows the live value (typing marks it edited first).
      r.now.textContent = "";
      const v = typeof cur === "number" ? cur.toFixed(digits) : "";
      if (r.inp.value !== v) {
        // keep a select-all (you are about to type over it) when the live value changes under the cursor
        const all = document.activeElement === r.inp && r.inp.value !== "" && r.inp.selectionStart === 0 && r.inp.selectionEnd === r.inp.value.length;
        r.inp.value = v;
        if (all) r.inp.select();
      }
    }
  }
  if (ui.toggle) ui.toggle.b.set(st[ui.toggle.key]);
  setKids(ui.facts, (FACTS[ui.kind] || []).filter(([k]) => k in st).map(([k, label, unit]) =>
    h("span", {}, `${label} `, h("b", {}, fmt(st[k]) + (unit ? ` ${unit}` : "")))));
  $(".err", ui.el).textContent = st.error || "";
}

function heaterCard(mod, d) {
  const m = modules[mod];
  const el = card(mod, d);
  const temp = h("div", { class: "figure" }, "n/a");
  const facts = h("div", { class: "facts" });
  // Development mode: no first-heat caps on the page either; the service still refuses targets above the
  // heater's max_c and timeouts above its max. Empty volts = the heater's own boost/hold voltages.
  const t = devMode ? numInput(30, 1, 175, 1, "target C") : numInput(30, 1, 40, 1, "target C");
  const to = devMode ? numInput(300, 10, 7200, 10, "timeout s") : numInput(300, 10, 600, 10, "timeout s");
  const v = devMode ? numInput("", 0.5, 30, 0.5, "volts (empty = heater's own boost/hold)") : numInput(6.5, 0.5, 6.5, 0.5, "volts");
  el.append(temp, facts,
    h("div", { class: "actions" }, h("label", {}, "target ", t, " C"), h("label", {}, "for ", to, " s"), h("label", {}, "at ", v, " V")),
    h("div", { class: "actions" },
      guarded(mod, "heat", "Heat", () => {
        const p = { device: d.id, target_c: Number(t.value), timeout_s: Number(to.value) };
        if (v.value !== "") p.volts = Number(v.value);
        return p;
      }, "primary"),
      guarded(mod, "heat_off", "Heat off", () => ({ device: d.id }))),
    h("div", { class: "err" }));
  m.cards[d.id] = { el, render: (st) => {
    setDot(el, st, st.phase && st.phase !== "off");
    setKids(temp, [fmt(st.temp_c, 1), h("small", {}, "°C")]);
    const f = [["target", st.target_c, "C"], ["set", st.set_v, "V"], ["phase", st.phase, ""]];
    if (st.deadline_unix && st.phase !== "off") f.push(["off in", Math.max(0, Math.round(st.deadline_unix - Date.now() / 1000)), "s"]);
    if (st.last_off_reason) f.push(["last off", st.last_off_reason, ""]);
    setKids(facts, f.map(([k, val, u]) => h("span", {}, `${k} `, h("b", {}, fmt(val, 1) + (u ? ` ${u}` : "")))));
    $(".err", el).textContent = st.error || "";
  } };
  return el;
}

/* A relay (vacuum, fan): one row, one toggle. Development mode: it stays on until you turn it off. Strict mode:
   it needs an auto-off time (the service caps it), shown as a small box beside the toggle. */
function switchCard(mod, d) {
  const m = modules[mod];
  const el = card(mod, d, "card--row");
  const auto = devMode ? null : numInput(10, 1, 10, 1, "auto-off seconds");
  const tog = toggleButton(mod, "set_switch", LABELS[d.id] || d.id, (on) =>
    (on && auto ? { device: d.id, on, auto_off_s: Number(auto.value) } : { device: d.id, on }));
  $(".toggle__label", tog).classList.add("visually-hidden");
  const note = h("span", { class: "muted small card__note" });
  $(".card__head", el).append(note, tog);
  if (auto) $(".card__head", el).append(h("label", { class: "small muted" }, "off after ", auto, " s"));
  el.append(h("div", { class: "err" }));
  m.cards[d.id] = { el, render: (st) => {
    setDot(el, st);
    const unconfirmed = st.on !== null && st.on !== undefined && st.confirmed === false;  // null = never switched yet
    tog.set(st.on, unconfirmed ? "relay did not confirm" : "");
    note.textContent = unconfirmed ? "not confirmed" : "";
    note.title = unconfirmed && st.raw_reply !== undefined ? `relay reply: ${fmt(st.raw_reply)}` : "";
    $(".err", el).textContent = st.error || "";
  } };
  return el;
}

function genericCard(mod, d) {
  const m = modules[mod];
  const el = card(mod, d);
  const facts = h("div", { class: "facts" });
  el.append(facts);
  const act = h("div", { class: "actions" });
  if (d.kind === "tape_reels" && m.describe.commands.advance_tape) {
    const deg = devMode ? numInput(5, 0.1, 3600, 1, "degrees") : numInput(5, 1, 10, 1, "degrees");
    act.append(h("label", {}, "feed tape ", deg, " deg"), guarded(mod, "advance_tape", "Advance", () => ({ deg: Number(deg.value) }), "sm"));
  }
  if (d.kind === "turret") act.append(...objectiveButtons(mod));
  if (act.childNodes.length) el.append(act);
  el.append(h("div", { class: "err" }));
  m.cards[d.id] = { el, render: (st) => {
    setDot(el, st);
    const keys = Object.keys(st).filter((k) => !["kind", "connected", "error"].includes(k));
    setKids(facts, keys.map((k) => h("span", {}, `${k.replace(/_/g, " ")} `, h("b", {}, fmt(st[k])))));
    $(".err", el).textContent = st.error || "";
  } };
  return el;
}

function objectiveButtons(mod) {
  return [5, 10, 20, 100].map((mag) => guarded(mod, "set_objective", `${mag}x`, () => ({ objective: mag }), "sm"));
}

/* ------------------------------------------------------------------ cameras (live preview, read only) */
/* Each camera shows its live picture (about 5 per second, nothing saved) while its module is on screen, the page
   is visible and Live is on. The service grabs frames only while someone asks for them. */
const camLoops = {};

function cameraCard(mod, d, turret) {
  const m = modules[mod];
  const el = h("article", { class: "cam", id: `dev-${mod}-${d.id}` });
  const img = h("img", { alt: `Live picture from ${d.id}` });
  const info = h("span", { class: "cam__info" }, "off");
  const live = h("button", { type: "button", "aria-pressed": "false", title: "live picture on / off" }, "Live");
  let saved = null;
  try { saved = localStorage.getItem(`qumus-camlive-${d.id}`); } catch { saved = null; }
  const c = { mod, img, info, on: saved !== "off", running: false, live };
  camLoops[d.id] = c;
  const sync = () => { live.classList.toggle("is-on", c.on); live.setAttribute("aria-pressed", String(c.on)); };
  live.addEventListener("click", () => {
    c.on = !c.on;
    try { localStorage.setItem(`qumus-camlive-${d.id}`, c.on ? "on" : "off"); } catch { /* private window */ }
    sync();
    startCameras();
  });
  sync();
  const last = h("a", { class: "cam__last", target: "_blank", rel: "noopener", hidden: "", title: "last captured picture" },
    h("img", { alt: `last captured picture from ${d.id}` }));
  const tools = h("div", { class: "cam__tools" }, live, guarded(mod, "capture", "Capture", () => ({ camera: d.id })));
  const objective = h("span", { class: "cam__info" });
  const view = h("div", { class: "cam__view" }, img, last);
  const bar = h("div", { class: "cam__bar" }, h("span", { class: "dot" }), h("span", { class: "cam__name" }, d.id.replace(/_camera/, "").replace(/_/g, " ")), info, tools);
  // objectives sit on the picture (top left), so the microscope tile is exactly as big as the others
  if (turret) view.append(h("div", { class: "cam__obj" }, objective, ...objectiveButtons(mod)));
  el.append(bar, view, h("div", { class: "err" }));
  m.cards[d.id] = { el, render: (st) => {
    setDot(el, st);
    if (st.last_image_id) {
      const src = `/m/${mod}/api/images/${encodeURIComponent(st.last_image_id)}`;
      if (last.dataset.src !== src) { last.dataset.src = src; last.href = src; $("img", last).src = src; last.hidden = false; }
    }
    $(".err", el).textContent = st.error || "";
  } };
  if (turret) {
    // the turret lives on the microscope tile: its own connection and error show here too
    m.cards[turret.id] = { el, render: (st) => {
      const bad = !st.connected || st.error;
      objective.textContent = !st.connected ? "turret not connected" : st.error ? `turret: ${st.error}` : st.objective ? `${st.objective}x` : "objective ?";
      objective.className = "cam__info" + (bad ? " warn" : "");
      objective.title = st.error ? `turret: ${st.error}` : "";
    } };
  }
  return el;
}

const camWanted = (c) => c.on && c.mod === current && !document.hidden;

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
        c.info.textContent = "";
        c.info.className = "cam__info ok";
      } else {
        const body = await r.json().catch(() => ({}));
        c.info.textContent = body.error || `error ${r.status}`;
        c.info.className = "cam__info warn";
      }
    } catch (e) {
      c.info.textContent = "not reachable";
      c.info.className = "cam__info warn";
    }
    await sleep(Math.max(80, 200 - (Date.now() - t0)));
  }
  c.info.textContent = c.on ? "paused" : "off";  // shown only when not live
  c.info.className = "cam__info";
  c.running = false;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) startCameras(); });

/* ------------------------------------------------------------------ status bar */
function renderStatus(m) {
  const st = m.state;
  const box = $(".status", m.el);
  if (!st) { setKids(box, [h("span", { class: "pill locked" }, "Service not reachable")]); return; }
  const parts = [];
  parts.push(st.locked ? h("span", { class: "pill locked" }, "Locked")
    : h("span", { class: "pill" }, h("span", { class: "dot " + (st.busy ? "busy" : "ok") }), st.busy ? `Busy: ${st.busy_action}` : "Ready"));
  if (st.locked && st.fault) parts.push(h("span", {}, `${st.fault.code}: ${st.fault.message}`));
  if (st.locked) parts.push(h("button", { type: "button", class: "primary sm", onclick: () => unlock(m.name) }, "Unlock"));
  const lease = st.lease ? (st.lease.holder === CLIENT ? "you have control" : `controlled by ${st.lease.holder}`) : "nobody has control";
  parts.push(h("span", { class: "muted small" }, lease));
  for (const [zone, z] of Object.entries(st.zones || {})) parts.push(...zoneParts(m, zone, z));
  const lc = st.last_command;
  const flashNewer = m.flash && m.flash.until > Date.now() && !(lc && lc.finished_at * 1000 > m.flash.at);
  if (flashNewer) parts.push(h("span", { class: "last " + m.flash.cls }, m.flash.text));
  else if (lc) parts.push(h("span", { class: "last " + lc.result }, `${lc.action}: ${lc.result}${lc.message ? ", " + lc.message : ""}`));
  setKids(box, parts);
}

/* Middle board occupancy lock: shared by both modules (one arm at a time). */
function zoneParts(m, zone, z) {
  const label = zone.replace(/_/g, " ");
  const c = m.describe.commands.zone_acquire;
  const arms = c ? c.params.properties.device.enum : [];
  const out = [h("span", { class: "zone" + (z.holder ? " held" : "") }, h("span", { class: "dot " + (z.holder ? "busy" : "ok") }),
    z.holder ? `${label}: held by ${z.holder}${z.inside.length ? " (inside)" : ""}` : `${label}: free`)];
  const ctl = canRun(m, "zone_acquire");
  for (const arm of arms) {
    if (!z.holder) {
      out.push(h("button", { type: "button", class: "sm", disabled: ctl.ok ? undefined : "", title: ctl.ok ? "" : ctl.why,
        onclick: () => send(m.name, "zone_acquire", { zone, device: arm }) }, `Take for ${arm}`));
    } else if (z.holder === arm) {
      out.push(h("button", { type: "button", class: "sm", disabled: ctl.ok && !z.inside.length ? undefined : "",
        title: z.inside.length ? "move the arm out first" : (ctl.ok ? "" : ctl.why),
        onclick: () => send(m.name, "zone_release", { zone, device: arm }) }, "Release"));
    }
  }
  if (z.holder) {
    out.push(h("button", { type: "button", class: "sm", onclick: () => {
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
    if (m.state) for (const [id, st] of Object.entries(m.state.devices)) if (m.cards[id]) m.cards[id].render(st);
    renderStatus(m);
    renderTab(m);
    refreshGuards(m);
  }
  const holders = Object.values(modules).map((m) => m.state && m.state.lease && m.state.lease.holder).filter(Boolean);
  const other = holders.find((x) => x !== CLIENT);
  // the top bar says something only when another client holds control; the rest is the button's tooltip
  const info = $("#leaseinfo");
  info.textContent = other ? `Controlled by ${other}` : "";
  info.hidden = !other;
  $("#take").title = control ? "You have control of both modules. Click to release." : "Reading only. Moves, switches and heating need control.";
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

/* ------------------------------------------------------------------ module tabs */
function tabFor(name, title, service) {
  const state = h("span", { class: "tabstate" }, "connecting");
  const dot = h("span", { class: "dot" });
  const b = h("button", { type: "button", class: "tab", role: "tab", title: service, onclick: () => select(name) }, dot, title, state);
  return { button: b, state, dot };
}

function renderTab(m) {
  const st = m.state;
  const t = m.tab;
  if (!st) { t.state.textContent = "not reachable"; t.dot.className = "dot bad"; return; }
  if (st.locked) { t.state.textContent = "locked"; t.dot.className = "dot bad"; return; }
  t.state.textContent = st.busy ? `busy: ${st.busy_action}` : "ready";
  t.dot.className = "dot " + (st.busy ? "busy" : "ok");
}

function select(name) {
  if (!modules[name]) name = Object.keys(modules)[0];
  current = name;
  for (const [n, m] of Object.entries(modules)) {
    m.el.hidden = n !== name;
    m.tab.button.classList.toggle("is-active", n === name);
    m.tab.button.setAttribute("aria-selected", String(n === name));
  }
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  startCameras();
}

/* ------------------------------------------------------------------ boot */
function buildModule(name, d) {
  const el = $("#tpl-module").content.firstElementChild.cloneNode(true);
  const m = { name, describe: d, el, state: null, evNext: 0, cards: {} };
  modules[name] = m;
  const readall = $(".readall", el);
  readall.dataset.action = "read_all";
  readall.addEventListener("click", () => send(name, "read_all"));
  const turret = d.devices.find((x) => x.kind === "turret");
  const scope = d.devices.find((x) => x.kind === "camera" && x.id.includes("microscope"));
  const cams = d.devices.filter((x) => x.kind === "camera").sort((a, b) => (b === scope) - (a === scope));
  for (const dev of cams) $(".cams", el).append(cameraCard(name, dev, dev === scope ? turret : null));
  if (!cams.length) $(".band--cams", el).classList.add("is-empty");
  // one block per physical unit; devices not in UNITS: an arm to "Arm", the rest to "Other devices"
  const blocks = (UNITS[name] || []).map(([title, ids]) => ({ title, ids, devs: [] }));
  const block = (title) => blocks.find((b) => b.title === title) || (blocks.push({ title, ids: [], devs: [] }), blocks[blocks.length - 1]);
  for (const dev of d.devices) {
    if (dev.kind === "camera" || (dev === turret && scope)) continue;
    const home = blocks.find((b) => b.ids.includes(dev.id));
    (home || block(dev.kind === "arm" ? "Arm" : "Other devices")).devs.push(dev);
  }
  const units = $(".units", el);
  for (const b of blocks) {
    if (!b.devs.length) continue;
    b.devs.sort((x, y) => (b.ids.indexOf(x.id) + 1 || 99) - (b.ids.indexOf(y.id) + 1 || 99));
    const body = h("div", { class: "unit__body" });
    for (const dev of b.devs) {
      if (MOTION[dev.kind]) body.append(motionCard(name, dev));
      else if (dev.kind === "heater") body.append(heaterCard(name, dev));
      else if (dev.kind === "switch") body.append(switchCard(name, dev));
      else body.append(genericCard(name, dev));
    }
    units.append(h("section", { class: "unit", "aria-label": b.title }, h("h2", { class: "unit__title" }, b.title), body));
  }
  m.tab = tabFor(name, MODULE_TITLES[name] || name, d.service);
  $("#modnav").append(m.tab.button);
  return el;
}

async function boot() {
  const list = await (await fetch("/modules.json")).json();
  const bench = $("#bench");
  let anyDemo = false;
  const described = [];
  for (const name of list.modules) {
    const d = (await api(name, "GET", "/api/describe")).data;
    devMode = devMode || Boolean(d.dev_mode);
    anyDemo = anyDemo || d.demo;
    described.push([name, d]);
  }
  for (const [name, d] of described) bench.append(buildModule(name, d));
  if (devMode) $("#devmode").hidden = false;
  select(location.hash.slice(1));
  window.addEventListener("hashchange", () => select(location.hash.slice(1)));
  const mode = $("#mode");
  mode.textContent = anyDemo ? "Demo · simulated" : "Real instruments";
  mode.className = "badge " + (anyDemo ? "demo" : "real");
  const take = $("#take");
  take.addEventListener("click", async () => {
    control = !control;
    take.setAttribute("aria-pressed", String(control));
    take.textContent = control ? "Release control" : "Take control";
    if (control) {
      await heartbeat();
      // relays, arm suction and gantry motors cannot be read: put the unknown ones into their safe state so the
      // toggles show on / off (Jialei's choice, 2026-11-13; known ones are left as they are)
      for (const [name, m] of Object.entries(modules)) if (m.describe.commands.known_state) await send(name, "known_state");
    } else for (const name of Object.keys(modules)) await api(name, "POST", "/api/release", {});
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

boot().catch((e) => { document.body.prepend(h("p", { class: "fatal" }, `Debug page failed to start: ${e}`)); });
