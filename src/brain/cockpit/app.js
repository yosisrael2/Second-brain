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
  traceHighlight: null,
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
      renderKpis();
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
  const buildDot = $("build-dot");
  buildDot.className = `dot ${health.overall}`;
  const apiDetails = health.components?.api?.details;
  $("sha").textContent = apiDetails?.version ? `@${String(apiDetails.version).slice(0, 7)}` : "";
}

// ---------- KPI row (computed from the live /ops/flow payload) ----------

// Pure derivation so the served page's own code can be tested (tests execute
// this function via node). Disabled nodes are legend-only, matching the mock.
function statusCounts(nodes) {
  const counts = { ok: 0, "not probed": 0, degraded: 0, down: 0 };
  for (const node of nodes || []) {
    if (node.status in counts) counts[node.status] += 1;
  }
  return counts;
}

function renderKpis() {
  if (!state.flow) return;
  const counts = statusCounts(state.flow.nodes);
  for (const [status, count] of Object.entries(counts)) {
    $(`kpi-${status.replace(" ", "")}`).textContent = String(count);
  }
}

// ---------- flow tab ----------

// Desktop layout follows the approved mock: 240x64 cards, 408px column pitch,
// 112px row pitch, stage labels above the grid, infrastructure band separated
// underneath with no arrows. Phone: vertical top->bottom stack.
const STAGE_LABELS = ["Inputs", "Processing", "AI / Stores"];
const STATUS_CLASS = { "not probed": "notprobed" };

const NODE_W = 240;
const NODE_H = 64;
const COL_PITCH = 408;
const ROW_PITCH = 112;
const FIRST_ROW_Y = 48;
// Exit/enter anchor positions as a fraction of card height, by degree.
const ANCHOR_FRACS = { 1: [0.5], 2: [0.3, 0.7], 3: [0.25, 0.5, 0.75] };

const TRACE_PATHS = {
  capture: ["api->ingest", "parser->ingest", "ingest->jev", "ingest->postgres",
            "ingest->embedder", "embedder->qdrant"],
  search: ["search->embedder", "search->qdrant", "search->rerank"],
  ask: ["ask->search", "ask->llm"],
};

function setTraceHighlight(kind) {
  state.traceHighlight = kind && TRACE_PATHS[kind] ? TRACE_PATHS[kind] : null;
  if (state.tab === "flow") renderFlow();
}

function applyTraceHighlight() {
  const hops = state.traceHighlight || [];
  for (const path of document.querySelectorAll("#flow .flow-edge")) {
    path.classList.toggle("hl", hops.includes(path.dataset.hop));
  }
}

function renderFlow() {
  const flow = state.flow;
  if (!flow) return;
  const svg = $("flow");
  const phone = window.innerWidth < 700;
  const { width, height, pos } = layoutNodes(flow.nodes, phone);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.replaceChildren(
    svgEl("defs", {}, ...arrowMarkers()),
    ...flowEdges(flow, pos, phone, width, height),
    ...(phone ? [] : flowDecorations(flow, pos, width)),
    ...flowNodes(flow, pos),
  );

  const degraded = flow.degraded || [];
  $("flow-note").textContent = degraded.length
    ? `attention: ${degraded.join(", ")}`
    : "all hops healthy · click a node to open its health card";
  applyTraceHighlight();
}

function arrowMarkers() {
  // One arrowhead marker per edge color; fills use the same CSS vars as the
  // edge strokes so light/dark stay in sync.
  return ["green", "amber", "red", "grey", "notprobed"].map((status) => {
    const marker = svgEl("marker", {
      id: `arrow-${status}`, viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "6.5", markerHeight: "6.5", orient: "auto-start-reverse",
    });
    marker.append(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", class: `marker-fill edge-${status}` }));
    return marker;
  });
}

function layoutNodes(nodes, phone) {
  const pos = new Map();
  if (phone) {
    const ordered = [...nodes].sort((a, b) =>
      (a.band === "infra") - (b.band === "infra") || a.column - b.column || a.row - b.row);
    const W = Math.max(320, Math.min(window.innerWidth, 700)) - 16;
    const boxW = W - 42; // right lane reserved for skip-edge routing
    const rowH = 56;
    let y = 12;
    for (const node of ordered) {
      if (node.band === "infra" && pos.get("__dividerY") === undefined) {
        y += 24;
        pos.set("__dividerY", { x: 0, y: y - 10, w: 0, h: 0 });
      }
      pos.set(node.id, { x: 8, y, w: boxW, h: 42 });
      y += rowH;
    }
    pos.set("__laneX", { x: W - 8, y: 0, w: 0, h: 0 });
    return { width: W, height: y + 2, pos };
  }
  const width = 2 * COL_PITCH + NODE_W;
  pos.set("__labels", { x: 0, y: 16, w: 0, h: 0 });
  // Flow nodes first; the infrastructure band starts BELOW the tallest
  // column's last node (+ heading + divider gap) so they can never overlap.
  let infraTop = 0;
  for (const node of nodes.filter((n) => n.band !== "infra")) {
    const y = FIRST_ROW_Y + node.row * ROW_PITCH;
    pos.set(node.id, {
      x: node.column * COL_PITCH,
      y,
      w: NODE_W,
      h: NODE_H,
    });
    infraTop = Math.max(infraTop, y + NODE_H);
  }
  const dividerY = infraTop + 44;
  const infraY = dividerY + 28;
  for (const node of nodes.filter((n) => n.band === "infra")) {
    pos.set(node.id, {
      x: node.column * COL_PITCH,
      y: infraY,
      w: NODE_W,
      h: NODE_H,
    });
  }
  const height = infraY + NODE_H + 4;
  pos.set("__dividerY", { x: 0, y: dividerY, w: width, h: 0 });
  return { width, height, pos };
}

function flowDecorations(flow, pos, width) {
  const els = [];
  const labels = pos.get("__labels");
  if (labels) {
    STAGE_LABELS.forEach((label, index) => {
      const text = svgEl("text", {
        class: "stage-label", x: index * COL_PITCH, y: labels.y,
      });
      text.textContent = label;
      els.push(text);
    });
  }
  const divider = pos.get("__dividerY");
  if (divider) {
    const text = svgEl("text", { class: "stage-label", x: divider.x, y: divider.y - 16 });
    text.textContent = "Infrastructure";
    els.push(text);
    els.push(svgEl("line", {
      x1: divider.x, y1: divider.y, x2: divider.x + divider.w, y2: divider.y,
      class: "infra-divider",
    }));
  }
  return els;
}

function nodeAnchor(p, side) {
  return side === "left" ? { x: p.x, y: p.y + p.h / 2 }
       : side === "right" ? { x: p.x + p.w, y: p.y + p.h / 2 }
       : side === "top" ? { x: p.x + p.w / 2, y: p.y }
       : { x: p.x + p.w / 2, y: p.y + p.h };
}

function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
}

function edgeGeometry(edge, pos, phone, anchors) {
  const from = pos.get(edge.from);
  const to = pos.get(edge.to);
  if (!from || !to) return null;
  if (phone) {
    const lane = pos.get("__laneX");
    const a = nodeAnchor(from, "right");
    const b = nodeAnchor(to, "right");
    if (lane) {
      // Skip edge: leave the source to the right, run through the lane beside
      // the boxes, enter the target from the right.
      const laneX = lane.x;
      return {
        d: `M ${a.x} ${a.y} C ${laneX} ${a.y}, ${laneX} ${b.y}, ${b.x} ${b.y}`,
        label: { x: laneX - 8, y: (a.y + b.y) / 2, anchor: "end" },
      };
    }
    const downward = to.y > from.y;
    const a2 = nodeAnchor(from, downward ? "bottom" : "top");
    const b2 = nodeAnchor(to, downward ? "top" : "bottom");
    const midY = (a2.y + b2.y) / 2;
    return {
      d: `M ${a2.x} ${a2.y} C ${a2.x} ${midY}, ${b2.x} ${midY}, ${b2.x} ${b2.y}`,
      label: { x: a2.x, y: midY, anchor: "middle" },
    };
  }
  const sameColumn = from.x === to.x;
  if (sameColumn) {
    // Straight vertical between the shared anchor points.
    const downward = to.y > from.y;
    const a = nodeAnchor(from, downward ? "bottom" : "top");
    const b = nodeAnchor(to, downward ? "top" : "bottom");
    const midY = (a.y + b.y) / 2;
    return {
      d: `M ${a.x} ${a.y} L ${b.x} ${b.y}`,
      label: { x: a.x + 8, y: midY + 4, anchor: "start" },
    };
  }
  // Orthogonal right-angle route: leave the source's right edge, run
  // horizontally down a per-edge bend lane in the column gap, turn
  // vertically, then enter the target's left edge.
  const a = {
    x: from.x + from.w,
    y: from.y + from.h * anchors.exitFrac,
  };
  const b = { x: to.x, y: to.y + to.h * anchors.enterFrac };
  const bendX = a.x + 28 + anchors.bendIndex * 20;
  return {
    d: `M ${a.x} ${a.y} L ${bendX} ${a.y} L ${bendX} ${b.y} L ${b.x} ${b.y}`,
    label: { x: (a.x + bendX) / 2, y: a.y - 6, anchor: "middle" },
  };
}

function flowEdges(flow, pos, phone, width, height) {
  // Deterministic stagger: edges leaving/entering the same card get distinct
  // anchor rows; edges crossing the same column gap get distinct bend lanes.
  const exitCount = new Map(), enterCount = new Map(), lanesPerGap = new Map();
  for (const edge of flow.edges) {
    const from = pos.get(edge.from), to = pos.get(edge.to);
    if (!from || !to) continue;
    exitCount.set(edge.from, (exitCount.get(edge.from) || 0) + 1);
    enterCount.set(edge.to, (enterCount.get(edge.to) || 0) + 1);
    if (!phone && from.x !== to.x) {
      lanesPerGap.set(from.x, (lanesPerGap.get(from.x) || 0) + 1);
    }
  }
  const seen = { exit: new Map(), enter: new Map(), lane: new Map() };
  const els = [];
  for (const edge of flow.edges) {
    // Phone: readability over completeness — idle grey edges and infra
    // hops are dropped; only traffic/problems stay.
    if (phone && edge.count === 0 && edge.status === "grey") continue;
    const from = pos.get(edge.from), to = pos.get(edge.to);
    if (!from || !to) continue;
    let anchors = null;
    if (!phone) {
      const exitIdx = seen.exit.get(edge.from) || 0;
      seen.exit.set(edge.from, exitIdx + 1);
      const enterIdx = seen.enter.get(edge.to) || 0;
      seen.enter.set(edge.to, enterIdx + 1);
      let bendIndex = 0;
      if (from.x !== to.x) {
        bendIndex = seen.lane.get(from.x) || 0;
        seen.lane.set(from.x, bendIndex + 1);
      }
      anchors = {
        exitFrac: (ANCHOR_FRACS[Math.min(exitCount.get(edge.from), 3)] || ANCHOR_FRACS[3])[exitIdx],
        enterFrac: (ANCHOR_FRACS[Math.min(enterCount.get(edge.to), 3)] || ANCHOR_FRACS[3])[enterIdx],
        bendIndex,
      };
    }
    const geo = edgeGeometry(edge, pos, phone, anchors);
    if (!geo) continue;
    const path = svgEl("path", {
      class: `flow-edge edge-${edge.status.replace(" ", "")}${edge.active ? " active" : ""}`,
      d: geo.d,
      "marker-end": `url(#arrow-${edge.status.replace(" ", "")})`,
    });
    path.dataset.hop = edge.hop;
    const title = svgEl("title");
    title.textContent = `${edge.hop} — ${edge.status}` +
      (edge.count ? ` · ${edge.count} calls/15m · p50 ${edge.p50 ?? "?"}ms` : " · no traffic/15m") +
      (edge.last_error ? ` · last error: ${jsonPreview(edge.last_error, 120)}` : "");
    path.append(title);
    els.push(path);
    // Labels only where they earn their space: traffic on phone, always on
    // desktop — positioned off the arrow tip and clamped inside the canvas.
    if (phone && edge.count === 0) continue;
    const lx = Math.max(30, Math.min(geo.label.x, (width || 390) - 20));
    const ly = Math.max(16, Math.min(geo.label.y, (height || 800) - 8));
    const label = svgEl("text", {
      class: "flow-elabel", x: lx, y: ly,
      "text-anchor": geo.label.anchor || "middle",
    });
    label.textContent = edgeLabel(edge);
    els.push(label);
  }
  return els;
}

function edgeLabel(edge) {
  if (!edge.count) return "idle";
  return `${edge.count} · ${edge.p50 != null ? `${Math.round(edge.p50)}ms` : "?"}`;
}

function flowNodes(flow, pos) {
  return flow.nodes.map((node) => {
    const p = pos.get(node.id);
    const statusClass = STATUS_CLASS[node.status] || node.status;
    const notProbed = statusClass === "notprobed";
    const group = svgEl("g", {
      class: `flow-node st-${statusClass}`, "data-node": node.id,
    });
    group.addEventListener("click", () => {
      switchTab("health");
      const card = document.querySelector(`[data-component="${node.component}"]`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    group.append(svgEl("rect", {
      class: "body", x: p.x, y: p.y, width: p.w, height: p.h, rx: 14,
    }));
    // Status dot: 10px solid disc, or a small dashed circle when not probed.
    if (notProbed) {
      group.append(svgEl("circle", {
        class: "dot-dashed", cx: p.x + 23, cy: p.y + p.h / 2, r: 4,
      }));
    } else {
      group.append(svgEl("circle", {
        class: "dot-solid", cx: p.x + 23, cy: p.y + p.h / 2, r: 5,
      }));
    }
    const label = svgEl("text", { class: "label", x: p.x + 40, y: p.y + p.h / 2 - 3 });
    label.textContent = node.label;
    group.append(label);
    const sub = svgEl("text", { class: "sub", x: p.x + 40, y: p.y + p.h / 2 + 15 });
    sub.textContent = node.status;
    group.append(sub);
    const title = svgEl("title");
    const latency = node.latency_ms != null ? ` · ${node.latency_ms}ms` : "";
    title.textContent = `${node.summary || node.component}${latency}`;
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
    setTraceHighlight("capture");
  } catch (error) { toast(`capture trace failed: ${error.message}`); }
}

async function runSearchTrace() {
  const query = $("trace-search-q").value.trim();
  if (!query) { toast("enter a query first"); return; }
  try {
    const body = await api(`/ops/trace/search?q=${encodeURIComponent(query)}`);
    renderSteps($("trace-search-out"), body);
    setTraceHighlight("search");
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
    setTraceHighlight("ask");
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

const TAB_NAMES = ["flow", "health", "events", "stats", "debug"];

function tabFromHash() {
  const name = decodeURIComponent(location.hash.replace("#", ""));
  return TAB_NAMES.includes(name) ? name : "flow";
}

function switchTab(name) {
  state.tab = name;
  history.replaceState(null, "", `#${name}`);
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    if (active) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.classList.toggle("is-active", panel.id === `tab-${name}`);
  }
  const title = $("page-title");
  if (title) title.textContent = name.charAt(0).toUpperCase() + name.slice(1);
  refreshAll();
}

function tick() {
  if (state.paused) return;
  state.countdown -= 1;
  if (state.countdown <= 0) {
    state.countdown = REFRESH_MS / 1000;
    refreshAll();
  }
}

function init() {
  // Test hooks: ?hl=capture|search|ask pre-highlights a trace path;
  // ?measure=1 reports layout widths via console + title (no visual change).
  const params = new URLSearchParams(location.search);
  if (params.get("hl")) setTraceHighlight(params.get("hl"));
  if (params.get("measure")) {
    const report = () => {
      const doc = document.documentElement;
      const rects = [...document.querySelectorAll("#flow .flow-node rect.body")]
        .map((r) => r.getBoundingClientRect());
      let overlaps = 0;
      for (let i = 0; i < rects.length; i++) {
        for (let j = i + 1; j < rects.length; j++) {
          const a = rects[i], b = rects[j];
          if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
            overlaps += 1;
          }
        }
      }
      window.__nodeOverlaps = overlaps;
      const offenders = [...document.querySelectorAll("body *")]
        .map((elm) => ({ elm, r: Math.round(elm.getBoundingClientRect().right), sw: elm.scrollWidth }))
        .filter((entry) => entry.r > doc.clientWidth + 1 || entry.sw > doc.clientWidth + 1)
        .sort((a, b) => Math.max(b.r, b.sw) - Math.max(a.r, a.sw))
        .slice(0, 6)
        .map(({ elm, r, sw }) => `${elm.tagName}.${String(elm.className.baseVal ?? elm.className).split(" ")[0]} r=${r} sw=${sw}`);
      const widest = [...document.querySelectorAll("body *")]
        .map((elm) => ({ tag: `${elm.tagName}.${String(elm.className.baseVal ?? elm.className).split(" ")[0]}`, r: Math.round(elm.getBoundingClientRect().right), sw: elm.scrollWidth }))
        .sort((a, b) => Math.max(b.r, b.sw) - Math.max(a.r, a.sw))
        .slice(0, 3);
      document.title = `w=${doc.scrollWidth} cw=${doc.clientWidth} overlaps=${window.__nodeOverlaps} top:${widest.map((x) => `${x.tag} r=${x.r} sw=${x.sw}`).join(" | ")}`;
      console.info(`scrollWidth=${doc.scrollWidth} offenders: ${offenders.join(", ")}`);
    };
    setTimeout(report, 300);
    setTimeout(report, 3000);
  }
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  }
  window.addEventListener("hashchange", () => {
    if (tabFromHash() !== state.tab) switchTab(tabFromHash());
  });
  const intervalLabel = () => `every ${REFRESH_MS / 1000}s`;
  $("refresh-in").textContent = intervalLabel();
  $("pause-btn").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause-btn").classList.toggle("is-paused", state.paused);
    $("pause-label").textContent = state.paused ? "Resume" : "Pause";
    $("refresh-in").textContent = state.paused ? "paused" : intervalLabel();
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

  switchTab(tabFromHash());
  setInterval(tick, 1000);
}

document.addEventListener("DOMContentLoaded", init);
