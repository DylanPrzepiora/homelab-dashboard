"use strict";
let csrf = null, busy = new Set(), timer = null;
const $ = id => document.getElementById(id);
const h = (tag, attrs, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
};
const bytes = n => { if (n == null) return "–"; const u = ["B","KB","MB","GB","TB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toFixed(n < 10 && i ? 1 : 0) + " " + u[i]; };
const dur = s => { if (s == null) return "–"; s = Math.floor(s); const d = Math.floor(s/86400), hh = Math.floor(s%86400/3600), m = Math.floor(s%3600/60); return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : `${m}m ${s%60}s`; };
const lvl = p => p >= 90 ? "crit" : p >= 75 ? "warn" : "";
const bar = (p, cls) => h("div", {class: "bar"}, h("i", {class: cls ?? lvl(p), style: `width:${Math.min(100, p)}%`}));
const tag = (text, cls) => h("span", {class: "tag " + cls}, text);

async function api(path, opts = {}) {
  const o = {credentials: "same-origin", ...opts, headers: {...(opts.headers || {})}};
  if (o.method === "POST") { o.headers["X-CSRF-Token"] = csrf || ""; o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.endsWith("login")) { showLogin(); throw new Error("login required"); }
  return r;
}
function showLogin() { clearTimeout(timer); $("app").hidden = true; $("login").hidden = false; $("pw").focus(); }
async function showApp() { $("login").hidden = true; $("app").hidden = false; poll(); }

$("loginform").addEventListener("submit", async e => {
  e.preventDefault(); $("loginerr").textContent = "";
  const r = await fetch("/api/login", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: $("pw").value})});
  const j = await r.json().catch(() => ({}));
  if (r.ok) { csrf = j.csrf; $("pw").value = ""; showApp(); } else $("loginerr").textContent = j.error || "Login failed";
});
$("logout").addEventListener("click", async () => { await api("/api/logout", {method: "POST", body: "{}"}).catch(() => {}); csrf = null; showLogin(); });

// ---- dialog
function dialog(title, body, okLabel) {
  return new Promise(res => {
    $("dlgtitle").textContent = title; $("dlgbody").textContent = body;
    const ok = $("dlgok"); ok.hidden = !okLabel; ok.textContent = okLabel || "";
    const d = $("dlg"), done = v => { ok.onclick = null; d.onclose = null; d.close(); res(v); };
    ok.onclick = () => done(true); $("dlgcancel").onclick = () => done(false); d.onclose = () => res(false);
    d.showModal();
  });
}

// ---- host / raid / smart
function stat(k, v, extra) { return h("div", {class: "card"}, h("div", {class: "k"}, k), h("div", {class: "v"}, v), extra); }
function renderHost(x) {
  const g = $("host"); if (!x) return g.replaceChildren(h("div", {class: "muted"}, "Waiting for data…"));
  const nets = x.net.map(n => h("div", {class: "small"}, `${n.name}: ↓ ${bytes(n.rx_bps)}/s ↑ ${bytes(n.tx_bps)}/s`));
  g.replaceChildren(
    stat("CPU", x.cpu.toFixed(0) + " %", [bar(x.cpu), h("div", {class: "cores"}, x.cores.map(c => h("div", {class: "bar", title: c + " %"}, h("i", {class: lvl(c), style: `height:${c}%`})))), h("div", {class: "small muted"}, `${x.cores.length} cores, load ${x.load.map(l => l.toFixed(2)).join(" ")}`)]),
    stat("RAM", `${bytes(x.mem.used)} / ${bytes(x.mem.total)}`, [bar(x.mem.percent), h("div", {class: "small muted"}, `${x.mem.percent}% used, ${bytes(x.mem.cached)} cache`)]),
    stat("Swap", `${bytes(x.swap.used)} / ${bytes(x.swap.total)}`, bar(x.swap.percent, x.swap.percent >= 50 ? "warn" : "")),
    stat("Uptime", dur(x.uptime)),
    ...x.disks.map(d => stat("Disk " + d.label, d.error ? "error" : `${d.percent}%`, d.error ? h("div", {class: "small err"}, d.error) : [bar(d.percent), h("div", {class: "small muted"}, `${bytes(d.used)} of ${bytes(d.total)}`)])),
    stat("Network", "", nets),
    stat("Temperatures", "", x.temps.length ? x.temps.map(t => h("div", {class: "small"}, `${t.chip} ${t.label}: ${t.c} °C`)) : h("div", {class: "small muted"}, "no sensors")));
}
function renderRaid(r) {
  const box = $("raid"); $("raidsec").hidden = !r;   // no md RAID on this host: hide the whole section
  if (!r) return box.replaceChildren();
  $("raidname").textContent = r.name || "";
  if (r.error) return box.replaceChildren(h("div", {class: "card err"}, r.error));
  const bad = r.degraded || (r.mdstat_flags || "").includes("_");
  const rows = (r.members || []).map(m => h("tr", {}, h("td", {class: "mono"}, m.dev), h("td", {class: "mono small"}, m.by_id || "?"), h("td", {}, tag(m.state, m.state.includes("faulty") ? "crit" : m.state.includes("in_sync") ? "ok" : "warn")), h("td", {}, "slot " + m.slot), h("td", {}, m.errors + " errors")));
  const act = r.sync_action !== "idle" ? h("div", {}, `${r.sync_action}: ${r.sync_pct ?? "?"}%`, r.finish ? ` (done in ${r.finish})` : "", r.sync_speed_kbs ? `, ${bytes(r.sync_speed_kbs * 1024)}/s` : "", r.sync_pct != null ? bar(r.sync_pct, "") : null) : null;
  box.replaceChildren(h("div", {class: "card"},
    h("div", {}, tag(bad ? "DEGRADED" : "healthy", bad ? "crit" : "ok"), ` ${r.level} · state ${r.array_state} · ${r.raid_disks} disks · [${r.mdstat_flags || "?"}] · mismatch ${r.mismatch}`),
    act, h("table", {}, h("tr", {}, ["Member", "Disk (by-id)", "State", "Slot", "Errors"].map(t => h("th", {}, t))), rows)));
}
function renderSmart(s) {
  const box = $("smart"); if (!s) return box.replaceChildren();
  if (s.error) return box.replaceChildren(h("div", {class: "card err"}, s.error));
  if (!s.disks.length) return box.replaceChildren(h("div", {class: "card muted"}, "No disks are configured. List them under smart.disks in config.yaml (see docs/smart-setup.md)."));
  const stale = !!s.stale;
  const rows = s.disks.map(d => {
    const warn = d.reallocated || d.pending || d.uncorrectable;
    const state = d.standby ? tag("standby", "off") : d.error ? tag("error", "warn") : d.passed === false ? tag("FAILING", "crit") : d.passed ? tag(warn ? "passed, watch" : "passed", warn ? "warn" : "ok") : tag("unknown", "off");
    return h("tr", {}, h("td", {}, h("div", {}, d.label || d.model || "?"), h("div", {class: "small muted"}, d.label && d.model ? d.model : ""), h("div", {class: "mono small muted"}, d.id), d.error ? h("div", {class: "small err"}, d.error) : null),
      h("td", {}, state),
      h("td", {}, d.temp_c != null ? d.temp_c + " °C" : "–"), h("td", {}, d.power_on_hours != null ? `${d.power_on_hours.toLocaleString()} h (${(d.power_on_hours / 8766).toFixed(1)} y)` : "–"),
      h("td", {}, d.reallocated ?? "–"), h("td", {}, d.pending ?? "–"), h("td", {}, d.uncorrectable ?? "–"));
  });
  box.replaceChildren(h("div", {class: "card" + (stale ? " stale" : "")},
    stale ? h("div", {class: "alert warn"}, `⚠ STALE: this data is ${dur(s.age)} old and is NOT current. The host collector (dashboard-smart.timer) has stopped or is failing.`) : null,
    h("table", {}, h("tr", {}, ["Disk", "SMART", "Temp", "Power-on", "Realloc", "Pending", "Uncorr."].map(t => h("th", {}, t))), rows),
    h("div", {class: "small muted"}, `Collected ${dur(s.age)} ago by the host timer`)));
}

// ---- alerts / audit
function renderAlerts(list) {
  $("alerts").replaceChildren(...(list.length ? list.map(a => h("div", {class: "alert " + a.level}, (a.level === "crit" ? "⛔ " : a.level === "warn" ? "⚠ " : "ℹ ") + a.text)) : [h("div", {class: "alert okay"}, "✓ All checks OK")]));
}
function renderAudit(list) {
  $("audit").replaceChildren(...(list.length ? list.map(a => h("div", {}, `${a.ts.replace("T", " ")}  ${a.event}${a.container ? " " + a.container : ""}${a.result ? " → " + a.result : ""}  (${a.ip})`)) : [h("span", {class: "muted"}, "nothing yet")]));
}

// ---- containers (keyed, so buttons survive refreshes)
const cards = new Map(), groups = new Map();
function buildCard(c) {
  const el = {};
  const btn = (label, act, cls) => { const b = h("button", {class: "btn " + (cls || ""), onclick: () => doAction(el.name, act)}, label); return b; };
  el.b = {start: btn("Start", "start"), stop: btn("Stop", "stop"), restart: btn("Restart", "restart"), logs: h("button", {class: "btn", onclick: () => showLogs(el.name)}, "Logs")};
  el.title = h("span"); el.tag = h("span"); el.img = h("div", {class: "cimg"}); el.stats = h("div", {class: "stats"}); el.note = h("div", {class: "small muted"});
  el.root = h("div", {class: "card"}, h("div", {class: "cname"}, el.title, el.tag), el.img, el.stats, el.note, h("div", {class: "row"}, Object.values(el.b)));
  return el;
}
function updateCard(el, c) {
  el.name = c.name; el.title.textContent = c.name; el.img.textContent = c.image;
  const run = c.status === "running";
  const cls = c.unexpected ? "crit" : run ? (c.health === "unhealthy" ? "warn" : "ok") : "off";
  el.tag.className = "tag " + cls; el.tag.textContent = c.status + (c.health ? ` (${c.health})` : "");
  el.stats.replaceChildren(...[["Uptime", run ? dur(c.uptime) : "stopped " + (c.finished ? dur(Date.now() / 1000 - c.finished) + " ago" : "")], ["Restarts", `${c.restarts} (${c.policy})`], ["CPU", c.cpu != null ? c.cpu + " %" : "–"], ["RAM", c.mem != null ? bytes(c.mem) + (c.mem_limit && c.mem_limit < 8e9 ? " / " + bytes(c.mem_limit) : "") : "–"]].flatMap(([k, v]) => [h("span", {class: "muted"}, k), h("span", {}, v)]));
  el.note.textContent = c.protection === "self" ? "Protected: cannot stop or restart the dashboard itself" : c.protection === "confirm" ? "Asks for confirmation (access-critical)" : (c.status === "exited" && !c.unexpected ? `exited cleanly (code ${c.exit_code})` : c.unexpected ? `unexpected exit, code ${c.exit_code}${c.oom ? ", OOM-killed" : ""}` : "");
  const self = c.protection === "self", b = el.b, locked = busy.has(c.name);
  b.start.disabled = run || locked; b.stop.disabled = !run || self || locked; b.restart.disabled = !run && c.status !== "exited" || self || locked;
}
function renderContainers(ct) {
  const box = $("containers");
  if (!ct) return;
  if (ct.error) { box.replaceChildren(h("div", {class: "card err"}, ct.error)); cards.clear(); groups.clear(); return; }
  const seen = new Set(), byProj = new Map();
  for (const c of ct.list) { (byProj.get(c.project) || byProj.set(c.project, []).get(c.project)).push(c); }
  const order = [];
  for (const [proj, list] of byProj) {
    let g = groups.get(proj);
    if (!g) { g = {root: h("div", {}, h("div", {class: "proj"}, proj), h("div", {class: "cgrid"})), grid: null}; g.grid = g.root.lastChild; groups.set(proj, g); }
    const kids = [];
    for (const c of list) { let el = cards.get(c.name); if (!el) { el = buildCard(c); cards.set(c.name, el); } updateCard(el, c); seen.add(c.name); kids.push(el.root); }
    g.grid.replaceChildren(...kids); order.push(g.root);
  }
  for (const k of [...cards.keys()]) if (!seen.has(k)) cards.delete(k);
  for (const k of [...groups.keys()]) if (!byProj.has(k)) groups.delete(k);
  box.replaceChildren(...order);
}
async function doAction(name, act, confirm = false) {
  busy.add(name); refreshButtons();
  try {
    const r = await api(`/api/containers/${encodeURIComponent(name)}/${act}`, {method: "POST", body: JSON.stringify({confirm})});
    const j = await r.json().catch(() => ({}));
    if (r.status === 409 && j.needs_confirm) {
      busy.delete(name); refreshButtons();
      if (await dialog(`${act} ${name}?`, j.error + "\n\nYou may lose remote access or DNS until it is started again.", `Yes, ${act} ${name}`)) return doAction(name, act, true);
      return;
    }
    if (!r.ok) await dialog("Action failed", j.error || r.statusText);
  } catch (e) { if (e.message !== "login required") await dialog("Action failed", String(e)); }
  busy.delete(name); refreshButtons(); poll(true);
}
let lastState = null;
function refreshButtons() { if (lastState?.containers?.list) for (const c of lastState.containers.list) { const el = cards.get(c.name); if (el) updateCard(el, c); } }
async function showLogs(name) {
  $("dlgtitle").textContent = "Logs: " + name; $("dlgbody").textContent = "loading…"; $("dlgok").hidden = true;
  const d = $("dlg"); $("dlgcancel").onclick = () => d.close(); d.showModal();
  try { const r = await api(`/api/containers/${encodeURIComponent(name)}/logs`); $("dlgbody").textContent = r.ok ? (await r.text() || "(no output)") : "error " + r.status; const pre = $("dlgbody"); pre.scrollTop = pre.scrollHeight; } catch (e) { $("dlgbody").textContent = String(e); }
}

async function poll(once) {
  clearTimeout(timer);
  if (document.hidden && !once) { timer = setTimeout(poll, 3000); return; }
  try {
    const r = await api("/api/state");
    if (r.ok) {
      const s = lastState = await r.json();
      document.title = s.hostname; for (const e of document.querySelectorAll(".sitetitle")) e.textContent = s.hostname;
      renderAlerts(s.alerts); renderHost(s.host); renderRaid(s.raid); renderSmart(s.smart); renderContainers(s.containers); renderAudit(s.audit);
      $("updated").textContent = "updated " + new Date().toLocaleTimeString();
    }
  } catch (e) { if (e.message === "login required") return; $("updated").textContent = "connection lost, retrying…"; }
  timer = setTimeout(poll, 3000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("app").hidden) poll(true); });

fetch("/api/info").then(r => r.json()).then(j => { document.title = j.title; for (const e of document.querySelectorAll(".sitetitle")) e.textContent = j.title; }).catch(() => {});
(async () => { const r = await fetch("/api/session", {credentials: "same-origin"}); if (r.ok) { csrf = (await r.json()).csrf; showApp(); } else showLogin(); })();
