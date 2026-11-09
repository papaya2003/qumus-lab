/* View-only bench page: camera pictures and readings. Sends GET requests only. */
"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const SKIP = new Set(["kind", "connected", "error", "fields"]);

function fmt(v) {
  if (v === null || v === undefined) return "n/a";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

async function setupCameras() {
  const r = await fetch("cameras.json", { cache: "no-store" });
  const { cameras } = await r.json();
  const box = $("#cams");
  box.textContent = "";
  for (const c of cameras) {
    const card = el("figure", "cam");
    card.style.margin = "0";
    const img = el("img");
    img.alt = `Live picture from ${c.id}`;
    const cap = el("figcaption", "cap");
    const name = el("span", "", `${c.id} (${c.module})`);
    const info = el("span", "muted", "loading");
    cap.append(name, info);
    card.append(img, cap);
    box.append(card);
    loop(c.id, img, info);
  }
}

async function loop(id, img, info) {
  for (;;) {
    const t0 = Date.now();
    try {
      const r = await fetch(`cam/${id}?t=${t0}`, { cache: "no-store" });
      if (r.ok) {
        const url = URL.createObjectURL(await r.blob());
        const old = img.src;
        img.src = url;
        if (old.startsWith("blob:")) URL.revokeObjectURL(old);
        info.textContent = new Date().toLocaleTimeString();
        info.className = "muted";
      } else {
        const body = await r.json().catch(() => ({}));
        info.textContent = body.error || `error ${r.status}`;
        info.className = "warn";
      }
    } catch (e) {
      info.textContent = "lab PC not reachable";
      info.className = "bad";
    }
    await new Promise((res) => setTimeout(res, Math.max(200, 600 - (Date.now() - t0))));
  }
}

function moduleCard(name, m) {
  const card = el("section", "mod");
  card.append(el("h2", "", `${name}${m.demo ? " (demo)" : ""}`));
  const flags = el("div", "flags");
  flags.append(m.locked ? el("span", "bad", `LOCKED: ${m.fault ? m.fault.code : ""}`) : el("span", "ok", "not locked"));
  flags.append(el("span", m.busy ? "warn" : "muted", m.busy ? `busy: ${m.busy_action}` : "idle"));
  flags.append(el("span", "muted", `control: ${m.lease ? m.lease.holder : "nobody"}`));
  card.append(flags);
  if (m.last_command) {
    const lc = m.last_command;
    card.append(el("div", lc.result === "ok" ? "muted" : "warn", `last: ${lc.action} ${lc.result}${lc.message ? " (" + lc.message + ")" : ""}`));
  }
  const table = el("table");
  for (const [id, d] of Object.entries(m.devices || {})) {
    const tr = el("tr");
    tr.append(el("td", "", id));
    const vals = Object.entries(d).filter(([k, v]) => !SKIP.has(k) && v !== null && typeof v !== "object")
      .map(([k, v]) => `${k} ${fmt(v)}`).join(", ");
    const td = el("td", d.connected === false ? "bad" : "", d.connected === false ? `not connected ${d.error || ""}` : vals || "no reading yet");
    tr.append(td);
    table.append(tr);
  }
  card.append(table);
  return card;
}

async function status() {
  try {
    const r = await fetch("status.json", { cache: "no-store" });
    const { modules } = await r.json();
    const box = $("#mods");
    box.textContent = "";
    for (const [name, m] of Object.entries(modules)) box.append(moduleCard(name, m));
    $("#updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $("#updated").textContent = "lab PC not reachable";
  }
}

setupCameras().catch(() => { $("#cams").textContent = "cameras not available"; });
status();
setInterval(status, 2000);
