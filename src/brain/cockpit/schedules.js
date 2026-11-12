/* Cockpit Scheduled tab — recurring prompts run against the memory.
 *
 * Each task is a name + a free-text prompt (Hebrew or English: dir="auto")
 * + a schedule (presets: daily at HH:MM, weekly on <days> at HH:MM, or a
 * custom 5-field cron) in an IANA timezone. The Telegram bot runs due tasks
 * through the /ask pipeline and sends the answer to every allowed user.
 *
 * Cards show the schedule in words, Active/Paused, the next run and the last
 * result, with Edit / Pause|Resume / Run now / History / Delete. "Run now"
 * QUEUES a run: the bot picks it up on its next poll (within ~a minute).
 *
 * Depends only on app.js's globals (el, $, api, toast, ago) and exposes ONE
 * global: window.SchedulesTab.refresh(). All dynamic values render via
 * textContent / .value — never innerHTML.
 *
 * Data sources (same origin):
 *   GET    /schedules                list
 *   POST   /schedules                create (422 detail shown inline)
 *   PUT    /schedules/{id}           edit
 *   DELETE /schedules/{id}           delete (runs go with it)
 *   POST   /schedules/{id}/pause|resume|run
 *   GET    /schedules/{id}/runs      history (newest first)
 *   POST   /schedules/preview        live "in words + next run" hint
 */
"use strict";

const SC_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const SC_DEFAULT_TZ = "Asia/Jerusalem";

const schedState = {
  tasks: [],
  listKey: "",
  editing: null,    // null | { id: string|null, name, prompt, preset, time, days:Set, cron, timezone }
  error: "",
  preview: "",
  previewOk: true,
  busy: false,      // a save is in flight (editor buttons)
  acting: false,    // a card action is in flight (card buttons)
  runsFor: null,    // task id whose history is open
  runs: [],
  runsKey: "",
};

// ---------- schedule <-> presets ----------

function scPad(n) { return String(n).padStart(2, "0"); }

function scParse(cron) {
  // Inverse of scCron for the preset shapes; anything else stays custom.
  const parts = String(cron || "").trim().split(/\s+/);
  const out = { preset: "custom", time: "07:00", days: new Set([5]), cron: String(cron || "") };
  if (parts.length !== 5 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return out;
  const time = `${scPad(parts[1])}:${scPad(parts[0])}`;
  if (parts[2] !== "*" || parts[3] !== "*") return out;
  if (parts[4] === "*") return { ...out, preset: "daily", time };
  if (/^[0-6](,[0-6])*$/.test(parts[4])) {
    return { ...out, preset: "weekly", time, days: new Set(parts[4].split(",").map(Number)) };
  }
  return out;
}

function scCron(draft) {
  if (draft.preset === "custom") return draft.cron.trim();
  const [h, m] = (draft.time || "").split(":").map((x) => Number.parseInt(x, 10));
  const hour = Number.isFinite(h) ? h : 0;
  const minute = Number.isFinite(m) ? m : 0;
  if (draft.preset === "daily") return `${minute} ${hour} * * *`;
  const days = [...draft.days].sort((a, b) => a - b);
  return `${minute} ${hour} * * ${days.length ? days.join(",") : "?"}`;
}

function scWhen(iso, timezone) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("en-GB", {
      timeZone: timezone || SC_DEFAULT_TZ, weekday: "short", day: "2-digit",
      month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function scStatusBadge(status) {
  if (!status) return null;
  const cls = status === "ok" ? "ok" : status === "error" ? "down" : status === "running" ? "degraded" : "pr-muted";
  return el("span", { class: `badge ${cls}`, text: status });
}

// ---------- list ----------

function renderScheduleList() {
  const box = $("sched-list");
  if (!box) return;
  if (!schedState.tasks.length) {
    box.replaceChildren(el("div", { class: "debug-card empty", text: "No scheduled tasks yet — “+ New task” creates one." }));
    return;
  }
  box.replaceChildren(...schedState.tasks.map(renderScheduleCard));
}

function renderScheduleCard(task) {
  const preview = task.prompt.length > 180 ? `${task.prompt.slice(0, 180)}…` : task.prompt;
  const state = task.enabled
    ? el("span", { class: "badge ok", text: "Active" })
    : el("span", { class: "badge pr-muted", text: "Paused" });
  const next = task.enabled && task.next_run_at
    ? `Next run: ${scWhen(task.next_run_at, task.timezone)}`
    : "Paused — not scheduled";
  const last = task.last_run_at
    ? el("span", { class: "hint", title: task.last_run_at }, `Last run ${ago(task.last_run_at)} `, scStatusBadge(task.last_status))
    : el("span", { class: "hint", text: "Never run" });
  const btn = (label, action, extra = {}) => el("button", {
    class: "btn btn-sm", type: "button", "data-action": action, text: label, ...extra,
  });
  const edit = btn("Edit", "edit", { onclick: () => openEditor(task) });
  const toggle = btn(task.enabled ? "Pause" : "Resume", task.enabled ? "pause" : "resume", {
    onclick: () => scAction(task, task.enabled ? "pause" : "resume"),
  });
  const run = btn("Run now", "run", { onclick: () => scAction(task, "run") });
  run.disabled = task.run_requested || schedState.acting;
  const history = btn(schedState.runsFor === task.id ? "Hide history" : "History", "history", {
    onclick: () => toggleRuns(task.id),
  });
  const del = el("button", {
    class: "btn btn-sm btn-ghost", type: "button", "data-action": "delete", text: "Delete",
    onclick: () => deleteTask(task),
  });
  for (const node of [edit, toggle, history, del]) node.disabled = schedState.acting;
  const card = el("article", { class: "debug-card sc-card", "data-id": task.id },
    el("div", { class: "sc-head" },
      el("h3", { class: "sc-name", dir: "auto", text: task.name }),
      state,
    ),
    el("p", { class: "sc-prompt", dir: "auto", text: preview }),
    el("div", { class: "sc-meta" },
      el("span", { class: "sc-when", text: task.schedule_text }),
      el("span", { class: "hint", text: `· ${task.timezone}` }),
    ),
    el("div", { class: "sc-meta" },
      el("span", { class: "sc-next", text: next }),
      last,
    ),
    task.run_requested ? el("p", { class: "hint warn sc-queued", text: "Run queued — the bot picks it up within a minute." }) : null,
    el("div", { class: "pr-actions" }, edit, toggle, run, history, del),
  );
  if (schedState.runsFor === task.id) card.append(renderRuns(task));
  return card;
}

function renderRuns(task) {
  const wrap = el("div", { class: "pr-history sc-runs" }, el("h3", { text: "Run history" }));
  if (!schedState.runs.length) {
    wrap.append(el("div", { class: "hint", text: "no runs yet" }));
    return wrap;
  }
  for (const run of schedState.runs) {
    wrap.append(el("div", { class: "pr-hrow sc-run" },
      el("span", { class: "pr-hver", text: scWhen(run.started_at, task.timezone) }),
      el("span", { class: "pr-hsrc", text: run.trigger === "manual" ? "run now" : "scheduled" }),
      scStatusBadge(run.status),
    ));
    const body = run.output != null ? scRefs(run.output, run.cited_ids) : run.error;
    if (body) {
      wrap.append(el("pre", { class: `pr-preview sc-output${run.output == null ? " sc-err" : ""}`, dir: "auto", text: body }));
    }
  }
  return wrap;
}

function scRefs(text, cited) {
  // [<uuid>] citation markers -> [1], [2] … in cited order (raw ids mean nothing here)
  const order = new Map((cited || []).map((id, n) => [id, n + 1]));
  return String(text).replace(/\[([0-9a-fA-F-]{36})\]/g, (match, id) => (order.has(id) ? `[${order.get(id)}]` : match));
}

// ---------- editor ----------

function openEditor(task) {
  if (schedState.editing && !window.confirm("Discard the task you are editing?")) return;
  const parsed = scParse(task ? task.schedule : "0 7 * * 5");
  schedState.editing = {
    id: task ? task.id : null,
    name: task ? task.name : "",
    prompt: task ? task.prompt : "",
    timezone: task ? task.timezone : SC_DEFAULT_TZ,
    ...parsed,
  };
  schedState.error = "";
  schedState.preview = task ? task.schedule_text : "";
  schedState.previewOk = true;
  renderEditor();
  refreshPreview();
  const name = $("sched-name");
  if (name) name.focus();
}

function closeEditor() {
  schedState.editing = null;
  schedState.error = "";
  renderEditor();
}

function renderEditor() {
  const box = $("sched-editor");
  if (!box) return;
  const draft = schedState.editing;
  box.hidden = !draft;
  if (!draft) { box.replaceChildren(); return; }

  const field = (label, id, control) => el("div", { class: "sc-field" }, el("label", { for: id, text: label }), control);
  const name = el("input", { id: "sched-name", type: "text", dir: "auto", maxlength: "120", placeholder: "e.g. Romi's school mail" });
  name.value = draft.name;
  name.addEventListener("input", () => { draft.name = name.value; });
  const prompt = el("textarea", { id: "sched-prompt", class: "pr-text", dir: "auto", rows: "5", maxlength: "4000",
    placeholder: "What should the brain check and summarize? e.g. בדוק אם התקבלו מיילים חדשים 7 ימים אחרונים מבית הספר ותסכם" });
  prompt.value = draft.prompt;
  prompt.addEventListener("input", () => { draft.prompt = prompt.value; });

  const preset = el("select", { id: "sched-preset" },
    el("option", { value: "daily", text: "Daily" }),
    el("option", { value: "weekly", text: "Weekly" }),
    el("option", { value: "custom", text: "Custom (cron)" }));
  preset.value = draft.preset;
  preset.addEventListener("change", () => {
    if (preset.value === "custom" && draft.preset !== "custom") draft.cron = scCron(draft);
    draft.preset = preset.value;
    renderEditor();
    refreshPreview();
  });
  const time = el("input", { id: "sched-time", type: "time", step: "60" });
  time.value = draft.time;
  time.addEventListener("input", () => { draft.time = time.value; refreshPreview(); });
  const days = el("div", { class: "sc-days", role: "group", "aria-label": "Days of the week" },
    ...SC_DAYS.map((label, n) => {
      const box = el("input", { type: "checkbox", id: `sched-day-${n}`, value: String(n) });
      box.checked = draft.days.has(n);
      box.addEventListener("change", () => {
        if (box.checked) draft.days.add(n); else draft.days.delete(n);
        refreshPreview();
      });
      return el("label", { class: "checkbox", for: `sched-day-${n}` }, box, label);
    }));
  const cron = el("input", { id: "sched-cron", type: "text", dir: "ltr", spellcheck: "false", placeholder: "minute hour day-of-month month day-of-week, e.g. 0 7 * * 5" });
  cron.value = draft.cron;
  cron.addEventListener("input", () => { draft.cron = cron.value; refreshPreview(); });
  const tz = el("input", { id: "sched-tz", type: "text", dir: "ltr", spellcheck: "false", placeholder: SC_DEFAULT_TZ });
  tz.value = draft.timezone;
  tz.addEventListener("input", () => { draft.timezone = tz.value; refreshPreview(); });

  const rows = [field("Repeat", "sched-preset", preset)];
  if (draft.preset !== "custom") rows.push(field("At", "sched-time", time));
  if (draft.preset === "weekly") rows.push(el("div", { class: "sc-field" }, el("span", { class: "sc-label", text: "On" }), days));
  if (draft.preset === "custom") rows.push(field("Cron", "sched-cron", cron));
  rows.push(field("Timezone", "sched-tz", tz));

  const preview = el("p", { id: "sched-preview", class: `hint${schedState.previewOk ? "" : " warn"}`, role: "status", text: schedState.preview });
  const error = el("div", { id: "sched-error", class: "pr-error", role: "alert", text: schedState.error });
  error.hidden = !schedState.error;
  const save = el("button", { id: "sched-save", class: "btn btn-sm btn-primary", type: "button", text: draft.id ? "Save" : "Create", onclick: saveTask });
  const cancel = el("button", { id: "sched-cancel", class: "btn btn-sm", type: "button", text: "Cancel", onclick: closeEditor });
  save.disabled = schedState.busy;
  box.replaceChildren(
    el("h3", { text: draft.id ? "Edit task" : "New task" }),
    field("Name", "sched-name", name),
    field("Prompt", "sched-prompt", prompt),
    el("div", { class: "sc-row" }, ...rows),
    preview,
    error,
    el("div", { class: "pr-actions" }, save, cancel),
  );
}

let scPreviewTimer = null;
function refreshPreview() {
  clearTimeout(scPreviewTimer);
  scPreviewTimer = setTimeout(async () => {
    const draft = schedState.editing;
    if (!draft) return;
    const schedule = scCron(draft);
    const node = () => $("sched-preview");
    try {
      const result = await api("/schedules/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ schedule, timezone: draft.timezone.trim() || SC_DEFAULT_TZ }),
      });
      if (schedState.editing !== draft) return;
      schedState.preview = `${result.schedule_text} · next: ${scWhen(result.next_runs[0], draft.timezone.trim() || SC_DEFAULT_TZ)}`;
      schedState.previewOk = true;
    } catch (error) {
      if (schedState.editing !== draft) return;
      schedState.preview = error.message.replace(/^\d+: /, "");
      schedState.previewOk = false;
    }
    const target = node();
    if (target) {
      target.textContent = schedState.preview;
      target.classList.toggle("warn", !schedState.previewOk);
    }
  }, 250);
}

async function saveTask() {
  const draft = schedState.editing;
  if (!draft) return;
  const body = {
    name: draft.name,
    prompt: draft.prompt,
    schedule: scCron(draft),
    timezone: draft.timezone.trim() || SC_DEFAULT_TZ,
  };
  schedState.busy = true;
  renderEditor();
  try {
    await api(draft.id ? `/schedules/${encodeURIComponent(draft.id)}` : "/schedules", {
      method: draft.id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    schedState.editing = null;
    schedState.error = "";
    toast(draft.id ? "Task saved" : "Task created");
    await refreshScheduleList(true);
  } catch (error) {
    // 422 = the server rejected the schedule / timezone / text: keep the
    // draft and show why, right under the form.
    schedState.error = error.message;
  } finally {
    schedState.busy = false;
    renderEditor();
  }
}

// ---------- actions ----------

async function scAction(task, action) {
  schedState.acting = true;
  renderScheduleList();
  try {
    await api(`/schedules/${encodeURIComponent(task.id)}/${action}`, { method: "POST" });
    if (action === "run") toast("Run queued — the answer arrives in Telegram");
    await refreshScheduleList(true);
  } catch (error) {
    toast(`${action} failed — ${error.message}`);
  } finally {
    schedState.acting = false;
    renderScheduleList();
  }
}

async function deleteTask(task) {
  if (!window.confirm(`Delete “${task.name}” and its run history?`)) return;
  try {
    await api(`/schedules/${encodeURIComponent(task.id)}`, { method: "DELETE" });
    if (schedState.runsFor === task.id) schedState.runsFor = null;
    if (schedState.editing && schedState.editing.id === task.id) closeEditor();
    toast("Task deleted");
    await refreshScheduleList(true);
  } catch (error) {
    toast(`delete failed — ${error.message}`);
  }
}

async function toggleRuns(taskId) {
  if (schedState.runsFor === taskId) {
    schedState.runsFor = null;
    renderScheduleList();
    return;
  }
  schedState.runsFor = taskId;
  schedState.runs = [];
  schedState.runsKey = "";
  renderScheduleList();
  await loadRuns();
}

async function loadRuns() {
  const taskId = schedState.runsFor;
  if (!taskId) return;
  const payload = await api(`/schedules/${encodeURIComponent(taskId)}/runs?limit=20`);
  if (schedState.runsFor !== taskId) return;
  const key = JSON.stringify(payload.runs);
  if (key === schedState.runsKey) return;
  schedState.runsKey = key;
  schedState.runs = payload.runs;
  renderScheduleList();
}

async function refreshScheduleList(force = false) {
  const payload = await api("/schedules");
  const key = JSON.stringify(payload.tasks);
  if (force || key !== schedState.listKey) {
    schedState.listKey = key;
    schedState.tasks = payload.tasks;
    if (schedState.runsFor && !payload.tasks.some((task) => task.id === schedState.runsFor)) {
      schedState.runsFor = null;
    }
    renderScheduleList();
  }
  if (schedState.runsFor) await loadRuns();
}

function initSchedulesTab() {
  const button = $("sched-new");
  if (button) button.addEventListener("click", () => openEditor(null));
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initSchedulesTab);
else initSchedulesTab();

window.SchedulesTab = {
  // Called by app.js's refresh loop while the tab is open: cards (and an
  // open history) stay live; the editor is never re-rendered by a refresh.
  async refresh() {
    await refreshScheduleList();
  },
};
