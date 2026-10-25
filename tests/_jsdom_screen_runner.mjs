// jsdom runner: boots the REAL served cockpit files against a fetch stub,
// then runs one scenario and prints ONE JSON line with its checks.
// Placeholders (replaced by tests/_jsdom_screen.py before execution):
// are replaced by tests/_jsdom_screen.py before execution.
let JSDOMCtor = null;
try { ({ JSDOM: JSDOMCtor } = await import('jsdom')); }
catch { console.log(JSON.stringify({ skipped: 'jsdom not installed' })); process.exit(0); }

const JSDOM = JSDOMCtor;

const dom = new JSDOM(__HTML__, { url: 'http://localhost/cockpit', runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
const { document } = window;

// ---- fetch stub with call log ----
const fixtures = __FIXTURES__;
const calls = [];
window.fetch = async (path, options) => {
  const url = String(path);
  calls.push({ url, method: (options?.method ?? 'GET'), body: options?.body ? JSON.parse(options.body) : null });
  const respond = (status, payload) => ({
    ok: status < 400, status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  if (url.startsWith('/memory?status=proposed')) return respond(200, { entities: fixtures.proposed ?? [] });
  if (url === '/memory' || url.startsWith('/memory?')) return respond(200, { entities: fixtures.entities ?? [] });
  if (url.startsWith('/ops/health')) return respond(200, fixtures.health ?? { overall: 'ok', degraded: [], components: { api: { details: { version: 'jsdom' } } } });
  if (url.startsWith('/ops/flow')) return respond(200, fixtures.flow ?? {});
  if (url.startsWith('/ops/stats')) return respond(200, fixtures.stats ?? {});
  if (url.startsWith('/ops/events')) return respond(200, []);
  if (url.startsWith('/ops/')) return respond(200, {});
  // mutations echo a plausible fact/entity
  const body = options?.body ? JSON.parse(options.body) : null;
  if (options?.method === 'DELETE') return respond(204, null);
  return respond(201, { id: 'new-1', ...body });
};

// jsdom does no layout: report desktop (>=721px) so the screen's own
// matchMedia gates behave like the desktop browser the screen targets.
window.matchMedia = (query) => ({
  matches: query.includes("min-width") && !query.includes("max-width"),
  media: query,
  addEventListener() {}, removeEventListener() {},
  addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false,
});

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
  armConfirm, postFact, patchFact, mm: () => mm,
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
