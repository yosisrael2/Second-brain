/* Cockpit Prompts tab — see and edit the LLM system prompts + the memory file.
 *
 * LEFT: the memory file, then every registered prompt (changed-from-default
 * badge, current version). RIGHT: the editor for the selected key — a
 * textarea (dir="auto", so Hebrew/RTL text renders correctly), Save (the
 * server validates placeholders; its 422 detail is shown inline), Revert
 * (discard unsaved edits), Reset to default, and the version history with
 * View / Restore. Every save, reset and restore appends a version on the
 * server — nothing is ever overwritten.
 *
 * Depends only on app.js's globals (el, $, api, toast, ago) and exposes ONE
 * global: window.PromptsTab.refresh(). All dynamic values render via
 * textContent.
 *
 * Data sources (same origin):
 *   GET  /prompts                          list
 *   GET  /prompts/{key}                    current + default text, history
 *   PUT  /prompts/{key}                    save (422 = rejected, detail shown)
 *   POST /prompts/{key}/reset              back to the code default
 *   POST /prompts/{key}/restore/{version}  copy a version forward
 */
"use strict";

const MEMORY_FILE_KEY = "memory_file";

const promptsState = {
  list: [],
  listKey: "",       // last rendered list payload (re-render only on change)
  selected: MEMORY_FILE_KEY,
  detail: null,      // GET /prompts/{key} of the selected key
  draft: null,       // textarea value while it differs from detail.text
  error: "",         // last save error (422 detail), shown under the editor
  viewing: null,     // history version shown in the read-only preview
  busy: false,
};

function prDirty() {
  return promptsState.detail != null && promptsState.draft != null
    && promptsState.draft !== promptsState.detail.text;
}

function prBadge(entry) {
  if (entry.kind === "memory_file") {
    return entry.is_overridden
      ? el("span", { class: "badge ok", text: `v${entry.version}` })
      : el("span", { class: "badge pr-muted", text: "empty" });
  }
  return entry.is_overridden
    ? el("span", { class: "badge degraded", title: "changed from the code default", text: `changed · v${entry.version}` })
    : el("span", { class: "badge pr-muted", text: "default" });
}

function renderPromptList() {
  const box = $("prompts-list");
  if (!box) return;
  const memory = promptsState.list.filter((entry) => entry.kind === "memory_file");
  const prompts = promptsState.list.filter((entry) => entry.kind !== "memory_file");
  const row = (entry) => el("button", {
    class: `pr-item${entry.key === promptsState.selected ? " is-active" : ""}`,
    type: "button",
    "data-key": entry.key,
    onclick: () => selectPrompt(entry.key),
  },
  el("span", { class: "pr-item-title", text: entry.title }),
  prBadge(entry));
  box.replaceChildren(
    el("h3", { text: "Memory file" }),
    ...memory.map(row),
    el("h3", { text: "System prompts" }),
    ...prompts.map(row),
  );
}

function renderPromptEditor() {
  const box = $("prompts-editor");
  if (!box) return;
  const detail = promptsState.detail;
  if (!detail) {
    box.replaceChildren(el("div", { class: "empty", text: "loading…" }));
    return;
  }
  const isMemory = detail.kind === "memory_file";
  const text = promptsState.draft ?? detail.text;
  const counter = el("span", { class: "hint pr-count", text: `${text.length} / ${detail.max_chars} chars` });
  const unsaved = el("span", { class: "hint warn", text: "unsaved changes" });
  unsaved.hidden = !prDirty();
  const saveBtn = el("button", { class: "btn btn-sm btn-primary", type: "button", id: "prompt-save", text: "Save", onclick: savePrompt });
  const revertBtn = el("button", { class: "btn btn-sm", type: "button", id: "prompt-revert", text: "Revert edits", onclick: revertPrompt });
  saveBtn.disabled = !prDirty() || promptsState.busy;
  revertBtn.disabled = !prDirty() || promptsState.busy;
  const area = el("textarea", {
    id: "prompt-text",
    class: "pr-text",
    dir: "auto",
    spellcheck: "false",
    rows: isMemory ? "16" : "24",
    placeholder: isMemory ? "Standing instructions and context for every prompt (markdown). Empty = nothing is added." : "",
  });
  area.value = text;
  area.addEventListener("input", () => {
    promptsState.draft = area.value;
    promptsState.error = "";
    const dirty = prDirty();
    unsaved.hidden = !dirty;
    saveBtn.disabled = !dirty || promptsState.busy;
    revertBtn.disabled = !dirty || promptsState.busy;
    counter.textContent = `${area.value.length} / ${detail.max_chars} chars`;
    counter.classList.toggle("warn", area.value.length > detail.max_chars);
    errorBox.hidden = true;
  });
  const errorBox = el("div", { class: "pr-error", id: "prompt-error", role: "alert", text: promptsState.error });
  errorBox.hidden = !promptsState.error;

  const status = isMemory
    ? (detail.is_overridden ? `version ${detail.version}` : "empty — nothing is added to prompts")
    : (detail.is_overridden ? `changed from default · version ${detail.version}` : "code default");
  const header = el("div", { class: "pr-head" },
    el("h3", { text: detail.title }),
    prBadge(detail),
  );
  const placeholders = detail.placeholders.length
    ? el("p", { class: "hint" },
      "Placeholders the code fills in — keep all of them, add no others, write literal braces as {{ }}: ",
      ...detail.placeholders.map((name) => el("code", { class: "pr-ph", text: `{${name}}` })))
    : null;
  const resetBtn = el("button", {
    class: "btn btn-sm", type: "button", id: "prompt-reset",
    text: isMemory ? "Clear" : "Reset to default",
    onclick: resetPrompt,
  });
  resetBtn.disabled = !detail.is_overridden || promptsState.busy;

  box.replaceChildren(
    header,
    el("p", { class: "hint", text: detail.description }),
    el("p", { class: "hint", text: status }),
    placeholders,
    area,
    errorBox,
    el("div", { class: "pr-actions" }, saveBtn, revertBtn, resetBtn, unsaved, counter),
    renderPromptHistory(detail),
  );
}

function renderPromptHistory(detail) {
  const wrap = el("div", { class: "pr-history" }, el("h3", { text: "History" }));
  if (!detail.history.length) {
    wrap.append(el("div", { class: "hint", text: "no versions yet — this key has never been edited" }));
    return wrap;
  }
  const current = detail.history[0].version;
  for (const entry of detail.history) {
    const label = entry.is_default
      ? (detail.kind === "memory_file" ? "cleared" : "default")
      : entry.source;
    const viewBtn = el("button", {
      class: "btn btn-sm btn-ghost", type: "button",
      text: promptsState.viewing === entry.version ? "Hide" : "View",
      onclick: () => {
        promptsState.viewing = promptsState.viewing === entry.version ? null : entry.version;
        renderPromptEditor();
      },
    });
    const restoreBtn = entry.version === current ? el("span", { class: "hint", text: "current" }) : el("button", {
      class: "btn btn-sm", type: "button", "data-restore": String(entry.version),
      text: "Restore", onclick: () => restorePrompt(entry.version),
    });
    if (restoreBtn.tagName === "BUTTON") restoreBtn.disabled = promptsState.busy;
    wrap.append(el("div", { class: "pr-hrow" },
      el("span", { class: "pr-hver", text: `v${entry.version}` }),
      el("span", { class: "pr-hsrc", text: label }),
      el("span", { class: "hint", title: entry.created_at, text: ago(entry.created_at) }),
      el("span", { class: "pr-hgrow" }),
      viewBtn,
      restoreBtn,
    ));
    if (promptsState.viewing === entry.version) {
      wrap.append(el("pre", { class: "pr-preview", dir: "auto", text: entry.text || "(empty)" }));
    }
  }
  return wrap;
}

async function loadPromptDetail(key) {
  const detail = await api(`/prompts/${encodeURIComponent(key)}`);
  if (promptsState.selected !== key) return;  // the user moved on meanwhile
  promptsState.detail = detail;
  renderPromptEditor();
}

function selectPrompt(key) {
  if (key === promptsState.selected && promptsState.detail) return;
  if (prDirty() && !window.confirm("Discard your unsaved changes?")) return;
  promptsState.selected = key;
  promptsState.detail = null;
  promptsState.draft = null;
  promptsState.error = "";
  promptsState.viewing = null;
  renderPromptList();
  renderPromptEditor();
  loadPromptDetail(key).catch((error) => toast(`load failed — ${error.message}`));
}

async function prMutate(request) {
  promptsState.busy = true;
  renderPromptEditor();
  try {
    const detail = await request();
    promptsState.detail = detail;
    promptsState.draft = null;
    promptsState.error = "";
    promptsState.viewing = null;
    await refreshPromptList(true);
  } catch (error) {
    // 422 = the server rejected the text (placeholders, braces, length):
    // keep the draft and show why, right under the editor.
    promptsState.error = error.message;
  } finally {
    promptsState.busy = false;
    renderPromptEditor();
  }
}

function savePrompt() {
  if (!prDirty()) return;
  const key = promptsState.selected;
  const text = promptsState.draft;
  return prMutate(() => api(`/prompts/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }));
}

function revertPrompt() {
  promptsState.draft = null;
  promptsState.error = "";
  renderPromptEditor();
}

function resetPrompt() {
  const memory = promptsState.selected === MEMORY_FILE_KEY;
  const question = memory
    ? "Clear the memory file? (the old text stays in the history)"
    : "Reset this prompt to the code default? (your versions stay in the history)";
  if (!window.confirm(question)) return;
  const key = promptsState.selected;
  return prMutate(() => api(`/prompts/${encodeURIComponent(key)}/reset`, { method: "POST" }));
}

function restorePrompt(version) {
  if (prDirty() && !window.confirm("Discard your unsaved changes and restore this version?")) return;
  const key = promptsState.selected;
  return prMutate(() => api(`/prompts/${encodeURIComponent(key)}/restore/${version}`, { method: "POST" }));
}

async function refreshPromptList(force = false) {
  const payload = await api("/prompts");
  const key = JSON.stringify(payload.prompts);
  if (!force && key === promptsState.listKey) return;
  promptsState.listKey = key;
  promptsState.list = payload.prompts;
  renderPromptList();
}

window.PromptsTab = {
  // Called by app.js's refresh loop while the tab is open: the list stays
  // live; the editor only loads once per selection, so a refresh never
  // clobbers what the user is typing.
  async refresh() {
    await refreshPromptList();
    if (!promptsState.detail) await loadPromptDetail(promptsState.selected);
  },
};
