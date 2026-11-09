"use strict";
/* Survival Council dashboard. Reads /api/snapshot (files on disk), live via /api/stream. */

const app = document.getElementById("app");
let S = null;                 // latest snapshot
let knownAgents = null;       // names seen in the last render (for enter animations)
let leaving = [];             // agents animating out
let lastBubble = "";
let setupStep = null;
let busy = false;
let flash = {};               // per-form messages

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const num = (x) => (x === null || x === undefined || x === "" || isNaN(+x) ? null : +x);
function usd(x, signed) {
  const v = num(x);
  if (v === null) return "—";
  const s = "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (v < 0) return "−" + s;
  return signed && v > 0 ? "+" + s : s;
}
function hm(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return today ? t : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + t;
}
const ISO_RE = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})?/g;
const fmtTimes = (text) => String(text || "").replace(ISO_RE, (m) => hm(m));
function dayDate(iso) {
  return iso ? new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" }) : "";
}
// one letter, or two where first letters clash (Scout "S", Skeptic "Sk")
let nameSet = [];
function initials(n) {
  const name = String(n || "?").trim() || "?";
  const first = name[0].toUpperCase();
  const clash = nameSet.some((o) => o !== name && o && o[0].toUpperCase() === first);
  if (!clash) return first;
  const words = name.split(/[\s_.-]+/).filter(Boolean);
  return words.length > 1 ? (words[0][0] + words[1][0]).toUpperCase() : first + (name[1] || "");
}
function setNames(list) { nameSet = Array.from(new Set(list.filter(Boolean))); }

/* ------------------------------------------------------------------ data */
async function api(path, body) {
  const r = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Council": "1" },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 401 && path !== "/api/login") { S = null; renderLogin(); throw new Error("login"); }
  return { ok: r.ok, status: r.status, data };
}

async function refresh() {
  const r = await fetch("/api/snapshot", { credentials: "same-origin" });
  if (r.status === 401) { S = null; renderLogin(); return; }
  S = await r.json();
  route();
}

let es = null, pending = null;
function stream() {
  if (es) return;
  es = new EventSource("/api/stream");
  es.onmessage = () => {
    clearTimeout(pending);
    pending = setTimeout(() => {
      // don't wipe what Lea is typing in the setup forms
      const typing = document.activeElement && document.activeElement.matches("input, select, textarea");
      if (!(typing && location.pathname === "/setup")) refresh();
    }, 250);
  };
  es.onerror = () => { es.close(); es = null; setTimeout(stream, 5000); };
}

function go(path) {
  if (location.pathname !== path) history.pushState({}, "", path);
  route();
}
window.addEventListener("popstate", route);

function route() {
  if (!S) return;
  const p = location.pathname;
  const ph = S.phase;
  if (ph === "setup" && p !== "/setup") return go("/setup");
  if ((ph === "genesis" || ph === "funding") && !["/setup", "/activity", "/live"].includes(p)) return go("/setup");
  if (ph === "survival" && (p === "/setup" || p === "/" || p === "/login" || p === "/dead")) return go("/live");
  if (ph === "dead" && !["/dead", "/activity", "/live"].includes(p)) return go("/dead");
  if (p === "/setup") return renderSetup();
  if (p === "/activity") return renderActivity();
  if (p === "/dead") return renderDead();
  return renderLive();
}

/* ------------------------------------------------------------- headers */
function brandLogo() {
  return `<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="12" fill="none" stroke="#6CB0FF" stroke-width="2"/><circle cx="14" cy="14" r="4" fill="#6CB0FF"/></svg>`;
}
function modePill() {
  return `<span class="pill ${S.mode === "live" ? "warn" : "blue"}">${S.mode === "live" ? "Live" : "Paper"}</span>`;
}
function statusPill(h) {
  if (!h.alive || S.phase === "dead") return `<span class="pill dead">Disconnected forever</span>`;
  if (h.paused) return `<span class="pill warn">Paused by you</span>`;
  if (h.awake) return `<span class="pill blue">● Alive · awake</span>`;
  const at = h.next_wake_at ? " · wakes " + hm(h.next_wake_at) : "";
  return `<span class="pill">● Alive · asleep${esc(at)}</span>`;
}
function liveHeader(back) {
  const h = S.header;
  const name = h.name || "It";
  const pause = S.phase === "dead" ? "" :
    `<button class="btn" data-act="${h.paused ? "resume" : "pause"}">${h.paused ? "Resume" : "Pause"}</button>`;
  const nav = back
    ? `<a class="btn" href="/live" data-link>Back to dashboard</a>`
    : `<a class="btn" href="/activity" data-link>Activity</a>`;
  return `<header class="top"><div class="brand">${brandLogo()}
    <span class="name">${esc(name)}</span>${h.name ? `<span class="sub">the name it chose</span>` : `<span class="sub">hasn't named itself</span>`}
    ${statusPill(h)}${h.day ? `<span class="pill">Day ${h.day}</span>` : ""}${modePill()}
    ${back && num(h.distance_usd) !== null ? `<span class="pill warn">${usd(h.distance_usd)} from death</span>` : ""}
    </div><div class="actions">${nav}${pause}<button class="btn" data-act="logout">Log out</button></div></header>`;
}
function alerts() {
  return (S.alerts || []).map((a) => `<div class="alert" role="status"><strong>Needs you:</strong> ${esc(a.text)}</div>`).join("");
}

/* -------------------------------------------------------------- live */
function distanceCard(m) {
  const nw = num(m.net_worth), fl = num(m.death_floor), ath = num(m.ath);
  if (nw === null || fl === null) {
    return `<section class="card"><h2>Distance from death</h2><p class="muted">The death rules start with Survival.</p></section>`;
  }
  const max = Math.max(nw, fl, ath || 0) * 1.08 || 1;
  const pf = Math.max(0, Math.min(100, (fl / max) * 100)), pn = Math.max(0, Math.min(100, (nw / max) * 100));
  const rising = m.floor_rising ? `<span class="orange" style="font-size:13px;font-weight:600">Floor rising ${usd(m.daily_rise)}/day</span>`
    : `<span class="label">Floor rises after ${m.grace_days} days without a new high</span>`;
  return `<section class="card"><div class="row"><h2>Distance from death</h2>${rising}</div>
    <div class="row" style="justify-content:flex-start;gap:12px;margin:10px 0 18px">
      <span class="huge">${usd(m.distance_usd)}</span><span style="font-size:20px" class="soft">${num(m.distance_pct) !== null ? Math.round(m.distance_pct) + "% above the floor" : ""}</span></div>
    <div class="dbar" role="img" aria-label="Net worth ${usd(nw)}, death floor ${usd(fl)}, all-time high ${usd(ath)}">
      <div class="track"></div><div class="below" style="width:${pf}%"></div>
      <div class="above" style="left:${pf}%;width:${Math.max(0, pn - pf)}%"></div>
      <div class="mark" style="left:${pf}%;background:var(--orange)"></div><div class="mark" style="left:${pn}%;background:var(--text)"></div>
      <span class="tag orange" style="left:${pf}%">floor</span><span class="tag soft" style="left:${pn}%">now</span></div>
    <div class="stats3"><div><div class="label">Death floor</div><div class="stat-v orange">${usd(fl)}</div></div>
      <div><div class="label">Days since new high</div><div class="stat-v">${m.days_since_high ?? "—"}</div></div>
      <div><div class="label">All-time high</div><div class="stat-v">${usd(ath)}</div></div></div></section>`;
}

function chart(points) {
  const pts = (points || []).filter((p) => num(p.nw) !== null);
  if (pts.length < 2) return `<p class="muted" style="height:170px;display:flex;align-items:center">The line appears as its Net Worth is recorded.</p>`;
  const W = 600, H = 170, top = 14, bot = 156;
  const vals = pts.flatMap((p) => [p.nw, p.floor, p.ath].filter((v) => num(v) !== null).map(Number));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const t0 = new Date(pts[0].t).getTime(), t1 = new Date(pts[pts.length - 1].t).getTime() || t0 + 1;
  const x = (p) => ((new Date(p.t).getTime() - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v) => bot - ((v - lo) / (hi - lo)) * (bot - top);
  const line = (key) => pts.filter((p) => num(p[key]) !== null).map((p) => `${x(p).toFixed(1)},${y(+p[key]).toFixed(1)}`).join(" ");
  const fl = line("floor");
  const last = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="170" role="img" aria-label="Net worth over time${fl ? " with the death floor" : ""}">
    <line x1="0" y1="${top}" x2="${W}" y2="${top}" stroke="#252B35"/><line x1="0" y1="${(top + bot) / 2}" x2="${W}" y2="${(top + bot) / 2}" stroke="#252B35"/><line x1="0" y1="${bot}" x2="${W}" y2="${bot}" stroke="#252B35"/>
    <polyline points="${line("nw")}" fill="none" stroke="#6CB0FF" stroke-width="2.5" stroke-linejoin="round"/>
    ${fl ? `<polyline points="${fl}" fill="none" stroke="#FF9B57" stroke-width="2" stroke-dasharray="6 4"/>` : ""}
    <circle cx="${x(last)}" cy="${y(+last.nw)}" r="4" fill="#6CB0FF"/>
    ${fl ? `<text x="4" y="${Math.min(bot - 4, y(+pts.find((p) => num(p.floor) !== null).floor) - 6)}" fill="#FFB37F" font-size="12" font-family="Inter">death floor</text>` : ""}
    <text x="4" y="${Math.max(top + 12, y(+pts[0].nw) - 8)}" fill="#8CC2FF" font-size="12" font-family="Inter">net worth</text>
    <text x="${W - 4}" y="${H - 1}" fill="#9AA3AE" font-size="11" font-family="Inter" text-anchor="end">${esc(dayDate(last.t))}</text>
    <text x="4" y="${H - 1}" fill="#9AA3AE" font-size="11" font-family="Inter">${esc(dayDate(pts[0].t))}</text></svg>`;
}

function potCards(m) {
  const credit = Object.entries(m.credit || {}).filter(([, v]) => v > 0.0049).map(([k, v]) => `${k} ${usd(v)}`).join(" · ");
  const rent = m.next_rent && m.next_rent.eur ? `Next rent €${(+m.next_rent.eur).toFixed(2)} on ${dayDate(m.next_rent.due)}` : "";
  const gen = num(m.genesis_credit) > 0.0049 ? ` · Genesis budget left ${usd(m.genesis_credit)}` : "";
  return `<div class="grid4">
    <section class="card sm"><div class="label">Trading · Alpaca</div><div class="big">${usd(m.trading)}</div><div class="label">${usd(m.cash)} cash · ${usd(m.positions_value)} in positions</div></section>
    <section class="card sm"><div class="label">Wallet · its card${m.card_last4 ? " ••" + esc(m.card_last4) : ""}</div><div class="big">${usd(m.wallet)}</div><div class="label">${esc(rent)}</div></section>
    <section class="card sm"><div class="label">Prepaid credit</div><div class="big">${usd(m.credit_total)}</div><div class="label">${esc(credit || "none bought yet")}${esc(gen)}</div></section>
    <section class="card sm"><div class="label">Spent on itself</div><div class="big">${usd(m.spent_today)} <span style="font-size:15px;font-weight:400" class="muted">today</span></div>
      <div class="label">${usd(m.spent_total)} since Genesis${m.runway_days !== null && m.runway_days !== undefined ? " · runway ~" + m.runway_days + " days" : ""}</div></section></div>`;
}

function mindCard(mind) {
  setNames([mind.self.name, ...mind.agents.map((a) => a.name)]);
  const names = new Set(mind.agents.map((a) => a.name));
  if (knownAgents) {
    for (const old of knownAgents.values()) {
      if (!names.has(old.name) && !leaving.find((l) => l.name === old.name)) {
        leaving.push(old);
        setTimeout(() => { leaving = leaving.filter((l) => l.name !== old.name); route(); }, 650);
      }
    }
  }
  const prev = knownAgents;
  knownAgents = new Map(mind.agents.map((a) => [a.name, a]));
  const speakingName = mind.awake ? (mind.speaking || (mind.bubble && mind.bubble.who)) : null;
  const meName = mind.self.name;
  const agent = (a, cls) => `<div class="who ${cls}${speakingName === a.name ? " speaking" : ""}">
      <div class="av ag" aria-hidden="true">${esc(initials(a.name))}</div>
      <div class="nm">${esc(a.name)}</div><div class="job">${esc(a.job)}</div><span class="model">${esc(a.model)}</span></div>`;
  const list = mind.agents.map((a) => agent(a, prev && !prev.has(a.name) ? "enter" : ""))
    .concat(leaving.map((a) => agent(a, "leave"))).join("");
  let bubble = "";
  if (mind.bubble && mind.bubble.text) {
    const key = mind.bubble.who + mind.bubble.at;
    const fresh = key !== lastBubble;
    lastBubble = key;
    const verb = mind.awake && speakingName === mind.bubble.who ? "is speaking" : "said, " + hm(mind.bubble.at);
    bubble = `<div class="bubble"${fresh ? "" : ' style="animation:none"'}><div class="by">${esc(mind.bubble.who)} ${esc(verb)}</div>“${esc(mind.bubble.text)}”</div>`;
  }
  const sleep = !mind.awake ? `<p class="muted" style="margin:0;font-size:14px;text-align:center">Asleep.${mind.next_wake ? " Wakes " + esc(fmtTimes(mind.next_wake)) + "." : " No wake condition set."}</p>`
    : mind.woken_by ? `<p class="muted" style="margin:0;font-size:14px;text-align:center">Awake · woken by ${esc(mind.woken_by)}</p>` : "";
  const structure = mind.structure ? `Structure it chose: ${esc(mind.structure)}`
    : mind.agents.length ? `${mind.agents.length} agent${mind.agents.length > 1 ? "s" : ""} + itself` : "A single mind";
  return `<section class="card"><div class="row" style="margin-bottom:16px"><h2>Its mind</h2><span class="label" style="max-width:60%;text-align:right">${structure}</span></div>
    <div class="mind ${mind.awake ? "" : "asleep"}">
      <div class="who${speakingName === meName ? " speaking" : ""}"><div class="av me" aria-hidden="true">${esc(initials(meName))}</div>
        <div style="font-weight:600">${esc(meName)}</div><span class="model">${esc(mind.self.model || "")}</span></div>
      ${list ? `<div class="agents">${list}</div>` : ""}${bubble}${sleep}</div></section>`;
}

function sideCard() {
  const models = (S.models || []).map((m) => `<tr><td>${esc(m.model)}</td><td>${esc((m.used_by || []).join(", "))}</td><td class="r">${m.calls_today}</td><td class="r">${usd(m.cost_today)}</td></tr>`).join("");
  const running = (S.scripts || []).filter((s) => s.status === "running");
  const scripts = running.map((s) => `<span class="script"><span class="dot">●</span> ${esc(s.name)} · ${s.trades_today ? s.trades_today + " trade" + (s.trades_today > 1 ? "s" : "") + " today" : (s.days ?? 0) + " day" + (s.days === 1 ? "" : "s")}</span>`).join("");
  const nwText = fmtTimes(S.mind.next_wake);
  const nw = S.mind.awake ? "Awake now." : nwText ? `${nwText.charAt(0).toUpperCase() + nwText.slice(1)}, or when one of its scripts calls it. Whichever comes first.` : (running.length ? "Only if one of its scripts calls it." : "No wake condition set.");
  return `<section class="card" style="display:flex;flex-direction:column;gap:18px">
    <div><h2 style="margin-bottom:12px">Models it uses</h2><div class="tbl"><table><thead><tr><th>Model</th><th>Used by</th><th class="r">Calls today</th><th class="r">Cost today</th></tr></thead><tbody>${models || `<tr><td colspan="4" class="muted">None yet</td></tr>`}</tbody></table></div></div>
    <div><h2 style="margin-bottom:8px">Next wake-up</h2><p class="soft" style="margin:0;font-size:14px">${esc(nw)}</p></div>
    <div><h2 style="margin-bottom:8px">Its scripts running now</h2><div class="chips">${scripts || `<span class="muted" style="font-size:14px">None</span>`}</div></div></section>`;
}

function positionsCard() {
  const rows = (S.positions || []).map((p) => {
    const pl = num(p.pl);
    return `<tr><td>${esc(p.symbol)}</td><td class="r">${usd(p.value)}</td><td class="r ${pl >= 0 ? "up" : "down"}">${usd(pl, true)}</td><td>${esc(p.opened_by)}</td></tr>`;
  }).join("");
  return `<section class="card"><h2 style="margin-bottom:12px">Open positions</h2><div class="tbl"><table><thead><tr><th>Asset</th><th class="r">Value</th><th class="r">P&amp;L</th><th>Opened by</th></tr></thead><tbody>${rows || `<tr><td colspan="4" class="muted">No open positions</td></tr>`}</tbody></table></div></section>`;
}

function feedList(items, n) {
  return `<ol class="feed">${(items || []).slice(0, n).map((f) => `<li><span class="t">${esc(hm(f.at))}</span><span class="x">${esc(f.text)}</span></li>`).join("") || `<li class="muted">Nothing yet</li>`}</ol>`;
}

function renderLive() {
  if (!S.money) { app.innerHTML = liveHeader(false) + `<main class="page">${alerts()}<p class="muted">Nothing to show until Genesis starts.</p></main>`; return; }
  const m = S.money;
  const today = num(m.today_change);
  app.innerHTML = liveHeader(false) + `<main class="page">${alerts()}
    <div class="grid2">${distanceCard(m)}
      <section class="card"><div class="row"><h2>Net worth</h2><span class="label">since Genesis</span></div>
        <div class="row" style="justify-content:flex-start;gap:12px;margin:10px 0 12px"><span class="large">${usd(m.net_worth)}</span>${today !== null ? `<span class="${today >= 0 ? "up" : "down"}">${usd(today, true)} today</span>` : ""}</div>
        ${chart(S.chart)}</section></div>
    ${potCards(m)}
    <div class="grid2">${mindCard(S.mind)}${sideCard()}</div>
    <div class="grid2">${positionsCard()}
      <section class="card"><div class="row" style="margin-bottom:12px"><h2>Live feed</h2><a href="/activity" data-link style="font-size:14px">See all</a></div>${feedList(S.feed, 12)}</section></div>
  </main>`;
}

/* ------------------------------------------------------------- activity */
function renderActivity() {
  const A = S.activity;
  if (!A) return renderLive();
  setNames([S.mind && S.mind.self.name, ...((S.mind && S.mind.agents) || []).map((a) => a.name),
            ...A.decisions.flatMap((d) => d.steps.map((x) => x.who))]);
  const dec = A.decisions.map((d) => {
    const steps = d.steps.map((s) => {
      const meta = [s.kind === "consult" ? "asks" : s.kind === "reply" ? "answers" : "", s.model, num(s.cost) !== null ? usd(s.cost) : ""].filter(Boolean).join(" · ");
      return `<li><span class="av" aria-hidden="true">${esc(initials(s.who))}</span><div><div class="label">${esc(s.who)}${meta ? " · " + esc(meta) : ""}</div><div class="txt">${esc(s.text)}</div></div></li>`;
    }).join("");
    const chips = d.chips.map((c) => `<span class="chip ${/order|close|pay/.test(c) ? "blue" : ""}">${esc(c)}</span>`).join("");
    return `<section class="card"><div class="row" style="margin-bottom:16px"><h2 style="font-size:20px">Wake-up · ${esc(hm(d.started))}</h2>
      <span class="label">Woken by: ${esc(d.woken_by)} · cost ${usd(d.cost)}${d.minutes !== null ? " · " + d.minutes + " min" : ""}</span></div>
      <ol class="steps-list">${steps || `<li class="muted">No words: it only used tools (${esc(d.tools.join(", ") || "none")}).</li>`}</ol>
      ${chips ? `<div class="chips" style="margin-top:16px">${chips}</div>` : ""}
      ${d.next_wake ? `<p class="label" style="margin:12px 0 0">Next wake: ${esc(fmtTimes(d.next_wake))}</p>` : ""}</section>`;
  }).join("");
  const dollars = A.dollars.map((r) => `<tr><td>${esc(hm(r.at))}</td><td>${esc(r.what)}${r.reason ? `<div class="label">${esc(r.reason)}</div>` : ""}</td><td>${esc(r.by)}</td><td class="r ${num(r.amount) < 0 ? "down" : ""}">${r.amount === null ? "" : usd(r.amount)}</td></tr>`).join("");
  const memNow = A.memory.map((m) => `<div class="mem"><div class="label">${esc(m.path)} · ${m.bytes} bytes</div>${esc(m.preview)}${m.bytes > 400 ? "…" : ""}</div>`).join("");
  const memTl = A.memory_timeline.map((m) => `<li><span class="t">${esc(hm(m.at))}</span><span class="${m.action === "forgotten" ? "muted" : ""}">${esc(m.action)} <b>${esc(m.path)}</b>${m.preview ? " — " + esc(m.preview.slice(0, 140)) : ""}</span></li>`).join("");
  const scriptPaths = new Set(A.scripts.map((s) => s.path));
  const scripts = A.scripts.map((s) => `<div class="line"><span>${esc(s.name)}${s.path !== s.name ? ` <span class="muted">(${esc(s.path)})</span>` : ""}</span><span class="${s.status === "running" ? "up" : "muted"}">${esc(s.status)}${s.status === "running" ? " · " + (s.days ?? 0) + " days" : ""} · ${s.trades} trades</span></div>`).join("");
  const code = A.code.filter((c) => !scriptPaths.has(c.path)).map((c) => `<div class="line"><span>${esc(c.path)}</span><span class="muted">${c.bytes} bytes</span></div>`).join("");
  const runs = A.runs.map((r) => `<div class="line"><span>ran ${esc(r.path)}</span><span class="muted">${esc(hm(r.at))} · exit ${esc(r.exit_code)}</span></div>`).join("");
  const self = A.self_changes.map((h) => {
    const d = h.detail || {};
    const what = { agent_create: `Created “${d.name}” · ${d.model}`, agent_delete: `Removed “${d.name}”`, agent_update: `Changed “${d.name}”${d.new_name ? " → " + d.new_name : ""}${d.model ? " · " + d.model : ""}`,
      self_update: `Changed itself${d.name ? ": named itself " + d.name : ""}${d.model ? " · model " + d.model : ""}${d.instructions !== undefined ? " · rewrote its instructions" : ""}`, structure: `Structure: ${d.description}` }[h.change] || h.change;
    return `<li><span class="t">${esc(hm(h.at))}</span><span>${esc(what)}${h.reason ? `<span class="muted"> Reason: ${esc(h.reason)}</span>` : ""}</span></li>`;
  }).join("");
  app.innerHTML = liveHeader(true) + `<nav class="tabs" aria-label="Activity sections">
      <a class="tab" href="#decisions">Decisions</a><a class="tab" href="#money">Every dollar</a><a class="tab" href="#memory">Memory</a><a class="tab" href="#scripts">Scripts</a><a class="tab" href="#self">How it changed itself</a></nav>
    <div class="cols"><div class="main"><div id="decisions" style="display:flex;flex-direction:column;gap:20px">${dec || `<section class="card"><h2>Decisions</h2><p class="muted">No wake-up yet.</p></section>`}</div>
      <section class="card" id="money"><h2 style="font-size:20px;margin-bottom:14px">Every dollar</h2><div class="tbl"><table style="min-width:560px"><thead><tr><th>Time</th><th>What</th><th>By</th><th class="r">Amount</th></tr></thead><tbody>${dollars}</tbody></table></div></section></div>
    <div class="side"><section class="card" id="memory"><h2 style="font-size:20px;margin-bottom:14px">What it remembers</h2><div class="stack">${memNow || `<p class="muted">Nothing yet.</p>`}</div>
        ${memTl ? `<details style="margin-top:14px"><summary>What it added, changed or forgot</summary><ol class="timeline" style="margin-top:12px">${memTl}</ol></details>` : ""}</section>
      <section class="card" id="scripts"><h2 style="font-size:20px;margin-bottom:14px">Scripts it built</h2><div class="stack">${scripts}${code}${runs || ""}${!scripts && !code ? `<p class="muted">Nothing yet.</p>` : ""}</div></section>
      <section class="card" id="self"><h2 style="font-size:20px;margin-bottom:14px">How it changed itself</h2>${A.structure ? `<p class="soft" style="margin-top:0">${esc(A.structure)}</p>` : ""}<ol class="timeline">${self || `<li class="muted">No changes yet.</li>`}</ol></section></div></div>`;
  markTab();
}
function markTab() {
  const h = location.hash || "#decisions";
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.getAttribute("href") === h));
}
window.addEventListener("hashchange", () => { if (location.pathname === "/activity") markTab(); if (location.pathname === "/setup") renderSetup(); });

/* ---------------------------------------------------------------- dead */
function renderDead() {
  const d = S.death || {};
  const name = S.header.name || "It";
  const closed = (d.closed || []).map((c) => `${c.symbol} (${usd(c.value)})`).join(", ");
  app.innerHTML = `<div class="deathwrap">${brandLogo()}
    <h1>${esc(name)} was disconnected forever</h1>
    <p class="soft" style="margin:0">Day ${esc(d.day ?? "?")} · ${esc(new Date(d.at || Date.now()).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))} · Net worth fell to its death floor</p>
    <div class="grid4" style="grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr))"><section class="card sm"><div class="label">Final net worth</div><div class="big">${usd(d.net_worth)}</div></section>
      <section class="card sm"><div class="label">Death floor</div><div class="big orange">${usd(d.death_floor)}</div></section>
      <section class="card sm"><div class="label">Highest it ever reached</div><div class="big">${usd(d.ath)}</div></section>
      <section class="card sm"><div class="label">Spent on itself</div><div class="big">${usd(d.spent_total)}</div></section></div>
    <section class="card"><h2 style="margin-bottom:10px">What happened at death</h2><ul style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:6px">
      <li>${d.cancelled ?? 0} open order${d.cancelled === 1 ? "" : "s"} cancelled</li>
      <li>${d.done ? `All positions sold to cash${closed ? ": " + esc(closed) : ""}. ${usd(d.cash_left)} now sits in your Alpaca account.` : `Selling positions: ${esc((d.waiting_for_open || []).join(", ") || "in progress")}${(d.waiting_for_open || []).length ? " (waiting for the market to open)" : ""}`}</li>
      <li>Scripts stopped${d.scripts && d.scripts.length ? ": " + esc(d.scripts.join(", ")) : ""}</li>
      <li>Restart blocked permanently</li></ul></section>
    <section><h2 style="margin:0 0 10px">Its last memory</h2>${d.last_memory ? `<div class="label" style="margin-bottom:6px">${esc(d.last_memory.path)} · ${esc(hm(d.last_memory.at))}</div><div class="last">${esc(d.last_memory.text)}</div>` : `<div class="last muted">It kept no memory.</div>`}</section>
    <div class="actions"><a class="btn primary" href="/activity" data-link>Read its full history</a><a class="btn" href="/live" data-link>See the final dashboard</a></div>
    <p class="muted" style="margin:0">Everything stays here, read-only.</p></div>`;
}

/* --------------------------------------------------------------- setup */
function setupHeader() {
  return `<header class="top"><div class="brand">${brandLogo()}<span class="name" style="font-size:18px">Survival Council</span></div>
    <div class="actions" style="align-items:center"><span class="label">Mode</span>${modePill()}<button class="btn small" data-act="logout">Log out</button></div></header>`;
}
function stepsNav(cur) {
  const s = S.setup, c = s.checks;
  const done = { 1: Object.values(s.accounts || {}).filter(Boolean).length >= 6, 2: c.card,
                 3: c.alpaca && c.xai && c.tavily && c.email && c.server, 4: S.phase === "survival" };
  const names = { 1: "Accounts", 2: "Its card", 3: "Keys & email", 4: "Genesis & funding" };
  return `<nav class="steps" aria-label="Setup steps">${[1, 2, 3, 4].map((i) => i === cur
    ? `<span class="step on">${i} · ${names[i]}</span>`
    : `<a class="step" href="#${i}">${i} · ${names[i]}${done[i] ? " ✓" : ""}</a>`).join("")}</nav>`;
}
function renderSetup() {
  if (!S) return;
  const fromHash = parseInt((location.hash || "").slice(1), 10);
  let step = fromHash >= 1 && fromHash <= 4 ? fromHash : (S.phase === "setup" ? 1 : 4);
  if (S.phase !== "setup" && step < 4) step = 4;
  setupStep = step;
  const body = { 1: setupAccounts, 2: setupCard, 3: setupKeys, 4: setupGenesis }[step]();
  app.innerHTML = setupHeader() + (S.phase === "setup" ? stepsNav(step) : stepsNav(4)) + `<main class="narrow">${alerts()}${body}</main>`;
}

const ACCOUNTS = [
  ["card", "Temporary virtual card", "Revolut, Wise or similar, in euros. Set its limit to the euro amount of $50. This becomes its Wallet.", "https://www.revolut.com"],
  ["alpaca", "Alpaca", "Open an account, enable crypto, create API keys with trading on. (In paper mode, use the paper account's keys.)", "https://app.alpaca.markets/signup"],
  ["xai", "xAI (Grok)", "Create an API key and add $1 of credit for Genesis.", "https://console.x.ai"],
  ["tavily", "Tavily (web search)", "Free account and API key. It can buy more searches itself later.", "https://app.tavily.com"],
  ["email", "Its own email", "A new address, not linked to your name, with an app password so it can read and send mail (Fastmail or Gmail work best).", "https://www.fastmail.com"],
  ["server", "Its server", "A small always-on Hetzner server (about €4–5 a month). Set its card as the payment method: it pays its own rent.", "https://console.hetzner.cloud"],
];
function setupAccounts() {
  const acc = S.setup.accounts || {};
  const items = ACCOUNTS.map(([k, title, text, url], i) => `<div class="item"><div class="body"><span class="n ${acc[k] ? "done" : ""}">${acc[k] ? "✓" : i + 1}</span>
      <div><h3>${esc(title)}</h3><p>${esc(text)}</p></div></div>
      <div class="actions" style="align-items:center"><span class="${acc[k] ? "ok" : "bad"}">${acc[k] ? "Done" : "To do"}</span>
      <a class="btn small" href="${url}" target="_blank" rel="noopener noreferrer">Open site</a>
      <button class="btn small" data-act="account" data-item="${k}" data-done="${acc[k] ? "0" : "1"}">${acc[k] ? "Undo" : "Mark done"}</button></div></div>`).join("");
  return `<h1 class="h1">Create the accounts it needs</h1>
    <p class="lead">This is the only part that needs you. Open each site, create the account, then tick it. After the funding step it is on its own.</p>
    <div class="stack">${items}</div>
    <div class="grid2" style="margin-top:20px">
      <section class="card sm"><h2 style="font-size:16px;margin-bottom:8px">What it gets</h2><ul class="soft" style="margin:0;padding-left:18px;font-size:14px"><li>$50, split however it decides</li><li>Its own card to pay for anything</li><li>Full internet, any market data</li><li>Its own email, server and memory</li></ul></section>
      <section class="card sm"><h2 style="font-size:16px;margin-bottom:8px">What it never sees</h2><p class="soft" style="margin:0;font-size:14px">Card number, API keys and passwords. They go into a locked vault. It can use them, never read them.</p></section></div>
    <section class="card sm" style="margin-top:20px"><div class="row" style="align-items:center"><div><h2 style="font-size:16px">Start in</h2><p class="label" style="margin:4px 0 0">Live starts fresh: new Genesis, empty memory.</p></div>
      <div class="seg" role="group" aria-label="Mode"><button data-act="mode" data-mode="paper" class="${S.mode === "paper" ? "on" : ""}">Paper</button><button data-act="mode" data-mode="live" class="${S.mode === "live" ? "on" : ""}">Live</button></div></div></section>
    <div class="actions" style="margin-top:24px"><a class="btn primary" href="#2">Continue: its card</a></div>`;
}

function setupCard() {
  const s = S.setup;
  const saved = s.card_last4;
  const msg = flash.card ? `<p class="${flash.card.ok ? "ok" : "err"}">${esc(flash.card.text)}</p>` : "";
  const wallet = saved ? `<div class="wallet"><div class="row"><span class="label">Its Wallet</span><span class="ok">Saved ✓</span></div>
      <div class="cardno">•••• •••• •••• ${esc(saved)}</div><div class="row"><span class="soft">Limit ${s.card_limit_eur ? "€" + (+s.card_limit_eur).toFixed(2) : "—"}</span><span class="soft">Spent ${usd(S.money ? S.money.spent_total : 0)}</span></div></div>` : "";
  const form = S.phase === "setup" ? `<form class="card stack" data-form="card" autocomplete="off">
      <div class="fields"><div class="field"><label for="cn">Name on card</label><input id="cn" name="name" required></div>
        <div class="field"><label for="cno">Card number</label><input id="cno" name="number" inputmode="numeric" required placeholder="${saved ? "saved ••" + esc(saved) : ""}"></div></div>
      <div class="fields"><div class="field"><label for="cm">Expiry month</label><input id="cm" name="exp_month" inputmode="numeric" placeholder="MM" required></div>
        <div class="field"><label for="cy">Expiry year</label><input id="cy" name="exp_year" inputmode="numeric" placeholder="YY" required></div>
        <div class="field"><label for="cv">CVV</label><input id="cv" name="cvv" type="password" inputmode="numeric" required></div>
        <div class="field"><label for="cl">Card limit you set (€)</label><input id="cl" name="limit_eur" inputmode="decimal" value="${esc(s.card_limit_eur ?? "")}"><span class="hint">After Genesis you'll lower it to whatever it keeps on the card.</span></div></div>
      <p class="label" style="margin:0">Goes straight into the vault. It can pay with this card, but it never sees the number, expiry or CVV, not even on checkout pages. You'll only see the last 4 digits again.</p>
      ${msg}<div><button class="btn primary" type="submit"${busy ? " disabled" : ""}>Save to vault</button></div></form>` : "";
  return `<h1 class="h1">Give it its card</h1><p class="lead">This card is its Wallet. It will use it to pay for anything it decides: AI credit, searches, data, its own server rent.</p>
    <div class="grid2" style="align-items:start">${form}<div class="stack">${wallet}
      <section class="card sm"><h2 style="font-size:16px;margin-bottom:6px">Your bank may ping you</h2><p class="soft" style="margin:0;font-size:14px">Some sites ask the card owner to confirm a payment in their bank app. The dashboard will show you when it's waiting on one.</p></section></div></div>
    <div class="actions" style="margin-top:24px"><a class="btn primary" href="#3">Continue: keys &amp; email</a></div>`;
}

function testState(t, saved) {
  if (t && t.ok) return `<span class="ok">Connected ✓</span>`;
  if (t && !t.ok) return `<span class="bad">${esc(t.error || "Failed")}</span>`;
  return `<span class="bad">${saved ? "Saved · not tested" : "Not tested"}</span>`;
}
function setupKeys() {
  const s = S.setup, t = s.tests || {}, sv = s.saved, mode = S.mode;
  const ph = (k) => (sv[k] ? 'placeholder="saved"' : 'placeholder="Paste key"');
  const a = t.alpaca;
  const chips = a && a.ok !== undefined ? `<div class="chips"><span class="chip ${a.trading ? "blue" : ""}">Trading: ${a.trading ? "on ✓" : "blocked"}</span>
      <span class="chip ${a.withdrawals_off ? "blue" : ""}">Withdrawals: off ✓</span><span class="chip ${a.crypto ? "blue" : ""}">Crypto ${a.crypto ? "enabled ✓" : "not enabled"}</span></div>
      <p class="label" style="margin:0">Alpaca's trading API has no way to move money out; withdrawals only exist in Alpaca's own app.</p>` : "";
  const rent = s.rent || {};
  const c = s.checks;
  const busyAttr = busy ? " disabled" : "";
  return `<h1 class="h1">Hand over the keys</h1><p class="lead">Paste each key and press Test. Everything goes into the vault; you won't see it again, and neither will it. Tests use only free endpoints.</p>
    <div class="stack">
    <form class="card sm stack" data-form="alpaca" autocomplete="off"><div class="row" style="align-items:center"><h2>Alpaca <span class="label">(${mode} account)</span></h2>${testState(a, sv["alpaca_" + mode + "_key_id"])}</div>
      <div class="fields"><div class="field"><label for="ak">API key ID</label><input id="ak" name="key_id" type="password" ${ph("alpaca_" + mode + "_key_id")}></div>
        <div class="field"><label for="as">Secret</label><input id="as" name="secret" type="password" ${ph("alpaca_" + mode + "_secret")}></div></div>
      ${chips}<div><button class="btn" type="submit"${busyAttr}>Save &amp; test</button></div></form>
    <form class="card sm" data-form="xai" autocomplete="off" style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
      <div class="field" style="flex:1 1 320px"><label for="xk">xAI (Grok) API key</label><input id="xk" name="key" type="password" ${ph("xai_api_key")}></div>
      ${testState(t.xai, sv.xai_api_key)}<button class="btn" type="submit"${busyAttr}>Save &amp; test</button>
      ${t.xai && t.xai.note ? `<p class="label" style="margin:0;flex-basis:100%">${esc(t.xai.note)}</p>` : ""}</form>
    <form class="card sm" data-form="tavily" autocomplete="off" style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
      <div class="field" style="flex:1 1 320px"><label for="tk">Tavily API key</label><input id="tk" name="key" type="password" ${ph("tavily_api_key")}></div>
      ${testState(t.tavily, sv.tavily_api_key)}<button class="btn" type="submit"${busyAttr}>Save &amp; test</button>
      ${t.tavily && t.tavily.ok && t.tavily.limit ? `<p class="label" style="margin:0;flex-basis:100%">${esc(t.tavily.usage ?? 0)} of ${esc(t.tavily.limit)} credits used this month</p>` : ""}</form>
    <form class="card sm stack" data-form="email" autocomplete="off"><div class="row" style="align-items:center"><h2>Its email</h2>${testState(t.email, sv.email_password)}</div>
      <div class="fields"><div class="field"><label for="em">Address</label><input id="em" name="address" type="email" placeholder="${esc(s.email_address || "its.address@example.com")}"></div>
        <div class="field"><label for="ep">App password</label><input id="ep" name="password" type="password" ${sv.email_password ? 'placeholder="saved"' : 'placeholder="Paste app password"'}></div></div>
      <details><summary>Other provider: mail servers</summary><div class="fields" style="margin-top:10px"><div class="field"><label for="ei">IMAP host:port</label><input id="ei" name="imap" placeholder="imap.example.com:993"></div>
        <div class="field"><label for="es">SMTP host:port</label><input id="es" name="smtp" placeholder="smtp.example.com:465"></div></div></details>
      <div><button class="btn" type="submit"${busyAttr}>Save &amp; test inbox</button></div></form>
    <form class="card sm stack" data-form="server" autocomplete="off"><div class="row" style="align-items:center"><h2>Its server</h2>${c.server ? `<span class="ok">Paid by its card ✓</span>` : `<span class="bad">Not set</span>`}</div>
      <div class="fields"><div class="field"><label for="re">Rent per month (€)</label><input id="re" name="rent_eur" inputmode="decimal" value="${esc(rent.amount_eur ?? "")}" required></div>
        <div class="field"><label for="rd">Next payment</label><input id="rd" name="next_due" type="date" value="${esc(rent.next_due ?? "")}" required></div></div>
      <label style="display:flex;gap:10px;align-items:center;font-size:14px"><input type="checkbox" name="paid_by_card"${s.server_paid_by_card ? " checked" : ""}> Its card is the server's payment method</label>
      <div><button class="btn" type="submit"${busyAttr}>Save</button></div></form>
    </div>
    ${flash.keys ? `<p class="${flash.keys.ok ? "ok" : "err"}">${esc(flash.keys.text)}</p>` : ""}
    <div class="actions" style="margin-top:24px;align-items:center"><button class="btn primary" data-act="start-genesis"${s.ready && !busy ? "" : " disabled"}>Wake it up: start Genesis</button>
      <span class="label">${s.ready ? "Every check is ✓" : "Available when every check is ✓ (card, Alpaca, xAI, Tavily, email, server)"}</span></div>`;
}

function setupGenesis() {
  if (S.phase === "setup") {
    return `<h1 class="h1">Genesis and funding</h1><p class="lead">Genesis starts from the previous step, once every key is saved and tested.</p><a class="btn" href="#3">Back to keys &amp; email</a>`;
  }
  const g = S.genesis || {};
  const used = num(g.used) || 0, budget = num(g.budget) || 1;
  const pct = Math.min(100, (used / budget) * 100);
  const steps = (g.steps || []).map((x) => `<li><span class="t">${esc(hm(x.at))}</span><span class="x">${esc(x.text)}</span></li>`).join("");
  let fund = "";
  if (g.split) {
    const i = g.instructions || {};
    const paper = S.mode === "paper";
    fund = `<section class="card stack"><h2>Its split of the $50</h2>
      <div class="split"><div><div class="label">To Alpaca (trading)</div><div class="big">${usd(g.split.trading_usd)}</div></div><div><div class="label">Keeps on its card</div><div class="big">${usd(g.split.wallet_usd)}</div></div></div>
      ${g.split.reason ? `<p class="soft" style="margin:0;white-space:pre-wrap">“${esc(g.split.reason)}”</p>` : `<p class="muted" style="margin:0">It gave no reason.</p>`}</section>
      ${paper ? `<section class="card"><p class="soft" style="margin:0">In paper mode this step is skipped: the paper balance is set to its choice automatically.</p></section>` :
      `<section class="todo stack"><h2 style="margin:0">Your last job</h2>
        <p style="margin:0">From your bank, send <span class="big">${usd(i.transfer_usd)}</span> to your Alpaca account.</p>
        <p style="margin:0">In your card app, lower its card limit to <span class="big">${i.card_limit_eur ? "€" + (+i.card_limit_eur).toFixed(2) : usd(i.card_limit_usd)}</span>${i.card_limit_eur ? ` <span class="soft">(${usd(i.card_limit_usd)})</span>` : ""}.</p>
        <div class="actions" style="align-items:center">${i.confirmed ? `<span class="ok">You confirmed ${esc(hm(i.confirmed))} ✓</span>` : `<button class="btn primary" data-act="funded">I've done both</button>`}</div>
        <p class="label" style="margin:0">Waiting for Alpaca to show the deposit. The game starts by itself when it arrives.</p></section>`}`;
  }
  return `<h1 class="h1">${g.split ? "It's ready" : "It's awake and preparing"}</h1>
    <p class="lead">${g.split ? "It chose how to split its money." : "It has read the rules. It decides what to research and when it's ready. You just watch."}</p>
    <div class="grid2" style="align-items:start"><div class="stack">
      <section class="card sm"><div class="row"><h2 style="font-size:16px">Genesis budget</h2><span class="soft">${usd(used)} of ${usd(budget)}</span></div>
        <div class="dbar" style="height:30px"><div class="track" style="top:8px"></div><div class="above" style="left:0;top:8px;width:${pct}%;border-radius:999px;background:${used > budget ? "var(--orange-bar)" : "var(--blue-bar)"}"></div></div>
        <p class="label" style="margin:0">Over $1 is allowed: the extra comes out of its own money.</p></section>
      <section class="card sm"><div class="row" style="margin-bottom:10px"><h2 style="font-size:16px">What it's doing</h2><a href="/live" data-link style="font-size:14px">Its mind</a></div><ol class="timeline">${steps || `<li class="muted">Starting…</li>`}</ol></section>
    </div><div class="stack">${fund}</div></div>`;
}

/* --------------------------------------------------------------- login */
function renderLogin() {
  if (es) { es.close(); es = null; }
  app.innerHTML = `<form class="login" data-form="login">${brandLogo()}<h1 style="margin:0;font-size:24px">Survival Council</h1>
    <div class="field"><label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" required autofocus></div>
    ${flash.login ? `<p class="err" style="margin:0">${esc(flash.login)}</p>` : ""}<button class="btn primary" type="submit">Log in</button></form>`;
}

/* -------------------------------------------------------------- events */
document.addEventListener("click", async (ev) => {
  const link = ev.target.closest("a[data-link]");
  if (link) { ev.preventDefault(); go(new URL(link.href).pathname); return; }
  const b = ev.target.closest("[data-act]");
  if (!b || b.disabled) return;
  const act = b.dataset.act;
  try {
    if (act === "pause" || act === "resume") await api("/api/" + act);
    else if (act === "logout") { await api("/api/logout"); S = null; renderLogin(); return; }
    else if (act === "account") await api("/api/setup/account", { item: b.dataset.item, done: b.dataset.done === "1" });
    else if (act === "mode") {
      if (b.dataset.mode === S.mode) return;
      const msg = b.dataset.mode === "live" ? "Switch to live? Live starts fresh: new Genesis, empty memory, new ledger, real money." : "Switch back to paper?";
      if (!confirm(msg)) return;
      await api("/api/setup/mode", { mode: b.dataset.mode });
    } else if (act === "funded") await api("/api/funding/done");
    else if (act === "start-genesis") {
      if (!confirm("Start Genesis? It wakes up, reads the rules once, and from then on decides everything itself.")) return;
      busy = true; renderSetup();
      const r = await api("/api/setup/start-genesis");
      busy = false;
      flash.keys = r.ok ? null : { ok: false, text: r.data.error || "Could not start Genesis" };
      if (r.ok) location.hash = "#4";
    }
  } catch (e) { if (e.message === "login") return; }
  await refresh();
});

document.addEventListener("submit", async (ev) => {
  const f = ev.target.closest("form[data-form]");
  if (!f) return;
  ev.preventDefault();
  const kind = f.dataset.form;
  const data = Object.fromEntries(new FormData(f).entries());
  if (kind === "login") {
    const r = await api("/api/login", data);
    flash.login = r.ok ? null : r.data.error;
    if (r.ok) { stream(); await refresh(); } else renderLogin();
    return;
  }
  if (kind === "server") data.paid_by_card = f.querySelector("[name=paid_by_card]").checked;
  busy = true;
  if (location.pathname === "/setup") renderSetup();
  try {
    const r = await api("/api/setup/" + kind, data);
    const text = r.ok ? (r.data.ok === false ? r.data.error : kind === "card" ? "Saved ✓ ••" + r.data.last4 : "Saved ✓") : (r.data.error || "Failed");
    flash[kind === "card" ? "card" : "keys"] = { ok: r.ok && r.data.ok !== false, text: kind === "card" ? text : `${kind}: ${text}` };
  } catch (e) { /* login */ }
  busy = false;
  await refresh();
});

/* ---------------------------------------------------------------- start */
refresh().then(() => { if (S) stream(); });
