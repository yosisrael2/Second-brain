// jsdom runner for tests/test_prompts_screen.py: boots the REAL served
// cockpit files (index.html + app.js + prompts.js) against a fetch stub of
// /prompts and prints ONE JSON line with its checks. Placeholders
// (__HTML__, __APP_JS__, __PROMPTS_JS__, __DEFAULTS__) are filled by the test.
let JSDOM = null;
try { ({ JSDOM } = await import('jsdom')); }
catch {
  console.log(JSON.stringify({ skipped: 'jsdom not installed' }));
  process.exit(0);
}
const dom = new JSDOM(__HTML__, { url: 'http://localhost/cockpit#prompts', runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
const { document } = window;
window.confirm = () => true;

// ---- fetch stub: an append-only store like brain.services.prompts ----
const DEFAULTS = __DEFAULTS__;
const META = {
  memory_file: { title: 'Memory file', kind: 'memory_file', placeholders: [], max_chars: 8000 },
  whatsapp_triage: { title: 'WhatsApp triage', kind: 'prompt', placeholders: ['text', 'version'], max_chars: 50000 },
};
const rows = { memory_file: [], whatsapp_triage: [] };   // newest last
const calls = [];
const latest = (key) => rows[key][rows[key].length - 1] ?? null;
const summary = (key) => {
  const row = latest(key);
  const over = row != null && row.text != null;
  return { key, title: META[key].title, description: `${key} description`, kind: META[key].kind,
    is_overridden: over, version: over ? row.version : null,
    latest_version: row ? row.version : null, updated_at: row ? '2026-11-08T10:00:00Z' : null };
};
const detail = (key) => {
  const row = latest(key);
  const over = row != null && row.text != null;
  return { ...summary(key), text: over ? row.text : DEFAULTS[key], default_text: DEFAULTS[key],
    placeholders: META[key].placeholders, max_chars: META[key].max_chars,
    history: [...rows[key]].reverse().map((r) => ({ version: r.version, source: r.source,
      is_default: r.text == null, text: r.text ?? DEFAULTS[key], created_at: '2026-11-08T10:00:00Z' })) };
};
const append = (key, text, source) => {
  const stored = text === DEFAULTS[key] ? null : text;
  const cur = latest(key);
  if ((cur?.text ?? null) === stored) return;
  rows[key].push({ version: (cur?.version ?? 0) + 1, text: stored, source });
};
window.fetch = async (path, options) => {
  const url = String(path);
  const method = options?.method ?? 'GET';
  const body = options?.body ? JSON.parse(options.body) : null;
  const respond = (status, payload) => {
    calls.push({ url, method, body, status });
    return { ok: status < 400, status, statusText: String(status), json: async () => payload };
  };
  if (url === '/prompts') return respond(200, { prompts: Object.keys(META).map(summary) });
  let m = url.match(/^\/prompts\/([^/]+)$/);
  if (m && method === 'GET') return respond(200, detail(m[1]));
  if (m && method === 'PUT') {
    const key = m[1];
    const missing = META[key].placeholders.filter((p) => !body.text.includes(`{${p}}`));
    if (missing.length) return respond(422, { detail: `missing placeholder(s): ${missing.map((p) => `{${p}}`).join(', ')}` });
    append(key, body.text, 'edit');
    return respond(200, detail(key));
  }
  m = url.match(/^\/prompts\/([^/]+)\/reset$/);
  if (m) { if (latest(m[1])?.text != null) rows[m[1]].push({ version: latest(m[1]).version + 1, text: null, source: 'reset' }); return respond(200, detail(m[1])); }
  m = url.match(/^\/prompts\/([^/]+)\/restore\/(\d+)$/);
  if (m) {
    const row = rows[m[1]].find((r) => r.version === Number(m[2]));
    if (!row) return respond(404, { detail: 'no such version' });
    append(m[1], row.text ?? DEFAULTS[m[1]], 'restore');
    return respond(200, detail(m[1]));
  }
  if (url.startsWith('/ops/health')) return respond(200, { overall: 'ok', degraded: [], components: {} });
  return respond(200, {});
};

const dcl = [];
const addEventListener = document.addEventListener.bind(document);
document.addEventListener = (type, fn, opts) => {
  if (type === 'DOMContentLoaded') { dcl.push(fn); return; }
  addEventListener(type, fn, opts);
};
window.eval(__APP_JS__ + "\n" + __PROMPTS_JS__);
for (const fn of dcl) fn();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const settle = async () => { for (let i = 0; i < 6; i++) await sleep(5); };
const checks = [];
const check = (name, pass, detail) => checks.push({ name, pass: Boolean(pass), detail: String(detail ?? '') });
const $ = (id) => document.getElementById(id);
const type = (value) => {
  const area = $('prompt-text');
  area.value = value;
  area.dispatchEvent(new window.Event('input', { bubbles: true }));
};
const item = (key) => document.querySelector(`.pr-item[data-key="${key}"]`);

await settle();
check('tab active', $('tab-prompts').classList.contains('is-active'));
check('list rendered', item('memory_file') && item('whatsapp_triage'), $('prompts-list').textContent);
check('memory file selected first', $('prompt-text')?.value === '', $('prompts-editor').textContent);
check('textarea dir auto', $('prompt-text')?.getAttribute('dir') === 'auto');

item('whatsapp_triage').click();
await settle();
check('default text loaded', $('prompt-text').value === DEFAULTS.whatsapp_triage);
check('default badge', item('whatsapp_triage').textContent.includes('default'));
check('save disabled while clean', $('prompt-save').disabled);
check('placeholders listed', $('prompts-editor').textContent.includes('{version}'));

type('no placeholders here');
check('save enabled when dirty', !$('prompt-save').disabled);
$('prompt-save').click();
await settle();
check('422 detail shown', !$('prompt-error').hidden && $('prompt-error').textContent.includes('missing placeholder(s)'), $('prompt-error').textContent);
check('draft kept after rejection', $('prompt-text').value === 'no placeholders here');

type('Strict.\n{text}\n{version}');
$('prompt-save').click();
await settle();
const put = calls.filter((c) => c.method === 'PUT').pop();
check('valid save PUTs the text', put?.status === 200 && put.body.text === 'Strict.\n{text}\n{version}', JSON.stringify(put));
check('error cleared', $('prompt-error').hidden);
check('changed badge', item('whatsapp_triage').textContent.includes('changed · v1'), item('whatsapp_triage').textContent);

$('prompt-reset').click();
await settle();
check('reset posts', calls.some((c) => c.method === 'POST' && c.url === '/prompts/whatsapp_triage/reset'));
check('back on default', $('prompt-text').value === DEFAULTS.whatsapp_triage);
check('history kept', document.querySelectorAll('.pr-hrow').length === 2);

document.querySelector('[data-restore="1"]').click();
await settle();
check('restore posts', calls.some((c) => c.method === 'POST' && c.url === '/prompts/whatsapp_triage/restore/1'));
check('restored text', $('prompt-text').value === 'Strict.\n{text}\n{version}');
check('restore badge', item('whatsapp_triage').textContent.includes('changed · v3'));

item('memory_file').click();
await settle();
type('רונה בכיתה ה׳ — answer in Hebrew');
$('prompt-save').click();
await settle();
check('memory file saved', calls.some((c) => c.method === 'PUT' && c.url === '/prompts/memory_file' && c.status === 200));
check('memory text shown', $('prompt-text').value === 'רונה בכיתה ה׳ — answer in Hebrew');
check('memory badge', item('memory_file').textContent.includes('v1'));

console.log(JSON.stringify({ ok: checks.every((c) => c.pass), checks }));
process.exit(0);
