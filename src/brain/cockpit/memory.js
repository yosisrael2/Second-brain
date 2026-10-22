/* Cockpit Memory tab — entities + versioned facts (PR 0b).
 *
 * Kept in its OWN file so the cockpit restyle can adopt/replace it without
 * touching the shared shell. Depends only on app.js's global helpers
 * (el, $, api, toast, shortId, switchTab, openInspector) and exposes ONE
 * global: window.MemoryTab.refresh() — app.js's refresh loop calls it when
 * the Memory tab is active. All dynamic values render via textContent.
 *
 * Data sources (same origin, localhost only):
 *   GET    /memory                     overview (?status=, ?q=)
 *   POST   /memory/entities            add entity (+aliases)
 *   PATCH  /memory/entities/{id}       rename
 *   DELETE /memory/entities/{id}       hard delete (cascades)
 *   POST   /memory/facts               add fact (status=confirmed|proposed)
 *   PATCH  /memory/facts/{id}          edit = new version (old closed)
 *   DELETE /memory/facts/{id}          soft delete (history kept)
 *   POST   /memory/facts/{id}/confirm  proposed -> confirmed
 *   GET    /memory/facts/{id}/history  version chain (+provenance)
 */
"use strict";

const memoryState = {
  status: "confirmed",
  q: "",
  entities: [],
  adding: false,          // add-entity form open
  editingFact: null,      // fact id being inline-edited
  addingFactFor: null,    // entity id with an open add-fact form
  drawerFact: null,       // fact id whose history is in the drawer
};

// Predicates that mark a person entity as one of the kids ("kids first").
const KID_PREDICATES = new Set(["grade", "class", "gender", "teacher"]);
const KID_LINE_ORDER = ["class", "grade", "gender", "teacher"];

function isKid(entity) {
  return entity.kind === "person" && (entity.facts || []).some((f) => KID_PREDICATES.has(f.predicate));
}

function memoryOrder(a, b) {
  const rank = (entity) =>
    entity.kind === "person" ? (isKid(entity) ? 0 : 1) : entity.kind === "group" ? 2 : 3;
  if (rank(a) !== rank(b)) return rank(a) - rank(b);
  return a.name.localeCompare(b.name);
}

function kidFacts(entity) {
  const kids = entity.facts.filter((f) => KID_PREDICATES.has(f.predicate));
  return [...kids].sort(
    (a, b) => KID_LINE_ORDER.indexOf(a.predicate) - KID_LINE_ORDER.indexOf(b.predicate),
  );
}

function otherFacts(entity) {
  return (entity.facts || []).filter((f) => !KID_PREDICATES.has(f.predicate));
}

function refreshMemory() {
  const params = new URLSearchParams();
  params.set("status", memoryState.status);
  if (memoryState.q) params.set("q", memoryState.q);
  return api(`/memory?${params}`).then((body) => {
    memoryState.entities = body.entities || [];
    if (memoryState.editingFact === null && memoryState.addingFactFor === null) {
      renderMemory();
    }
  });
}

// A two-click destructive confirm: first click arms ("Sure?"), second acts.
function armConfirm(button, onConfirm) {
  if (button.dataset.armed) {
    onConfirm();
    return;
  }
  button.dataset.armed = "1";
  const original = button.textContent;
  button.textContent = "Sure?";
  button.classList.add("armed");
  setTimeout(() => {
    if (!button.isConnected) return;
    delete button.dataset.armed;
    button.textContent = original;
    button.classList.remove("armed");
  }, 2500);
}

function memoryEntityCard(entity) {
  const card = el("div", { class: `mcard kind-${entity.kind}`, "data-entity": entity.id });
  const head = el("div", { class: "mhead" });
  const title = el("span", { class: "mname", text: entity.name, dir: "auto" });
  const actions = el("span", { class: "mactions" });
  const renameBtn = el("button", { class: "btn btn-ghost", title: "rename", text: "✏️" });
  renameBtn.addEventListener("click", () => {
    const input = el("input", { type: "text", value: entity.name, dir: "auto", class: "rename-input" });
    title.replaceWith(input);
    input.focus();
    const save = async () => {
      const name = input.value.trim();
      if (!name || name === entity.name) { input.replaceWith(title); return; }
      try {
        await api(`/memory/entities/${entity.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        toast("entity renamed");
      } catch (error) { toast(`rename failed: ${error.message}`); }
      refreshMemory();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") save();
      if (event.key === "Escape") input.replaceWith(title);
    });
    input.addEventListener("blur", save);
  });
  const deleteBtn = el("button", { class: "btn btn-ghost", title: "delete entity (and its facts)", text: "🗑" });
  deleteBtn.addEventListener("click", () => {
    armConfirm(deleteBtn, async () => {
      try {
        await api(`/memory/entities/${entity.id}`, { method: "DELETE" });
        toast(`deleted ${entity.name}`);
      } catch (error) { toast(`delete failed: ${error.message}`); }
      refreshMemory();
    });
  });
  actions.append(renameBtn, deleteBtn);
  head.append(title, el("span", { class: `chip kind-chip`, text: entity.kind }), actions);
  card.append(head);
  if ((entity.aliases || []).length) {
    card.append(el("div", { class: "maliases", text: `aka ${entity.aliases.join(", ")}`, dir: "auto" }));
  }
  const facts = entity.facts || [];
  const proposed = memoryState.status === "proposed";
  // Kids-first summary line only in the confirmed view; in the proposed inbox
  // every fact needs its own row (each carries ✅/❌).
  if (isKid(entity) && !proposed) {
    const line = kidFacts(entity)
      .map((f) => f.value)
      .filter(Boolean)
      .join(" · ");
    if (line) card.append(el("div", { class: "kid-line", text: line, dir: "auto" }));
  }
  const factList = el("div", { class: "mfacts" });
  const rowFacts = proposed ? facts : otherFacts(entity);
  for (const fact of rowFacts) {
    factList.append(memoryFactRow(entity, fact));
  }
  if (factList.childNodes.length) card.append(factList);
  // add-fact
  const addWrap = el("div", { class: "madd-fact" });
  const addBtn = el("button", { class: "btn btn-ghost", text: "＋ fact" });
  addBtn.addEventListener("click", () => {
    memoryState.addingFactFor = entity.id;
    memoryState.editingFact = null;
    renderMemory();
  });
  addWrap.append(addBtn);
  if (memoryState.addingFactFor === entity.id) {
    addWrap.append(memoryFactAddForm(entity));
  }
  card.append(addWrap);
  return card;
}

function factStatusChip(fact) {
  if (fact.status === "confirmed" && !fact.valid_to) return null;
  const cls = fact.status === "proposed" ? "proposed" : "rejected";
  const label = fact.valid_to && fact.status === "confirmed" ? "superseded" : fact.status;
  return el("span", { class: `chip ${cls}`, text: label });
}

function factValidity(fact) {
  if (!fact.valid_to) return null;
  return el("span", {
    class: "mvalid",
    text: `${new Date(fact.valid_from).toLocaleString()} → ${new Date(fact.valid_to).toLocaleString()}`,
  });
}

function memoryFactRow(entity, fact) {
  const current = !fact.valid_to;
  const row = el("div", { class: `mrow st-${fact.status}` });
  row.append(
    el("span", { class: "mpred", text: fact.predicate }),
    el("span", { class: "mval", text: fact.value ?? "", dir: "auto" }),
  );
  const validity = factValidity(fact);
  if (validity) row.append(validity);
  const chip = factStatusChip(fact);
  if (chip) row.append(chip);
  const actions = el("span", { class: "mrow-actions" });
  if (current && memoryState.status === "confirmed") {
    const editBtn = el("button", { class: "btn btn-ghost", title: "edit (creates a new version)", text: "✏️" });
    editBtn.addEventListener("click", () => {
      memoryState.editingFact = fact.id;
      memoryState.addingFactFor = null;
      renderMemory();
    });
    const deleteBtn = el("button", { class: "btn btn-ghost", title: "delete (soft; history kept)", text: "🗑" });
    deleteBtn.addEventListener("click", () => {
      armConfirm(deleteBtn, async () => {
        try { await api(`/memory/facts/${fact.id}`, { method: "DELETE" }); toast("fact deleted (history kept)"); }
        catch (error) { toast(`delete failed: ${error.message}`); }
        refreshMemory();
      });
    });
    actions.append(editBtn, deleteBtn);
  } else if (fact.status === "proposed") {
    const confirmBtn = el("button", { class: "btn ok-btn", text: "✅" });
    confirmBtn.addEventListener("click", async () => {
      try { await api(`/memory/facts/${fact.id}/confirm`, { method: "POST" }); }
      catch (error) { toast(`confirm failed: ${error.message}`); }
      refreshMemory();
    });
    const rejectBtn = el("button", { class: "btn btn-ghost", title: "reject (soft delete)", text: "❌" });
    rejectBtn.addEventListener("click", () => {
      armConfirm(rejectBtn, async () => {
        try { await api(`/memory/facts/${fact.id}`, { method: "DELETE" }); }
        catch (error) { toast(`reject failed: ${error.message}`); }
        refreshMemory();
      });
    });
    actions.append(confirmBtn, rejectBtn);
  }
  const historyBtn = el("button", { class: "btn btn-ghost", title: "history + provenance", text: "🕘" });
  historyBtn.addEventListener("click", () => openMemoryDrawer(fact.id));
  actions.append(historyBtn);
  row.append(actions);
  if (memoryState.editingFact === fact.id) {
    row.append(memoryFactEditForm(fact));
  }
  return row;
}

function memoryFactEditForm(fact) {
  const form = el("div", { class: "mfact-form" });
  const predInput = el("input", { type: "text", value: fact.predicate, dir: "auto", class: "fpred" });
  const valInput = el("input", { type: "text", value: fact.value ?? "", dir: "auto", class: "fval" });
  const saveBtn = el("button", { class: "btn", text: "Save" });
  const cancelBtn = el("button", { class: "btn btn-ghost", text: "Cancel" });
  cancelBtn.addEventListener("click", () => {
    memoryState.editingFact = null;
    renderMemory();
  });
  saveBtn.addEventListener("click", async () => {
    const value = valInput.value.trim();
    if (!value) { toast("value must not be empty"); return; }
    try {
      await api(`/memory/facts/${fact.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ predicate: predInput.value.trim(), value }),
      });
      memoryState.editingFact = null;
    } catch (error) { toast(`edit failed: ${error.message}`); }
    refreshMemory();
  });
  form.append(predInput, valInput, saveBtn, cancelBtn);
  return form;
}

function memoryFactAddForm(entity) {
  const form = el("div", { class: "mfact-form" });
  const predInput = el("input", { type: "text", placeholder: "predicate (e.g. teacher)", dir: "auto", class: "fpred" });
  const valInput = el("input", { type: "text", placeholder: "value (e.g. Rinat)", dir: "auto", class: "fval" });
  const saveBtn = el("button", { class: "btn", text: "Save" });
  const cancelBtn = el("button", { class: "btn btn-ghost", text: "Cancel" });
  cancelBtn.addEventListener("click", () => {
    memoryState.addingFactFor = null;
    renderMemory();
  });
  saveBtn.addEventListener("click", async () => {
    const predicate = predInput.value.trim();
    const value = valInput.value.trim();
    if (!predicate || !value) { toast("predicate and value are required"); return; }
    try {
      await api("/memory/facts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subject_id: entity.id, predicate, value }),
      });
      memoryState.addingFactFor = null;
    } catch (error) { toast(`add failed: ${error.message}`); }
    refreshMemory();
  });
  form.append(predInput, valInput, saveBtn, cancelBtn);
  return form;
}

async function openMemoryDrawer(factId) {
  memoryState.drawerFact = factId;
  const drawer = $("memory-drawer");
  const body = $("memory-drawer-body");
  drawer.hidden = false;
  body.replaceChildren(el("div", { class: "empty", text: "loading…" }));
  let chain;
  try {
    chain = await api(`/memory/facts/${factId}/history`);
  } catch (error) {
    body.replaceChildren(el("div", { class: "empty", text: `not found: ${shortId(factId)}` }));
    return;
  }
  body.replaceChildren();
  for (const version of chain) {
    const row = el("div", { class: `hrow st-${version.status}` });
    const top = el("div", { class: "hrow-main" });
    top.append(
      el("span", { class: "mpred", text: version.predicate }),
      el("span", { class: "mval", text: version.value ?? "", dir: "auto" }),
      el("span", { class: `chip ${version.status}`, text: version.status }),
    );
    row.append(top);
    const validity = factValidity(version);
    if (validity) row.append(validity);
    const provenance = version.provenance || {};
    if (provenance.quote) {
      row.append(el("div", { class: "hquote", text: `“${provenance.quote}”`, dir: "auto" }));
    }
    const sourceLine = el("div", { class: "hsource" });
    if (provenance.source === "item" && (provenance.item_ids || []).length) {
      sourceLine.append(el("span", { text: "source item" + (provenance.item_ids.length > 1 ? "s" : "") + ": " }));
      for (const itemId of provenance.item_ids) {
        const btn = el("button", { class: "btn btn-ghost item-link", text: shortId(itemId), title: "open in the Debug inspector" });
        btn.addEventListener("click", () => {
          closeMemoryDrawer();
          switchTab("debug");
          openInspector(itemId);
        });
        sourceLine.append(btn);
      }
    } else {
      sourceLine.append(el("span", { text: provenance.source === "item" ? "source item (id missing)" : "👤 added by you" }));
    }
    row.append(sourceLine);
    body.append(row);
  }
}

function closeMemoryDrawer() {
  $("memory-drawer").hidden = true;
  memoryState.drawerFact = null;
}

function renderMemory() {
  const list = $("memory-list");
  const entities = [...memoryState.entities].sort(memoryOrder);
  $("memory-count").textContent = entities.length
    ? `${entities.length} entit${entities.length === 1 ? "y" : "ies"}`
    : "";
  if (!entities.length) {
    list.replaceChildren(el("div", { class: "empty", text: memoryState.q
      ? `no entities match “${memoryState.q}”`
      : memoryState.status === "proposed"
        ? "inbox empty — nothing proposed"
        : "no memory yet — add an entity" }));
    return;
  }
  list.replaceChildren(...entities.map(memoryEntityCard));
}


// ---------- memory tab init (own file; called from memory.js's DOMContentLoaded) ----------

function initMemoryTab() {
  $("memory-status").addEventListener("change", (event) => {
    memoryState.status = event.target.value;
    memoryState.editingFact = null;
    memoryState.addingFactFor = null;
    refreshMemory();
  });
  let memorySearchTimer = null;
  $("memory-q").addEventListener("input", (event) => {
    clearTimeout(memorySearchTimer);
    memorySearchTimer = setTimeout(() => {
      memoryState.q = event.target.value.trim();
      refreshMemory();
    }, 250);
  });
  $("memory-add-entity-btn").addEventListener("click", () => {
    const form = $("memory-add-entity");
    form.hidden = !form.hidden;
    if (!form.hidden) $("memory-entity-name").focus();
  });
  $("memory-entity-cancel").addEventListener("click", () => {
    $("memory-add-entity").hidden = true;
    $("memory-entity-name").value = "";
    $("memory-entity-aliases").value = "";
  });
  $("memory-entity-save").addEventListener("click", async () => {
    const name = $("memory-entity-name").value.trim();
    const kind = $("memory-entity-kind").value;
    const aliases = $("memory-entity-aliases").value
      .split(",")
      .map((alias) => alias.trim())
      .filter(Boolean);
    if (!name) { toast("name is required"); return; }
    try {
      await api("/memory/entities", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, name, aliases }),
      });
      $("memory-add-entity").hidden = true;
      $("memory-entity-name").value = "";
      $("memory-entity-aliases").value = "";
      toast("entity added");
    } catch (error) { toast(`add entity failed: ${error.message}`); }
    refreshMemory();
  });
  $("memory-drawer-close").addEventListener("click", closeMemoryDrawer);
}

document.addEventListener("DOMContentLoaded", initMemoryTab);

// The single symbol the shared shell knows about.
window.MemoryTab = { refresh: refreshMemory };
