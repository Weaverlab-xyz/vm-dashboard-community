/**
 * A policy refusal must reach the operator, with the reason and any offer intact.
 *
 * The admission guardrail answers a denied action with
 *
 *     403 {"detail": {"error": "policy", "reasons": ["region \"eu-west-1\" is not …"]}}
 *
 * and a change-window refusal adds a `schedule` block naming the next occurrence.
 * Neither carries `detail.message`, and `API.request` only understood a string
 * `detail` or `detail.message` — so every guardrail denial in the product surfaced
 * as a bare `HTTP 403`. The reasons were produced, serialised, and thrown away one
 * hop before the screen, which is the same shape of bug
 * `toast_request_access_check.js` exists for.
 *
 * That also made "refuse, and offer to book it" impossible: the offer cannot be
 * rendered by a caller that never receives it.
 *
 * The function under test is cut out of the file that ships it, so editing the real
 * code fails this rather than passing against a copy.
 *
 * Run:  node tests/api_error_reasons_check.js
 * (also driven by tests/test_templates_parse.py so CI picks it up)
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, ...rel.split('/')), 'utf8');
const APP = read('web_dashboard/static/js/app.js');

/** Balanced-brace cut, same technique as tests/toast_request_access_check.js. */
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

// ── a browser, reduced to what API.request touches ───────────────────────────
global.window = {};
let nextResponse = null;
global.fetch = async () => nextResponse;
global.Alpine = { store: () => ({ token: 'test-token', logout() {} }) };

const api = eval('({' + cut(APP, method('request'), 'API.request') + '})');
const sendBlob = eval('({' + cut(APP, method('sendBlob'), 'API.sendBlob') + '})');

function respond(status, body) {
  nextResponse = {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'Forbidden',
    json: async () => body,
  };
}

let failures = 0;
async function check(label, fn) {
  try {
    await fn();
    console.log('  ok   ' + label);
  } catch (e) {
    failures++;
    console.log('  FAIL ' + label + ': ' + e.message);
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

(async () => {
  // ── the bug this file exists for ───────────────────────────────────────────
  await check('a policy denial surfaces its reasons, not "HTTP 403"', async () => {
    respond(403, { detail: { error: 'policy', reasons: ['region "eu-west-1" is not allowed'] } });
    try {
      await api.request('POST', '/api/aws/deploy', {});
      throw new Error('did not throw');
    } catch (e) {
      assert(!/^HTTP 403$/.test(e.message),
        'message was the bare status, so the operator is told nothing: ' + e.message);
      assert(e.message.includes('eu-west-1'), 'reason text missing: ' + e.message);
    }
  });

  await check('several reasons are all shown', async () => {
    respond(403, { detail: { error: 'policy', reasons: ['first rule', 'second rule'] } });
    try {
      await api.request('POST', '/x', {});
      throw new Error('did not throw');
    } catch (e) {
      assert(e.message.includes('first rule') && e.message.includes('second rule'),
        'dropped a reason: ' + e.message);
    }
  });

  await check('reasons are also exposed structurally', async () => {
    respond(403, { detail: { error: 'policy', reasons: ['a', 'b'] } });
    try {
      await api.request('POST', '/x', {});
    } catch (e) {
      assert(Array.isArray(e.reasons) && e.reasons.length === 2,
        'e.reasons missing — a caller cannot render them as a list');
      assert(e.policyError === 'policy', 'e.policyError missing');
    }
  });

  // ── the change-window offer ────────────────────────────────────────────────
  await check('a change-window refusal carries the offer', async () => {
    respond(403, {
      detail: {
        error: 'change_window',
        reasons: ['workgroup "prod" may only be changed during Prod Weekend'],
        schedule: {
          change_window_id: 'cw-1', window_name: 'Prod Weekend',
          next_start: '2026-10-03T06:00:00', next_end: '2026-10-03T10:00:00',
        },
      },
    });
    try {
      await api.request('POST', '/api/aws/deploy', {});
      throw new Error('did not throw');
    } catch (e) {
      assert(e.schedule, 'e.schedule missing — "book it instead" cannot be offered');
      assert(e.schedule.window_name === 'Prod Weekend', 'window name lost');
      assert(e.schedule.change_window_id === 'cw-1', 'window id lost');
      assert(e.policyError === 'change_window',
        'caller cannot tell a window refusal from an ordinary policy denial');
    }
  });

  // ── nothing else changes ───────────────────────────────────────────────────
  await check('a string detail is unchanged', async () => {
    respond(400, { detail: 'Target is not a configured hypervisor.' });
    try {
      await api.request('POST', '/x', {});
    } catch (e) {
      assert(e.message === 'Target is not a configured hypervisor.', e.message);
    }
  });

  await check('detail.message still wins over reasons', async () => {
    respond(403, { detail: { message: 'explicit message', reasons: ['ignored'] } });
    try {
      await api.request('POST', '/x', {});
    } catch (e) {
      assert(e.message === 'explicit message', e.message);
    }
  });

  await check('an empty reasons array falls back to the status', async () => {
    respond(500, { detail: { error: 'policy', reasons: [] } });
    try {
      await api.request('POST', '/x', {});
    } catch (e) {
      assert(e.message === 'HTTP 500', e.message);
    }
  });

  await check('a body with no detail at all still throws usefully', async () => {
    respond(502, {});
    try {
      await api.request('POST', '/x', {});
    } catch (e) {
      assert(e.message === 'HTTP 502', e.message);
    }
  });

  await check('the Entitle deep-link fields still survive', async () => {
    respond(403, {
      detail: {
        message: 'Requires k8s:write', code: 'permission_denied',
        request_access_url: 'https://entitle.example/req', missing_scope: 'k8s',
        missing_level: 'write',
      },
    });
    try {
      await api.request('POST', '/x', {});
    } catch (e) {
      assert(e.requestAccessUrl === 'https://entitle.example/req', 'deep link lost');
      assert(e.missingScope === 'k8s' && e.missingLevel === 'write', 'scope/level lost');
    }
  });

  // ── the upload path must not drift from the main one ───────────────────────
  await check('sendBlob shows reasons too', async () => {
    respond(403, { detail: { error: 'policy', reasons: ['asset refused by policy'] } });
    try {
      await sendBlob.sendBlob('PUT', '/api/storage/x', new Uint8Array(), {});
      throw new Error('did not throw');
    } catch (e) {
      assert(e.message.includes('asset refused by policy'),
        'the upload path still drops the reason: ' + e.message);
    }
  });

  console.log(failures ? `\n${failures} check(s) failed` : '\nall checks passed');
  process.exit(failures ? 1 : 0);
})();
