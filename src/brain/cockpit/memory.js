/* Cockpit Memory tab — the redesigned Memory screen.
 *
 * Three views over the same data:
 *   profile  LEFT = Household / Other people / Groups / Places; RIGHT = the
 *            selected entity's profile: Basic info (typed fields, one value
 *            each) + Connections (typed, two-way) beside the Memories feed
 *            (free text that can mention several people, with a category).
 *   review   every PROPOSED fact (picked up from chats) waits here until it
 *            is confirmed, edited first, or rejected — nothing is saved on
 *            its own.
 *   map      the family map: solid lines = family connections, dashed =
 *            group membership, dotted amber = people with no connection yet.
 *
 * Data model (no new tables — all facts on /memory):
 *   basic info   facts with a typed predicate (relation, birthday, gender,
 *                work, phone, email, grade, class, school, teacher, ...)
 *   memories     facts whose predicate is a category: note / routine /
 *                preference / date
 *   connections  'connection:<type>' + object_id ("subject is object's
 *                <type>"); the reverse side is derived here, so one fact
 *                shows on both profiles
 *   groups       channel entities; applies_to + channel_kind label them by
 *                meaning ("Itamar · parents' chat")
 *
 * Depends only on app.js's globals (el, $, api, toast, shortId,
 * switchTab, openInspector) and exposes ONE global:
 * window.MemoryTab.refresh(). All dynamic values render via textContent.
 *
 * Data sources (same origin, localhost only):
 *   GET    /memory                              overview (confirmed facts)
 *   GET    /memory?status=proposed              the review inbox
 *   POST   /memory/entities                     add entity (+aliases)
 *   PATCH  /memory/entities/{id}                rename
 *   POST   /memory/entities/{id}/aliases        add alias
 *   DELETE /memory/entities/{id}/aliases/{a}    remove alias
 *   POST   /memory/entities/{id}/archive        soft-remove
 *   POST   /memory/entities/{id}/restore        Undo the archive
 *   POST   /memory/facts                        add fact (source=user)
 *   PATCH  /memory/facts/{id}                   edit = new version
 *   DELETE /memory/facts/{id}                   soft delete (history kept)
 *   POST   /memory/facts/{id}/restore           Undo the soft delete
 *   POST   /memory/facts/{id}/confirm           proposed -> confirmed
 *   GET    /memory/facts/{id}/history           version chain (+provenance)
 */
"use strict";

const memoryState = {
  mode: "profile",        // profile | review | map
  q: "",                  // sidebar search (client-side)
  entities: [],           // GET /memory (confirmed facts)
  proposed: [],           // GET /memory?status=proposed (entities with proposals)
  online: true,
  dataKey: "",            // last rendered payload — re-render only on change
  selected: null,         // entity id whose profile is open
  editMode: false,        // the profile header's Edit toggle (rename/remove)
  editingField: null,     // basic-info predicate being edited inline
  addingAlias: false,
  addingConnection: false,
  editingFact: null,      // memory fact id being edited inline
  menuFact: null,         // memory fact id whose actions row is open
  promoting: null,        // memory fact id with the "move to basic info" form
  memoryFilter: "all",
  reviewFilter: "all",
  reviewEditing: null,    // proposed fact id being edited before confirm
  showAllGroups: false,
  drawerFact: null,
  undoFn: null,
  undoStack: [],          // pending undo toasts, newest last
  drafts: {},             // in-progress inline input values, keyed per form —
                          // re-renders (filter chips, menus, Edit) rebuild the
                          // DOM and would otherwise wipe half-written text
};

// Inline drafts survive re-renders: forms seed from the store, inputs
// update it on every keystroke, saves/cancels clear their key.
function mmDraft(key, fallback = "") {
  return Object.prototype.hasOwnProperty.call(memoryState.drafts, key)
    ? memoryState.drafts[key]
    : fallback;
}
function mmTrackDraft(input, key) {
  input.addEventListener("input", () => { memoryState.drafts[key] = input.value; });
  return input;
}
function mmClearDraft(key) { delete memoryState.drafts[key]; }

// Free-text memory categories (the predicate IS the category).
const MEMORY_CATEGORIES = ["routine", "preference", "date", "note"];
const CATEGORY_LABEL = {
  routine: "Routine", preference: "Preference", date: "Date", note: "Note",
};
const CATEGORY_PLURAL = {
  routine: "Routines", preference: "Preferences", date: "Dates", note: "Notes",
};
const CONNECTION_PREFIX = "connection:";
// type -> labels (gendered by the entity the label describes) + the reverse.
const CONNECTION_TYPES = {
  spouse: { label: "Spouse", m: "Husband", f: "Wife", inverse: "spouse", of: "Married to" },
  parent: { label: "Parent", m: "Dad", f: "Mom", inverse: "child" },
  child: { label: "Child", m: "Son", f: "Daughter", inverse: "parent" },
  sibling: { label: "Sibling", m: "Brother", f: "Sister", inverse: "sibling" },
  grandparent: { label: "Grandparent", m: "Grandpa", f: "Grandma", inverse: "grandchild" },
  grandchild: { label: "Grandchild", m: "Grandson", f: "Granddaughter", inverse: "grandparent" },
  friend: { label: "Friend", inverse: "friend" },
  teacher: { label: "Teacher", inverse: "student" },
  student: { label: "Student", inverse: "teacher" },
  coach: { label: "Coach", inverse: "trainee" },
  trainee: { label: "Trainee", inverse: "coach" },
  member: { label: "Member", inverse: "group" },
  group: { label: "Group", inverse: "member" },
  other: { label: "Connected", inverse: "other", of: "Connected to" },
};
// What "+ Add connection" offers (the rest are reverses).
const CONNECTION_CHOICES = [
  "spouse", "parent", "child", "sibling", "grandparent", "grandchild",
  "friend", "teacher", "student", "coach", "member", "other",
];
const FAMILY_TYPES = new Set(["spouse", "parent", "child", "sibling", "grandparent", "grandchild"]);
// Basic info: one row per field, in this order. Kid fields show for kids.
const PERSON_BASIC = [
  ["relation", "Role"], ["birthday", "Birthday"], ["gender", "Gender"],
  ["aliases", "Also known as"], ["work", "Work"], ["phone", "Phone"], ["email", "Email"],
];
const KID_BASIC = [["grade", "Grade"], ["class", "Class"], ["school", "School"], ["teacher", "Teacher"]];
const CHANNEL_BASIC = [["applies_to", "For"], ["channel_kind", "Kind"], ["aliases", "Also known as"]];
const CHANNEL_KINDS = ["parents", "kids", "teacher", "committee", "club", "community"];
const CHANNEL_KIND_LABEL = {
  parents: "parents' chat", kids: "kids' chat", teacher: "teacher's channel",
  committee: "class committee", club: "club", community: "community",
};
// Kids are recognised by school facts (gender alone is not enough: adults have one).
const KID_PREDICATES = new Set(["grade", "class", "teacher"]);
const PARENT_ROLE_RE = /\b(father|mother|dad|mom|parent)\b/i;
const FAMILY_ROLE_RE = /\b(father|mother|dad|mom|parent|wife|husband|spouse)\b/i;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const AVATAR_TONES = 6;
const GROUPS_SHOWN = 6;
// The one-step add-person fields: each becomes a confirmed fact.
const PERSON_FIELDS = [
  ["relation", "memory-person-relation"],
  ["gender", "memory-person-gender"],
  ["birthday", "memory-person-birthday"],
  ["grade", "memory-person-grade"],
  ["class", "memory-person-class"],
  ["school", "memory-person-school"],
  ["teacher", "memory-person-teacher"],
];

const SVG_NS = "http://www.w3.org/2000/svg";

// ---------- small model helpers ----------

function factsOf(entity, predicate) {
  const wanted = predicate.toLowerCase();
  return (entity.facts || []).filter((fact) => fact.predicate.toLowerCase() === wanted);
}

function factValue(entity, predicate) {
  const fact = factsOf(entity, predicate).find((candidate) => candidate.value);
  return fact ? fact.value : null;
}

function isKid(entity) {
  return Boolean(entity) && entity.kind === "person"
    && (entity.facts || []).some((fact) => KID_PREDICATES.has(fact.predicate.toLowerCase()));
}

function isMemoryFact(fact) {
  return MEMORY_CATEGORIES.includes(fact.predicate.toLowerCase());
}

function isConnectionFact(fact) {
  return fact.predicate.startsWith(CONNECTION_PREFIX);
}

function genderOf(entity) {
  const value = (entity && factValue(entity, "gender")) || "";
  if (/^(boy|man|male|m)$/i.test(value.trim())) return "m";
  if (/^(girl|woman|female|f)$/i.test(value.trim())) return "f";
  return null;
}

function typeLabel(type, entity) {
  const spec = CONNECTION_TYPES[type];
  if (!spec) return type;
  const gender = genderOf(entity);
  return (gender === "m" && spec.m) || (gender === "f" && spec.f) || spec.label;
}

function inverseType(type) {
  return (CONNECTION_TYPES[type] || {}).inverse || type;
}

function capitalize(text) {
  const value = String(text ?? "");
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

function initialOf(name) {
  const first = [...String(name || "?").trim()][0] || "?";
  return first.toUpperCase();
}

function toneOf(id) {
  let hash = 0;
  for (const char of String(id)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash % AVATAR_TONES;
}

function shortDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

function gradeText(value) {
  const grade = String(value).trim();
  const number = Number.parseInt(grade, 10);
  if (String(number) !== grade) return `grade ${grade}`;
  const suffix = number % 100 >= 11 && number % 100 <= 13 ? "th"
    : ({ 1: "st", 2: "nd", 3: "rd" })[number % 10] || "th";
  return `${number}${suffix} grade`;
}

// "05.12.1985" / "1985-12-05" / "5.12" -> {day, month, year|null}
function parseBirthday(raw) {
  const text = String(raw ?? "").trim();
  let day; let month; let year = null;
  let match = text.match(/^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?$/);
  if (match) {
    day = Number(match[1]); month = Number(match[2]);
    if (match[3]) {
      year = Number(match[3]);
      if (year < 100) year += year > 30 ? 1900 : 2000;
    }
  } else if ((match = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) {
    year = Number(match[1]); month = Number(match[2]); day = Number(match[3]);
  } else {
    return null;
  }
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return { day, month, year };
}

// "5 Dec 1985 · 40 · turns 41 in 41 days" — the form people use.
function birthdayText(raw) {
  const birthday = parseBirthday(raw);
  if (!birthday) return String(raw);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const label = `${birthday.day} ${MONTHS[birthday.month - 1]}${birthday.year ? ` ${birthday.year}` : ""}`;
  let next = new Date(today.getFullYear(), birthday.month - 1, birthday.day);
  if (next < today) next = new Date(today.getFullYear() + 1, birthday.month - 1, birthday.day);
  const days = Math.round((next - today) / 86400000);
  const when = days === 0 ? "today" : `in ${days} day${days === 1 ? "" : "s"}`;
  if (!birthday.year) return `${label} · ${days === 0 ? "birthday today" : `birthday ${when}`}`;
  const turning = next.getFullYear() - birthday.year;
  const age = days === 0 ? turning : turning - 1;
  return `${label} · ${age} · turns ${turning} ${when}`;
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// ---------- the derived model (rebuilt per render) ----------

let mm = null;

function buildModel() {
  const entities = memoryState.entities;
  const byId = new Map(entities.map((entity) => [entity.id, entity]));
  const links = [];   // {from, to, type, fact|null} — "from is to's <type>"
  for (const entity of entities) {
    for (const fact of entity.facts || []) {
      if (isConnectionFact(fact) && fact.object_id && byId.has(fact.object_id)) {
        links.push({
          from: entity.id, to: fact.object_id,
          type: fact.predicate.slice(CONNECTION_PREFIX.length), fact,
        });
      }
    }
  }
  const kids = entities.filter(isKid);
  // A "father"/"mother" role is a parent of the household's kids unless an
  // explicit connection between the two already says something.
  for (const entity of entities) {
    if (entity.kind !== "person" || isKid(entity)) continue;
    if (!PARENT_ROLE_RE.test(factValue(entity, "relation") || "")) continue;
    for (const kid of kids) {
      const linked = links.some((link) => (link.from === entity.id && link.to === kid.id)
        || (link.from === kid.id && link.to === entity.id));
      if (!linked) links.push({ from: entity.id, to: kid.id, type: "parent", fact: null });
    }
  }
  const proposedBySubject = new Map();
  for (const entity of memoryState.proposed) {
    if ((entity.facts || []).length) proposedBySubject.set(entity.id, entity.facts);
  }
  // Mention tokens: names + aliases of people/groups (not channels: their
  // names are long chat titles), longest first so "Rona C." beats "Rona".
  const tokens = [];
  for (const entity of entities) {
    if (entity.kind === "channel" || entity.name === "me") continue;
    for (const token of new Set([entity.name, ...(entity.aliases || [])])) {
      if (token && token.trim().length >= 2) tokens.push({ token: token.trim(), entity });
    }
  }
  tokens.sort((a, b) => b.token.length - a.token.length);
  const mentionRes = tokens.map(({ token, entity }) => ({
    entity,
    re: new RegExp(`(?<![\\p{L}\\p{N}])${escapeRegExp(token)}(?![\\p{L}\\p{N}])`, "giu"),
  }));
  return { byId, links, kids, proposedBySubject, mentionRes };
}

function isHousehold(entity) {
  if (!entity || entity.kind !== "person") return false;
  if (isKid(entity) || entity.name === "me") return true;
  if (FAMILY_ROLE_RE.test(factValue(entity, "relation") || "")) return true;
  return mm.links.some((link) => FAMILY_TYPES.has(link.type)
    && (link.from === entity.id || link.to === entity.id)
    && isKid(mm.byId.get(link.from === entity.id ? link.to : link.from)));
}

function channelsFor(entity) {
  return memoryState.entities.filter((candidate) => candidate.kind === "channel"
    && factsOf(candidate, "applies_to").some((fact) => fact.value === entity.name));
}

// [{start, end, entity}] — non-overlapping mention spans in a memory text.
function findMentions(text) {
  const spans = [];
  const source = String(text ?? "");
  for (const { entity, re } of mm.mentionRes) {
    re.lastIndex = 0;
    for (const match of source.matchAll(re)) {
      const start = match.index;
      const end = start + match[0].length;
      if (spans.some((span) => start < span.end && end > span.start)) continue;
      spans.push({ start, end, entity });
    }
  }
  return spans.sort((a, b) => a.start - b.start);
}

function mentionedIds(text) {
  return new Set(findMentions(text).map((span) => span.entity.id));
}

// Connection rows for one profile: [{other, label, link|null, group}]
function connectionsOf(entity) {
  const rows = [];
  for (const link of mm.links) {
    if (link.from === entity.id) {
      const other = mm.byId.get(link.to);
      rows.push({ other, label: typeLabel(inverseType(link.type), other), link, myType: link.type });
    } else if (link.to === entity.id) {
      const other = mm.byId.get(link.from);
      rows.push({ other, label: typeLabel(link.type, other), link, myType: inverseType(link.type) });
    }
  }
  if (entity.kind === "person") {
    for (const channel of channelsFor(entity)) {
      rows.push({ other: channel, label: "Group", link: null, group: true });
    }
  }
  if (entity.kind === "channel") {
    for (const fact of factsOf(entity, "applies_to")) {
      const target = memoryState.entities.find((candidate) => candidate.kind === "person"
        && candidate.name === fact.value);
      if (target) rows.push({ other: target, label: "For", link: null });
    }
  }
  return rows;
}

// "Dad of Itamar and Rona · Married to Zohar · in 5 groups"
function connectionSummary(entity, rows) {
  const byRole = new Map();
  let groups = 0;
  for (const row of rows) {
    if (row.group) { groups += 1; continue; }
    if (!row.myType) continue;
    const spec = CONNECTION_TYPES[row.myType];
    const role = spec && spec.of ? spec.of : `${typeLabel(row.myType, entity)} of`;
    if (!byRole.has(role)) byRole.set(role, []);
    byRole.get(role).push(row.other.name);
  }
  const parts = [];
  for (const [role, names] of byRole) {
    const joined = names.length > 1
      ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0];
    parts.push(`${role} ${joined}`);
  }
  if (groups) parts.push(`in ${groups} group${groups === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

// The sidebar's sub-line: role/class for people, meaning for groups.
function subLine(entity) {
  if (entity.kind === "channel") {
    const appliesTo = factValue(entity, "applies_to");
    const kind = factValue(entity, "channel_kind");
    const who = appliesTo === "family" ? "Family" : appliesTo === "me" ? "You" : appliesTo;
    const what = CHANNEL_KIND_LABEL[kind] || kind;
    return [who, what].filter(Boolean).join(" · ") || "WhatsApp group";
  }
  if (entity.kind !== "person") return capitalize(entity.kind);
  if (isKid(entity)) {
    const parentLink = mm.links.find((link) => link.to === entity.id && link.type === "parent");
    const role = parentLink ? typeLabel("child", entity) : null;
    const grade = factValue(entity, "grade");
    return [role, grade ? gradeText(grade) : null, factValue(entity, "class")]
      .filter(Boolean).join(" · ");
  }
  const relation = factValue(entity, "relation");
  let role = null;
  if (relation) {
    role = relation.length <= 24 ? capitalize(relation)
      : /mother|mom/i.test(relation) ? "Mom" : /father|dad/i.test(relation) ? "Dad" : capitalize(relation);
  } else {
    const row = connectionsOf(entity).find((candidate) => !candidate.group);
    if (row) role = `${typeLabel(row.myType, entity)} of ${row.other.name}`;
  }
  const work = factValue(entity, "work");
  const alias = (entity.aliases || []).find((candidate) => candidate.toLowerCase() !== entity.name.toLowerCase());
  const extra = work ? `works at ${work}` : alias ? `aka ${alias}` : null;
  return [role, extra].filter(Boolean).join(" · ");
}

function hasNoConnection(entity) {
  return entity.kind === "person" && !factValue(entity, "relation")
    && !mm.links.some((link) => link.from === entity.id || link.to === entity.id);
}

function selectedEntity() {
  return memoryState.entities.find((entity) => entity.id === memoryState.selected) || null;
}

function proposedCount() {
  let count = 0;
  for (const facts of mm.proposedBySubject.values()) count += facts.length;
  return count;
}

// ---------- shared bits of UI ----------

function avatar(entity, size = "md") {
  if (entity.kind !== "person") {
    const box = el("span", { class: `mm-av mm-av-${size} mm-av-group`, "aria-hidden": "true" });
    box.append(groupIcon());
    return box;
  }
  const photo = photoFactOf(entity);
  if (photo) {
    // ?v=<sha> busts the browser cache: every photo shares ONE URL and GET
    // sends max-age=3600 — without v, a Change (or an undo) would keep
    // showing the OLD bytes for up to an hour. The server ignores v.
    const img = el("img", {
      class: `mm-av mm-av-${size} mm-av-photo`,
      src: `/memory/entities/${entity.id}/photo?v=${photo.value}`,
      alt: entity.name, loading: "lazy",
    });
    // a failed image load (no file on disk, a removed fact, a fetch error)
    // falls back to the initials avatar — the initials are never gone.
    img.addEventListener("error", () => img.replaceWith(initialsAvatar(entity, size)));
    return img;
  }
  return initialsAvatar(entity, size);
}

function initialsAvatar(entity, size) {
  return el("span", {
    class: `mm-av mm-av-${size} tone-${toneOf(entity.id)}`, "aria-hidden": "true",
    text: initialOf(entity.name),
  });
}

// The current 'photo' fact of an entity (single-valued: at most one), or
// undefined. The value is the sha256; the bytes ride the local file store.
function photoFactOf(entity) {
  return (entity.facts || []).find((fact) => fact.predicate.toLowerCase() === "photo" && fact.value);
}

// Upload/Change/Remove for a person's avatar (the profile header). Upload
// is a POST with the picked file (multipart); the SERVER supersedes — its
// response carries `replaced`, and the Undo is ONE /undo-supersede call.
// Remove is the soft DELETE + Undo via /restore on the fact id the entity
// payload already carried (deleteFactWithUndo's pattern, no arm-confirm).
function photoControls(entity) {
  const photo = photoFactOf(entity);
  const wrap = el("div", { class: "mm-photo-controls" });
  const file = el("input", {
    type: "file", accept: "image/jpeg,image/png,image/webp",
    "aria-label": "photo file",
  });
  file.hidden = true;
  const pick = el("button", {
    type: "button", class: "btn mm-photo-btn",
    text: photo ? "Change photo" : "Upload photo",
  });
  pick.addEventListener("click", () => file.click());
  file.addEventListener("change", async () => {
    const picked = file.files && file.files[0];
    file.value = ""; // re-picking the SAME file must still fire change
    if (!picked) return;
    const body = new FormData();
    body.append("file", picked);
    try {
      const made = await api(`/memory/entities/${entity.id}/photo`, { method: "POST", body });
      if (made && (made.replaced || []).length) {
        undoToast("photo updated — Undo brings the previous one back", async () => {
          try {
            await api(`/memory/facts/${made.id}/undo-supersede`, { method: "POST" });
            toast("the previous photo is back");
          } catch (error) { toast(`undo failed: ${error.message}`); }
          reloadMemory();
        });
      } else {
        toast("photo saved");
      }
    } catch (error) {
      toast(`photo upload failed: ${error.message}`);
    }
    reloadMemory();
  });
  wrap.append(file, pick);
  if (photo) {
    const remove = el("button", {
      type: "button", class: "btn mm-photo-remove", text: "Remove",
      "data-photo-remove": "1",
      title: "remove the photo — Undo restores it",
    });
    remove.addEventListener("click", async () => {
      const factId = photo.id;
      try {
        await api(`/memory/entities/${entity.id}/photo`, { method: "DELETE" });
        undoToast("photo removed — Undo restores it", async () => {
          try {
            await api(`/memory/facts/${factId}/restore`, { method: "POST" });
            toast("restored");
          } catch (error) { toast(`restore failed: ${error.message}`); }
          reloadMemory();
        });
      } catch (error) {
        toast(`remove failed: ${error.message}`);
      }
      reloadMemory();
    });
    wrap.append(remove);
  }
  return wrap;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}

function groupIcon() {
  const svg = svgEl("svg", {
    width: "18", height: "18", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
  });
  svg.append(
    svgEl("circle", { cx: "9", cy: "8", r: "3" }),
    svgEl("circle", { cx: "17", cy: "9", r: "2.5" }),
    svgEl("path", { d: "M3 19c0-3 3-5 6-5s6 2 6 5M15 14.5c3 0 6 1.5 6 4.5" }),
  );
  return svg;
}

function bidi(text) {
  return el("bdi", { text: text ?? "" });
}

// A tappable chip naming an entity (opens its profile).
function entityChip(entity) {
  const chip = el("button", {
    type: "button", class: `mm-mention tone-${toneOf(entity.id)}${entity.kind !== "person" ? " is-group" : ""}`,
    title: `open ${entity.name}`,
  });
  chip.append(bidi(entity.name));
  chip.addEventListener("click", (event) => {
    event.stopPropagation();
    openProfile(entity.id);
  });
  return chip;
}

// Memory text with mentioned people rendered as chips.
function mentionText(text) {
  const node = el("p", { class: "mm-memtext", dir: "auto" });
  const source = String(text ?? "");
  let cursor = 0;
  for (const span of findMentions(source)) {
    if (span.start > cursor) node.append(document.createTextNode(source.slice(cursor, span.start)));
    node.append(entityChip(span.entity));
    cursor = span.end;
  }
  if (cursor < source.length) node.append(document.createTextNode(source.slice(cursor)));
  return node;
}

// "From <chat> · confirmed by you · Oct 23" (nodes: chat names stay bidi-safe).
function sourceLine(fact) {
  const provenance = fact.provenance || {};
  const line = el("span", { class: "mm-source" });
  const bits = [];
  if (provenance.source === "item") {
    if (provenance.channel) {
      const from = el("span", { text: "From " });
      from.append(bidi(provenance.channel));
      bits.push(from);
    } else {
      const count = (provenance.item_ids || []).length;
      bits.push(el("span", { text: count > 1 ? `Learned from ${count} messages` : "Learned from a message" }));
    }
    if (fact.status === "confirmed") bits.push(el("span", { text: "confirmed by you" }));
  } else {
    bits.push(el("span", { text: "Added by you" }));
  }
  if (fact.valid_from) bits.push(el("span", { text: shortDate(fact.valid_from) }));
  bits.forEach((bit, index) => {
    if (index) line.append(document.createTextNode(" · "));
    line.append(bit);
  });
  return line;
}

function openSourceItem(fact) {
  const itemIds = (fact.provenance || {}).item_ids || [];
  if (!itemIds.length) return null;
  const button = el("button", { type: "button", class: "mm-linkbtn", text: "Show message" });
  button.addEventListener("click", () => {
    switchTab("debug");
    openInspector(itemIds[0]);
  });
  return button;
}

// ---------- refresh ----------

let memoryRefreshSeq = 0;

function memoryBusy() {
  // Only an ACTIVE focus blocks the poll's re-render (mid-typing caret
  // protection). Forms that are open but blurred re-render — their typed
  // text survives via the drafts store (mmTrackDraft on every form input).
  const active = document.activeElement;
  const panel = $("tab-memory");
  return Boolean(active && panel && panel.contains(active)
    && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName) && active.id !== "memory-q");
}

// The 5s poll re-renders only when the data changed and nobody is typing
// (a re-render would wipe a half-written memory). Actions pass force.
function refreshMemory({ force = false } = {}) {
  const seq = ++memoryRefreshSeq;
  return Promise.all([api("/memory"), api("/memory?status=proposed")]).then(([all, proposed]) => {
    if (seq !== memoryRefreshSeq) return; // a newer refresh superseded this one
    memoryState.online = true;
    const key = JSON.stringify([all.entities, proposed.entities]);
    const changed = key !== memoryState.dataKey;
    memoryState.entities = all.entities || [];
    memoryState.proposed = proposed.entities || [];
    // Stamp the seen-key ONLY when the render really runs: a poll that
    // lands while the user types must not mark the fresh payload as seen,
    // or the next poll (user idle) sees "no change" and the screen stays
    // stale. renderSync (below) keeps the sync line + review count current.
    if (force || (changed && !memoryBusy())) {
      memoryState.dataKey = key;
      renderMemory();
    } else {
      renderSync();
    }
  }, (error) => {
    memoryState.online = false;
    renderSync();
    throw error;
  });
}

function reloadMemory() {
  return refreshMemory({ force: true }).catch((error) => toast(`refresh failed: ${error.message}`));
}

async function postFact(body) {
  return api("/memory/facts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "confirmed", ...body }),
  });
}

async function patchFact(factId, body) {
  return api(`/memory/facts/${factId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---------- undo toast (a destructive action's Undo) ----------

// Undo toasts stack (last 3): a second destructive action must not erase
// the first one's Undo — the button pops the latest, then the previous.
function undoToast(message, undoFn) {
  memoryState.undoStack.push({ message, undoFn });
  if (memoryState.undoStack.length > 3) memoryState.undoStack.shift();
  showUndoTop();
  clearTimeout(memoryState.undoTimer);
  memoryState.undoTimer = setTimeout(() => {
    memoryState.undoStack = [];
    showUndoTop();
  }, 6000);
}

function showUndoTop() {
  const top = memoryState.undoStack[memoryState.undoStack.length - 1];
  if (!top) {
    $("memory-undo").hidden = true;
    memoryState.undoFn = null;
    return;
  }
  $("memory-undo-text").textContent = memoryState.undoStack.length > 1
    ? `${top.message} (+${memoryState.undoStack.length - 1} more)`
    : top.message;
  $("memory-undo").hidden = false;
  memoryState.undoFn = top.undoFn;
}

// Soft-delete a fact with Undo (reopens the same row).
async function deleteFactWithUndo(fact, message) {
  try {
    await api(`/memory/facts/${fact.id}`, { method: "DELETE" });
    undoToast(message, async () => {
      try {
        await api(`/memory/facts/${fact.id}/restore`, { method: "POST" });
        toast("restored");
      } catch (error) { toast(`restore failed: ${error.message}`); }
      reloadMemory();
    });
  } catch (error) { toast(`delete failed: ${error.message}`); }
  reloadMemory();
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

// ---------- render: dispatch ----------

function resetInlineState() {
  memoryState.editMode = false;
  memoryState.editingField = null;
  memoryState.addingAlias = false;
  memoryState.addingConnection = false;
  memoryState.editingFact = null;
  memoryState.menuFact = null;
  memoryState.promoting = null;
  memoryState.memoryFilter = "all";
}

function setMode(mode) {
  memoryState.mode = mode;
  memoryState.reviewEditing = null;
  renderMemory();
  window.scrollTo({ top: 0 });
}

function openProfile(entityId) {
  memoryState.selected = entityId;
  resetInlineState();
  memoryState.mode = "profile";
  $("memory-wrap").dataset.view = "detail";
  renderMemory();
}

function renderMemory() {
  mm = buildModel();
  const mode = memoryState.mode;
  $("memory-wrap").hidden = mode !== "profile";
  $("memory-review").hidden = mode !== "review";
  $("memory-map").hidden = mode !== "map";
  $("memory-view-list").setAttribute("aria-pressed", String(mode === "profile"));
  $("memory-view-map").setAttribute("aria-pressed", String(mode === "map"));
  renderSync();
  if (mode === "review") renderReview();
  else if (mode === "map") renderMap();
  else {
    // desktop opens on a profile; the phone layout starts on the list
    if (!selectedEntity() && window.matchMedia("(min-width: 721px)").matches) {
      const first = sortedEntities().find(isHousehold) || sortedEntities()[0];
      memoryState.selected = first ? first.id : null;
    }
    renderList();
    renderDetail();
  }
}

function renderSync() {
  const channels = memoryState.entities.filter((entity) => entity.kind === "channel").length;
  const sync = $("memory-sync");
  sync.replaceChildren(
    el("span", { class: `dot ${memoryState.online ? "ok" : "degraded"}`, "aria-hidden": "true" }),
    el("span", {
      text: memoryState.online
        ? `Synced · listening to ${channels} group${channels === 1 ? "" : "s"}`
        : "Can't reach the server — retrying",
    }),
  );
  const count = mm ? proposedCount() : 0;
  $("memory-review-count").textContent = String(count);
  $("memory-review-btn").classList.toggle("has-items", count > 0);
  $("memory-review-btn").setAttribute("aria-label", `Review, ${count} waiting`);
}

// ---------- left list ----------

function sortedEntities() {
  return [...memoryState.entities].sort((a, b) => {
    const rank = (entity) => (entity.name === "Family" ? -1
      : entity.kind === "person" ? (isKid(entity) ? 1 : 0)
        : entity.kind === "channel" || entity.kind === "group" ? 3 : 4);
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    return a.name.localeCompare(b.name);
  });
}

function matchesSearch(entity, needle) {
  if (!needle) return true;
  const haystack = [entity.name, ...(entity.aliases || []), subLine(entity),
    ...(entity.facts || []).map((fact) => fact.value || "")].join("\n").toLowerCase();
  return haystack.includes(needle);
}

function sectionOf(entity) {
  if (entity.name === "Family" || isHousehold(entity)) return "Household";
  if (entity.kind === "person") return "Other people";
  if (entity.kind === "channel" || entity.kind === "group") return "Groups";
  return "Places & orgs";
}

const SECTION_ORDER = ["Household", "Other people", "Groups", "Places & orgs"];

function renderList() {
  const needle = memoryState.q.toLowerCase();
  const bySection = new Map();
  for (const entity of sortedEntities()) {
    if (!matchesSearch(entity, needle)) continue;
    const section = sectionOf(entity);
    if (!bySection.has(section)) bySection.set(section, []);
    bySection.get(section).push(entity);
  }
  if (bySection.has("Groups")) {
    // groups by who they are for, then name
    bySection.get("Groups").sort((a, b) => (factValue(a, "applies_to") || "~")
      .localeCompare(factValue(b, "applies_to") || "~") || a.name.localeCompare(b.name));
  }
  const nodes = [];
  for (const section of SECTION_ORDER) {
    let members = bySection.get(section) || [];
    if (!members.length) continue;
    const block = el("div", { class: "mm-sect" });
    const head = el("div", { class: "mm-sect-head" });
    head.append(el("span", { text: section }), el("span", { text: String(members.length) }));
    block.append(head);
    const collapse = section === "Groups" && !needle && !memoryState.showAllGroups
      && members.length > GROUPS_SHOWN + 1;
    const hidden = collapse ? members.length - GROUPS_SHOWN : 0;
    if (collapse) members = members.slice(0, GROUPS_SHOWN);
    for (const entity of members) block.append(memoryRow(entity));
    if (hidden) {
      const more = el("button", { type: "button", class: "mm-more", text: `Show ${hidden} more` });
      more.addEventListener("click", () => {
        memoryState.showAllGroups = true;
        renderList();
      });
      block.append(more);
    }
    nodes.push(block);
  }
  if (!nodes.length) {
    nodes.push(el("div", {
      class: "empty",
      text: needle ? `nothing matches “${memoryState.q}”` : "no memory yet — add a person",
    }));
  }
  $("memory-list").replaceChildren(...nodes);
}

function memoryRow(entity) {
  const selected = entity.id === memoryState.selected;
  const row = el("button", {
    type: "button", class: `mm-row kind-${entity.kind}${selected ? " sel" : ""}`,
    "data-entity": entity.id,
  });
  if (selected) row.setAttribute("aria-current", "true");
  row.append(avatar(entity));
  const text = el("span", { class: "mm-row-text" });
  text.append(el("span", { class: "mm-row-name", text: entity.name, dir: "auto" }));
  const unknown = hasNoConnection(entity);
  const sub = unknown ? "Relation unknown" : subLine(entity);
  text.append(el("span", { class: `mm-row-sub${unknown ? " is-unknown" : ""}`, text: sub, dir: "auto" }));
  row.append(text);
  const waiting = (mm.proposedBySubject.get(entity.id) || []).length;
  if (waiting) {
    row.append(el("span", { class: "mm-row-dot", title: `${waiting} to review`, "aria-label": `${waiting} to review` }));
  }
  row.addEventListener("click", () => openProfile(entity.id));
  return row;
}

// ---------- right: the profile ----------

function renderDetail() {
  const head = $("memory-detail-head");
  const body = $("memory-detail-body");
  const entity = selectedEntity();
  if (!entity) {
    head.replaceChildren();
    body.replaceChildren(el("div", { class: "empty", text: "pick a person — their profile appears here" }));
    return;
  }
  head.replaceChildren(profileHeader(entity));
  const nodes = [];
  const waiting = (mm.proposedBySubject.get(entity.id) || []).length;
  if (waiting) nodes.push(reviewBanner(entity, waiting));
  const columns = el("div", { class: "mm-columns" });
  const left = el("div", { class: "mm-col-left" });
  left.append(basicInfoCard(entity), connectionsCard(entity));
  columns.append(left, memoriesCard(entity));
  nodes.push(columns);
  body.replaceChildren(...nodes);
}

function profileHeader(entity) {
  const card = el("section", { class: "mm-card mm-profile" });
  if (entity.kind === "person") {
    const photoBox = el("div", { class: "mm-profile-photo" });
    photoBox.append(avatar(entity, "lg"), photoControls(entity));
    card.append(photoBox);
  } else {
    card.append(avatar(entity, "lg"));
  }
  const main = el("div", { class: "mm-profile-main" });
  const titleRow = el("div", { class: "mm-profile-title" });
  if (memoryState.editMode) {
    const input = el("input", {
      type: "text", value: entity.name, dir: "auto", class: "mm-rename", "aria-label": "name",
    });
    let saved = false;
    const save = async () => {
      const name = input.value.trim();
      if (saved) return; // Enter AND change both land here — one PATCH only
      if (!name || name === entity.name) return;
      saved = true;
      try {
        await api(`/memory/entities/${entity.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        toast("renamed");
      } catch (error) {
        saved = false; // a failed rename must stay retryable
        toast(`rename failed: ${error.message}`);
      }
      reloadMemory();
    };
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") save(); });
    input.addEventListener("change", save);
    titleRow.append(input);
  } else {
    titleRow.append(el("h2", { class: "mm-dname", text: entity.name, dir: "auto" }));
  }
  const aliases = (entity.aliases || []).filter((alias) => alias.toLowerCase() !== entity.name.toLowerCase());
  if (aliases.length) {
    const also = el("span", { class: "mm-also", text: "also " });
    aliases.forEach((alias, index) => {
      if (index) also.append(document.createTextNode(", "));
      also.append(bidi(alias));
    });
    titleRow.append(also);
  }
  main.append(titleRow);
  const rows = connectionsOf(entity);
  const summary = entity.kind === "channel" ? subLine(entity) : connectionSummary(entity, rows);
  if (summary) main.append(el("p", { class: "mm-dsub", text: summary, dir: "auto" }));
  card.append(main);

  const actions = el("div", { class: "mm-profile-actions" });
  const editBtn = el("button", {
    type: "button", class: "btn", text: memoryState.editMode ? "Done" : "Edit",
  });
  editBtn.addEventListener("click", () => {
    memoryState.editMode = !memoryState.editMode;
    renderDetail();
  });
  actions.append(editBtn);
  if (memoryState.editMode) {
    const removeBtn = el("button", {
      type: "button", class: "btn mm-remove", text: "Remove",
      title: "remove (archived — Undo restores it)",
    });
    removeBtn.addEventListener("click", () => {
      // a kid's removal has a consequence beyond this screen: triage and
      // A2 key on these facts to recognise the child — say so BEFORE the
      // second click, not just "Sure?".
      const kid = isKid(entity);
      const armedLabel = kid ? `Triage and A2 stop recognising ${entity.name} — sure?` : undefined;
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
            reloadMemory();
          });
        } catch (error) { toast(`remove failed: ${error.message}`); }
        reloadMemory();
      }, armedLabel);
    });
    actions.append(removeBtn);
  } else {
    const addBtn = el("button", { type: "button", class: "btn btn-pri", text: "+ Add memory" });
    addBtn.addEventListener("click", () => {
      const input = $("memory-new");
      if (input) {
        input.scrollIntoView({ block: "center", behavior: "smooth" });
        input.focus();
      }
    });
    actions.append(addBtn);
  }
  card.append(actions);
  return card;
}

function reviewBanner(entity, waiting) {
  const banner = el("section", { class: "mm-banner" });
  const text = el("span", { class: "mm-banner-text" });
  text.append(
    el("b", { text: `${waiting} new thing${waiting === 1 ? "" : "s"} about ${entity.name}` }),
    document.createTextNode(" picked up from your chats. Nothing is saved until you confirm it."),
  );
  const go = el("button", { type: "button", class: "btn btn-sm mm-banner-btn", text: "Review" });
  go.addEventListener("click", () => setMode("review"));
  banner.append(text, go);
  return banner;
}

// ---------- basic info ----------

function basicFieldsFor(entity) {
  if (entity.kind === "channel") return CHANNEL_BASIC;
  if (entity.kind !== "person") return [["aliases", "Also known as"]];
  const fields = [...PERSON_BASIC];
  if (isKid(entity)) fields.splice(3, 0, ...KID_BASIC);
  return fields;
}

function basicInfoCard(entity) {
  const card = el("section", { class: "mm-card", "aria-labelledby": "mm-basic-h" });
  const head = el("div", { class: "mm-card-head" });
  head.append(el("h3", { id: "mm-basic-h", text: "Basic info" }));
  card.append(head);
  card.append(el("p", { class: "mm-card-hint", text: "Stable facts, one value each." }));
  const list = el("dl", { class: "mm-dl" });
  const fields = basicFieldsFor(entity);
  const known = new Set(fields.map(([predicate]) => predicate));
  for (const [predicate, label] of fields) {
    list.append(el("dt", { text: label }), basicValue(entity, predicate, label));
  }
  // any other typed fact this entity carries (e.g. 'activity') still shows
  // — except 'photo': the avatar renders it; a row here would print the sha
  const extra = new Map();
  for (const fact of entity.facts || []) {
    const predicate = fact.predicate.toLowerCase();
    if (predicate === "photo") continue;
    if (known.has(predicate) || isMemoryFact(fact) || isConnectionFact(fact)) continue;
    extra.set(predicate, fact.predicate);
  }
  for (const [predicate, original] of extra) {
    list.append(el("dt", { text: capitalize(original) }), basicValue(entity, predicate, capitalize(original)));
  }
  card.append(list);
  const stamps = (entity.facts || []).filter((fact) => fact.predicate.toLowerCase() !== "photo"
    && !isMemoryFact(fact) && !isConnectionFact(fact))
    .map((fact) => fact.valid_from).filter(Boolean).sort();
  if (stamps.length) {
    const last = new Date(stamps[stamps.length - 1]);
    card.append(el("p", {
      class: "mm-card-foot",
      text: `Last edited ${last.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`,
    }));
  }
  return card;
}

function displayValue(predicate, value) {
  if (predicate === "birthday") return birthdayText(value);
  if (predicate === "grade") return gradeText(value);
  if (predicate === "gender" || predicate === "relation") return capitalize(value);
  if (predicate === "channel_kind") return capitalize(CHANNEL_KIND_LABEL[value] || value);
  if (predicate === "applies_to") return value === "family" ? "The whole family" : value === "me" ? "You" : value;
  return value;
}

function basicValue(entity, predicate, label) {
  const dd = el("dd", {});
  if (predicate === "aliases") return aliasValue(entity, dd);
  const facts = factsOf(entity, predicate);
  if (memoryState.editingField === predicate) {
    dd.append(basicEditor(entity, predicate, label, facts[0] || null));
    return dd;
  }
  if (!facts.length) {
    const add = el("button", { type: "button", class: "mm-linkbtn", text: "+ Add", "aria-label": `add ${label}` });
    add.addEventListener("click", () => {
      memoryState.editingField = predicate;
      renderDetail();
    });
    dd.append(add);
    return dd;
  }
  const value = el("span", { class: "mm-dd-value" });
  facts.forEach((fact, index) => {
    if (index) value.append(document.createTextNode(", "));
    value.append(el("span", { text: displayValue(predicate, fact.value ?? ""), dir: "auto" }));
  });
  dd.append(value);
  const tools = el("span", { class: "mm-dd-tools" });
  const edit = el("button", { type: "button", class: "mm-icon", text: "Edit", "aria-label": `edit ${label}` });
  edit.addEventListener("click", () => {
    memoryState.editingField = predicate;
    renderDetail();
  });
  const history = el("button", { type: "button", class: "mm-icon", text: "History", "aria-label": `${label} history` });
  history.addEventListener("click", () => openMemoryDrawer(facts[0].id));
  tools.append(edit, history);
  dd.append(tools);
  return dd;
}

function basicEditor(entity, predicate, label, fact) {
  const form = el("span", { class: "mm-form" });
  const draftKey = `field:${entity.id}:${predicate}`;
  let input;
  if (predicate === "channel_kind" || predicate === "applies_to" || predicate === "gender") {
    input = el("select", { "aria-label": label });
    const options = predicate === "channel_kind" ? [...CHANNEL_KINDS]
      : predicate === "gender" ? ["girl", "boy", "woman", "man"]
        : [...mm.kids.map((kid) => kid.name), "family", "me"];
    if (fact && fact.value && !options.includes(fact.value)) options.unshift(fact.value);
    for (const option of options) {
      const node = el("option", { value: option, text: displayValue(predicate, option) });
      if (fact && fact.value === option) node.selected = true;
      input.append(node);
    }
    if (mmDraft(draftKey) && input.querySelector(`option[value="${CSS.escape(mmDraft(draftKey))}"]`)) {
      input.value = mmDraft(draftKey);
    }
  } else {
    input = el("input", {
      type: "text", value: mmDraft(draftKey, fact ? fact.value ?? "" : ""), dir: "auto", "aria-label": label,
      placeholder: predicate === "birthday" ? "dd.mm.yyyy" : label,
    });
  }
  mmTrackDraft(input, draftKey);
  const save = el("button", { type: "button", class: "btn btn-sm btn-pri", text: "Save" });
  const cancel = el("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel" });
  const close = () => {
    memoryState.editingField = null;
    mmClearDraft(draftKey);
    renderDetail();
  };
  cancel.addEventListener("click", close);
  const submit = async () => {
    const value = input.value.trim();
    if (!value) { toast(`${label} must not be empty`); return; }
    if (fact && value === fact.value) { close(); return; }
    try {
      if (fact) {
        await patchFact(fact.id, { value });
      } else {
        const made = await postFact({ subject_id: entity.id, predicate, value });
        const replaced = (made && made.replaced) || [];
        if (replaced.length) {
          undoToast(`replaced the previous ${label} — Undo brings it back`, async () => {
            // One server op (the replaced row is history, not deleted):
            // /undo-supersede closes the new row and reopens the old one.
            if (made && made.id) {
              try {
                await api(`/memory/facts/${made.id}/undo-supersede`, { method: "POST" });
                toast("the previous value is back");
              } catch (error) { toast(`undo failed: ${error.message}`); }
            }
            reloadMemory();
          });
        }
      }
      memoryState.editingField = null;
      mmClearDraft(draftKey);
      toast(`${label} saved`);
    } catch (error) { toast(`save failed: ${error.message}`); }
    reloadMemory();
  };
  save.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
    if (event.key === "Escape") close();
  });
  form.append(input, save, cancel);
  if (fact) {
    const remove = el("button", { type: "button", class: "btn btn-sm btn-ghost mm-remove", text: "Clear" });
    remove.addEventListener("click", () => {
      memoryState.editingField = null;
      deleteFactWithUndo(fact, `${label} cleared — Undo reopens it`);
    });
    form.append(remove);
  }
  setTimeout(() => input.focus(), 0);
  return form;
}

function aliasValue(entity, dd) {
  const chips = el("span", { class: "mm-chips" });
  for (const alias of entity.aliases || []) {
    const chip = el("span", { class: "mm-chip" });
    chip.append(bidi(alias));
    const remove = el("button", { type: "button", class: "mm-chip-x", text: "×", "aria-label": `remove alias ${alias}` });
    remove.addEventListener("click", async () => {
      try {
        await api(`/memory/entities/${entity.id}/aliases/${encodeURIComponent(alias)}`, { method: "DELETE" });
        toast("alias removed");
      } catch (error) { toast(`remove failed: ${error.message}`); }
      reloadMemory();
    });
    chip.append(remove);
    chips.append(chip);
  }
  if (memoryState.addingAlias) {
    chips.append(aliasAddForm(entity));
  } else {
    const add = el("button", { type: "button", class: "mm-linkbtn", text: "+ Add", "aria-label": "add alias" });
    add.addEventListener("click", () => {
      memoryState.addingAlias = true;
      renderDetail();
    });
    chips.append(add);
  }
  dd.append(chips);
  return dd;
}

function aliasAddForm(entity) {
  const form = el("span", { class: "mm-form" });
  const draftKey = `alias:${entity.id}`;
  const input = el("input", { type: "text", placeholder: "alias (Hebrew or English)", dir: "auto", "aria-label": "alias" });
  input.value = mmDraft(draftKey);
  mmTrackDraft(input, draftKey);
  const save = el("button", { type: "button", class: "btn btn-sm btn-pri", text: "Save" });
  const cancel = el("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancel.addEventListener("click", () => {
    memoryState.addingAlias = false;
    mmClearDraft(draftKey);
    renderDetail();
  });
  const submit = async () => {
    const alias = input.value.trim();
    if (!alias) { toast("write the alias first"); return; }
    try {
      await api(`/memory/entities/${entity.id}/aliases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alias }),
      });
      memoryState.addingAlias = false;
      mmClearDraft(draftKey);
      toast("alias added");
    } catch (error) { toast(`add failed: ${error.message}`); }
    reloadMemory();
  };
  save.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
    if (event.key === "Escape") cancel.click();
  });
  form.append(input, save, cancel);
  setTimeout(() => input.focus(), 0);
  return form;
}

// ---------- connections ----------

function connectionsCard(entity) {
  const card = el("section", { class: "mm-card", "aria-labelledby": "mm-conn-h" });
  const head = el("div", { class: "mm-card-head" });
  head.append(el("h3", { id: "mm-conn-h", text: "Connections" }));
  const mapLink = el("button", { type: "button", class: "mm-linkbtn", text: "Open family map" });
  mapLink.addEventListener("click", () => setMode("map"));
  head.append(mapLink);
  card.append(head);
  const rows = connectionsOf(entity);
  const list = el("div", { class: "mm-conn-list" });
  for (const row of rows) list.append(connectionRow(row));
  if (!rows.length) list.append(el("p", { class: "mm-card-hint", text: `No connections yet. How is ${entity.name} connected?` }));
  card.append(list);
  if (memoryState.addingConnection) {
    card.append(connectionAddForm(entity));
  } else {
    const add = el("button", { type: "button", class: "mm-dashed", text: "+ Add connection" });
    add.addEventListener("click", () => {
      memoryState.addingConnection = true;
      renderDetail();
    });
    card.append(add);
  }
  card.append(el("p", { class: "mm-card-foot", text: "Connections are two-way: they show on both profiles." }));
  return card;
}

function connectionRow(row) {
  const wrap = el("div", { class: "mm-conn" });
  const open = el("button", { type: "button", class: "mm-conn-open" });
  open.append(avatar(row.other, "sm"));
  open.append(el("span", { class: "mm-conn-name", text: row.other.name, dir: "auto" }));
  const label = row.group ? subLine(row.other) || "Group" : row.label;
  open.append(el("span", { class: "mm-conn-label", text: label, dir: "auto" }));
  open.addEventListener("click", () => openProfile(row.other.id));
  wrap.append(open);
  if (row.link && row.link.fact) {
    const remove = el("button", {
      type: "button", class: "mm-icon", text: "×", "aria-label": `remove connection with ${row.other.name}`,
    });
    remove.addEventListener("click", () => deleteFactWithUndo(row.link.fact, "connection removed — Undo restores it"));
    wrap.append(remove);
  } else if (row.link) {
    wrap.append(el("span", { class: "mm-conn-from", text: "from role", title: "derived from the Role field" }));
  }
  return wrap;
}

function connectionAddForm(entity) {
  const form = el("div", { class: "mm-form mm-conn-form" });
  const typeKey = `conn:${entity.id}:type`;
  const targetKey = `conn:${entity.id}:target`;
  const lead = el("span", { class: "mm-form-lead", text: `${entity.name} is the` });
  const typeSelect = el("select", { "aria-label": "connection type" });
  for (const type of CONNECTION_CHOICES) {
    typeSelect.append(el("option", { value: type, text: CONNECTION_TYPES[type].label.toLowerCase() }));
  }
  if (mmDraft(typeKey)) typeSelect.value = mmDraft(typeKey);
  typeSelect.addEventListener("change", () => { memoryState.drafts[typeKey] = typeSelect.value; });
  const ofLead = el("span", { class: "mm-form-lead", text: "of" });
  // "other" is not a noun: the sentence must read "A is connected to B",
  // not "A is the connected of B".
  const updateLead = () => {
    const isOther = typeSelect.value === "other";
    lead.textContent = isOther ? `${entity.name} is connected to` : `${entity.name} is the`;
    ofLead.hidden = isOther;
  };
  typeSelect.addEventListener("change", updateLead);
  form.append(lead, typeSelect, ofLead);
  updateLead();
  const targetSelect = el("select", { "aria-label": "connected to" });
  for (const candidate of sortedEntities()) {
    if (candidate.id === entity.id || candidate.kind === "channel") continue;
    targetSelect.append(el("option", { value: candidate.id, text: candidate.name }));
  }
  if (mmDraft(targetKey) && targetSelect.querySelector(`option[value="${mmDraft(targetKey)}"]`)) {
    targetSelect.value = mmDraft(targetKey);
  }
  targetSelect.addEventListener("change", () => { memoryState.drafts[targetKey] = targetSelect.value; });
  form.append(targetSelect);
  const save = el("button", { type: "button", class: "btn btn-sm btn-pri", text: "Save" });
  const cancel = el("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancel.addEventListener("click", () => {
    memoryState.addingConnection = false;
    mmClearDraft(typeKey);
    mmClearDraft(targetKey);
    renderDetail();
  });
  save.addEventListener("click", async () => {
    if (!targetSelect.value) { toast("pick who they are connected to"); return; }
    // A link that already exists would just be the same fact twice — in
    // EITHER storage direction: stored here (from===me, type===selected) or
    // stored on the other profile (from===them, type===the inverse of
    // selected). An inverse-typed link stored HERE (them is my <inverse>)
    // is a contradiction, not a duplicate — not this guard's call.
    const selected = typeSelect.value;
    const isDuplicate = (link) => Boolean(link) && (
      (link.from === entity.id && link.to === targetSelect.value && link.type === selected)
      || (link.from === targetSelect.value && link.to === entity.id
        && link.type === inverseType(selected)));
    const duplicate = (mm.links || []).find(isDuplicate);
    if (duplicate) {
      const other = mm.byId.get(targetSelect.value);
      // A derived link (fact: null — from the Role field) has no row to
      // edit or delete; pointing at one would dead-end the user.
      toast(duplicate.fact
        ? `already connected to ${other ? other.name : "them"} — edit or delete the existing link instead`
        : `already connected to ${other ? other.name : "them"} — that link comes from their Role field`);
      return;
    }
    try {
      await postFact({
        subject_id: entity.id,
        predicate: `${CONNECTION_PREFIX}${typeSelect.value}`,
        object_id: targetSelect.value,
      });
      memoryState.addingConnection = false;
      mmClearDraft(typeKey);
      mmClearDraft(targetKey);
      toast("connection added — it shows on both profiles");
    } catch (error) { toast(`add failed: ${error.message}`); }
    reloadMemory();
  });
  form.append(save, cancel);
  return form;
}

// ---------- memories ----------

// This entity's own memories plus other entities' memories that mention it.
function memoriesFor(entity) {
  const out = [];
  for (const owner of memoryState.entities) {
    for (const fact of owner.facts || []) {
      if (!isMemoryFact(fact)) continue;
      if (owner.id === entity.id || mentionedIds(fact.value).has(entity.id)) out.push({ fact, owner });
    }
  }
  return out.sort((a, b) => (b.fact.valid_from || "").localeCompare(a.fact.valid_from || ""));
}

function memoriesCard(entity) {
  const card = el("section", { class: "mm-card mm-memories", "aria-labelledby": "mm-mem-h" });
  const all = memoriesFor(entity);
  const head = el("div", { class: "mm-card-head" });
  const title = el("h3", { id: "mm-mem-h", text: "Memories " });
  title.append(el("span", { class: "mm-count-muted", text: `· ${all.length}` }));
  head.append(title);
  card.append(head);
  card.append(el("p", {
    class: "mm-card-hint",
    text: "Things worth remembering: routines, preferences, dates, notes. Names of people link automatically.",
  }));
  card.append(memoryComposer(entity));

  const counts = new Map();
  for (const { fact } of all) {
    const category = fact.predicate.toLowerCase();
    counts.set(category, (counts.get(category) || 0) + 1);
  }
  if (all.length) {
    const filters = el("div", { class: "mm-filters", role: "group", "aria-label": "Filter memories" });
    const chip = (key, label) => {
      const pressed = memoryState.memoryFilter === key;
      const button = el("button", { type: "button", class: "mm-filter", "aria-pressed": String(pressed), text: label });
      button.addEventListener("click", () => {
        memoryState.memoryFilter = key;
        renderDetail();
      });
      return button;
    };
    filters.append(chip("all", `All ${all.length}`));
    for (const category of MEMORY_CATEGORIES) {
      if (counts.get(category)) filters.append(chip(category, `${CATEGORY_PLURAL[category]} ${counts.get(category)}`));
    }
    card.append(filters);
  }
  const shown = all.filter(({ fact }) => memoryState.memoryFilter === "all"
    || fact.predicate.toLowerCase() === memoryState.memoryFilter);
  for (const { fact, owner } of shown) card.append(memoryCard(entity, fact, owner));
  if (!all.length) {
    card.append(el("div", { class: "empty", text: `Nothing remembered about ${entity.name} yet.` }));
  }
  return card;
}

function memoryComposer(entity) {
  const form = el("div", { class: "mm-composer" });
  const draftKey = `composer:${entity.id}`;
  const titleKey = `composer:${entity.id}:title`;
  const titleInput = el("input", {
    type: "text", dir: "auto", class: "mm-composer-title",
    placeholder: "Title (optional)", "aria-label": "title",
    value: mmDraft(titleKey),
  });
  mmTrackDraft(titleInput, titleKey);
  const input = el("input", {
    id: "memory-new", type: "text", dir: "auto",
    placeholder: "Add a memory…",
    "aria-label": `Add a memory about ${entity.name}`,
    value: mmDraft(draftKey),
  });
  mmTrackDraft(input, draftKey);
  const category = el("select", { "aria-label": "category", class: "mm-composer-cat" });
  for (const key of MEMORY_CATEGORIES) {
    const option = el("option", { value: key, text: CATEGORY_LABEL[key] });
    if (key === mmDraft(`${draftKey}:category`, "note")) option.selected = true;
    category.append(option);
  }
  category.addEventListener("change", () => {
    memoryState.drafts[`${draftKey}:category`] = category.value;
  });
  const save = el("button", { type: "button", class: "btn btn-pri", text: "Save" });
  const submit = async () => {
    const value = input.value.trim();
    if (!value) { toast("write the memory first"); return; }
    const title = titleInput.value.trim();
    try {
      await postFact({
        subject_id: entity.id, predicate: category.value, value,
        ...(title ? { title } : {}),
      });
      mmClearDraft(draftKey);
      mmClearDraft(titleKey);
      mmClearDraft(`${draftKey}:category`);
      input.value = "";
      titleInput.value = "";
      category.value = "note";
      input.blur();
      toast("remembered");
    } catch (error) { toast(`add failed: ${error.message}`); }
    reloadMemory();
  };
  save.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") submit(); });
  form.append(titleInput, input, category, save);
  return form;
}

function memoryCard(entity, fact, owner) {
  const card = el("article", { class: "mm-mem" });
  const category = fact.predicate.toLowerCase();
  if (memoryState.editingFact === fact.id) {
    card.append(memoryEditForm(fact));
    return card;
  }
  const top = el("div", { class: "mm-mem-top" });
  const factTitle = fact.provenance && fact.provenance.title;
  if (factTitle) top.append(el("span", { class: "mm-mem-title", text: factTitle, dir: "auto" }));
  top.append(mentionText(fact.value));
  const more = el("button", {
    type: "button", class: "mm-more-btn", "aria-label": "More actions",
    "aria-expanded": String(memoryState.menuFact === fact.id), text: "···",
  });
  more.addEventListener("click", () => {
    memoryState.menuFact = memoryState.menuFact === fact.id ? null : fact.id;
    renderDetail();
  });
  top.append(more);
  card.append(top);
  const meta = el("div", { class: "mm-mem-meta" });
  meta.append(el("span", { class: `mm-tag cat-${category}`, text: CATEGORY_LABEL[category] || category }));
  if (owner.id !== entity.id) {
    const about = el("span", { class: "mm-about", text: "about " });
    about.append(entityChip(owner));
    meta.append(about);
  }
  meta.append(sourceLine(fact));
  card.append(meta);

  if (memoryState.menuFact === fact.id) card.append(memoryActions(fact));
  if (memoryState.promoting === fact.id) {
    card.append(promoteForm(owner, fact));
  } else if (category === "date") {
    const hint = el("div", { class: "mm-promote" });
    hint.append(el("span", { text: "A fixed date is a stable fact. Make it basic info?" }));
    const go = el("button", { type: "button", class: "btn btn-sm", text: "Move to basic info" });
    go.addEventListener("click", () => {
      memoryState.promoting = fact.id;
      renderDetail();
    });
    hint.append(go);
    card.append(hint);
  }
  return card;
}

function memoryActions(fact) {
  const row = el("div", { class: "mm-mem-actions" });
  const edit = el("button", { type: "button", class: "btn btn-sm", text: "Edit" });
  edit.addEventListener("click", () => {
    memoryState.editingFact = fact.id;
    memoryState.menuFact = null;
    renderDetail();
  });
  const promote = el("button", { type: "button", class: "btn btn-sm", text: "Move to basic info" });
  promote.addEventListener("click", () => {
    memoryState.promoting = fact.id;
    memoryState.menuFact = null;
    renderDetail();
  });
  const history = el("button", { type: "button", class: "btn btn-sm", text: "History" });
  history.addEventListener("click", () => openMemoryDrawer(fact.id));
  const remove = el("button", { type: "button", class: "btn btn-sm mm-remove", text: "Delete" });
  remove.addEventListener("click", () => {
    armConfirm(remove, () => {
      memoryState.menuFact = null;
      deleteFactWithUndo(fact, "memory deleted — Undo reopens it");
    });
  });
  row.append(edit, promote, history, remove);
  return row;
}

function memoryEditForm(fact) {
  const form = el("div", { class: "mm-form mm-mem-edit" });
  const draftKey = `edit:${fact.id}`;
  const input = el("textarea", { dir: "auto", rows: "2", "aria-label": "memory text" });
  input.value = mmDraft(draftKey, fact.value ?? "");
  mmTrackDraft(input, draftKey);
  const category = el("select", { "aria-label": "category" });
  for (const key of MEMORY_CATEGORIES) {
    const option = el("option", { value: key, text: CATEGORY_LABEL[key] });
    if (key === mmDraft(`${draftKey}:cat`, fact.predicate.toLowerCase())) option.selected = true;
    category.append(option);
  }
  category.addEventListener("change", () => {
    memoryState.drafts[`${draftKey}:cat`] = category.value;
  });
  const save = el("button", { type: "button", class: "btn btn-sm btn-pri", text: "Save" });
  const cancel = el("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancel.addEventListener("click", () => {
    memoryState.editingFact = null;
    mmClearDraft(draftKey);
    mmClearDraft(`${draftKey}:cat`);
    renderDetail();
  });
  save.addEventListener("click", async () => {
    const value = input.value.trim();
    if (!value) { toast("the memory must not be empty"); return; }
    try {
      await patchFact(fact.id, { value, predicate: category.value });
      memoryState.editingFact = null;
      mmClearDraft(draftKey);
      mmClearDraft(`${draftKey}:cat`);
    } catch (error) { toast(`edit failed: ${error.message}`); }
    reloadMemory();
  });
  form.append(input, category, save, cancel);
  setTimeout(() => input.focus(), 0);
  return form;
}

// "Move to basic info": a typed field on the owner (and, for a shared date,
// on the people it mentions), then the memory is retired (Undo-able).
function promoteForm(owner, fact) {
  const form = el("div", { class: "mm-form mm-promote-form" });
  const fieldKey = `promote:${fact.id}:field`;
  const valueKey = `promote:${fact.id}:value`;
  const field = el("select", { "aria-label": "basic info field" });
  const fields = [["birthday", "Birthday"], ["anniversary", "Anniversary"], ["relation", "Role"],
    ["work", "Work"], ["phone", "Phone"], ["email", "Email"], ["school", "School"], ["teacher", "Teacher"]];
  for (const [predicate, label] of fields) field.append(el("option", { value: predicate, text: label }));
  if (mmDraft(fieldKey)) field.value = mmDraft(fieldKey);
  else if (/anniversar/i.test(fact.value || "")) field.value = "anniversary";
  field.addEventListener("change", () => { memoryState.drafts[fieldKey] = field.value; });
  const dateMatch = String(fact.value || "").match(/\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b/);
  const value = el("input", {
    type: "text", dir: "auto", "aria-label": "value",
    value: mmDraft(valueKey, dateMatch ? dateMatch[0] : fact.value || ""),
  });
  mmTrackDraft(value, valueKey);
  form.append(field, value);
  const others = [...mentionedIds(fact.value)].filter((id) => id !== owner.id)
    .map((id) => mm.byId.get(id)).filter((entity) => entity && entity.kind === "person");
  const boxes = [];
  // The extra-subject checkboxes default to UNCHECKED and are offered only
  // for the shared-date fields (birthday/anniversary) — writing relation/
  // work/phone onto somebody else's profile was silent data corruption.
  for (const other of others) {
    const label = el("label", { class: "mm-check" });
    const box = el("input", { type: "checkbox" });
    box.checked = false;
    boxes.push([box, other, label]);
    label.append(box, document.createTextNode(` also on ${other.name}'s profile`));
    label.hidden = !/^(birthday|anniversary)$/.test(field.value);
    form.append(label);
    field.addEventListener("change", () => {
      const hidden = !/^(birthday|anniversary)$/.test(field.value);
      label.hidden = hidden;
      if (hidden) box.checked = false; // hidden-but-checked still writes
    });
  }
  const save = el("button", { type: "button", class: "btn btn-sm btn-pri", text: "Move" });
  const cancel = el("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancel.addEventListener("click", () => {
    memoryState.promoting = null;
    mmClearDraft(fieldKey);
    mmClearDraft(valueKey);
    renderDetail();
  });
  save.addEventListener("click", async () => {
    const text = value.value.trim();
    if (!text) { toast("the value must not be empty"); return; }
    const subjects = [owner, ...boxes
      .filter(([box, , label]) => box.checked && !label.hidden)
      .map(([, other]) => other)];
    const created = [];
    const patched = [];
    const errors = [];
    for (const subject of subjects) {
      // PATCH the profile's existing value for this field (edit = new
      // version) instead of POSTing a second parallel one; POST only when
      // the field is empty.
      const existing = factsOf(subject, field.value);
      try {
        if (existing.length) {
          const edited = await patchFact(existing[0].id, { value: text });
          // patch_fact closes the old row and opens a NEW one — the undo
          // must retarget the row the server created, or it edits a closed
          // row and 409s. (Legacy duplicates: only existing[0] is patched.)
          patched.push({ id: (edited && edited.id) || existing[0].id, value: existing[0].value });
        } else {
          const made = await postFact({ subject_id: subject.id, predicate: field.value, value: text });
          created.push({ id: made ? made.id : null });
        }
      } catch (error) {
        errors.push(`${subject.name}: ${error.message}`);
        break; // atomic: stop at the first failure and roll back below
      }
    }
    if (errors.length) {
      // atomic with rollback: whatever was already written to earlier
      // subjects is undone (created rows deleted, patched rows restored),
      // and the source memory is KEPT — nothing is lost.
      for (const row of created) {
        if (!row.id) continue;
        try { await api(`/memory/facts/${row.id}`, { method: "DELETE" }); }
        catch (error) { toast(`rollback failed: ${error.message}`); }
      }
      for (const previous of patched) {
        try { await patchFact(previous.id, { value: previous.value }); }
        catch (error) { toast(`rollback failed: ${error.message}`); }
      }
      toast(`move failed (${errors.join("; ")}) — the writes were rolled back`);
      memoryState.promoting = null;
      reloadMemory();
      return;
    }
    // A MOVE, not a copy: once every basic-info write succeeded the source
    // memory is soft-deleted (history kept), so the Undo below can restore
    // it — a memory that stayed would duplicate the value forever.
    try { await api(`/memory/facts/${fact.id}`, { method: "DELETE" }); }
    catch (error) { toast(`could not remove the memory: ${error.message}`); }
    memoryState.promoting = null;
    mmClearDraft(fieldKey);
    mmClearDraft(valueKey);
    const names = subjects.map((s2) => s2.name).join(" and ");
    undoToast(`moved to basic info on ${names} — Undo puts it back`, async () => {
      for (const row of created) {
        if (!row.id) continue;
        try { await api(`/memory/facts/${row.id}`, { method: "DELETE" }); }
        catch (error) { toast(`undo failed: ${error.message}`); }
      }
      for (const previous of patched) {
        try { await patchFact(previous.id, { value: previous.value }); }
        catch (error) { toast(`undo failed: ${error.message}`); }
      }
      try {
        await api(`/memory/facts/${fact.id}/restore`, { method: "POST" });
        toast("memory restored");
      } catch (error) { toast(`restore failed: ${error.message}`); }
      reloadMemory();
    });
    reloadMemory();
  });
  form.append(save, cancel);
  return form;
}

// ---------- review inbox ----------

function reviewKind(fact) {
  if (isConnectionFact(fact) || fact.object_id) return "connection";
  if (isMemoryFact(fact)) return "memory";
  return "basic";
}

const REVIEW_KIND_LABEL = { basic: "Basic info", memory: "Memory", connection: "Connection" };

function reviewItems() {
  const items = [];
  for (const entity of memoryState.proposed) {
    const subject = mm.byId.get(entity.id) || entity;
    for (const fact of entity.facts || []) {
      // 'photo' is never proposed through the product; a hand-made proposal
      // must not print its sha in the inbox — skip it.
      if (fact.predicate.toLowerCase() === "photo") continue;
      items.push({ subject, fact, kind: reviewKind(fact) });
    }
  }
  return items.sort((a, b) => (b.fact.valid_from || "").localeCompare(a.fact.valid_from || ""));
}

function renderReview() {
  const items = reviewItems();
  const main = $("memory-review-list");
  const nodes = [];
  const header = el("div", { class: "mm-review-intro" });
  header.append(el("h2", {
    text: items.length ? `${items.length} thing${items.length === 1 ? "" : "s"} to review` : "All caught up",
  }));
  header.append(el("p", {
    text: items.length
      ? "Picked up from your chats. Nothing is saved until you confirm it, and every confirmed item keeps a link back to where it came from."
      : "Suggestions picked up from your chats wait here. Nothing is saved until you confirm it.",
  }));
  nodes.push(header);
  if (items.length) {
    const counts = { basic: 0, memory: 0, connection: 0 };
    for (const item of items) counts[item.kind] += 1;
    const filters = el("div", { class: "mm-filters", role: "group", "aria-label": "Filter suggestions" });
    const chip = (key, label) => {
      const button = el("button", {
        type: "button", class: "mm-filter", text: label,
        "aria-pressed": String(memoryState.reviewFilter === key),
      });
      button.addEventListener("click", () => {
        memoryState.reviewFilter = key;
        renderReview();
      });
      return button;
    };
    filters.append(chip("all", `All ${items.length}`));
    for (const key of ["basic", "memory", "connection"]) {
      if (counts[key]) filters.append(chip(key, `${REVIEW_KIND_LABEL[key]} ${counts[key]}`));
    }
    nodes.push(filters);
  }
  for (const item of items) {
    if (memoryState.reviewFilter !== "all" && memoryState.reviewFilter !== item.kind) continue;
    nodes.push(reviewCard(item));
  }
  main.replaceChildren(...nodes);
}

function reviewCard({ subject, fact, kind }) {
  const card = el("article", { class: "mm-card mm-rcard" });
  const head = el("div", { class: "mm-rhead" });
  head.append(el("span", { class: `mm-rtag rk-${kind}`, text: REVIEW_KIND_LABEL[kind] }));
  head.append(el("span", { class: "mm-rhead-word", text: kind === "memory" ? "about" : "for" }));
  head.append(entityChip(subject));
  if (kind === "memory") {
    for (const id of mentionedIds(fact.value)) {
      if (id === subject.id) continue;
      head.append(el("span", { class: "mm-rhead-word", text: "and" }), entityChip(mm.byId.get(id)));
    }
  }
  card.append(head);

  const editing = memoryState.reviewEditing === fact.id;
  if (kind === "basic") {
    const grid = el("div", { class: "mm-rdiff" });
    grid.append(el("span", { class: "mm-rdiff-label", text: capitalize(fact.predicate) }));
    const change = el("span", { class: "mm-rdiff-change" });
    const confirmed = mm.byId.get(subject.id);
    const current = confirmed ? factsOf(confirmed, fact.predicate) : [];
    const predicate = fact.predicate.toLowerCase();
    change.append(current.length
      ? el("span", { class: "mm-rdiff-old", text: current.map((old) => displayValue(predicate, old.value ?? "")).join(", "), dir: "auto" })
      : el("span", { class: "mm-rdiff-empty", text: "empty" }));
    change.append(el("span", { class: "mm-rdiff-arrow", "aria-hidden": "true", text: "→" }));
    change.append(el("b", { class: "mm-rdiff-new", text: displayValue(predicate, fact.value ?? ""), dir: "auto" }));
    grid.append(change);
    card.append(grid);
  } else if (kind === "memory") {
    const block = el("div", { class: "mm-rbody" });
    block.append(mentionText(fact.value));
    card.append(block);
  } else {
    const target = fact.object_id ? mm.byId.get(fact.object_id) : null;
    const type = isConnectionFact(fact) ? fact.predicate.slice(CONNECTION_PREFIX.length) : fact.predicate;
    const pair = el("div", { class: "mm-rpair" });
    pair.append(avatar(subject, "sm"), el("b", { text: subject.name, dir: "auto" }));
    pair.append(el("span", { class: "mm-rpair-line", "aria-hidden": "true" }));
    if (target) pair.append(avatar(target, "sm"), el("b", { text: target.name, dir: "auto" }));
    card.append(pair);
    const spec = CONNECTION_TYPES[type] || { label: type };
    card.append(el("p", {
      class: "mm-rquestion",
      text: target ? `${subject.name} is ${target.name}'s ${spec.label.toLowerCase()}?` : `${subject.name}: ${type}`,
    }));
  }
  if (editing) card.append(reviewEditForm(fact));

  const provenance = fact.provenance || {};
  const source = el("div", { class: "mm-rsource" });
  if (provenance.quote) source.append(el("q", { text: provenance.quote, dir: "auto" }));
  source.append(sourceLine(fact));
  const show = openSourceItem(fact);
  if (show) source.append(show);
  card.append(source);

  if (!editing) {
    const actions = el("div", { class: "mm-ractions" });
    const confirm = el("button", { type: "button", class: "btn btn-pri", text: "Confirm" });
    confirm.addEventListener("click", () => confirmProposal(subject, fact, kind));
    actions.append(confirm);
    if (kind !== "connection") {
      const edit = el("button", { type: "button", class: "btn", text: "Edit first" });
      edit.addEventListener("click", () => {
        memoryState.reviewEditing = fact.id;
        renderReview();
      });
      actions.append(edit);
    }
    const reject = el("button", { type: "button", class: "btn btn-ghost", text: "Not right" });
    reject.addEventListener("click", () => deleteFactWithUndo(fact, "dismissed — Undo brings it back"));
    actions.append(reject);
    card.append(actions);
  }
  return card;
}

function reviewEditForm(fact) {
  const form = el("div", { class: "mm-form" });
  const draftKey = `review:${fact.id}`;
  const input = el("input", { type: "text", dir: "auto", "aria-label": "corrected value" });
  input.value = mmDraft(draftKey, fact.value ?? "");
  mmTrackDraft(input, draftKey);
  const save = el("button", { type: "button", class: "btn btn-sm btn-pri", text: "Save & confirm" });
  const cancel = el("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel" });
  cancel.addEventListener("click", () => {
    memoryState.reviewEditing = null;
    mmClearDraft(draftKey);
    renderReview();
  });
  save.addEventListener("click", async () => {
    const value = input.value.trim();
    if (!value) { toast("the value must not be empty"); return; }
    try {
      const edited = await patchFact(fact.id, { value });
      memoryState.reviewEditing = null;
      mmClearDraft(draftKey);
      const subject = mm.byId.get(fact.subject_id) || { id: fact.subject_id, name: "" };
      await confirmProposal(subject, edited, reviewKind(edited));
    } catch (error) {
      toast(`edit failed: ${error.message}`);
      reloadMemory();
    }
  });
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") save.click(); });
  form.append(input, save, cancel);
  setTimeout(() => input.focus(), 0);
  return form;
}

// Confirm in place. The server supersedes single-valued basic fields
// itself (SINGLE_VALUED_PREDICATES in services/memory.py) and returns the
// rows it closed as `replaced`; the Undo toast restores them. The client
// never deletes siblings on its own — multi-valued fields keep every
// existing entry.
async function confirmProposal(subject, fact, kind) {
  try {
    const saved = await api(`/memory/facts/${fact.id}/confirm`, { method: "POST" });
    const replaced = (saved && saved.replaced) || [];
    if (replaced.length) {
      undoToast(`replaced the previous ${fact.predicate} — Undo brings it back`, async () => {
        // One server op: close the new row, reopen the replaced one. The
        // replaced row is closed HISTORY (never 'rejected'), so /restore
        // would 409 it — only /undo-supersede can bring it back.
        try {
          await api(`/memory/facts/${saved.id}/undo-supersede`, { method: "POST" });
          toast("the previous value is back");
        } catch (error) { toast(`undo failed: ${error.message}`); }
        reloadMemory();
      });
    }
    toast(`saved to ${subject.name || "memory"}`);
  } catch (error) { toast(`confirm failed: ${error.message}`); }
  reloadMemory();
}

// ---------- family map ----------

function renderMap() {
  const canvas = $("memory-map-canvas");
  const width = Math.max(320, canvas.clientWidth || 960);
  const people = sortedEntities().filter((entity) => entity.kind === "person");
  const adults = people.filter((entity) => isHousehold(entity) && !isKid(entity));
  const kids = people.filter(isKid);
  const others = people.filter((entity) => !isHousehold(entity));
  const gap = 16;
  const widest = Math.max(adults.length, kids.length, 1);
  const nodeW = Math.max(132, Math.min(220, Math.floor((width - gap * (widest + 1)) / widest)));
  const nodeH = 64;
  const pillH = 40;
  const pos = new Map();
  let y = 24;

  // even slots per row; wraps when the row does not fit (phone width)
  const placeRow = (list, rowY) => {
    const perRow = Math.max(1, Math.floor((width - gap) / (nodeW + gap)));
    list.forEach((entity, index) => {
      const row = Math.floor(index / perRow);
      const inRow = Math.min(perRow, list.length - row * perRow);
      const slot = width / inRow;
      const x = Math.round(slot * (index % perRow) + slot / 2 - nodeW / 2);
      pos.set(entity.id, { x, y: rowY + row * (nodeH + gap), w: nodeW, h: nodeH });
    });
    return list.length ? Math.ceil(list.length / perRow) * (nodeH + gap) : 0;
  };

  if (adults.length) y += placeRow(adults, y) + 56;
  if (kids.length) y += placeRow(kids, y) + 28;
  // each kid's groups stack under the kid (two, then "+N more")
  let groupsBottom = y;
  const groupPills = [];
  for (const kid of kids) {
    const anchor = pos.get(kid.id);
    const top = anchor.y + nodeH + 28;
    const groups = channelsFor(kid);
    groups.slice(0, 2).forEach((group, index) => {
      const pill = { x: anchor.x, y: top + index * (pillH + 10), w: nodeW, h: pillH };
      pos.set(group.id, pill);
      groupPills.push({ group, kid, pill });
      groupsBottom = Math.max(groupsBottom, pill.y + pillH + 10);
    });
    if (groups.length > 2) {
      const pill = { x: anchor.x, y: top + 2 * (pillH + 10), w: nodeW, h: 32 };
      groupPills.push({ more: groups.length - 2, kid, pill });
      groupsBottom = Math.max(groupsBottom, pill.y + 32 + 10);
    }
  }
  y = Math.max(y, groupsBottom) + 16;
  let othersY = null;
  if (others.length) {
    othersY = y + 32;
    y = othersY + placeRow(others, othersY);
  }
  const height = y + 16;

  const svg = svgEl("svg", {
    class: "mm-map-lines", width: String(width), height: String(height),
    viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true",
  });
  const center = (box) => [box.x + box.w / 2, box.y + box.h / 2];
  const labels = [];
  for (const link of mm.links) {
    const from = pos.get(link.from);
    const to = pos.get(link.to);
    if (!from || !to) continue;
    const [x1, y1] = center(from);
    const [x2, y2] = center(to);
    svg.append(svgEl("line", {
      x1, y1, x2, y2, class: `mm-edge ${FAMILY_TYPES.has(link.type) ? "is-family" : "is-other"}`,
    }));
    if (link.type !== "parent" && link.type !== "child") {
      const text = link.type === "spouse" ? "married" : (CONNECTION_TYPES[link.type] || { label: link.type }).label.toLowerCase();
      labels.push({ text, x: (x1 + x2) / 2, y: (y1 + y2) / 2 });
    }
  }
  for (const { kid, pill } of groupPills) {
    const anchor = pos.get(kid.id);
    svg.append(svgEl("path", {
      d: `M ${anchor.x + 24} ${anchor.y + anchor.h} V ${pill.y + pill.h / 2} H ${pill.x + 16}`,
      class: "mm-edge is-member",
    }));
  }

  const canvasNodes = [];
  for (const entity of people) {
    const box = pos.get(entity.id);
    if (!box) continue;
    const unknown = hasNoConnection(entity);
    const node = el("button", {
      type: "button", class: `mm-map-node${unknown ? " is-unknown" : ""}`,
      style: `left:${box.x}px;top:${box.y}px;width:${box.w}px;height:${box.h}px`,
    });
    node.append(avatar(entity));
    const text = el("span", { class: "mm-row-text" });
    text.append(el("span", { class: "mm-row-name", text: entity.name, dir: "auto" }));
    text.append(el("span", {
      class: `mm-row-sub${unknown ? " is-unknown" : ""}`,
      text: unknown ? "How are they connected?" : subLine(entity), dir: "auto",
    }));
    node.append(text);
    node.addEventListener("click", () => openProfile(entity.id));
    canvasNodes.push(node);
  }
  for (const { group, kid, more, pill } of groupPills) {
    const node = el("button", {
      type: "button", class: `mm-map-pill${more ? " is-more" : ""}`,
      style: `left:${pill.x + 16}px;top:${pill.y}px;width:${pill.w - 16}px;height:${pill.h}px`,
    });
    if (more) {
      node.textContent = `+${more} more group${more === 1 ? "" : "s"}`;
      node.addEventListener("click", () => openProfile(kid.id));
    } else {
      node.append(groupIcon(), el("span", { class: "mm-map-pill-name", text: group.name, dir: "auto" }));
      node.title = subLine(group);
      node.addEventListener("click", () => openProfile(group.id));
    }
    canvasNodes.push(node);
  }
  for (const label of labels) {
    canvasNodes.push(el("span", {
      class: "mm-edge-label", text: label.text,
      style: `left:${Math.round(label.x)}px;top:${Math.round(label.y)}px`,
    }));
  }
  if (othersY !== null) {
    canvasNodes.push(el("span", { class: "mm-map-row-label", text: "Other people", style: `top:${othersY - 26}px` }));
  }
  if (!people.length) canvasNodes.push(el("div", { class: "empty", text: "no people yet — add a person" }));
  canvas.style.height = `${height}px`;
  canvas.replaceChildren(svg, ...canvasNodes);
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
      // a photo version's value IS the sha — show the word, the row's dates
      // (validity line below) carry the when; the sha never prints
      el("span", {
        class: "mval", dir: "auto",
        text: version.predicate.toLowerCase() === "photo" ? "photo" : version.value ?? "",
      }),
      el("span", { class: `chip ${version.status}`, text: version.status }),
    );
    row.append(top);
    const validity = factValidity(version);
    if (validity) row.append(validity);
    const provenance = version.provenance || {};
    if (provenance.quote) {
      row.append(el("div", { class: "hquote", text: `“${provenance.quote}”`, dir: "auto" }));
    }
    const sourceLineNode = el("div", { class: "hsource" });
    if (provenance.source === "item" && (provenance.item_ids || []).length) {
      sourceLineNode.append(el("span", { text: "source item" + (provenance.item_ids.length > 1 ? "s" : "") + ": " }));
      for (const itemId of provenance.item_ids) {
        const btn = el("button", { class: "btn btn-ghost item-link", text: shortId(itemId), title: "open in the Debug inspector" });
        btn.addEventListener("click", () => {
          closeMemoryDrawer();
          switchTab("debug");
          openInspector(itemId);
        });
        sourceLineNode.append(btn);
      }
    } else {
      sourceLineNode.append(el("span", { text: provenance.source === "item" ? "source item (id missing)" : "added by you" }));
    }
    row.append(sourceLineNode);
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
  if (!existed) {
    for (const [predicate, inputId] of PERSON_FIELDS) {
      const value = $(inputId).value.trim();
      if (!value) continue;
      try {
        await postFact({ subject_id: entity.id, predicate, value });
      } catch (error) { toast(`fact ${predicate} failed: ${error.message}`); }
    }
  }
  $("memory-add-person").hidden = true;
  for (const [, inputId] of PERSON_FIELDS) $(inputId).value = "";
  $("memory-person-name").value = "";
  $("memory-person-aliases").value = "";
  toast(existed ? `${name} already existed — opened it` : `${name} added`);
  memoryState.selected = entity.id;
  memoryState.mode = "profile";
  resetInlineState();
  $("memory-wrap").dataset.view = "detail";
  reloadMemory();
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
  memoryState.mode = "profile";
  resetInlineState();
  $("memory-wrap").dataset.view = "detail";
  reloadMemory();
}

// ---------- tab init ----------

function initMemoryTab() {
  let memorySearchTimer = null;
  $("memory-q").addEventListener("input", (event) => {
    clearTimeout(memorySearchTimer);
    memorySearchTimer = setTimeout(() => {
      memoryState.q = event.target.value.trim();
      if (memoryState.mode !== "profile") {
        memoryState.mode = "profile";
        $("memory-wrap").dataset.view = "list";
        renderMemory();
      } else {
        mm = buildModel();
        renderList();
      }
    }, 200);
  });
  $("memory-review-btn").addEventListener("click", () => setMode(memoryState.mode === "review" ? "profile" : "review"));
  $("memory-review-back").addEventListener("click", () => setMode("profile"));
  $("memory-view-list").addEventListener("click", () => setMode("profile"));
  $("memory-view-map").addEventListener("click", () => setMode("map"));
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
    resetInlineState();
    $("memory-wrap").dataset.view = "list";
    renderMemory();
  });
  $("memory-undo-btn").addEventListener("click", async () => {
    const entry = memoryState.undoStack.pop();
    if (!entry) {
      $("memory-undo").hidden = true;
      return;
    }
    try { await entry.undoFn(); }
    finally {
      if (memoryState.undoStack.length) {
        clearTimeout(memoryState.undoTimer);
        memoryState.undoTimer = setTimeout(() => {
          memoryState.undoStack = [];
          showUndoTop();
        }, 6000);
      }
      showUndoTop();
    }
  });
  $("memory-drawer-close").addEventListener("click", closeMemoryDrawer);
  let mapResizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(mapResizeTimer);
    mapResizeTimer = setTimeout(() => {
      if (memoryState.mode === "map" && mm) renderMap();
    }, 150);
  });
}

document.addEventListener("DOMContentLoaded", initMemoryTab);

// The single symbol the shared shell knows about.
window.MemoryTab = { refresh: refreshMemory };
