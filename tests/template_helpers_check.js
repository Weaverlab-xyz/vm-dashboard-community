const fs = require('fs');
const T = require('path').join(__dirname, '..', 'web_dashboard', 'templates') + require('path').sep;

// Pull a helper's source straight out of the template so we exercise the real
// code. Anchor on the definition (line-leading `name(...) {`), not markup refs.
function extract(file, name) {
  const src = fs.readFileSync(T + file, 'utf8');
  const re = new RegExp(String.raw`\n[ \t]*` + name + String.raw`\s*\([^)]*\)\s*\{`);
  const m = re.exec(src);
  if (!m) throw new Error(file + ': definition of ' + name + ' not found');
  const start = m.index + m[0].indexOf(name);
  let depth = 0, end = -1;
  for (let j = src.indexOf('{', start); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) { end = j; break; } }
  }
  return src.slice(start, end + 1);
}

const build = (file, name, state) =>
  Object.assign(eval('({' + extract(file, name) + '})'), state);

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

process.exit(fail ? 1 : 0);
