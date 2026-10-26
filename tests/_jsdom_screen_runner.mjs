// jsdom runner: boots the REAL served cockpit files against a fetch stub,
// then runs one scenario and prints ONE JSON line with its checks.
// Placeholders (replaced by tests/_jsdom_screen.py before execution):
// are replaced by tests/_jsdom_screen.py before execution.
// A missing jsdom is a loud FAILURE under CI=1 — a silent skip let the
// screen-behavior suite "pass" in CI without running a single check.
let JSDOMCtor = null;
try { ({ JSDOM: JSDOMCtor } = await import('jsdom')); }
catch {
  if (process.env.CI) {
    console.log(JSON.stringify({ failed: 'jsdom not installed (CI requires it — npm install --prefix . jsdom)' }));
    process.exit(1);
  }
  console.log(JSON.stringify({ skipped: 'jsdom not installed' }));
  process.exit(0);
}

const JSDOM = JSDOMCtor;

const dom = new JSDOM(__HTML__, { url: 'http://localhost/cockpit', runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
const { document } = window;

// ---- fetch stub: a small HONEST backend, not an echo ----
// It mirrors services/memory.py so the client cannot pass on a lie:
//   • add/confirm of a single-valued predicate supersedes the current row
//     and the response carries the displaced rows as `replaced`;
//   • PATCH closes the old row and opens a NEW id — editing a closed row 409s;
//   • DELETE soft-deletes (status 'rejected'); POST /restore refuses to run
//     while a second current single-valued fact would result, and refuses
//     rows that were not soft-deleted (superseded history stays history);
//   • connection links refuse exact duplicates on add/patch/confirm.
const fixtures = __FIXTURES__;
const calls = [];
const SINGLE_VALUED = new Set(['gender', 'school', 'grade', 'class', 'teacher', 'birthday', 'anniversary', 'photo']);
const CONN = 'connection:';
const rows = new Map();   // id -> { fact, open, status, deletedFrom }
for (const group of [...(fixtures.entities ?? []), ...(fixtures.proposed ?? [])]) {
  for (const f of group.facts ?? []) {
    rows.set(f.id, {
      fact: { ...f },
      open: !f.valid_to,   // a row already closed in the fixture stays closed
      status: f.status ?? 'confirmed',
      deletedFrom: null,
    });
  }
}
let seq = 0;
const nextId = () => `new-${++seq}`;
const rowById = (id) => rows.get(id) ?? null;
const openRows = (subjectId, predicate) => [...rows.values()].filter((r) => r.open
  && r.fact.subject_id === subjectId && r.fact.predicate === predicate);
const supersedeClose = (row) => {
  row.open = false;   // superseded = closed HISTORY (status untouched),
                      // restorable only via /undo-supersede on the new row
};
window.fetch = async (path, options) => {
  const url = String(path);
  const method = options?.method ?? 'GET';
  let body = null;
  try { body = options?.body ? JSON.parse(options.body) : null; } catch { body = { __form: true }; }
  const respond = (status, payload) => {
    calls.push({ url, method, body, status });
    return {
      ok: status < 400, status,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    };
  };
  if (url.startsWith('/memory?status=proposed')) return respond(200, { entities: fixtures.proposed ?? [] });
  if (url === '/memory' || url.startsWith('/memory?')) return respond(200, { entities: fixtures.entities ?? [] });
  if (url.startsWith('/ops/health')) return respond(200, fixtures.health ?? { overall: 'ok', degraded: [], components: { api: { details: { version: 'jsdom' } } } });
  if (url.startsWith('/ops/flow')) return respond(200, fixtures.flow ?? {});
  if (url.startsWith('/ops/stats')) return respond(200, fixtures.stats ?? {});
  if (url.startsWith('/ops/events')) return respond(200, []);
  if (url.startsWith('/ops/')) return respond(200, {});
  if (method === 'POST' && url === '/memory/facts') {
    if (body.predicate?.startsWith(CONN) && body.object_id
      && openRows(body.subject_id, body.predicate).some((r) => r.fact.object_id === body.object_id)) {
      return respond(409, { detail: 'this link already exists' });
    }
    const made = { id: nextId(), ...body, supersedes_id: null, provenance: { source: 'user' } };
    if (body.status !== 'proposed' && SINGLE_VALUED.has(body.predicate) && body.value) {
      const cur = openRows(body.subject_id, body.predicate).find((r) => r.status === 'confirmed');
      if (cur) {
        if (cur.fact.value === body.value) return respond(200, { ...cur.fact, replaced: [] });
        supersedeClose(cur);
        made.supersedes_id = cur.fact.id;
        made.replaced = [cur.fact];
      }
    }
    rows.set(made.id, { fact: made, open: true, status: made.status ?? 'confirmed', deletedFrom: null });
    return respond(201, made);
  }
  const patchMatch = url.match(/^\/memory\/facts\/([^/]+)$/);
  if (method === 'PATCH' && patchMatch) {
    const row = rowById(patchMatch[1]);
    if (!row || !row.open) return respond(409, { detail: 'cannot edit a closed fact (superseded or deleted)' });
    if (row.fact.predicate?.startsWith(CONN) && body.object_id && body.object_id !== row.fact.object_id
      && openRows(row.fact.subject_id, row.fact.predicate)
        .some((r) => r.fact.id !== row.fact.id && r.fact.object_id === body.object_id)) {
      return respond(409, { detail: 'this link already exists' });
    }
    row.open = false;   // superseded by PATCH: history, NOT restorable
    const made = { ...row.fact, ...body, id: nextId(), supersedes_id: row.fact.id };
    rows.set(made.id, { fact: made, open: true, status: row.status, deletedFrom: null });
    return respond(200, made);
  }
  if (method === 'DELETE' && patchMatch) {
    const row = rowById(patchMatch[1]);
    if (!row || !row.open) return respond(409, { detail: 'fact is already closed' });
    row.open = false;
    row.deletedFrom = row.status;
    row.status = 'rejected';
    return respond(204, null);
  }
  const confirmMatch = url.match(/^\/memory\/facts\/([^/]+)\/confirm$/);
  if (method === 'POST' && confirmMatch) {
    const row = rowById(confirmMatch[1]);
    if (!row || !row.open || row.status !== 'proposed') {
      return respond(409, { detail: 'only proposed facts can be confirmed' });
    }
    if (SINGLE_VALUED.has(row.fact.predicate) && row.fact.value) {
      const cur = openRows(row.fact.subject_id, row.fact.predicate)
        .find((r) => r.status === 'confirmed' && r.fact.id !== row.fact.id);
      if (cur) {
        if (cur.fact.value === row.fact.value) {
          row.open = false; row.status = 'rejected';   // subsumed by the current row
          return respond(200, { ...cur.fact, replaced: [] });
        }
        supersedeClose(cur);
        row.status = 'confirmed';
        row.fact.supersedes_id = cur.fact.id;
        return respond(200, { ...row.fact, status: 'confirmed', replaced: [cur.fact] });
      }
    }
    row.status = 'confirmed';
    return respond(200, { ...row.fact, status: 'confirmed', replaced: [] });
  }
  const restoreMatch = url.match(/^\/memory\/facts\/([^/]+)\/restore$/);
  if (method === 'POST' && restoreMatch) {
    const row = rowById(restoreMatch[1]);
    if (!row || row.open || row.status !== 'rejected') {
      return respond(409, { detail: 'only a deleted fact can be restored' });
    }
    const prior = row.deletedFrom ?? 'confirmed';
    const others = openRows(row.fact.subject_id, row.fact.predicate).filter((r) => r.fact.id !== row.fact.id);
    if (SINGLE_VALUED.has(row.fact.predicate) && others.some((r) => r.status === prior)) {
      return respond(409, { detail: `cannot restore: a current ${row.fact.predicate} fact already exists for this entity` });
    }
    if (row.fact.predicate?.startsWith(CONN)
      && others.some((r) => r.status === prior && r.fact.object_id && r.fact.object_id === row.fact.object_id)) {
      return respond(409, { detail: `cannot restore: a current ${row.fact.predicate} link to the same person already exists` });
    }
    row.open = true;
    row.status = prior;
    return respond(200, row.fact);
  }
  const undoSupersedeMatch = url.match(/^\/memory\/facts\/([^/]+)\/undo-supersede$/);
  if (method === 'POST' && undoSupersedeMatch) {
    const row = rowById(undoSupersedeMatch[1]);
    if (!row || !row.open || !row.fact.supersedes_id) {
      return respond(409, { detail: 'only a current superseding fact can be undone' });
    }
    const prev = rowById(row.fact.supersedes_id);
    if (!prev || prev.open || prev.status === 'rejected') {
      return respond(409, { detail: 'the replaced fact is not undoable (deleted or already current)' });
    }
    row.open = false;
    prev.open = true;
    return respond(200, prev.fact);
  }
  const groupsMatch = url.match(/^\/memory\/entities\/([^/]+)\/groups$/);
  if (method === 'GET' && groupsMatch) {
    const payload = (fixtures.groupsByEntity ?? {})[groupsMatch[1]] ?? { groups: [] };
    return respond(200, payload);
  }
  const keepMatch = url.match(/^\/memory\/entities\/([^/]+)\/groups\/keep$/);
  if (method === 'POST' && keepMatch) {
    // honest mirror: the kept conversation's row flips to kept_fact_id, a
    // second keep of the same item 409s, the fact rides back
    const payload = (fixtures.groupsByEntity ?? {})[keepMatch[1]];
    const conv = payload ? (payload.groups ?? []).flatMap((g) => g.conversations)
      .find((c) => c.item_id === body?.item_id) : null;
    if (!conv) return respond(409, { detail: 'this conversation is not in this person\'s groups' });
    if (conv.kept_fact_id) return respond(409, { detail: 'this conversation is already kept' });
    const made = { id: nextId(), subject_id: keepMatch[1], predicate: 'note',
      object_id: null, value: body?.snippet ?? 'kept snippet', status: 'confirmed',
      provenance: { how: 'group-keep', item_id: conv.item_id } };
    conv.kept_fact_id = made.id;
    return respond(201, made);
  }
  const historyMatch = url.match(/^\/memory\/facts\/([^/]+)\/history$/);
  if (method === 'GET' && historyMatch) {
    // the version chain of ONE fact: walk the supersedes_id chain, oldest first
    const start = rowById(historyMatch[1]);
    if (!start) return respond(404, { detail: 'fact not found' });
    const chain = [start.fact];
    let cursor = start;
    while (cursor.fact.supersedes_id) {
      const prev = rowById(cursor.fact.supersedes_id);
      if (!prev) break;
      chain.unshift(prev.fact);
      cursor = prev;
    }
    return respond(200, chain);
  }
  const photoMatch = url.match(/^\/memory\/entities\/([^/]+)\/photo$/);
  if (method === 'POST' && photoMatch) {
    // multipart upload: the stub cannot see bytes; it mirrors the FACT side
    // of the real API (single-valued supersede, `replaced` rides back).
    const made = { id: nextId(), subject_id: photoMatch[1], predicate: 'photo',
      object_id: null, value: `sha-${nextId()}`, status: 'confirmed',
      provenance: { source: 'upload' } };
    const cur = openRows(photoMatch[1], 'photo').find((r) => r.status === 'confirmed');
    if (cur) { supersedeClose(cur); made.supersedes_id = cur.fact.id; made.replaced = [cur.fact]; }
    rows.set(made.id, { fact: made, open: true, status: 'confirmed', deletedFrom: null });
    return respond(201, made);
  }
  if (method === 'DELETE' && photoMatch) {
    const cur = openRows(photoMatch[1], 'photo').find((r) => r.status === 'confirmed');
    if (!cur) return respond(404, { detail: 'no photo' });
    cur.open = false; cur.deletedFrom = cur.status; cur.status = 'rejected';
    return respond(204, null);
  }
  if (method === 'DELETE') return respond(204, null);
  return respond(200, { id: url.split('/').filter(Boolean).pop(), ...body });
};

// jsdom does no layout: report desktop (>=721px) so the screen's own
// matchMedia gates behave like the desktop browser the screen targets.
window.matchMedia = (query) => ({
  matches: query.includes("min-width") && !query.includes("max-width"),
  media: query,
  addEventListener() {}, removeEventListener() {},
  addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false,
});

// Both files run as one script: in the browser, separate <script> tags
// share the global lexical environment, and a strict-mode eval scope is
// the only way to reproduce that here (memory.js's bare `api`/`el`/`$`
// resolve against app.js's declarations).
// Capture the DOMContentLoaded listeners and fire them EXACTLY once:
// jsdom fires the event itself after parsing AND the runner would
// dispatch it again — a double init double-attaches every listener
// (one click on the undo button ran its handler twice).
const dclListeners = [];
const origAddEventListener = document.addEventListener.bind(document);
document.addEventListener = (type, fn, opts) => {
  if (type === "DOMContentLoaded") { dclListeners.push(fn); return; }
  return origAddEventListener(type, fn, opts);
};

window.eval(__APP_JS__ + "\n" + __MEMORY_JS__ + `
;window.__mem = { memoryState, refreshMemory, reloadMemory, renderMemory,
  renderDetail, buildModel, confirmProposal, undoToast, memoryComposer,
  armConfirm, postFact, patchFact, mm: () => mm, openMemoryDrawer,
  init: typeof init === "function" ? init : null, initMemoryTab };`);
document.addEventListener = origAddEventListener;
for (const listener of dclListeners) listener();
document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));

const checks = [];
function check(name, pass, detail = '') { checks.push({ name, pass: Boolean(pass), detail }); }

const settle = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- scenario ----
await (async () => {
  const scenario = async () => {
__SCENARIO__
  };
  try { await scenario(); }
  catch (error) { check('scenario completed without throwing', false, String(error && error.stack || error)); }
})();

console.log(JSON.stringify({ ok: checks.every((c) => c.pass), checks, calls: calls.slice(0, 40) }));
window.close();
process.exit(0);
