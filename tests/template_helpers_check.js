const fs = require('fs');
const T = require('path').join(__dirname, '..', 'web_dashboard', 'templates') + require('path').sep;

// Pull a helper's source straight out of the template so we exercise the real
// code. Anchor on the definition (line-leading `name(...) {`), not markup refs.
// An `async` prefix is part of the definition and is kept — a body with `await` in
// it is a syntax error without it.
function extract(file, name) {
  const src = fs.readFileSync(T + file, 'utf8');
  const re = new RegExp(String.raw`\n[ \t]*(?:async[ \t]+)?` + name + String.raw`\s*\([^)]*\)\s*\{`);
  const m = re.exec(src);
  if (!m) throw new Error(file + ': definition of ' + name + ' not found');
  const start = m.index + m[0].search(/\S/);
  let depth = 0, end = -1;
  for (let j = src.indexOf('{', start); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) { end = j; break; } }
  }
  return src.slice(start, end + 1);
}

const build = (file, name, state) =>
  Object.assign(eval('({' + extract(file, name) + '})'), state);

// Sibling of extract() for a TOP-LEVEL `function name(...) {}` declaration. The globals
// in static/js/app.js (timeAgo, timeUntil, utcStamp) are written that way, so the
// method-shaped anchor above can't see them — and a function declaration can't be
// wrapped in an object literal the way build() does either. Returns the callable.
function extractFn(file, name) {
  const src = fs.readFileSync(T + file, 'utf8');
  const re = new RegExp(String.raw`\nfunction[ \t]+` + name + String.raw`\s*\([^)]*\)\s*\{`);
  const m = re.exec(src);
  if (!m) throw new Error(file + ': function ' + name + ' not found');
  const start = m.index + 1;
  let depth = 0, end = -1;
  for (let j = src.indexOf('{', start); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) { end = j; break; } }
  }
  return eval('(' + src.slice(start, end + 1) + ')');
}

let fail = 0;
const ok = (n, c) => { console.log((c ? 'ok   ' : 'FAIL ') + n); if (!c) fail++; };

const dash = build('dashboard.html', 'regionBreakdown', {});
ok('regionBreakdown sorts desc by total',
  JSON.stringify(dash.regionBreakdown({by_region:{'us-west-2':{total:1,running:1},'us-east-2':{total:3,running:2}}}))
  === JSON.stringify([['us-east-2',{total:3,running:2}],['us-west-2',{total:1,running:1}]]));
ok('regionBreakdown empty when no by_region', dash.regionBreakdown({value:5}).length === 0);
ok('regionBreakdown safe on undefined stat', dash.regionBreakdown(undefined).length === 0);
ok('single-region tile yields 1 entry so the line stays hidden',
  dash.regionBreakdown({by_region:{'us-east-2':{total:3,running:2}}}).length === 1);

for (const [file, fn, arr, field] of [
  ['aws/index.html','filteredInstances','instances','region'],
  ['gcp/index.html','filteredInstances','instances','region'],
  ['k8s/index.html','filteredClusters','clusters','region'],
  ['databases/index.html','filteredDatabases','databases','region'],
  ['azure/index.html','filteredVms','vms','location'],
]) {
  const rows = [{[field]:'r1'},{[field]:'r1'},{[field]:'r2'},{}];
  const key = field === 'location' ? 'filterLocation' : 'filterRegion';
  ok(file+' '+fn+'() unfiltered returns all',
     build(file, fn, {[arr]:rows, [key]:''})[fn]().length === 4);
  ok(file+' '+fn+'() filters to r1',
     build(file, fn, {[arr]:rows, [key]:'r1'})[fn]().length === 2);
}

for (const [file, fn, arr, field] of [
  ['aws/index.html','regions','instances','region'],
  ['gcp/index.html','regions','instances','region'],
  ['k8s/index.html','regions','clusters','region'],
  ['databases/index.html','regions','databases','region'],
  ['azure/index.html','vmLocations','vms','location'],
  ['inventory/list.html','regions','items','region'],
]) {
  const rows = [{[field]:'r2'},{[field]:'r1'},{[field]:'r1'},{},{[field]:''}];
  ok(file+' '+fn+'() distinct+sorted, blanks dropped',
     JSON.stringify(build(file, fn, {[arr]:rows})[fn]()) === JSON.stringify(['r1','r2']));
}

const inv = build('inventory/list.html', 'filtered', {
  items: [
    {cloud:'aws', kind:'vm', region:'us-east-2'},
    {cloud:'aws', kind:'vm', region:'us-west-2'},
    {cloud:'gcp', kind:'vm', region:'us-east-2'},
  ],
  filterProvider:'aws', filterKind:'vm', filterRegion:'us-east-2'});
ok('inventory filtered() ANDs provider+kind+region', inv.filtered().length === 1);

// --- admission allow-list picker (settings.html) ---
// The gate reads a comma-separated string, so the chips UI must round-trip it
// without dropping hand-typed entries the catalog doesn't know about.
const S = 'settings.html';
const mkPanel = (csv, catalog) => {
  const o = {};
  for (const m of ['allowedRegionList','addAllowedRegion','removeAllowedRegion','regionCatalogGroups'])
    Object.assign(o, eval('({' + extract(S, m) + '})'));
  o.panelCfg = { admission_allowed_regions: csv };
  o.regionCatalog = catalog || { aws: [], gcp: [], azure: [], oci: [] };
  return o;
};

let p = mkPanel('us-east-1, us-west-2');
ok('allowedRegionList parses CSV and trims',
   JSON.stringify(p.allowedRegionList()) === JSON.stringify(['us-east-1','us-west-2']));
ok('allowedRegionList empty on blank', mkPanel('').allowedRegionList().length === 0);
ok('allowedRegionList tolerates trailing commas / gaps',
   JSON.stringify(mkPanel(' us-east-1 , , us-west-2,').allowedRegionList())
   === JSON.stringify(['us-east-1','us-west-2']));

p = mkPanel('us-east-1');
p.addAllowedRegion('eastus');
ok('addAllowedRegion appends', p.panelCfg.admission_allowed_regions === 'us-east-1, eastus');
p.addAllowedRegion('eastus');
ok('addAllowedRegion is idempotent', p.panelCfg.admission_allowed_regions === 'us-east-1, eastus');
p.addAllowedRegion('  ');
ok('addAllowedRegion ignores blank', p.panelCfg.admission_allowed_regions === 'us-east-1, eastus');
p.removeAllowedRegion('us-east-1');
ok('removeAllowedRegion drops only that entry', p.panelCfg.admission_allowed_regions === 'eastus');

// A region typed by hand that isn't in any catalog must survive add/remove.
p = mkPanel('us-gov-west-1', {aws:[{id:'us-east-1'}], gcp:[], azure:[], oci:[]});
p.addAllowedRegion('us-east-1');
ok('hand-typed region outside the catalog is preserved',
   p.allowedRegionList().includes('us-gov-west-1'));

p = mkPanel('us-east-1', {aws:[{id:'us-east-1'},{id:'us-west-2'}], gcp:[], azure:[{id:'eastus'}], oci:[]});
const groups = p.regionCatalogGroups();
ok('regionCatalogGroups hides already-chosen regions',
   !JSON.stringify(groups).includes('"us-east-1"'));
ok('regionCatalogGroups drops empty clouds',
   groups.every(g => g.regions.length > 0) && groups.map(g => g.cloud).sort().join(',') === 'aws,azure');

// ── deploy count: the name preview ───────────────────────────────────────────
// The browser previews the series the SERVER will generate, so these fixtures are a
// contract with services/vm_naming.py. The same table is asserted on the Python side
// in tests/test_vm_naming.py::test_shared_fixtures_with_the_js_preview — change one
// and the other fails.
global.window = global.window || {};
window.DEPLOY_COUNT_MAX = 20;
const N = build('../static/js/app.js', 'nameSeries', {});
Object.assign(N, eval('({' + extract('../static/js/app.js', 'namePreview') + '})'));

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

ok('nameSeries numbers from 01',
   eq(N.nameSeries('web', 3, 63), ['web-01', 'web-02', 'web-03']));
ok('nameSeries leaves a count of 1 unsuffixed',
   eq(N.nameSeries('web', 1, 63), ['web']));
ok('nameSeries matches the server on a long azure base',
   eq(N.nameSeries('verylongbasename', 2, 15), ['verylongbase-01', 'verylongbase-02']));
ok('nameSeries lowercases for gcp when asked',
   eq(N.nameSeries('Web-Server', 2, 63, {lower: true}), ['web-server-01', 'web-server-02']));

// The property the whole feature rests on: N names in, N DISTINCT names out. Azure is
// the case that bites — azure_service truncates the guest hostname to 15 characters,
// so a series that only differs past there yields two VMs with one hostname.
const azure = N.nameSeries('web-server-vm-cluster', 4, 15);
ok('azure names fit 15 characters', azure.every(n => n.length <= 15));
ok('azure names stay distinct within the 15-char guest truncation',
   new Set(azure.map(n => n.slice(0, 15))).size === 4);

ok('truncation trims the base, never the suffix',
   N.nameSeries('w'.repeat(100), 3, 63).every(n => n.length === 63 && /-0\d$/.test(n)));
ok('truncation leaves no doubled separator',
   !N.nameSeries('web-server-x-', 2, 15)[0].includes('--'));
ok('count is clamped to the shared ceiling',
   N.nameSeries('web', 999, 63).length === window.DEPLOY_COUNT_MAX);

ok('namePreview stays silent for a single deploy',
   N.namePreview('web', 1, 63).text === '');
ok('namePreview lists a short series in full',
   N.namePreview('web', 3, 63).text === 'will create: web-01, web-02, web-03');
ok('namePreview elides a long series',
   N.namePreview('web', 12, 63).text === 'will create: web-01, web-02, web-03 … web-12 (12 total)');
ok('namePreview flags a truncated base',
   N.namePreview('verylongbasename', 2, 15).truncated === true);
ok('namePreview does not flag a base that fits',
   N.namePreview('web', 2, 15).truncated === false);

// ── Azure: a Location change must clear the region-scoped pickers ─────────────
// The bug these pin: the Location <select> reloaded the subnet/NSG options for the
// new region but left the PREVIOUS region's selection in place. An Azure subnet id
// embeds the VNet's resource group, not its region, and the sandbox names its
// VNet/subnet identically in every region, so the stale pick reads as correct — and
// on the bulk route it fails every VM in the batch, after all N child job rows exist.
// The deploy routes reject the mismatch too (api/azure.py::_reject_cross_region_
// network, pinned in tests/test_azure_region.py); this is the half that keeps the
// form from offering it.
const AZ = 'azure/index.html';
for (const [fn, form] of [['onDeployLocationChange', 'deployModal'],
                          ['onBulkLocationChange', 'bulkModal']]) {
  const loaded = [];
  const o = build(AZ, fn, {
    [form]: {
      location: 'westeurope',                       // just changed to
      subnetId: '/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network'
                + '/virtualNetworks/sandbox-vnet/subnets/vm-subnet',   // centralus
      nsgIds: ['/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network'
               + '/networkSecurityGroups/sandbox-vm-nsg'],
    },
    loadNetworkOpts: (bust, loc) => { loaded.push([bust, loc]); },
  });
  o[fn]();   // async, but everything asserted below happens before its first await
  ok(AZ + ' ' + fn + '() clears the stale subnet', o[form].subnetId === '');
  ok(AZ + ' ' + fn + '() clears the stale NSGs', o[form].nsgIds.length === 0);
  ok(AZ + ' ' + fn + '() reloads the options for the new location',
     eq(loaded, [[true, 'westeurope']]));
}

// ── auto-delete timer: the expiry label + badge ───────────────────────────────
// timeUntil exists because timeAgo CANNOT be reused: timeAgo computes (now - t) and
// collapses every negative result to 'just now', so an expiry — always a future
// timestamp — would render "just now" on every unexpired resource. These pin that,
// the naive-string-is-UTC rule the server's datetime.utcnow() depends on, and that the
// warn colour is driven by the SERVER's warn_hours rather than a hardcoded 24.
const U = {
  timeUntil: extractFn('../static/js/app.js', 'timeUntil'),
  utcStamp: extractFn('../static/js/app.js', 'utcStamp'),
  timeAgo: extractFn('../static/js/app.js', 'timeAgo'),
};

// The reason timeUntil has to exist at all. If this ever stops holding, someone has
// "simplified" the expiry column by reusing timeAgo and every unexpired resource now
// reads "just now".
ok('timeAgo cannot express a future time (why timeUntil exists)',
   U.timeAgo(new Date(Date.now() + 6 * 3600000).toISOString()) === 'just now');

const inFuture = ms => new Date(Date.now() + ms).toISOString();
const MIN = 60000, HR = 3600000, DAY = 86400000;

ok('timeUntil says never for no expiry', U.timeUntil(null) === 'never'
   && U.timeUntil('') === 'never' && U.timeUntil(undefined) === 'never');
ok('timeUntil is overdue for a past time, never "just now"',
   U.timeUntil(new Date(Date.now() - 2 * HR).toISOString()) === 'overdue');
ok('timeUntil counts minutes under an hour', U.timeUntil(inFuture(45 * MIN)) === 'in 45m');
ok('timeUntil never shows 0m for a live timer', U.timeUntil(inFuture(20000)) === 'in 1m');
ok('timeUntil counts hours under 48h', U.timeUntil(inFuture(6 * HR)) === 'in 6h');
ok('timeUntil counts days past 48h', U.timeUntil(inFuture(12 * DAY)) === 'in 12d');
ok('timeUntil is – for an unreadable stamp', U.timeUntil('tomorrow') === '–');

// The server stores naive datetime.utcnow(). Read as LOCAL time, every expiry would be
// wrong by the host's UTC offset — invisible in UTC-based CI, wrong on a laptop.
const naive = new Date(Date.now() + 6 * HR).toISOString().replace('Z', '');
ok('timeUntil reads a naive stamp as UTC, not local',
   U.timeUntil(naive) === U.timeUntil(naive + 'Z'));
ok('utcStamp renders an absolute UTC deadline',
   /^\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$/.test(U.utcStamp('2026-07-28T14:03:00')));
ok('utcStamp is blank for no expiry', U.utcStamp(null) === '' && U.utcStamp('x') === '');

// expiryLabel calls the bare global timeUntil, exactly as it does in the page (app.js
// defines it at top level and the template re-exports it into the Alpine component).
global.timeUntil = U.timeUntil;

const badge = (row, ttl) =>
  build('inventory/list.html', 'expiryBadge',
        { now: Date.now(), ttl: ttl || {} }).expiryBadge(row);
const label = (row, ttl) =>
  build('inventory/list.html', 'expiryLabel',
        { now: Date.now(), ttl: ttl || {} }).expiryLabel(row);

ok('expiryLabel says never with no timer', label({ expires_at: null }) === 'never');
ok('expiryLabel says exempt for an exempt row',
   label({ expires_at: null, expiry_exempt: true }) === 'exempt');
ok('expiryBadge is unstyled with no timer',
   badge({ expires_at: null }).includes('bg-transparent'));
ok('expiryBadge is red inside the last hour',
   badge({ expires_at: inFuture(30 * MIN) }, { warn_hours: 24 }).includes('bg-red-100'));
ok('expiryBadge is yellow inside the warn window',
   badge({ expires_at: inFuture(6 * HR) }, { warn_hours: 24 }).includes('bg-yellow-100'));
ok('expiryBadge is plain outside the warn window',
   badge({ expires_at: inFuture(10 * DAY) }, { warn_hours: 24 }).includes('bg-transparent'));

// The threshold is the SERVER's. Hardcoding 24 here would let /inventory highlight a row
// the dashboard's warning ignores (and vice versa) with nothing to catch the drift.
ok('expiryBadge honours a server warn_hours of 1',
   badge({ expires_at: inFuture(6 * HR) }, { warn_hours: 1 }).includes('bg-transparent'));
ok('expiryBadge honours a server warn_hours of 240',
   badge({ expires_at: inFuture(5 * DAY) }, { warn_hours: 240 }).includes('bg-yellow-100'));

// Report-only vs live: the operator must not read "deleting…" when nothing deletes, nor
// a mild "overdue" when a destroy really is queued. The label keys off the server's single
// folded `deleting` flag — which already accounts for BOTH arming clocks as well as the two
// flags — so the page can't reach a different conclusion than the sweeper did.
const past = new Date(Date.now() - 2 * HR).toISOString();
ok('an overdue row reads "overdue" when nothing is being deleted',
   label({ expires_at: past }, { deleting: false }) === 'overdue');
ok('an overdue row reads "deleting…" when deletion is live',
   label({ expires_at: past }, { deleting: true }) === 'deleting…');
ok('an overdue row defaults to "overdue" when the flag is absent',
   label({ expires_at: past }, {}) === 'overdue');

// ttlWhyNot: the reason shown when nothing is being destroyed, in the order an operator
// would act on it — the setting they'd change first, then the clock they'd wait out.
const why = (ttl) => build('inventory/list.html', 'ttlWhyNot', { ttl, utcStamp: U.utcStamp }).ttlWhyNot();
global.utcStamp = U.utcStamp;
ok('ttlWhyNot names the disabled setting first',
   /not enabled/i.test(why({ enforce: false, dry_run: true })));
ok('ttlWhyNot names report-only when enforcement is on',
   /report-only/i.test(why({ enforce: true, dry_run: true })));
ok('ttlWhyNot names the feature arming delay',
   /still arming/i.test(why({ enforce: true, dry_run: false, armed: false })));
ok('ttlWhyNot names the deletion arming deadline',
   /arms at/i.test(why({ enforce: true, dry_run: false, armed: true,
                         enforce_arms_at: '2026-07-29T14:00:00' })));

process.exit(fail ? 1 : 0);
