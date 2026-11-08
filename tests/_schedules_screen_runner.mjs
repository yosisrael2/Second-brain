// jsdom runner for tests/test_schedules_screen.py: boots the REAL served
// cockpit files (index.html + app.js + schedules.js) against a fetch stub of
// /schedules and prints ONE JSON line with its checks. Placeholders
// (__HTML__, __APP_JS__, __SCHEDULES_JS__) are filled by the test.
let JSDOM = null;
try { ({ JSDOM } = await import('jsdom')); }
catch {
  console.log(JSON.stringify({ skipped: 'jsdom not installed' }));
  process.exit(0);
}
const dom = new JSDOM(__HTML__, { url: 'http://localhost/cockpit#scheduled', runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
const { document } = window;
const confirms = [];
window.confirm = (q) => { confirms.push(q); return true; };

// ---- fetch stub: a tiny /schedules store ----
const WORDS = { '0 7 * * 5': 'Every Friday at 07:00', '30 6 * * *': 'Every day at 06:30', '0 7 * * 0,3': 'Every Sunday, Wednesday at 07:00' };
const tasks = [];
const runs = {};
const calls = [];
const CITED = '11111111-2222-3333-4444-555555555555';
const validate = (body) => {
  if (!body.name.trim()) return 'the name is empty';
  if (body.schedule.trim().split(/\s+/).length !== 5) return 'a schedule needs 5 fields: minute hour day-of-month month day-of-week (got 2)';
  return null;
};
const shape = (t) => ({ ...t, schedule_text: WORDS[t.schedule] || `Cron: ${t.schedule}` });
window.fetch = async (path, options) => {
  const url = String(path);
  const method = options?.method ?? 'GET';
  const body = options?.body ? JSON.parse(options.body) : null;
  const respond = (status, payload) => {
    calls.push({ url, method, body, status });
    return { ok: status < 400, status, statusText: String(status), json: async () => payload };
  };
  if (url === '/schedules/preview') {
    const err = body.schedule.trim().split(/\s+/).length !== 5 ? 'a schedule needs 5 fields' : null;
    if (err) return respond(422, { detail: err });
    return respond(200, { schedule: body.schedule, schedule_text: WORDS[body.schedule] || `Cron: ${body.schedule}`, next_runs: ['2026-11-13T05:00:00Z'] });
  }
  if (url === '/schedules' && method === 'GET') return respond(200, { tasks: tasks.map(shape) });
  if (url === '/schedules' && method === 'POST') {
    const err = validate(body);
    if (err) return respond(422, { detail: err });
    const task = { id: `t${tasks.length + 1}`, ...body, enabled: true, next_run_at: '2026-11-13T05:00:00Z',
      last_run_at: null, last_status: null, run_requested: false, created_at: '2026-11-08T10:00:00Z', updated_at: '2026-11-08T10:00:00Z' };
    tasks.push(task);
    return respond(201, shape(task));
  }
  let m = url.match(/^\/schedules\/([^/?]+)$/);
  if (m) {
    const task = tasks.find((t) => t.id === m[1]);
    if (!task) return respond(404, { detail: 'not found' });
    if (method === 'PUT') {
      const err = validate(body);
      if (err) return respond(422, { detail: err });
      Object.assign(task, body);
      return respond(200, shape(task));
    }
    if (method === 'DELETE') {
      tasks.splice(tasks.indexOf(task), 1);
      calls.push({ url, method, body, status: 204 });
      return { ok: true, status: 204, statusText: '204', json: async () => { throw new Error('no body'); } };
    }
    return respond(200, shape(task));
  }
  m = url.match(/^\/schedules\/([^/]+)\/(pause|resume|run)$/);
  if (m) {
    const task = tasks.find((t) => t.id === m[1]);
    if (m[2] === 'pause') { task.enabled = false; task.next_run_at = null; }
    if (m[2] === 'resume') { task.enabled = true; task.next_run_at = '2026-11-13T05:00:00Z'; }
    if (m[2] === 'run') task.run_requested = true;
    return respond(m[2] === 'run' ? 202 : 200, shape(task));
  }
  m = url.match(/^\/schedules\/([^/]+)\/runs/);
  if (m) return respond(200, { runs: runs[m[1]] || [] });
  if (url.startsWith('/ops/health')) return respond(200, { overall: 'ok', degraded: [], components: {} });
  return respond(200, {});
};

const dcl = [];
const addEventListener = document.addEventListener.bind(document);
document.addEventListener = (type, fn, opts) => {
  if (type === 'DOMContentLoaded') { dcl.push(fn); return; }
  addEventListener(type, fn, opts);
};
window.eval(__APP_JS__ + "\n" + __SCHEDULES_JS__);
for (const fn of dcl) fn();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const settle = async () => { for (let i = 0; i < 6; i++) await sleep(5); };
const checks = [];
const check = (name, pass, detail) => checks.push({ name, pass: Boolean(pass), detail: String(detail ?? '') });
const $ = (id) => document.getElementById(id);
const setValue = (id, value, event = 'input') => {
  const node = $(id);
  node.value = value;
  node.dispatchEvent(new window.Event(event, { bubbles: true }));
};
const card = () => document.querySelector('.sc-card');
const action = (name) => card().querySelector(`[data-action="${name}"]`);

await settle();
check('tab active', $('tab-scheduled').classList.contains('is-active'));
check('nav tab present', document.querySelector('.tab[data-tab="scheduled"]')?.textContent === 'Scheduled');
check('empty state', $('sched-list').textContent.includes('No scheduled tasks yet'), $('sched-list').textContent);
check('editor hidden at start', $('sched-editor').hidden);

// ---- create (weekly preset: Friday 07:00 is the default) ----
$('sched-new').click();
await settle();
check('editor open', !$('sched-editor').hidden && $('sched-preset').value === 'weekly');
check('prompt textarea dir auto', $('sched-prompt').getAttribute('dir') === 'auto' && $('sched-name').getAttribute('dir') === 'auto');
setValue('sched-name', '<b>Rona</b> school mail');
setValue('sched-prompt', 'בדוק אם התקבלו מיילים חדשים מבית הספר של רונה ותסכם');
await sleep(400);
check('preview shows words', $('sched-preview').textContent.startsWith('Every Friday at 07:00 · next:'), $('sched-preview').textContent);
$('sched-save').click();
await settle();
const post = calls.filter((c) => c.method === 'POST' && c.url === '/schedules').pop();
check('create posts the cron', post?.status === 201 && post.body.schedule === '0 7 * * 5' && post.body.timezone === 'Asia/Jerusalem', JSON.stringify(post));
check('editor closed after save', $('sched-editor').hidden);
check('card rendered', card() && card().querySelector('.sc-when').textContent === 'Every Friday at 07:00');
check('name rendered as text', card().querySelector('.sc-name').textContent === '<b>Rona</b> school mail' && !card().querySelector('.sc-name b'));
check('prompt preview dir auto', card().querySelector('.sc-prompt').getAttribute('dir') === 'auto' && card().querySelector('.sc-prompt').textContent.includes('רונה'));
check('active + next run', card().textContent.includes('Active') && card().querySelector('.sc-next').textContent.startsWith('Next run: Fri'), card().querySelector('.sc-next').textContent);

// ---- weekly days + daily preset round-trip through edit ----
action('edit').click();
await settle();
check('edit prefilled', $('sched-name').value === '<b>Rona</b> school mail' && $('sched-preset').value === 'weekly' && $('sched-day-5').checked && $('sched-time').value === '07:00');
$('sched-day-0').click(); $('sched-day-3').click(); $('sched-day-5').click();
await settle();
$('sched-save').click();
await settle();
const put1 = calls.filter((c) => c.method === 'PUT').pop();
check('weekly days -> cron', put1?.body.schedule === '0 7 * * 0,3', JSON.stringify(put1?.body));

action('edit').click();
await settle();
setValue('sched-preset', 'daily', 'change');
setValue('sched-time', '06:30');
$('sched-save').click();
await settle();
const put2 = calls.filter((c) => c.method === 'PUT').pop();
check('daily preset -> cron', put2?.body.schedule === '30 6 * * *', JSON.stringify(put2?.body));

// ---- invalid custom cron: 422 detail shown, draft kept ----
action('edit').click();
await settle();
setValue('sched-preset', 'custom', 'change');
check('custom keeps the current cron', $('sched-cron').value === '30 6 * * *', $('sched-cron').value);
setValue('sched-cron', 'every friday');
await sleep(400);
check('preview warns on bad cron', $('sched-preview').classList.contains('warn'), $('sched-preview').textContent);
$('sched-save').click();
await settle();
check('422 detail shown', !$('sched-error').hidden && $('sched-error').textContent.includes('5 fields'), $('sched-error').textContent);
check('draft kept', $('sched-cron').value === 'every friday' && !$('sched-editor').hidden);
$('sched-cancel').click();
await settle();
check('cancel closes', $('sched-editor').hidden);

// ---- pause / resume / run now ----
action('pause').click();
await settle();
check('pause posts', calls.some((c) => c.method === 'POST' && c.url === '/schedules/t1/pause'));
check('paused badge', card().textContent.includes('Paused') && action('resume'));
action('resume').click();
await settle();
check('resume posts', calls.some((c) => c.method === 'POST' && c.url === '/schedules/t1/resume') && action('pause'));
action('run').click();
await settle();
check('run now posts', calls.some((c) => c.method === 'POST' && c.url === '/schedules/t1/run'));
check('queued hint + run disabled', card().querySelector('.sc-queued') && action('run').disabled);

// ---- history ----
runs.t1 = [
  { id: 'r2', task_id: 't1', trigger: 'manual', scheduled_for: null, started_at: '2026-11-08T10:00:00Z', finished_at: '2026-11-08T10:00:30Z',
    status: 'ok', output: `שני מיילים חדשים [${CITED}] <script>x</script>`, error: null, cited_ids: [CITED] },
  { id: 'r1', task_id: 't1', trigger: 'schedule', scheduled_for: '2026-11-06T05:00:00Z', started_at: '2026-11-06T05:00:10Z', finished_at: '2026-11-06T05:05:10Z',
    status: 'error', output: null, error: 'timed out after 300s', cited_ids: [] },
];
action('history').click();
await settle();
const outputs = [...card().querySelectorAll('.sc-output')];
check('history rows', card().querySelectorAll('.sc-run').length === 2);
check('output with refs', outputs[0]?.textContent === 'שני מיילים חדשים [1] <script>x</script>' && outputs[0].getAttribute('dir') === 'auto', outputs[0]?.textContent);
check('no injected markup', !card().querySelector('.sc-output script'));
check('error shown', outputs[1]?.textContent === 'timed out after 300s');

// ---- delete ----
action('delete').click();
await settle();
check('delete confirms + DELETEs', confirms.some((q) => q.includes('Delete')) && calls.some((c) => c.method === 'DELETE' && c.url === '/schedules/t1'));
check('back to empty', $('sched-list').textContent.includes('No scheduled tasks yet'));

console.log(JSON.stringify({ ok: checks.every((c) => c.pass), checks }));
process.exit(0);
