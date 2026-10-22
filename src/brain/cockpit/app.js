/* Second Brain cockpit — vanilla JS, no dependencies, no build step.
 *
 * Data sources (same origin, localhost only):
 *   GET  /ops/health            cheap checks (5s cache server-side)
 *   GET  /ops/flow              nodes/edges + live status
 *   GET  /ops/events?...        event log
 *   GET  /ops/stats             24h/7d stats
 *   POST /ops/probe/{c}         on-demand probe (button only)
 *   POST /ops/trace/capture     dry-run (no save)
 *   GET  /ops/trace/search?q=
 *   POST /ops/trace/ask         real LLM (button only)
 *   GET  /ops/item/{id}         inspector (shows item text — user-initiated)
 *
 * Secrets never enter the page: the API reports "set"/"unset" + last-4 only.
 * All dynamic values are rendered via textContent — never innerHTML.
 */
"use strict";

const REFRESH_MS = 5000;
const state = {
  tab: "flow",
  paused: false,
  countdown: 5,
  health: null,
  flow: null,
  events: { events: [], total: 0, offset: 0, limit: 25 },
  stats: null,
  eventFilters: { component: "", errorsOnly: false },
};

// ---------- tiny DOM helpers ----------

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child);
  }
  return node;
}

const $ = (id) => document.getElementById(id);

function jsonPreview(value, max = 220) {
  try {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > max ? text.slice(0, max) + "…" : text;
  } catch {
    return String(value);
  }
}

function shortId(id) {
  return typeof id === "string" && id.length > 8 ? id.slice(0, 8) : String(id ?? "");
}

function ago(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds.toFixed(0)}s ago`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(0)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

let toastTimer = null;
function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4000);
}

// ---------- api ----------

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* keep */ }
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json();
}

// ---------- data refresh ----------

async function refreshAll() {
  const jobs = [
    api("/ops/health").then((health) => {
      state.health = health;
      renderOverall();
      if (state.tab === "health") renderHealth();
    }).catch(() => {}),
    api("/ops/flow").then((flow) => {
      state.flow = flow;
      if (state.tab === "flow") renderFlow();
    }).catch(() => console.debug("flow refresh failed")),
  ];
  if (state.tab === "events") jobs.push(refreshEvents().catch(() => {}));
  if (state.tab === "stats") {
    jobs.push(api("/ops/stats").then((stats) => {
      state.stats = stats;
      renderStats();
    }).catch(() => {}));
  }
  await Promise.all(jobs);
}

function refreshEvents() {
  const params = new URLSearchParams();
  params.set("limit", String(state.events.limit));
  params.set("offset", String(state.events.offset));
  if (state.eventFilters.component) params.set("component", state.eventFilters.component);
  if (state.eventFilters.errorsOnly) params.set("ok", "false");
  return api(`/ops/events?${params}`).then((events) => {
    state.events = events;
    renderEvents();
  });
}

// ---------- header ----------

function renderOverall() {
  const health = state.health;
  if (!health) return;
  const badge = $("overall");
  badge.textContent = health.overall;
  badge.className = `badge ${health.overall}`;
  const degraded = (health.degraded || []).join(", ");
  badge.title = degraded ? `degraded/down: ${degraded}` : "all components ok";
  const apiDetails = health.components?.api?.details;
  $("sha").textContent = apiDetails?.version ? `@${String(apiDetails.version).slice(0, 7)}` : "";
}

// ---------- flow tab ----------

function renderFlow() {
  const flow = state.flow;
  if (!flow) return;
  const svg = $("flow");
  const phone = window.innerWidth < 700;
  const { width, height, pos } = layoutNodes(flow.nodes, phone);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.replaceChildren(...flowEdges(flow, pos, phone), ...flowNodes(flow, pos));

  const degraded = flow.degraded || [];
  $("flow-note").textContent = degraded.length
    ? `attention: ${degraded.join(", ")}`
    : "all hops healthy · click a node to open its health card";
}

function layoutNodes(nodes, phone) {
  const pos = new Map();
  if (phone) {
    // vertical stack, inputs first (column asc, then row)
    const ordered = [...nodes].sort((a, b) => a.column - b.column || a.row - b.row);
    const rowH = 64;
    ordered.forEach((node, index) => {
      pos.set(node.id, { x: 14, y: 16 + index * rowH, w: 168, h: 46 });
    });
    return { width: 196, height: 16 + ordered.length * rowH, pos };
  }
  const colW = 200, rowH = 92, padX = 16, padY = 14;
  let maxCol = 0, maxRow = 0;
  for (const node of nodes) {
    maxCol = Math.max(maxCol, node.column);
    maxRow = Math.max(maxRow, node.row);
  }
  // column x positions: pack used columns left-to-right
  const usedCols = [...new Set(nodes.map((n) => n.column))].sort((a, b) => a - b);
  const colIndex = new Map(usedCols.map((c, i) => [c, i]));
  const width = padX * 2 + usedCols.length * colW;
  const height = padY * 2 + (maxRow + 1) * rowH;
  for (const node of nodes) {
    const c = colIndex.get(node.column);
    pos.set(node.id, {
      x: padX + c * colW + 10,
      y: padY + node.row * rowH + 12,
      w: colW - 32,
      h: rowH - 28,
    });
  }
  return { width, height, pos };
}

function nodeAnchor(pos, side) {
  const { x, y, w, h } = pos;
  return side === "left" ? { x, y: y + h / 2 }
       : side === "right" ? { x: x + w, y: y + h / 2 }
       : side === "top" ? { x: x + w / 2, y }
       : { x: x + w / 2, y: y + h };
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}

function flowEdges(flow, pos, phone) {
  return flow.edges.map((edge) => {
    const from = pos.get(edge.from);
    const to = pos.get(edge.to);
    if (!from || !to) return svgEl("path", { class: "flow-edge" });
    const a = nodeAnchor(from, phone ? "bottom" : "right");
    const b = nodeAnchor(to, phone ? "top" : "left");
    const midX = (a.x + b.x) / 2;
    const d = phone
      ? `M ${a.x} ${a.y} C ${a.x} ${(a.y + b.y) / 2}, ${b.x} ${(a.y + b.y) / 2}, ${b.x} ${b.y}`
      : `M ${a.x} ${a.y} C ${midX} ${a.y}, ${midX} ${b.y}, ${b.x} ${b.y}`;
    const path = svgEl("path", {
      class: `flow-edge edge-${edge.status}${edge.active ? " active" : ""}`,
      d,
    });
    const title = svgEl("title");
    title.textContent = `${edge.hop} — ${edge.status}` +
      (edge.count ? ` · ${edge.count} calls/15m · p50 ${edge.p50 ?? "?"}ms` : " · no traffic/15m") +
      (edge.last_error ? ` · last error: ${jsonPreview(edge.last_error, 120)}` : "");
    path.append(title);
    return path;
  });
}

function flowNodes(flow, pos) {
  return flow.nodes.map((node) => {
    const p = pos.get(node.id);
    const group = svgEl("g", { class: "flow-node", "data-node": node.id });
    group.addEventListener("click", () => {
      switchTab("health");
      const card = document.querySelector(`[data-component="${node.component}"]`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    group.append(svgEl("rect", {
      class: "body", x: p.x, y: p.y, width: p.w, height: p.h, rx: 10,
    }));
    const dot = svgEl("circle", {
      cx: p.x + 14, cy: p.y + 15, r: 5,
      fill: { ok: "var(--ok)", degraded: "var(--degraded)", down: "var(--down)", disabled: "var(--disabled)" }[node.status] || "var(--disabled)",
    });
    group.append(dot);
    const label = svgEl("text", { class: "label", x: p.x + 26, y: p.y + 19 });
    label.textContent = node.label;
    group.append(label);
    const sub = svgEl("text", { class: "sub", x: p.x + 10, y: p.y + p.h - 8 });
    const latency = node.latency_ms != null ? ` · ${node.latency_ms}ms` : "";
    sub.textContent = `${node.status}${latency}`;
    group.append(sub);
    const title = svgEl("title");
    title.textContent = node.summary || node.component;
    group.append(title);
    return group;
  });
}

// ---------- health tab ----------

const PROBEABLE = ["postgres", "qdrant", "embedder", "jev", "llm", "telegram", "docker", "disk"];

function renderHealth() {
  const health = state.health;
  if (!health) return;
  const grid = $("health-grid");
  grid.replaceChildren();
  for (const [name, entry] of Object.entries(health.components)) {
    const card = el("div", { class: `hcard ${entry.status}`, "data-component": name });
    const head = el("div", { class: "hhead" },
      el("span", { class: "hname", text: name }),
      el("span", { class: `chip ${entry.status}`, text: entry.status }),
    );
    card.append(head, el("div", { class: "hsummary", text: entry.summary || "" }));
    const details = Object.entries(entry.details || {})
      .filter(([key]) => key !== "latency_ms")
      .map(([key, value]) => `${key}: ${jsonPreview(value, 90)}`)
      .join("\n");
    if (details) card.append(el("div", { class: "hdetails", text: details }));
    const actions = el("div", { class: "hactions" });
    if (PROBEABLE.includes(name)) {
      const button = el("button", { class: "btn", text: "Run probe" });
      button.addEventListener("click", async () => {
        button.disabled = true;
        button.textContent = "probing…";
        try {
          const result = await api(`/ops/probe/${name}`, { method: "POST" });
          const out = el("div", { class: "probe-result" });
          out.textContent = result.ok
            ? `✓ ${jsonPreview(result.summary ?? result, 200)}`
            : `✗ ${jsonPreview(result.error ?? result, 200)}`;
          const existing = card.querySelector(".probe-result");
          if (existing) existing.replaceWith(out); else card.append(out);
        } catch (error) {
          toast(`probe ${name} failed: ${error.message}`);
        } finally {
          button.disabled = false;
          button.textContent = "Run probe";
        }
      });
      actions.append(button);
    }
    const checked = el("span", { class: "hint", text: entry.checked_at ? ago(entry.checked_at) : "" });
    actions.append(checked);
    card.append(actions);
    grid.append(card);
  }
}

// ---------- events tab ----------

function renderEvents() {
  const select = $("events-component");
  const known = new Set(["api", "telegram", "search", "ask", "ingest", "indexer", "jev", "embedder"]);
  for (const event of state.events.events) known.add(event.component);
  for (const component of known) {
    if (![...select.options].some((option) => option.value === component)) {
      select.append(el("option", { value: component, text: component }));
    }
  }
  const table = $("events-table");
  if (!state.events.events.length) {
    table.replaceChildren(el("div", { class: "empty", text: "no events match the filters" }));
    $("events-pageinfo").textContent = "";
    return;
  }
  const tbody = el("tbody");
  for (const event of state.events.events) {
    const hasItem = !!(event.detail && event.detail.item_id);
    const row = el("tr", {
      class: `${event.ok ? "" : "error"}${hasItem ? " rowlink" : ""}`,
      title: hasItem ? "click to inspect the item" : "",
      onclick: hasItem ? () => openInspector(event.detail.item_id) : null,
    });
    row.append(
      el("td", { class: "t", text: ago(event.ts) }),
      el("td", { text: event.component }),
      el("td", { class: "k", text: event.kind }),
      el("td", { class: "edge hide-phone", text: event.edge || "" }),
      el("td", { class: `ok-${event.ok ? "yes" : "no"}`, text: event.ok ? "ok" : "FAIL" }),
      el("td", { class: "t", text: event.latency_ms != null ? `${event.latency_ms}ms` : "" }),
      el("td", { class: "detail", text: jsonPreview(event.detail ?? {}, 160) }),
    );
    tbody.append(row);
  }
  const thead = el("thead", {}, el("tr", {},
    el("th", { text: "when" }), el("th", { text: "component" }), el("th", { text: "kind" }),
    el("th", { class: "hide-phone", text: "edge" }), el("th", { text: "ok" }),
    el("th", { text: "ms" }), el("th", { text: "detail" }),
  ));
  table.replaceChildren(el("table", {}, thead, tbody));
  const { offset, limit, total } = state.events;
  $("events-pageinfo").textContent = `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`;
  $("events-prev").disabled = offset <= 0;
  $("events-next").disabled = offset + limit >= total;
}

// ---------- stats tab ----------

function renderStats() {
  const stats = state.stats;
  if (!stats) return;
  const grid = $("stats-components");
  grid.replaceChildren();
  for (const component of stats.components) {
    const errorPct = (component.error_rate * 100).toFixed(1);
    grid.append(el("div", { class: "scard" },
      el("div", { class: "sname", text: component.component }),
      el("div", { class: "srow" }, el("span", { text: "calls 24h" }), el("b", { text: String(component.count_24h) })),
      el("div", { class: "srow" }, el("span", { text: "calls 7d" }), el("b", { text: String(component.count) })),
      el("div", { class: "srow" }, el("span", { text: "errors 24h" }), el("b", { text: `${(component.error_rate_24h * 100).toFixed(1)}%` })),
      el("div", { class: "srow" }, el("span", { text: "p50 / p95 (7d)" }),
        el("b", { text: `${component.p50 ?? "–"} / ${component.p95 ?? "–"}ms` })),
      el("div", { class: "srow" }, el("span", { text: "error rate 7d" }),
        el("b", { text: `${errorPct}%`, class: errorPct !== "0.0" ? "ok-no" : "ok-yes" })),
    ));
  }
  const histogram = $("stats-histogram");
  histogram.classList.add("histogram-wrap");
  const max = Math.max(1, ...stats.histogram.map((bucket) => bucket.count));
  histogram.replaceChildren();
  for (const bucket of stats.histogram) {
    const bar = el("div", { class: "bar", style: `height:${(bucket.count / max) * 80}px` },
      el("em", { text: bucket.count ? String(bucket.count) : "" }),
      el("span", { text: bucket.range }),
    );
    bar.title = `confidence ${bucket.range}: ${bucket.count} items`;
    histogram.append(bar);
  }
  const shares = $("stats-status");
  shares.replaceChildren();
  const total = Object.values(stats.status_shares).reduce((a, b) => a + b, 0) || 1;
  for (const [status, count] of Object.entries(stats.status_shares)) {
    shares.append(el("span", { class: "share" },
      `${status} `, el("b", { text: `${count} (${((count / total) * 100).toFixed(0)}%)` })));
  }
}

// ---------- debug tab: traces + inspector ----------

function renderSteps(container, body) {
  const steps = el("div", { class: "steps" });
  for (const step of body.steps || []) {
    steps.append(el("div", { class: `step ${step.ok ? "ok" : "fail"}` },
      el("div", { class: "sname" }, `${step.ok ? "✓" : "✗"} ${step.step} `,
        el("span", { class: "ms", text: `${step.latency_ms}ms` })),
      el("div", { class: "sdata", text: jsonPreview(step.detail ?? {}, 400) }),
    ));
  }
  const existing = container.querySelector(".steps");
  if (existing) existing.replaceWith(steps); else container.append(steps);
}

async function runCaptureTrace() {
  const text = $("trace-capture-text").value.trim();
  if (!text) { toast("enter text to trace first"); return; }
  try {
    const body = await api("/ops/trace/capture", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    renderSteps($("trace-capture-out"), body);
  } catch (error) { toast(`capture trace failed: ${error.message}`); }
}

async function runSearchTrace() {
  const query = $("trace-search-q").value.trim();
  if (!query) { toast("enter a query first"); return; }
  try {
    const body = await api(`/ops/trace/search?q=${encodeURIComponent(query)}`);
    renderSteps($("trace-search-out"), body);
  } catch (error) { toast(`search trace failed: ${error.message}`); }
}

async function runAskTrace() {
  const question = $("trace-ask-q").value.trim();
  if (!question) { toast("enter a question first"); return; }
  try {
    const body = await api("/ops/trace/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    renderSteps($("trace-ask-out"), body);
  } catch (error) { toast(`ask trace failed: ${error.message}`); }
}

async function openInspector(itemId) {
  const container = $("inspector-out");
  $("inspector-id").value = itemId;
  try {
    const body = await api(`/ops/item/${itemId}`);
    const item = body.item;
    const wrap = el("div", { class: "inspector" });
    const meta = el("div");
    for (const key of ["id", "source", "type", "project", "importance", "actionability",
                       "confidence", "status", "occurred_at", "created_at"]) {
      meta.append(el("div", { class: "ifield" },
        el("span", { class: "ik", text: key }),
        el("span", { class: "iv", text: item[key] == null ? "–" : String(item[key]) }),
      ));
    }
    wrap.append(
      el("div", { class: "itext", text: item.raw_text ?? "" }),
      meta,
      el("div", { class: "ifield" },
        el("span", { class: "ik", text: "vector" }),
        el("span", { class: "iv", text: body.vector?.exists
          ? `yes · payload keys: ${(body.vector.payload_keys || []).join(", ")}`
          : `no${body.vector?.error ? ` (${body.vector.error})` : ""}` })),
      el("div", { class: "ifield" },
        el("span", { class: "ik", text: "meta" }),
        el("span", { class: "iv", text: jsonPreview(item.meta ?? {}, 300) })),
    );
    for (const event of (body.events || []).slice(0, 5)) {
      wrap.append(el("div", { class: "ifield" },
        el("span", { class: "ik", text: `event ${event.kind}` }),
        el("span", { class: "iv", text: `${ago(event.ts)} · ${event.ok ? "ok" : "FAIL"} · ${jsonPreview(event.detail ?? {}, 120)}` })));
    }
    container.replaceChildren(wrap);
  } catch (error) {
    container.replaceChildren(el("div", { class: "empty", text: `not found: ${shortId(itemId)}` }));
  }
}

// ---------- tabs + refresh loop ----------

function switchTab(name) {
  state.tab = name;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("is-active", tab.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.classList.toggle("is-active", panel.id === `tab-${name}`);
  }
  refreshAll();
}

function tick() {
  if (state.paused) return;
  state.countdown -= 1;
  if (state.countdown <= 0) {
    state.countdown = REFRESH_MS / 1000;
    refreshAll();
  }
  $("refresh-in").textContent = `${state.countdown}s`;
}

function init() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  }
  $("pause-btn").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause-btn").textContent = state.paused ? "▶ Resume" : "⏸ Pause";
    $("refresh-in").textContent = state.paused ? "paused" : `${state.countdown}s`;
  });
  $("theme-btn").addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("cockpit-theme", next);
  });
  $("events-component").addEventListener("change", (event) => {
    state.eventFilters.component = event.target.value;
    state.events.offset = 0;
    refreshEvents();
  });
  $("events-errors").addEventListener("change", (event) => {
    state.eventFilters.errorsOnly = event.target.checked;
    state.events.offset = 0;
    refreshEvents();
  });
  $("events-prev").addEventListener("click", () => {
    state.events.offset = Math.max(0, state.events.offset - state.events.limit);
    refreshEvents();
  });
  $("events-next").addEventListener("click", () => {
    state.events.offset += state.events.limit;
    refreshEvents();
  });
  $("trace-capture-btn").addEventListener("click", runCaptureTrace);
  $("trace-search-btn").addEventListener("click", runSearchTrace);
  $("trace-ask-btn").addEventListener("click", runAskTrace);
  $("inspector-btn").addEventListener("click", () => {
    const id = $("inspector-id").value.trim();
    if (id) openInspector(id);
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.tab === "flow") renderFlow(); }, 150);
  });

  refreshAll();
  setInterval(tick, 1000);
}

document.addEventListener("DOMContentLoaded", init);
