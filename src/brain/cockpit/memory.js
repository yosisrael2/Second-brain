/* Cockpit Memory tab — the Memory Management screen (part B).
 *
 * Master-detail per the user's reference: LEFT = search + sections
 * (Family / People / Groups / Places & orgs) with one row per entity
 * (name, sub-line, updated time); RIGHT = the selected entity's page —
 * header (name, sub-line, Edit toggle, + Add) over a scrollable stack of
 * memory cards, ONE self-contained card per fact, full CRUD with Undo
 * (delete-fact reopens via /facts/{id}/restore; remove-entity archives
 * via /archive and undoes via /restore). Phone: list first, tap opens
 * the detail, back button returns.
 *
 * Depends only on app.js's globals (el, $, api, toast, shortId,
 * switchTab, openInspector) and exposes ONE global:
 * window.MemoryTab.refresh(). All dynamic values render via textContent.
 *
 * Data sources (same origin, localhost only):
 *   GET    /memory                              overview (?status=, ?q=)
 *   POST   /memory/entities                     add entity (+aliases)
 *   PATCH  /memory/entities/{id}                rename
 *   POST   /memory/entities/{id}/aliases        add alias (part A)
 *   DELETE /memory/entities/{id}/aliases/{a}    remove alias (part A)
 *   POST   /memory/entities/{id}/archive        soft-remove (part A)
 *   POST   /memory/entities/{id}/restore        Undo the archive (part A)
 *   POST   /memory/facts                        add fact (source=user)
 *   PATCH  /memory/facts/{id}                   edit = new version
 *   DELETE /memory/facts/{id}                   soft delete (history kept)
 *   POST   /memory/facts/{id}/restore           Undo the soft delete (A)
 *   POST   /memory/facts/{id}/confirm           proposed -> confirmed
 *   GET    /memory/facts/{id}/history           version chain (+provenance)
 */
"use strict";

const memoryState = {
  status: "confirmed",
  q: "",
  entities: [],
  selected: null,         // entity id whose page is open (detail pane)
  editMode: false,        // the detail's Edit toggle
  editingFact: null,      // fact id being inline-edited
  addingFactFor: null,    // entity id with an open add-fact form
  addingMemoryFor: null,  // entity id with an open add-memory (note) form
  addingAliasFor: null,   // entity id with an open add-alias input
  drawerFact: null,       // fact id whose history is in the drawer
  undoFn: null,           // the pending Undo callback (destructive toast)
};

// Predicates that mark a person entity as one of the kids ("kids first").
const KID_PREDICATES = new Set(["grade", "class", "gender", "teacher"]);
const KID_LINE_ORDER = ["class", "grade", "gender", "teacher"];
// The one-step add-person fields (part B): each becomes a confirmed fact.
const PERSON_FIELDS = [
  ["relation", "memory-person-relation"],
  ["gender", "memory-person-gender"],
  ["grade", "memory-person-grade"],
  ["class", "memory-person-class"],
  ["school", "memory-person-school"],
  ["teacher", "memory-person-teacher"],
];
// Suggested predicates for the add-fact picker (no secrets — public facts).
const FACT_SUGGESTIONS = [
  "grade", "class", "school", "teacher", "gender", "relation",
  "activity", "phone-free only", "note",
];

function isKid(entity) {
  return entity.kind === "person"
    && (entity.facts || []).some((f) => KID_PREDICATES.has(f.predicate));
}

function kidFacts(entity) {
  const kids = entity.facts.filter((f) => KID_PREDICATES.has(f.predicate));
  return [...kids].sort(
    (a, b) => KID_LINE_ORDER.indexOf(a.predicate) - KID_LINE_ORDER.indexOf(b.predicate),
  );
}

function subLine(entity) {
  if (isKid(entity)) {
    return kidFacts(entity).map((f) => f.value).filter(Boolean).join(" · ");
  }
  const relation = (entity.facts || []).find((f) => f.predicate === "relation");
  if (relation) return relation.value;
  return entity.kind;
}

// "updated <local time>" — the newest fact stamp the overview carries.
function entityUpdated(entity) {
  let newest = entity.created_at;
  for (const fact of entity.facts || []) {
    if (fact.valid_from && (!newest || fact.valid_from > newest)) newest = fact.valid_from;
  }
  return newest ? new Date(newest).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function sectionOf(entity) {
  if (entity.name === "Family") return "Family"; // the Family unit (kind group) tops the list
  if (entity.kind === "person") return "People";
  if (entity.kind === "group") return "Groups";
  if (entity.kind === "place") return "Places";
  if (entity.kind === "org") return "Places";
  return "Channels";
}

const SECTION_ORDER = ["Family", "People", "Groups", "Places", "Channels"];

function selectedEntity() {
  return memoryState.entities.find((entity) => entity.id === memoryState.selected) || null;
}

let memoryRefreshSeq = 0;

function refreshMemory() {
  const seq = ++memoryRefreshSeq;
  const params = new URLSearchParams();
  // confirmed view: NO status param — the route drops fact-less entities
  // when an explicit status is given, and a freshly added person/group
  // must still be listed. The proposed inbox keeps its explicit filter.
  if (memoryState.status === "proposed") params.set("status", "proposed");
  if (memoryState.q) params.set("q", memoryState.q);
  return api(`/memory?${params}`).then((body) => {
    if (seq !== memoryRefreshSeq) return; // a newer refresh superseded this one
    memoryState.entities = body.entities || [];
    if (memoryState.editingFact === null && memoryState.addingFactFor === null) {
      renderMemory();
    }
  });
}

// ---------- undo toast (a destructive action's Undo) ----------

function undoToast(message, undoFn) {
  memoryState.undoFn = undoFn;
  $("memory-undo-text").textContent = message;
  $("memory-undo").hidden = false;
  clearTimeout(memoryState.undoTimer);
  memoryState.undoTimer = setTimeout(() => {
    $("memory-undo").hidden = true;
    memoryState.undoFn = null;
  }, 6000);
}

// ---------- left list ----------

function renderMemory() {
  const list = $("memory-list");
  const entities = [...memoryState.entities].sort(memoryOrder);
  const bySection = new Map();
  for (const entity of entities) {
    const section = sectionOf(entity);
    if (!bySection.has(section)) bySection.set(section, []);
    bySection.get(section).push(entity);
  }
  const nodes = [];
  for (const section of SECTION_ORDER) {
    const members = bySection.get(section) || [];
    if (!members.length) continue;
    nodes.push(el("div", { class: "mm-sect", text: section }));
    for (const entity of members) nodes.push(memoryRow(entity));
  }
  if (!nodes.length) {
    nodes.push(el("div", { class: "empty", text: memoryState.q
      ? `no entities match “${memoryState.q}”`
      : memoryState.status === "proposed"
        ? "inbox empty — nothing proposed"
        : "no memory yet — add a person" }));
  }
  list.replaceChildren(...nodes);
  renderDetail();
}

function memoryRow(entity) {
  const row = el("button", {
    class: `mm-row kind-${entity.kind}${entity.id === memoryState.selected ? " sel" : ""}`,
    "data-entity": entity.id, type: "button",
  });
  const top = el("span", { class: "mm-row-top" });
  top.append(
    el("span", { class: "mm-row-name", text: entity.name, dir: "auto" }),
    el("span", { class: "mm-row-upd", text: entityUpdated(entity) ? `updated ${entityUpdated(entity)}` : "" }),
  );
  row.append(top);
  const aliases = (entity.aliases || []).length ? ` · aka ${entity.aliases.join(", ")}` : "";
  row.append(el("span", { class: "mm-row-sub", text: `${subLine(entity)}${aliases}`, dir: "auto" }));
  row.addEventListener("click", () => {
    memoryState.selected = entity.id;
    memoryState.editMode = false;
    memoryState.editingFact = null;
    memoryState.addingFactFor = null;
    memoryState.addingMemoryFor = null;
    memoryState.addingAliasFor = null;
    $("memory-wrap").dataset.view = "detail";
    renderMemory();
  });
  return row;
}

function memoryOrder(a, b) {
  const rank = (entity) => {
    if (entity.name === "Family") return -1; // Family first, always
    return entity.kind === "person" ? (isKid(entity) ? 1 : 2)
      : entity.kind === "group" ? 3 : entity.kind === "org" || entity.kind === "place" ? 4 : 5;
  };
  if (rank(a) !== rank(b)) return rank(a) - rank(b);
  return a.name.localeCompare(b.name);
}

// ---------- right detail (the selected entity's page) ----------

function renderDetail() {
  const head = $("memory-detail-head");
  const body = $("memory-detail-body");
  const entity = selectedEntity();
  if (!entity) {
    head.replaceChildren();
    body.replaceChildren(el("div", {
      class: "empty",
      text: memoryState.status === "proposed"
        ? "pick a person to review their proposed memories"
        : "pick a person — their memories appear here",
    }));
    return;
  }
  head.replaceChildren(detailHeader(entity));
  const cards = [];
  for (const fact of entity.facts || []) {
    cards.push(memoryCard(entity, fact));
  }
  // add-fact / add-memory forms (open state renders inside the stack)
  if (memoryState.addingFactFor === entity.id) cards.push(memoryFactAddForm(entity));
  if (memoryState.addingMemoryFor === entity.id) cards.push(memoryNoteAddForm(entity));
  if (memoryState.addingAliasFor === entity.id) cards.push(aliasAddForm(entity));
  if (!cards.length) {
    cards.push(el("div", { class: "empty", text: "no memories yet — use + Add" }));
  }
  body.replaceChildren(...cards);
}

function detailHeader(entity) {
  const wrap = el("div", { class: "mm-dhead-wrap" });
  const main = el("div", { class: "mm-dhead-main" });
  main.append(el("div", { class: "mm-dname", text: entity.name, dir: "auto" }));
  const aliases = (entity.aliases || []).length
    ? ` · aka ${entity.aliases.join(", ")}` : "";
  main.append(el("div", { class: "mm-dsub", text: `${subLine(entity)}${aliases}`, dir: "auto" }));
  wrap.append(main);

  const actions = el("div", { class: "mm-dhead-actions" });
  const editBtn = el("button", { class: "btn btn-sm", text: memoryState.editMode ? "Done" : "Edit" });
  editBtn.addEventListener("click", () => {
    memoryState.editMode = !memoryState.editMode;
    memoryState.editingFact = null;
    memoryState.addingFactFor = null;
    memoryState.addingMemoryFor = null;
    memoryState.addingAliasFor = null;
    renderMemory();
  });
  actions.append(editBtn);
  const addBtn = el("button", { class: "btn btn-sm btn-pri", text: "+ Add" });
  addBtn.addEventListener("click", () => {
    const wantMemory = !memoryState.addingMemoryFor;
    memoryState.addingMemoryFor = wantMemory ? entity.id : null;
    memoryState.addingFactFor = null;
    memoryState.editingFact = null;
    renderMemory();
  });
  actions.append(addBtn);
  wrap.append(actions);

  // Edit mode: rename + alias chips + remove (archive with Undo).
  if (memoryState.editMode) {
    const tools = el("div", { class: "mm-tools" });
    const renameBtn = el("button", { class: "btn btn-sm btn-ghost", text: "✏️ rename", title: "rename" });
    renameBtn.addEventListener("click", () => {
      const input = el("input", { type: "text", value: entity.name, dir: "auto", class: "mm-rename" });
      const nameNode = wrap.querySelector(".mm-dname");
      nameNode.replaceWith(input);
      input.focus();
      const save = async () => {
        const name = input.value.trim();
        if (!name || name === entity.name) { input.replaceWith(nameNode); return; }
        try {
          await api(`/memory/entities/${entity.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          });
          toast("renamed");
        } catch (error) { toast(`rename failed: ${error.message}`); }
        refreshMemory();
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") save();
        if (event.key === "Escape") input.replaceWith(nameNode);
      });
      input.addEventListener("blur", save);
    });
    tools.append(renameBtn);
    for (const alias of entity.aliases || []) {
      const chip = el("span", { class: "mm-alias-chip", text: alias, dir: "auto" });
      const remove = el("button", { class: "icon", title: `remove alias ${alias}`, text: "✕" });
      remove.addEventListener("click", async () => {
        try {
          await api(`/memory/entities/${entity.id}/aliases/${encodeURIComponent(alias)}`, { method: "DELETE" });
          toast("alias removed");
        } catch (error) { toast(`remove failed: ${error.message}`); }
        refreshMemory();
      });
      chip.append(remove);
      tools.append(chip);
    }
    const addAlias = el("button", { class: "btn btn-sm btn-ghost", text: "+ alias" });
    addAlias.addEventListener("click", () => {
      memoryState.addingAliasFor = entity.id;
      renderMemory();
    });
    tools.append(addAlias);
    const removeBtn = el("button", { class: "btn btn-sm btn-ghost mm-remove", text: "🗑 Remove", title: "remove (archived — Undo restores it)" });
    removeBtn.addEventListener("click", () => {
      // a kid's removal has a consequence beyond this screen: triage and
      // A2 key on these facts to recognise the child — say so BEFORE the
      // second click, not just "Sure?".
      const kid = isKid(entity);
      const armedLabel = kid
        ? `Triage and A2 stop recognising ${entity.name} — sure?`
        : undefined;
      armConfirm(removeBtn, async () => {
        try {
          await api(`/memory/entities/${entity.id}/archive`, { method: "POST" });
          memoryState.selected = null;
          $("memory-wrap").dataset.view = "list";
          // the toast mirrors the consequence: the undo window is the only
          // place left that still names it once the confirm is gone.
          undoToast(kid
            ? `removed ${entity.name} — Triage and A2 stop recognising them until you Undo`
            : `removed ${entity.name} — items already captured stay`, async () => {
            try {
              await api(`/memory/entities/${entity.id}/restore`, { method: "POST" });
              memoryState.selected = entity.id;
              $("memory-wrap").dataset.view = "detail";
              toast("restored");
            } catch (error) { toast(`restore failed: ${error.message}`); }
            refreshMemory();
          });
        } catch (error) { toast(`remove failed: ${error.message}`); }
        refreshMemory();
      }, armedLabel);
    });
    tools.append(removeBtn);
    wrap.append(tools);
  }
  return wrap;
}

// A two-click destructive confirm: first click arms ("Sure?"), second acts.
function armConfirm(button, onConfirm, armedLabel = "Sure?") {
  if (button.dataset.armed) {
    onConfirm();
    return;
  }
  button.dataset.armed = "1";
  const original = button.textContent;
  button.textContent = armedLabel;
  button.classList.add("armed");
  setTimeout(() => {
    if (!button.isConnected) return;
    delete button.dataset.armed;
    button.textContent = original;
    button.classList.remove("armed");
  }, 2500);
}

// ---------- memory cards (one per fact) ----------

function factProvenanceLine(fact) {
  const provenance = fact.provenance || {};
  const bits = [];
  if (provenance.source === "item") bits.push("from WhatsApp");
  else if (provenance.source === "user") bits.push("added by you");
  else if (provenance.source) bits.push(provenance.source);
  if (fact.valid_from) {
    bits.push(new Date(fact.valid_from).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    }));
  }
  return bits.join(" · ");
}

function memoryCard(entity, fact) {
  const current = !fact.valid_to;
  const card = el("div", { class: `mm-card st-${fact.status}` });
  const textRow = el("div", { class: "mm-card-text" });
  const sentence = fact.predicate === "note"
    ? (fact.value ?? "")
    : `${fact.predicate}: ${fact.value ?? ""}`;
  textRow.append(el("span", { class: "mm-card-sentence", text: sentence, dir: "auto" }));
  if (fact.status === "proposed") {
    textRow.append(el("span", { class: "chip proposed", text: "proposed" }));
  }
  card.append(textRow);
  const meta = el("div", { class: "mm-card-meta" });
  meta.append(el("span", { text: factProvenanceLine(fact) }));
  const historyBtn = el("button", { class: "icon", title: "history + provenance", text: "🕘" });
  historyBtn.addEventListener("click", () => openMemoryDrawer(fact.id));
  meta.append(historyBtn);
  card.append(meta);

  const actions = el("div", { class: "mm-card-actions" });
  if (current && memoryState.status === "confirmed") {
    const editBtn = el("button", { class: "icon", title: "edit (creates a new version)", text: "✏️" });
    editBtn.addEventListener("click", () => {
      memoryState.editingFact = fact.id;
      memoryState.addingFactFor = null;
      memoryState.addingMemoryFor = null;
      renderMemory();
    });
    const deleteBtn = el("button", { class: "icon", title: "delete (soft; Undo reopens it)", text: "🗑" });
    deleteBtn.addEventListener("click", () => {
      armConfirm(deleteBtn, async () => {
        try {
          await api(`/memory/facts/${fact.id}`, { method: "DELETE" });
          undoToast("deleted — Undo reopens it", async () => {
            try {
              await api(`/memory/facts/${fact.id}/restore`, { method: "POST" });
              toast("restored");
            } catch (error) { toast(`restore failed: ${error.message}`); }
            refreshMemory();
          });
        } catch (error) { toast(`delete failed: ${error.message}`); }
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
    const rejectBtn = el("button", { class: "btn btn-ghost", title: "reject (soft delete; Undo reopens it)", text: "❌" });
    rejectBtn.addEventListener("click", () => {
      armConfirm(rejectBtn, async () => {
        try { await api(`/memory/facts/${fact.id}`, { method: "DELETE" }); }
        catch (error) { toast(`reject failed: ${error.message}`); }
        refreshMemory();
      });
    });
    actions.append(confirmBtn, rejectBtn);
  }
  if (actions.childNodes.length) card.append(actions);
  if (memoryState.editingFact === fact.id) card.append(memoryFactEditForm(fact));
  return card;
}

function memoryFactEditForm(fact) {
  const form = el("div", { class: "mm-form" });
  const predInput = el("input", { type: "text", value: fact.predicate, dir: "auto", class: "fpred" });
  const valInput = el("input", { type: "text", value: fact.value ?? "", dir: "auto", class: "fval" });
  const saveBtn = el("button", { class: "btn btn-sm", text: "Save" });
  const cancelBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Cancel" });
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
  const form = el("div", { class: "mm-form" });
  const predInput = el("input", { type: "text", placeholder: "predicate", dir: "auto", class: "fpred", list: "fact-suggestions" });
  const suggestions = el("datalist", { id: "fact-suggestions" });
  for (const suggestion of FACT_SUGGESTIONS) {
    suggestions.append(el("option", { value: suggestion }));
  }
  const valInput = el("input", { type: "text", placeholder: "value", dir: "auto", class: "fval" });
  const saveBtn = el("button", { class: "btn btn-sm", text: "Save" });
  const cancelBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Cancel" });
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
        body: JSON.stringify({
          subject_id: entity.id, predicate, value, status: "confirmed",
          provenance: { source: "user" },
        }),
      });
      memoryState.addingFactFor = null;
    } catch (error) { toast(`add failed: ${error.message}`); }
    refreshMemory();
  });
  form.append(suggestions, predInput, valInput, saveBtn, cancelBtn);
  return form;
}

function memoryNoteAddForm(entity) {
  const form = el("div", { class: "mm-form" });
  const input = el("input", { type: "text", placeholder: "one memory, one sentence…", dir: "auto", class: "fval" });
  const saveBtn = el("button", { class: "btn btn-sm", text: "Save" });
  const cancelBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancelBtn.addEventListener("click", () => {
    memoryState.addingMemoryFor = null;
    renderMemory();
  });
  saveBtn.addEventListener("click", async () => {
    const value = input.value.trim();
    if (!value) { toast("write the memory first"); return; }
    try {
      await api("/memory/facts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject_id: entity.id, predicate: "note", value, status: "confirmed",
          provenance: { source: "user" },
        }),
      });
      memoryState.addingMemoryFor = null;
    } catch (error) { toast(`add failed: ${error.message}`); }
    refreshMemory();
  });
  form.append(input, saveBtn, cancelBtn);
  return form;
}

function aliasAddForm(entity) {
  const form = el("div", { class: "mm-form" });
  const input = el("input", { type: "text", placeholder: "alias (Hebrew or English)", dir: "auto", class: "fval" });
  const saveBtn = el("button", { class: "btn btn-sm", text: "Save" });
  const cancelBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancelBtn.addEventListener("click", () => {
    memoryState.addingAliasFor = null;
    renderMemory();
  });
  saveBtn.addEventListener("click", async () => {
    const alias = input.value.trim();
    if (!alias) { toast("write the alias first"); return; }
    try {
      await api(`/memory/entities/${entity.id}/aliases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alias }),
      });
      memoryState.addingAliasFor = null;
      toast("alias added");
    } catch (error) { toast(`add failed: ${error.message}`); }
    refreshMemory();
  });
  form.append(input, saveBtn, cancelBtn);
  return form;
}

// ---------- fact history drawer (unchanged behavior) ----------

function factValidity(fact) {
  if (!fact.valid_to) return null;
  return el("span", {
    class: "mvalid",
    text: `${new Date(fact.valid_from).toLocaleString()} → ${new Date(fact.valid_to).toLocaleString()}`,
  });
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

// ---------- add-person (one step: entity + facts) ----------

async function addPersonFromForm() {
  const name = $("memory-person-name").value.trim();
  if (!name) { toast("name is required"); return; }
  const aliases = $("memory-person-aliases").value
    .split(",").map((alias) => alias.trim()).filter(Boolean);
  let entity;
  try {
    entity = await api("/memory/entities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "person", name, aliases }),
    });
  } catch (error) { toast(`add failed: ${error.message}`); return; }
  // no duplicates: if the API returned an entity that was already there,
  // say so and open it WITHOUT re-posting its facts — the modal is a
  // create form; re-adding facts it already has would pile up duplicates
  // (one extra grade fact per submit).
  const existed = memoryState.entities.some((candidate) => candidate.id === entity.id);
  const facts = [];
  for (const [predicate, inputId] of PERSON_FIELDS) {
    const value = $(inputId).value.trim();
    if (value) facts.push({ predicate, value });
  }
  if (!existed) {
    for (const { predicate, value } of facts) {
      try {
        await api("/memory/facts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            subject_id: entity.id, predicate, value, status: "confirmed",
            provenance: { source: "user" },
          }),
        });
      } catch (error) { toast(`fact ${predicate} failed: ${error.message}`); }
    }
  }
  $("memory-add-person").hidden = true;
  for (const [, inputId] of PERSON_FIELDS) $(inputId).value = "";
  $("memory-person-name").value = "";
  $("memory-person-aliases").value = "";
  toast(existed ? `${name} already existed — opened it` : `${name} added`);
  memoryState.selected = entity.id;
  $("memory-wrap").dataset.view = "detail";
  refreshMemory();
}

async function addOtherFromForm() {
  const kind = $("memory-other-kind").value;
  const name = $("memory-other-name").value.trim();
  if (!name) { toast("name is required"); return; }
  let entity;
  try {
    entity = await api("/memory/entities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, name }),
    });
  } catch (error) { toast(`add failed: ${error.message}`); return; }
  const existed = memoryState.entities.some((candidate) => candidate.id === entity.id);
  $("memory-add-other").hidden = true;
  $("memory-other-name").value = "";
  toast(existed ? `${name} already existed — opened it` : `${name} added`);
  memoryState.selected = entity.id;
  $("memory-wrap").dataset.view = "detail";
  refreshMemory();
}

// ---------- tab init ----------

function initMemoryTab() {
  $("memory-status").addEventListener("change", (event) => {
    memoryState.status = event.target.value;
    memoryState.editingFact = null;
    memoryState.addingFactFor = null;
    memoryState.addingMemoryFor = null;
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
  $("memory-add-person-btn").addEventListener("click", () => {
    $("memory-add-person").hidden = false;
    $("memory-person-name").focus();
  });
  $("memory-person-cancel").addEventListener("click", () => {
    $("memory-add-person").hidden = true;
  });
  $("memory-person-save").addEventListener("click", addPersonFromForm);
  $("memory-add-other-btn").addEventListener("click", () => {
    $("memory-add-other").hidden = false;
    $("memory-other-name").focus();
  });
  $("memory-other-cancel").addEventListener("click", () => {
    $("memory-add-other").hidden = true;
  });
  $("memory-other-save").addEventListener("click", addOtherFromForm);
  $("memory-back").addEventListener("click", () => {
    memoryState.selected = null;
    memoryState.editMode = false;
    $("memory-wrap").dataset.view = "list";
    renderMemory();
  });
  $("memory-undo-btn").addEventListener("click", async () => {
    $("memory-undo").hidden = true;
    const fn = memoryState.undoFn;
    memoryState.undoFn = null;
    if (fn) await fn();
  });
  $("memory-drawer-close").addEventListener("click", closeMemoryDrawer);
}

document.addEventListener("DOMContentLoaded", initMemoryTab);

// The single symbol the shared shell knows about.
window.MemoryTab = { refresh: refreshMemory };
