// Browser smoke test: every page loads with no uncaught JavaScript error.
//
// The text-level suite cannot see the bug class this exists for. An Alpine binding that
// reads a property of an object that is still null (`x-show="obj"` around
// `x-text="obj.name"`), a name the component never defines, a panel whose cards bind
// before their data arrives: each passes every template test and throws on every render
// in a real browser. Every one of those found in October 2026 was found this way, by
// hand, in seconds.
//
// Driven by scripts/ci/browser_smoke.sh, which starts the app, completes setup and logs
// in, and passes:
//   BASE      the app's URL               TOKEN  a bearer token for an admin
//   PAGES     a JSON list of paths         ALLOW  path of the allowlist (JSON)
//   LABEL     a name for this pass, for the report
//   REQUIRE_TAILWIND=1  also fail if the pinned Tailwind CDN script did not load (CI,
//                       where the network is open; a local sandbox may block it)
//
// For each page it: loads it; clicks every visible role="tab" button in turn; and, on
// /settings, opens every integration panel. Any `pageerror` not matched by an allowlist
// entry is a failure. Exit code 1 on any failure, with each one named.
const fs = require('fs');
const path = require('path');

function loadPlaywright() {
  // CI installs it into the repo (npm install); a dev box may have it globally.
  for (const p of ['playwright', '/opt/node22/lib/node_modules/playwright']) {
    try { return require(p); } catch (_) { /* try the next */ }
  }
  throw new Error('playwright is not installed (npm install playwright@1.56.1)');
}

const { chromium } = loadPlaywright();
const BASE = process.env.BASE;
const TOKEN = process.env.TOKEN;
const PAGES = JSON.parse(process.env.PAGES || '[]');
const LABEL = process.env.LABEL || 'smoke';
const ALLOW = JSON.parse(fs.readFileSync(process.env.ALLOW || path.join(__dirname, 'browser_smoke_allow.json'), 'utf8'));

function allowed(page, message) {
  return ALLOW.some(a => (a.page === '*' || a.page === page) && new RegExp(a.message).test(message));
}

(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const ctx = await browser.newContext();
  if (!process.env.REQUIRE_TAILWIND) {
    // Offline mode: nothing leaves the box. Alpine is vendored, so pages still run.
    await ctx.route(/^https?:\/\/(?!127\.0\.0\.1|localhost)/, r => r.abort());
  }
  await ctx.addInitScript(t => {
    localStorage.setItem('vm_cli_token', t);
    localStorage.setItem('vm_cli_username', 'admin');
    localStorage.setItem('vm_cli_is_admin', 'true');
  }, TOKEN);

  const failures = [];
  let visited = 0, tabs = 0, panels = 0, contextRetries = 0;
  for (const pagePath of PAGES) {
    const p = await ctx.newPage();
    const seen = [];
    p.on('pageerror', e => seen.push({ where: pagePath, message: String(e.message || e) }));
    // A 401 makes the API layer log the session out and send the browser to /login, which
    // would otherwise surface only as "Execution context was destroyed" in whatever runs
    // next. Recorded with the request that caused it, so the failure names its own cause.
    p.on('response', r => {
      if (r.status() === 401 && new URL(r.url()).pathname.startsWith('/api/')) {
        seen.push({ where: pagePath, message: `401 from ${r.request().method()} ${new URL(r.url()).pathname} (logs the session out)` });
      }
    });
    // Only once the page has loaded: a page that redirects on load (/login, for a session
    // that is already signed in) is doing its job. After that, nothing the script does
    // should navigate the page, so any navigation request is reported with its method and
    // full URL (a reload or a form post keeps the same path, so the path alone hides it).
    let watchNav = false, navigated = false;
    p.on('request', r => {
      if (watchNav && r.isNavigationRequest() && r.frame() === p.mainFrame()) {
        navigated = true;
        const u = new URL(r.url());
        seen.push({ where: pagePath, message: `navigated: ${r.method()} ${u.pathname}${u.search}` });
      }
    });
    // Diagnostics for an execution context that is destroyed with no navigation request
    // seen (CI only, so far): what the page is now, against what it was after load.
    const recent = [];
    p.on('request', r => { recent.push(`${r.method()} ${new URL(r.url()).pathname}`); if (recent.length > 6) recent.shift(); });
    p.on('framenavigated', fr => { recent.push(`${fr === p.mainFrame() ? 'main' : 'child'} frame → ${fr.url()}`); if (recent.length > 6) recent.shift(); });
    p.on('crash', () => seen.push({ where: pagePath, message: 'renderer crashed' }));
    let origin = 0;
    const docInfo = () => p.evaluate(() => ({ origin: performance.timeOrigin,
      nav: (performance.getEntriesByType('navigation')[0] || {}).type })).catch(e => ({ error: e.message.split('\n')[0] }));
    const diagnose = async () => {
      await p.waitForTimeout(500);
      const d = await docInfo();
      return ` [now ${p.url()}; ${d.error ? d.error : (d.origin === origin ? 'same document' : `NEW document, nav type ${d.nav}`)}; last events: ${recent.join(', ')}]`;
    };
    const recover = async () => {  // reload, so one navigation does not fail every later step
      watchNav = false;
      await p.goto(BASE + pagePath, { waitUntil: 'load' });
      await p.waitForTimeout(1200);
      navigated = false;
      origin = (await docInfo()).origin;
      watchNav = true;
    };
    let status = 0;
    try {
      const resp = await p.goto(BASE + pagePath, { waitUntil: 'load' });
      status = resp ? resp.status() : 0;
      await p.waitForTimeout(1200);
      origin = (await docInfo()).origin;
      watchNav = true;
      visited++;
      if (status >= 500) failures.push(`${pagePath}: HTTP ${status}`);
      if (process.env.REQUIRE_TAILWIND && !(await p.evaluate(() => !!window.tailwind))) {
        failures.push(`${pagePath}: the pinned Tailwind CDN script did not load`);
      }
      if (!(await p.evaluate(() => !!window.Alpine))) {
        failures.push(`${pagePath}: Alpine did not load`);
      }
      // Every tab, by clicking it: the same path a person takes, so each panel's bindings
      // and lazy loads run.
      const tabCount = await p.locator('[role=tab]:visible').count();
      for (let i = 0; i < tabCount; i++) {
        const tab = p.locator('[role=tab]:visible').nth(i);
        if (!(await tab.count())) break;
        const before = seen.length;
        const label = ((await tab.textContent().catch(() => '')) || '').trim().slice(0, 40);
        await tab.click({ timeout: 2000 }).catch(() => {});
        await p.waitForTimeout(250);
        for (const s of seen.slice(before)) s.where = `${pagePath} → tab "${label}"`;
        tabs++;
      }
      if (pagePath === '/settings') {
        if (navigated) await recover();
        const keys = await p.evaluate(() => {
          const el = [...document.querySelectorAll('[x-data]')].find(e => window.Alpine && typeof Alpine.$data(e).openPanel === 'function');
          return el ? (Alpine.$data(el).integrations || []).filter(i => i.hasConfig !== false).map(i => i.key) : [];
        });
        for (const key of keys) {
          if (navigated) await recover();
          const before = seen.length;
          const open = () => p.evaluate(async key => {
            const el = [...document.querySelectorAll('[x-data]')].find(e => typeof Alpine.$data(e).openPanel === 'function');
            const d = Alpine.$data(el);
            await d.openPanel(d.integrations.find(i => i.key === key));
          }, key);
          for (let attempt = 1; ; attempt++) {
            try { await open(); break; } catch (e) {
              const msg = e.message.split('\n')[0];
              // On GitHub's runners Playwright occasionally loses the page's execution
              // context mid-evaluate with the SAME document still loaded (no navigation, no
              // page error): the page is fine, the handle to it is not. One retry for that;
              // a new document, a second loss, or any other error is a failure.
              const d = await docInfo();
              if (attempt === 1 && /Execution context was destroyed/.test(msg) && d.origin === origin) {
                contextRetries++;
                continue;
              }
              seen.push({ where: pagePath, message: `openPanel(${key}) threw: ${msg}` + (await diagnose()) });
              navigated = true;  // whatever it was, start the next panel on a fresh page
              break;
            }
          }
          await p.waitForTimeout(500);
          for (const s of seen.slice(before)) s.where = `${pagePath} → ${key} panel`;
          panels++;
        }
      }
    } catch (e) {
      failures.push(`${pagePath}: ${e.message.split('\n')[0]}`);
    }
    for (const s of seen) {
      if (!allowed(pagePath, s.message)) failures.push(`${s.where}: ${s.message.slice(0, 600)}`);
    }
    await p.close();
  }
  await browser.close();

  console.log(`[${LABEL}] ${visited}/${PAGES.length} pages, ${tabs} tab clicks, ${panels} Settings panels` + (contextRetries ? `, ${contextRetries} retried after a lost execution context` : ''));
  if (failures.length) {
    console.log(`[${LABEL}] ${failures.length} failure(s):`);
    for (const f of [...new Set(failures)]) console.log('  FAIL ' + f);
    process.exit(1);
  }
  console.log(`[${LABEL}] no page errors`);
})().catch(e => { console.error(e); process.exit(1); });
