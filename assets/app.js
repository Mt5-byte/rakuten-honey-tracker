/* Rakuten vs Honey cash-back tracker — front end (no dependencies) */
(() => {
  "use strict";
  const $ = (sel, el = document) => el.querySelector(sel);
  const DAY = 86400;
  const SITE = { r: "Rakuten", h: "Honey" };
  const BAR_MAX = 15; // percent at which a bar fills its track; higher values are clipped with a notch

  const state = {
    meta: null, rows: [], byKey: new Map(),
    hist: null, histPromise: null, alerts: null,
    q: "", filter: "all", sort: "boost", view: "table", shown: 120,
    selected: null, range: "90d",
  };

  // ---------- utilities ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => (Math.round(n * 100) / 100).toLocaleString("en-US", { maximumFractionDigits: 2 });
  const pct = (n) => `${num(n)}%`;

  function rateText(rec, opts = {}) {
    if (!rec || rec.rate == null) return opts.none || "no reward";
    const upto = rec.up_to ? (opts.short ? "up to " : "Up to ") : "";
    if (rec.kind === "pct") return upto + pct(rec.rate);
    if (rec.kind === "usd") return upto + "$" + num(rec.rate);
    if (rec.kind === "pts") {
      const mn = rec.min != null && rec.min !== rec.rate ? `${num(rec.min)}–` : "";
      return `${mn}${num(rec.rate)} pts`;
    }
    return String(rec.rate);
  }
  function rateParts(rec) {
    // big number + small unit for the display figures
    if (!rec || rec.rate == null) return { big: "—", unit: "" };
    if (rec.kind === "pct") return { big: num(rec.rate), unit: "%" };
    if (rec.kind === "usd") return { big: "$" + num(rec.rate), unit: "" };
    if (rec.kind === "pts") return { big: num(rec.rate), unit: "pts" };
    return { big: String(rec.rate), unit: "" };
  }
  function baselineOf(rec, site) {
    if (!rec) return null;
    const b = site === "r" ? rec.was : rec.offer_prev;
    if (b != null && b > 0) return b;
    if (rec.typical != null && rec.typical > 0 && rec.typical < rec.rate) return rec.typical;
    return null;
  }
  function ratioOf(rec, site) {
    if (!rec || rec.rate == null || !rec.boosted) return 0;
    const b = baselineOf(rec, site);
    return b ? rec.rate / b : 1.01;
  }
  const fmtDate = (ts, withYear) => {
    const d = new Date(ts * 1000);
    const now = new Date();
    const opts = { month: "short", day: "numeric" };
    if (withYear || d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
    return d.toLocaleDateString("en-US", opts);
  };
  const fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  function fmtRel(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400 * 1.5) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }
  function nextCheck(meta) {
    const sched = meta.schedule_utc || [];
    if (!sched.length) return null;
    const now = new Date();
    let best = null;
    for (let dayOff = 0; dayOff <= 1; dayOff++) {
      for (const hm of sched) {
        const [h, m] = hm.split(":").map(Number);
        const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + dayOff, h, m));
        if (d > now && (!best || d < best)) best = d;
      }
    }
    return best;
  }
  const LOGO_PREFIX = "https://static.rakuten.com/img/store/";
  const storeUrl = (rec, site) => !rec ? null : site === "r" ? `https://www.rakuten.com/shop/${encodeURIComponent(rec.slug || "")}` : `https://www.joinhoney.com/shop/${encodeURIComponent(rec.label || "")}`;
  const siteUrl = (rec) => rec?.site ? `https://${rec.site}/` : null;
  const logoUrl = (row) => row.logo ? (/^https?:/.test(row.logo) ? row.logo : LOGO_PREFIX + row.logo) : null;
  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
  }
  function logoHtml(row, cls = "logo") {
    const src = logoUrl(row);
    if (src) return `<img class="${cls}" src="${esc(src)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'${cls} initial',textContent:'${esc(row.name[0] || "?")}'}))">`;
    return `<span class="${cls} initial" aria-hidden="true">${esc((row.name || "?")[0].toUpperCase())}</span>`;
  }

  // ---------- data ----------
  const bust = () => Math.floor(Date.now() / 300000);
  async function getJSON(path) {
    const res = await fetch(`${path}?v=${bust()}`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }
  function loadHistory() {
    if (!state.histPromise) {
      state.histPromise = Promise.all([getJSON("data/history/rakuten.json"), getJSON("data/history/honey.json")])
        .then(([r, h]) => { state.hist = { r: r.stores || {}, h: h.stores || {} }; return state.hist; })
        .catch((e) => { console.warn("history unavailable", e); state.hist = { r: {}, h: {} }; return state.hist; });
    }
    return state.histPromise;
  }

  // ---------- filtering & sorting ----------
  const normQ = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  function matches(row, q) {
    if (!q) return true;
    const hay = normQ(`${row.name} ${row.r?.slug || ""} ${row.h?.label || ""} ${row.h?.site || ""}`);
    return q.split(" ").every((t) => hay.includes(t));
  }
  function passesFilter(row, f) {
    const rr = row.r?.rate != null, hr = row.h?.rate != null;
    switch (f) {
      case "boosted": return !!(row.r?.boosted || row.h?.boosted);
      case "both": return rr && hr;
      case "rakuten": return rr;
      case "honey": return hr;
      default: return true;
    }
  }
  const rateSortVal = (rec) => (!rec || rec.rate == null) ? -1 : rec.kind === "pct" ? rec.rate + 1000 : rec.kind === "usd" ? rec.rate : rec.rate / 100;
  function sorter(kind) {
    const byName = (a, b) => a.name.localeCompare(b.name, "en", { sensitivity: "base" });
    switch (kind) {
      case "name": return byName;
      case "r": return (a, b) => rateSortVal(b.r) - rateSortVal(a.r) || byName(a, b);
      case "h": return (a, b) => rateSortVal(b.h) - rateSortVal(a.h) || byName(a, b);
      case "best": return (a, b) => (b.max_pct ?? -1) - (a.max_pct ?? -1) || byName(a, b);
      case "recent": return (a, b) => (Math.max(b.r?.changed_at || 0, b.h?.changed_at || 0) - Math.max(a.r?.changed_at || 0, a.h?.changed_at || 0)) || byName(a, b);
      default: return (a, b) => {
        const ra = Math.max(ratioOf(a.r, "r"), ratioOf(a.h, "h")), rb = Math.max(ratioOf(b.r, "r"), ratioOf(b.h, "h"));
        return (rb - ra) || ((b.max_pct ?? -1) - (a.max_pct ?? -1)) || byName(a, b);
      };
    }
  }
  function visibleRows() {
    const q = normQ(state.q);
    return state.rows.filter((r) => passesFilter(r, state.filter) && matches(r, q)).sort(sorter(state.sort));
  }

  // ---------- rendering: header & strip ----------
  function renderHeader() {
    const m = state.meta, c = m.counts || {};
    $("#last-check").innerHTML = `Last check <strong>${esc(fmtRel(m.ts))}</strong> <span class="visually-hidden">(${esc(fmtDateTime(m.ts))})</span>`;
    $("#last-check").title = fmtDateTime(m.ts);
    const nx = nextCheck(m);
    $("#next-check").innerHTML = nx ? `Next check about <strong>${esc(nx.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }))}</strong>` : "";
    $("#lede").innerHTML =
      `Every cash-back rate on both sites, checked twice a day. <b>${c.stores?.toLocaleString()}</b> stores: ` +
      `<b>${c.rakuten?.toLocaleString()}</b> on Rakuten, <b>${c.honey_active?.toLocaleString()}</b> paying rewards on Honey, ` +
      `<b>${c.paired?.toLocaleString()}</b> listed on both. Right now <b>${c.boosted_rakuten?.toLocaleString()}</b> Rakuten stores and ` +
      `<b>${c.boosted_honey?.toLocaleString()}</b> Honey stores are paying more than usual.`;
  }
  function renderStrip() {
    const cards = [];
    for (const row of state.rows) {
      for (const s of ["r", "h"]) {
        const rec = row[s];
        if (!rec?.boosted || rec.kind !== "pct") continue;
        cards.push({ row, s, rec, ratio: ratioOf(rec, s) });
      }
    }
    cards.sort((a, b) => (b.ratio - a.ratio) || (b.rec.rate - a.rec.rate));
    const top = cards.slice(0, 14);
    const sec = $("#boosts");
    if (!top.length) { sec.hidden = true; return; }
    sec.hidden = false;
    $("#boosts-count").textContent = `${cards.length.toLocaleString()} ${cards.length === 1 ? "store is" : "stores are"} above their usual rate`;
    $("#strip").innerHTML = top.map(({ row, s, rec, ratio }) => {
      const base = baselineOf(rec, s);
      const p = rateParts(rec);
      return `<button class="boost ${s}" data-key="${esc(row.key)}" type="button" aria-label="${esc(row.name)}: ${esc(SITE[s])} ${esc(rateText(rec))}${base ? `, usually ${esc(pct(base))}` : ""}">
        <span class="who">${logoHtml(row)}<span class="name">${esc(row.name)}</span></span>
        <span class="big">${rec.up_to ? '<small class="pre">up to</small>' : ""}${esc(p.big)}<small>${esc(p.unit)}</small></span>
        <span class="was">${base ? `was <s>${esc(pct(base))}</s> <b>${ratio >= 1.95 ? num(Math.round(ratio * 10) / 10) + "×" : "+" + num(rec.rate - base) + " pts"}</b>` : "new boost"}</span>
        <span class="site">${SITE[s]}</span>
      </button>`;
    }).join("");
  }

  // ---------- rendering: table ----------
  function rateCell(rec, site, isBest) {
    if (!rec) return `<div class="rate ${site} none" aria-label="${SITE[site]}: not listed"><span class="ctx"></span><span class="bar"></span><span class="val">not listed</span></div>`;
    if (rec.rate == null) return `<div class="rate ${site} none" aria-label="${SITE[site]}: no reward"><span class="ctx"></span><span class="bar"></span><span class="val">no reward</span></div>`;
    const isPct = rec.kind === "pct";
    const w = isPct ? Math.min(100, (rec.rate / BAR_MAX) * 100) : 0;
    const clip = isPct && rec.rate > BAR_MAX;
    const p = rateParts(rec);
    const base = baselineOf(rec, site);
    let ctx = "";
    if (rec.boosted) ctx += `<span class="tag">Boosted</span>`;
    if (rec.boosted && base) ctx += `<span class="was">was <s>${esc(pct(base))}</s></span>`;
    else if (rec.typical != null && isPct && Math.abs(rec.typical - rec.rate) > 0.01) ctx += `<span class="was">usually ${esc(pct(rec.typical))}</span>`;
    return `<div class="rate ${site}${isBest ? " win" : ""}${isPct ? "" : " nobar"}" aria-label="${SITE[site]}: ${esc(rateText(rec))}${rec.boosted && base ? `, boosted from ${esc(pct(base))}` : ""}${isBest ? ", pays more" : ""}">
      <span class="ctx">${ctx}</span>
      <span class="bar" aria-hidden="true">${isPct ? `<i class="${clip ? "clip" : ""}" style="width:${w}%"></i>` : ""}</span>
      <span class="val">${rec.up_to ? '<small class="pre">up to</small>' : ""}${esc(p.big)}${p.unit ? `<small>${esc(p.unit)}</small>` : ""}</span>
    </div>`;
  }
  function highCell(row) {
    const parts = [];
    for (const s of ["r", "h"]) {
      const rec = row[s];
      if (!rec || rec.kind !== "pct" || rec.high90 == null) continue;
      parts.push(`<div><span class="${s}">${SITE[s][0]}</span> <b>${esc(pct(rec.high90))}</b></div>`);
    }
    return parts.length ? parts.join("") : `<span style="color:var(--ink-3)">—</span>`;
  }
  function rowHtml(row) {
    const sub = row.h?.site ? row.h.site : (row.r && !row.h ? "Not on Honey" : (!row.r && row.h ? "Not on Rakuten" : ""));
    return `<button class="row" type="button" role="row" data-key="${esc(row.key)}" aria-selected="${state.selected === row.key}">
      <div class="store" role="cell">${logoHtml(row)}<div style="min-width:0"><div class="name">${esc(row.name)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div></div>
      <div role="cell">${rateCell(row.r, "r", row.best === "r")}</div>
      <div role="cell">${rateCell(row.h, "h", row.best === "h")}</div>
      <div class="high" role="cell">${highCell(row)}</div>
      <div class="spark" role="cell" data-key="${esc(row.key)}" aria-hidden="true">${state.hist ? sparkSvg(row) : ""}</div>
    </button>`;
  }
  function renderTable() {
    const rows = visibleRows();
    state.visible = rows;
    const n = rows.length;
    $("#count").textContent = `${n.toLocaleString()} ${n === 1 ? "store" : "stores"}`;
    const body = $("#tbody");
    if (!n) {
      body.innerHTML = `<div class="empty"><b>No stores match.</b> Try a shorter search, or clear the filter.</div>`;
      $("#more").hidden = true;
      return;
    }
    const slice = rows.slice(0, state.shown);
    body.innerHTML = slice.map(rowHtml).join("");
    $("#more").hidden = n <= state.shown;
    $("#more").textContent = `Show ${Math.min(200, n - state.shown).toLocaleString()} more of ${n.toLocaleString()}`;
    for (const b of document.querySelectorAll(".thead .sortable")) {
      const k = b.dataset.sort;
      if (k === state.sort) b.setAttribute("aria-sort", "descending"); else b.removeAttribute("aria-sort");
    }
  }
  function appendMore() {
    const rows = state.visible || [];
    const from = state.shown;
    state.shown += 200;
    const slice = rows.slice(from, state.shown);
    $("#tbody").insertAdjacentHTML("beforeend", slice.map(rowHtml).join(""));
    $("#more").hidden = rows.length <= state.shown;
    $("#more").textContent = `Show ${Math.min(200, rows.length - state.shown).toLocaleString()} more of ${rows.length.toLocaleString()}`;
  }

  // ---------- history helpers ----------
  function seriesFor(row, site) {
    const rec = row[site];
    if (!rec || !state.hist) return [];
    return state.hist[site][rec.id] || [];
  }
  // value of a step series at time t: last entry with ts <= t
  function valueAt(entries, t) {
    let v = null;
    for (const e of entries) { if (e[0] <= t) v = e; else break; }
    return v;
  }
  function stepPath(entries, t0, t1, x, y) {
    // returns SVG path string for the pct values between t0..t1 (gaps where rate is null / not pct)
    let d = "", pen = false;
    const start = valueAt(entries, t0);
    const pts = [];
    if (start) pts.push([t0, start]);
    for (const e of entries) if (e[0] > t0 && e[0] <= t1) pts.push([e[0], e]);
    pts.push([t1, null]);
    let cur = start && start[1] != null && start[2] === "pct" ? start[1] : null;
    for (let i = 0; i < pts.length; i++) {
      const [t, e] = pts[i];
      if (i > 0) {
        // horizontal segment from previous time to t at `cur`
        if (cur != null) { d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(cur).toFixed(1)} `; pen = true; }
      }
      if (e) {
        const nv = e[1] != null && e[2] === "pct" ? e[1] : null;
        if (nv != null) { d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(nv).toFixed(1)} `; pen = true; }
        else pen = false;
        cur = nv;
      }
    }
    return d.trim();
  }
  function sparkSvg(row) {
    const now = state.meta.ts, t0 = now - 90 * DAY;
    const sr = seriesFor(row, "r"), sh = seriesFor(row, "h");
    const vals = [];
    for (const s of [sr, sh]) for (const e of s) if (e[1] != null && e[2] === "pct" && e[0] <= now) vals.push(e[1]);
    if (!vals.length) return "";
    const max = Math.max(1, ...vals) * 1.1;
    const W = 100, H = 30, pad = 2;
    const x = (t) => pad + ((t - t0) / (now - t0)) * (W - 2 * pad);
    const y = (v) => H - pad - (v / max) * (H - 2 * pad);
    const pr = stepPath(sr, t0, now, x, y), ph = stepPath(sh, t0, now, x, y);
    if (!pr && !ph) return "";
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${ph ? `<path class="h" d="${ph}"/>` : ""}${pr ? `<path class="r" d="${pr}"/>` : ""}</svg>`;
  }
  function fillSparklines() {
    for (const el of document.querySelectorAll(".spark[data-key]")) {
      if (el.firstChild) continue;
      const row = state.byKey.get(el.dataset.key);
      if (row) el.innerHTML = sparkSvg(row);
    }
  }

  // ---------- drawer ----------
  function openStore(key, push = true) {
    const row = state.byKey.get(key);
    if (!row) return;
    state.selected = key;
    document.querySelectorAll(".row[aria-selected='true']").forEach((el) => el.setAttribute("aria-selected", "false"));
    const el = document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
    if (el) el.setAttribute("aria-selected", "true");
    renderDrawer(row);
    document.body.classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    $("#drawer .close").focus({ preventScroll: true });
    if (push) writeHash();
    loadHistory().then(() => { if (state.selected === key) renderDrawerHistory(row); });
  }
  function closeDrawer() {
    if (!document.body.classList.contains("open")) return;
    document.body.classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    const key = state.selected;
    state.selected = null;
    writeHash();
    const el = key && document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
    if (el) { el.setAttribute("aria-selected", "false"); el.focus({ preventScroll: true }); }
  }
  function nowCard(row, s) {
    const rec = row[s];
    const label = SITE[s];
    if (!rec) return `<div class="card ${s}"><div class="lbl">${label}</div><div class="big" style="color:var(--ink-3)">—</div><div class="meta">Not listed on ${label}.</div></div>`;
    const p = rateParts(rec);
    const base = baselineOf(rec, s);
    const meta = [];
    if (rec.rate == null) meta.push(`No reward at the moment.`);
    else {
      if (s === "h") meta.push(rec.kind === "pts" ? "Paid as PayPal Rewards points" : `${esc(rateText(rec))} back as PayPal Rewards points`);
      else if (rec.up_to) meta.push("The rate depends on the category you shop");
      if (rec.boosted && base) meta.push(`<span class="boost">Boosted</span> from the usual <b>${esc(pct(base))}</b>`);
      else if (rec.typical != null && rec.kind === "pct" && Math.abs(rec.typical - rec.rate) > 0.01) meta.push(`Usually <b>${esc(pct(rec.typical))}</b>`);
      if (rec.kind === "pts") meta.push(`100 points is about $1`);
    }
    if (rec.changed_at) meta.push(`This rate since <b>${esc(fmtDate(rec.changed_at, true))}</b>`);
    if (rec.kind === "pct" && rec.high90 != null) meta.push(`90-day high <b>${esc(pct(rec.high90))}</b>${rec.high90_at ? ` (${esc(fmtDate(rec.high90_at))})` : ""}`);
    if (rec.kind === "pct" && rec.highAll != null && rec.first_seen && (state.meta.ts - rec.first_seen) > 100 * DAY) meta.push(`Highest seen <b>${esc(pct(rec.highAll))}</b>${rec.highAll_at ? ` (${esc(fmtDate(rec.highAll_at, true))})` : ""}`);
    if (s === "h" && rec.coupons) meta.push(`${rec.coupons} coupon code${rec.coupons === 1 ? "" : "s"} listed`);
    return `<div class="card ${s}"><div class="lbl">${label}</div>
      <div class="big"${rec.rate == null ? ' style="color:var(--ink-3)"' : ""}>${rec.up_to ? '<small class="pre">up to</small>' : ""}${esc(p.big)}${p.unit ? `<small>${esc(p.unit)}</small>` : ""}</div>
      <div class="meta">${meta.join("<br>")}</div></div>`;
  }
  function renderDrawer(row) {
    const d = $("#drawer-body");
    const links = [];
    if (row.r) links.push(`<a class="r" href="${esc(storeUrl(row.r, "r"))}" target="_blank" rel="noopener">Open on Rakuten</a>`);
    if (row.h) links.push(`<a class="h" href="${esc(storeUrl(row.h, "h"))}" target="_blank" rel="noopener">Open on Honey</a>`);
    if (row.h?.site) links.push(`<a href="${esc(siteUrl(row.h))}" target="_blank" rel="noopener">${esc(row.h.site)}</a>`);
    d.innerHTML = `
      <div class="d-head">${logoHtml(row)}<h2>${esc(row.name)}</h2></div>
      <div class="d-links">${links.join("")}</div>
      <div class="now">${nowCard(row, "r")}${nowCard(row, "h")}</div>
      <div class="chart-head"><h3>Rate history</h3>
        <div class="seg" role="group" aria-label="Time range">
          ${["90d", "1y", "all"].map((r) => `<button type="button" data-range="${r}" aria-pressed="${state.range === r}">${r === "90d" ? "90 days" : r === "1y" ? "1 year" : "All"}</button>`).join("")}
        </div></div>
      <div class="legend"><span class="r"><i></i>Rakuten</span><span class="h"><i></i>Honey</span></div>
      <div class="chart" id="chart"><div class="loading" style="padding:40px 0">Loading history…</div></div>
      <div class="chart-note" id="chart-note"></div>
      <div class="changes" id="changes"></div>`;
  }
  function renderDrawerHistory(row) {
    const sr = seriesFor(row, "r"), sh = seriesFor(row, "h");
    const now = state.meta.ts;
    const firstSeen = Math.min(...[sr[0]?.[0], sh[0]?.[0]].filter((v) => v != null), now);
    const spanDays = (now - firstSeen) / DAY;
    const t0 = state.range === "90d" ? now - 90 * DAY : state.range === "1y" ? now - 365 * DAY : Math.min(firstSeen, now - 30 * DAY);
    const chart = $("#chart");
    const vals = [];
    for (const s of [sr, sh]) {
      const start = valueAt(s, t0);
      if (start && start[1] != null && start[2] === "pct") vals.push(start[1]);
      for (const e of s) if (e[0] > t0 && e[0] <= now && e[1] != null && e[2] === "pct") vals.push(e[1]);
    }
    if (!vals.length) {
      chart.innerHTML = `<div class="empty" style="padding:30px 0">No percentage history to chart for this range.</div>`;
    } else {
      const W = 520, H = 220, L = 36, R = 64, T = 12, B = 26;
      const { max, ticks } = niceScale(Math.max(...vals));
      const x = (t) => L + ((t - t0) / (now - t0)) * (W - L - R);
      const y = (v) => T + (1 - v / max) * (H - T - B);
      const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>`).join("");
      const ylab = ticks.map((v) => `<text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${esc(num(v))}%</text>`).join("");
      const xt = timeTicks(t0, now, 4).map((t) => `<text x="${x(t).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(fmtDate(t, (now - t0) > 300 * DAY))}</text>`).join("");
      const paths = [];
      const ends = [];
      for (const [s, ser] of [["h", sh], ["r", sr]]) {
        const p = stepPath(ser, t0, now, x, y);
        if (p) paths.push(`<path class="series ${s}" d="${p}"/>`);
        const last = valueAt(ser, now);
        if (last && last[1] != null && last[2] === "pct") {
          ends.push({ s, y: y(last[1]), text: `${SITE[s]} ${pct(last[1])}` });
          paths.push(`<circle class="series-end ${s}" cx="${x(now).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="3.5"/>`);
        }
      }
      // keep end labels from overlapping
      ends.sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
      const endLabels = ends.map((e) => `<text class="end-label ${e.s}" x="${W - R + 8}" y="${(e.y + 4).toFixed(1)}">${esc(e.text)}</text>`).join("");
      chart.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Rate history chart">
        <g class="grid">${grid}</g><g class="axis">${ylab}${xt}</g>${paths.join("")}${endLabels}
        <g class="hover" style="display:none"><line class="cross" y1="${T}" y2="${H - B}"/><circle class="dot r" r="4" fill="var(--rakuten)"/><circle class="dot h" r="4" fill="var(--honey)"/></g>
      </svg><div class="tip"></div>`;
      attachHover(chart, { t0, now, x, y, sr, sh, W, L, R });
    }
    $("#chart-note").textContent = spanDays < 2
      ? "History starts with this tracker's first check; Honey's own offer dates fill in a little earlier where they're known."
      : `Tracking since ${fmtDate(firstSeen, true)}. Each step is a rate change seen at a check.`;
    renderChanges(row, sr, sh);
  }
  function niceScale(v) {
    // a tick step from a short list of "nice" numbers, 3–5 ticks, max rounded up to a tick
    const steps = [0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100];
    const target = Math.max(v, 0.5) * 1.1;
    let step = steps.find((st) => target / st <= 4.5) || 100;
    const max = Math.ceil(target / step) * step;
    const ticks = [];
    for (let t = 0; t <= max + 1e-9; t += step) ticks.push(+t.toFixed(4));
    return { max, ticks };
  }
  function timeTicks(t0, t1, n) {
    const out = [];
    for (let i = 0; i < n; i++) out.push(Math.round(t0 + ((i + 0.5) / n) * (t1 - t0)));
    return out;
  }
  function attachHover(chart, ctx) {
    const svg = chart.querySelector("svg"), tip = chart.querySelector(".tip"), hover = svg.querySelector(".hover");
    const cross = hover.querySelector(".cross"), dr = hover.querySelector(".dot.r"), dh = hover.querySelector(".dot.h");
    const show = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const px = ((clientX - rect.left) / rect.width) * ctx.W;
      if (px < ctx.L || px > ctx.W - ctx.R) { hide(); return; }
      const t = ctx.t0 + ((px - ctx.L) / (ctx.W - ctx.L - ctx.R)) * (ctx.now - ctx.t0);
      const vr = valueAt(ctx.sr, t), vh = valueAt(ctx.sh, t);
      const lines = [`<b>${esc(fmtDate(t, true))}</b>`];
      for (const [s, v] of [["r", vr], ["h", vh]]) {
        const rec = v && v[1] != null ? { rate: v[1], kind: v[2], up_to: !!(v[3] && v[3].u), min: v[3] && v[3].m } : null;
        lines.push(`${SITE[s]}: ${esc(rec ? rateText(rec) : "no reward")}`);
      }
      tip.innerHTML = lines.join("<br>");
      tip.style.display = "block";
      const xpx = ctx.x(t);
      tip.style.left = `${(xpx / ctx.W) * rect.width}px`;
      tip.style.top = `${Math.max(0, (ctx.y(Math.max(vr && vr[2] === "pct" ? vr[1] : 0, vh && vh[2] === "pct" ? vh[1] : 0)) / 220) * rect.height - 10)}px`;
      hover.style.display = "";
      cross.setAttribute("x1", xpx); cross.setAttribute("x2", xpx);
      for (const [dot, v] of [[dr, vr], [dh, vh]]) {
        if (v && v[1] != null && v[2] === "pct") { dot.style.display = ""; dot.setAttribute("cx", xpx); dot.setAttribute("cy", ctx.y(v[1])); }
        else dot.style.display = "none";
      }
    };
    const hide = () => { tip.style.display = "none"; hover.style.display = "none"; };
    svg.addEventListener("mousemove", (e) => show(e.clientX));
    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("touchstart", (e) => show(e.touches[0].clientX), { passive: true });
    svg.addEventListener("touchmove", (e) => show(e.touches[0].clientX), { passive: true });
    svg.addEventListener("touchend", hide);
  }
  function renderChanges(row, sr, sh) {
    const items = [];
    for (const [s, ser] of [["r", sr], ["h", sh]]) {
      for (let i = 0; i < ser.length; i++) {
        const e = ser[i], prev = ser[i - 1];
        const to = { rate: e[1], kind: e[2], up_to: !!(e[3] && e[3].u), min: e[3] && e[3].m };
        const from = prev ? { rate: prev[1], kind: prev[2], up_to: !!(prev[3] && prev[3].u), min: prev[3] && prev[3].m } : null;
        items.push({ ts: e[0], s, from, to, first: !prev });
      }
    }
    items.sort((a, b) => b.ts - a.ts);
    const el = $("#changes");
    if (!items.length) { el.innerHTML = ""; return; }
    const rowsHtml = (list) => list.map((it) => `<tr><td>${esc(fmtDate(it.ts, true))}</td><td class="site ${it.s}">${SITE[it.s]}</td>
      <td>${it.first ? `first seen at <b>${esc(rateText(it.to))}</b>` : `${esc(rateText(it.from))} → <b>${esc(rateText(it.to))}</b>`}</td></tr>`).join("");
    const LIMIT = 10;
    el.innerHTML = `<h3>Changes</h3><table>${rowsHtml(items.slice(0, LIMIT))}</table>` +
      (items.length > LIMIT ? `<button type="button" class="more-changes">Show all ${items.length} changes</button>` : "");
    const btn = el.querySelector(".more-changes");
    if (btn) btn.addEventListener("click", () => { el.querySelector("table").innerHTML = rowsHtml(items); btn.remove(); });
  }

  // ---------- alerts view ----------
  async function renderAlerts() {
    const el = $("#alerts");
    el.innerHTML = `<div class="loading">Loading spikes…</div>`;
    try {
      if (!state.alerts) state.alerts = (await getJSON("data/alerts.json")).alerts || [];
    } catch { state.alerts = []; }
    const groups = new Map();
    for (const a of state.alerts) { if (!groups.has(a.ts)) groups.set(a.ts, []); groups.get(a.ts).push(a); }
    const rss = `<p><a class="rss" href="data/feed.xml"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="19" r="2.5"/><path d="M3 10a11 11 0 0 1 11 11h-3a8 8 0 0 0-8-8zm0-6a17 17 0 0 1 17 17h-3A14 14 0 0 0 3 7z"/></svg>Subscribe to spikes (RSS)</a></p>`;
    if (!groups.size) {
      el.innerHTML = `<div class="empty"><b>No spikes recorded yet.</b> A spike is a rate that jumped since the previous check: crossed 10%, up 5 points or more, at least double a store's usual rate, or a new all-time high. The first comparison happens on the second check.</div>${rss}`;
      return;
    }
    el.innerHTML = rss + [...groups.entries()].map(([ts, list]) => `
      <section class="run"><h3>${esc(fmtDateTime(ts))} — ${list.length} spike${list.length === 1 ? "" : "s"}</h3>
      ${list.map((a) => `<button type="button" class="alert ${a.site}" data-key="${esc(a.key)}">
        <span class="site">${esc(a.site_name)}</span>
        <span class="name"><b>${esc(a.name)}</b></span>
        <span class="chg"><s>${esc(a.from_text)}</s>${esc(a.to_text)}</span>
        <span class="why">${esc(a.reasons.join("; "))}</span>
      </button>`).join("")}</section>`).join("");
  }

  // ---------- views & hash ----------
  function setView(v) {
    state.view = v;
    $("#view-table").hidden = v !== "table";
    $("#view-alerts").hidden = v !== "alerts";
    $("#sort").closest("label").hidden = v !== "table";
    $("#count").hidden = v !== "table";
    for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === v));
    if (v === "alerts") renderAlerts();
    writeHash();
  }
  function writeHash() {
    const p = new URLSearchParams();
    if (state.view !== "table") p.set("view", state.view);
    if (state.q) p.set("q", state.q);
    if (state.filter !== "all") p.set("f", state.filter);
    if (state.sort !== "boost") p.set("sort", state.sort);
    if (state.selected) p.set("store", state.selected);
    const h = p.toString();
    history.replaceState(null, "", h ? `#${h}` : location.pathname + location.search);
  }
  function readHash() {
    const p = new URLSearchParams(location.hash.replace(/^#/, ""));
    state.q = p.get("q") || "";
    state.filter = p.get("f") || "all";
    state.sort = p.get("sort") || "boost";
    state.view = p.get("view") === "alerts" ? "alerts" : "table";
    return p.get("store");
  }

  // ---------- theme ----------
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("theme"); } catch {}
    if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
    $("#theme").addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch {}
    });
  }

  // ---------- wiring ----------
  function wire() {
    const input = $("#q");
    input.value = state.q;
    let timer;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = input.value.trim(); state.shown = 120; renderTable(); writeHash(); }, 120); });
    for (const b of document.querySelectorAll("#filters button")) {
      b.setAttribute("aria-pressed", String(b.dataset.filter === state.filter));
      b.addEventListener("click", () => {
        state.filter = b.dataset.filter; state.shown = 120;
        document.querySelectorAll("#filters button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        if (state.view !== "table") setView("table"); else writeHash();
        renderTable();
      });
    }
    for (const b of document.querySelectorAll("[data-view]")) b.addEventListener("click", () => setView(b.dataset.view));
    const sel = $("#sort");
    sel.value = state.sort;
    sel.addEventListener("change", () => { state.sort = sel.value; state.shown = 120; renderTable(); writeHash(); });
    for (const b of document.querySelectorAll(".thead .sortable")) b.addEventListener("click", () => { state.sort = b.dataset.sort; sel.value = state.sort; state.shown = 120; renderTable(); writeHash(); });
    $("#more").addEventListener("click", appendMore);
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-key]");
      if (t && !t.classList.contains("spark")) openStore(t.dataset.key);
      const r = e.target.closest("[data-range]");
      if (r) { state.range = r.dataset.range; document.querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", String(x === r))); const row = state.byKey.get(state.selected); if (row) renderDrawerHistory(row); }
    });
    $("#scrim").addEventListener("click", closeDrawer);
    $("#drawer .close").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); if (e.key === "/" && document.activeElement !== input && !document.body.classList.contains("open")) { e.preventDefault(); input.focus(); } });
    window.addEventListener("hashchange", () => { const key = readHash(); if (key && key !== state.selected) openStore(key, false); });
  }

  async function main() {
    initTheme();
    const storeFromHash = readHash();
    try {
      const data = await getJSON("data/latest.json");
      state.meta = data.meta;
      state.rows = data.stores || [];
      state.byKey = new Map(state.rows.map((r) => [r.key, r]));
    } catch (e) {
      $("#app").innerHTML = `<div class="error"><b>The tracker data could not be loaded.</b><br>${esc(e.message)}. If this site was just set up, the first check may still be running — try again in a few minutes.</div>`;
      return;
    }
    $("#app").hidden = false;
    $("#boot").remove();
    renderHeader();
    renderStrip();
    wire();
    renderTable();
    setView(state.view);
    if (storeFromHash) openStore(storeFromHash, false);
    const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 300));
    idle(() => loadHistory().then(fillSparklines));
  }
  main();
})();
