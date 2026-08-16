/**
 * Entitle user-JIT Phase 4: the request-access deep link must survive the whole
 * frontend chain — 403 response body → API.request's Error → toast() → the toast
 * object the Alpine renderer reads.
 *
 * Why this exists: the link was produced, parsed, and rendered correctly and still
 * never appeared, because of the one hop nothing covered. All ~142 toast() call
 * sites in this app pass a STRING derived from the caught Error
 * (`toast(e.message, 'error')`, `toast('Deploy failed: ' + e.message, 'error')`,
 * `` toast(`Sync failed: ${e.message}`, 'error') ``), so toast()'s `instanceof
 * Error` branch was unreachable and requestAccessUrl was dropped one step before
 * the renderer. API.request now stashes the Error and toast() re-adopts the link;
 * these cases pin that, and pin that a stale link can't leak onto a later toast.
 *
 * Every function under test is cut out of the file that ships it, so this fails if
 * someone edits the real code rather than passing against a copy.
 *
 * Run:  node tests/toast_request_access_check.js
 * (also driven by tests/test_templates_parse.py so CI picks it up)
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, ...rel.split('/')), 'utf8');

const APP = read('web_dashboard/static/js/app.js');
const BASE = read('web_dashboard/templates/base.html');

/**
 * Cut a definition out of its real source via a balanced-brace scan, like
 * tests/template_helpers_check.js. Two differences from that harness, both
 * required here:
 *   - the anchor allows an INDENTED `function name(...)`, because base.html
 *     declares toast() inside a <script> block rather than at column 0;
 *   - the scan starts at the body `{` the anchor itself matched, NOT at the first
 *     `{` after the name. Both functions here take an object default parameter
 *     (`opts = {}`, `extraHeaders = {}`), and an indexOf('{') would latch onto
 *     that and close the "body" after one character.
 */
function cut(src, re, what) {
  const m = re.exec(src);
  if (!m) throw new Error('definition of ' + what + ' not found');
  const start = m.index + m[0].search(/\S/);
  let depth = 0;
  for (let j = m.index + m[0].length - 1; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced braces in ' + what);
}

const method = (name) =>
  new RegExp(String.raw`\n[ \t]*(?:async[ \t]+)?` + name + String.raw`\s*\([^)]*\)\s*\{`);
const declaration = (name) =>
  new RegExp(String.raw`\n[ \t]*function[ \t]+` + name + String.raw`\s*\([^)]*\)\s*\{`);

// ── a browser, reduced to what the two shipped functions touch ────────────────
global.window = {};
let nextResponse = null;                 // the response fetch() will hand back
global.fetch = async () => nextResponse;
global.Alpine = { store: () => ({ token: 'test-token', logout() {} }) };

const api = eval('({' + cut(APP, method('request'), 'API.request') + '})');
const toast = eval('(' + cut(BASE, declaration('toast'), 'toast') + ')');
const toastManager = eval('(' + cut(BASE, declaration('toastManager'), 'toastManager') + ')');

// The real toastManager, so the assertions run against the object the x-for
// template actually iterates. show()'s duration never lands on that object, so it
// is captured on the way through.
const mgr = toastManager();
const realShow = mgr.show.bind(mgr);
let lastDuration = null;
mgr.show = (m, t, d, x) => { lastDuration = d; return realShow(m, t, d, x); };
window._toast = mgr;

let fail = 0;
const ok = (n, c) => { console.log((c ? 'ok   ' : 'FAIL ') + n); if (!c) fail++; };

// ── fixtures ─────────────────────────────────────────────────────────────────
// The exact 403 body web_dashboard/api/auth.py::require_permission builds once
// _build_request_access_link resolves a link. Pinned on the Python side in
// tests/test_entitle_request_access_link.py — change one, change both.
const URL_ = 'https://entitle.example.com/resources/res-abc123';
const MSG = "Requires 'aws:write' permission.";
const DENIAL = {
  message: MSG,
  missing_scope: 'aws',
  missing_level: 'write',
  request_access_url: URL_,
};

const resp = (status, detail) => ({
  status, ok: status < 400, statusText: 'Forbidden',
  json: async () => ({ detail }),
});

/** Drive a real request to a real failure and return the Error it threw. */
async function thrown(detail, status = 403) {
  nextResponse = resp(status, detail);
  try {
    await api.request('POST', '/api/aws/instances', { name: 'web' });
    return null;
  } catch (e) {
    return e;
  }
}

const last = () => mgr.toasts[mgr.toasts.length - 1] || {};

async function main() {
  // ── the parse hop (already worked; pinned so a refactor can't silently drop it)
  let e = await thrown(DENIAL);
  ok('API.request lifts request_access_url onto the Error', e.requestAccessUrl === URL_);
  ok('API.request keeps the human message', e.message === MSG);
  ok('API.request lifts the missing scope/level',
     e.missingScope === 'aws' && e.missingLevel === 'write');

  // ── the hop that was broken: the four string shapes the 142 call sites use ──
  // Each is a real shape from web_dashboard/templates — the point is that NONE of
  // them passes the Error, and the link has to arrive anyway.
  const shapes = [
    ['bare e.message', (err) => err.message],
    ['concatenated prefix', (err) => 'Deploy failed: ' + err.message],
    ['template literal', (err) => `Sync failed: ${err.message}`],
    ['|| fallback', (err) => err.message || 'Deploy failed'],
  ];
  for (const [name, render] of shapes) {
    e = await thrown(DENIAL);
    toast(render(e), 'error');                       // exactly what a call site does
    ok('link survives a call site passing a ' + name, last().requestAccessUrl === URL_);
    ok('  …and the message is left intact', last().message === render(e));
    ok('  …and the toast lives long enough to click', lastDuration === 8000);
  }

  // ── the renderer's contract ────────────────────────────────────────────────
  // The three hops have to agree on the field name; nothing else checks that the
  // markup reads the property toast() writes.
  ok('base.html renders the link off t.requestAccessUrl',
     /x-show="t\.requestAccessUrl"/.test(BASE) && /:href="t\.requestAccessUrl"/.test(BASE));
  ok('app.js reads the server-side field name request_access_url',
     /detail\.request_access_url/.test(APP));
  ok('the deep link opens safely in a new tab',
     /rel="noopener noreferrer"/.test(BASE));

  // ── no link where there should be none ─────────────────────────────────────
  // user-JIT switched off: auth.py's detail stays a plain string.
  e = await thrown('Requires \'aws:write\' permission.');
  toast(e.message, 'error');
  ok('a plain-string 403 yields no link', last().requestAccessUrl === undefined);
  ok('  …and still shows the reason', last().message === MSG);
  ok('  …and keeps the normal toast duration', lastDuration === 4000);

  // ── staleness: the whole risk of stashing the Error out of band ────────────
  e = await thrown(DENIAL);
  toast(e.message, 'error');
  ok('the linked toast rendered', last().requestAccessUrl === URL_);
  toast('Settings saved', 'success');
  ok('an unrelated later toast does not inherit the link',
     last().requestAccessUrl === undefined);

  // Same message twice: the stash is consumed, so only the first toast is linked.
  e = await thrown(DENIAL);
  toast(e.message, 'error');
  toast(e.message, 'error');
  ok('a repeat toast of the same message is not linked a second time',
     last().requestAccessUrl === undefined);

  // A 403 whose error is swallowed (`catch { this.items = [] }` — several pages do
  // this) must not leave a link waiting for whatever toasts next, however long
  // after, even when the text would have matched.
  await thrown(DENIAL);                              // thrown, never toasted
  ok('API.request stashes the Error for toast() to adopt',
     !!(window.__lastApiError && window.__lastApiError.error));
  if (window.__lastApiError) window.__lastApiError.at -= 6000;   // …and time passes
  toast(MSG, 'error');
  ok('a stash older than the adopt window is ignored',
     last().requestAccessUrl === undefined);

  // A later API failure with no link clears the stash, so a permission denial that
  // was never toasted cannot attach itself to an unrelated error.
  await thrown(DENIAL);                              // thrown, never toasted
  e = await thrown('Instance i-123 is already running.');
  toast(e.message, 'error');
  ok('a subsequent link-free API failure clears the stash',
     last().requestAccessUrl === undefined && window.__lastApiError === null);

  // A message that merely resembles the denial must not pick it up either — the
  // guard is a substring of THIS error's message, not a global text match.
  e = await thrown(DENIAL);
  toast('Requires something else entirely.', 'error');
  ok('a non-matching message does not adopt the link',
     last().requestAccessUrl === undefined);

  // ── the direct-Error branch still works ────────────────────────────────────
  // Unused by the app today, but it is the documented way to toast an Error and
  // the natural thing a new call site will reach for.
  e = await thrown(DENIAL);
  toast(e, 'error');
  ok('toast(Error) still renders the link', last().requestAccessUrl === URL_);
  ok('toast(Error) uses the error message', last().message === MSG);
  ok('toast(Error) consumes the stash too', window.__lastApiError === null);

  // ── toast() must stay safe before Alpine has mounted the container ─────────
  const mounted = window._toast;
  window._toast = null;
  toast(MSG, 'error');                               // must not throw
  window._toast = mounted;
  ok('toast() is a no-op before the container mounts', true);

  process.exit(fail ? 1 : 0);
}

main().catch((err) => { console.log('FAIL harness threw: ' + (err && err.stack)); process.exit(1); });
