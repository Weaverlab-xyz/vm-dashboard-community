// The Entitle panel's persona bundles: its own Alpine component, entitleBundles(), bound
// by x-data inside the Entitle panel rather than spread into settingsPage().
// Included by settings.html.

// The bundle targets, in the order the cards render. Must match
// entitle_persona_bundles.TARGETS (tests/test_entitle_persona_bundles.py pins it).
const ENTITLE_TARGETS = ['aws', 'azure', 'gcp', 'oci', 'ad'];

// One empty pin per target, the shape cloud_settings() returns. Every card binds
// clouds[c].integration_id and clouds[c].resource, so a target missing from `clouds`
// -- before load() returns, or after it fails -- would throw on every render.
function blankEntitleClouds() {
  return Object.fromEntries(ENTITLE_TARGETS.map(c => [c, { integration_id: '', resource: '' }]));
}

function entitleBundles() {
  return {
    loaded: false, configured: false, busy: false, error: '',
    catalog: [], byKey: {}, accesses: [], clouds: blankEntitleClouds(), cands: {}, sets: [], picked: [], preview: {},
    adDirs: [], showDeleg: '', grp: { directory_id: '', ou: '', include_cloud: false, queued: '', job_id: '' },
    cloudKeys: ENTITLE_TARGETS,
    cloudLabel: { aws: 'AWS', azure: 'Azure', gcp: 'GCP', oci: 'OCI', ad: 'On-prem AD' },
    async load() {
      try {
        const r = await API.get('/api/entitle/persona-bundles');
        this.configured = r.configured; this.catalog = r.catalog || [];
        this.clouds = { ...blankEntitleClouds(), ...(r.clouds || {}) };
        this.sets = r.sets || [];
        this.adDirs = r.ad_directories || [];
        if (!this.grp.directory_id && this.adDirs.length) this.grp.directory_id = this.adDirs[0].id;
        this.byKey = Object.fromEntries(this.catalog.map(b => [b.key, b]));
        const seen = {};
        this.accesses = this.catalog.filter(b => !seen[b.access] && (seen[b.access] = true));
      } catch (e) { this.error = e.message; }
      finally { this.loaded = true; }
    },
    async candidates(c) {
      if (this.cands[c]) return;
      try { const r = await API.get('/api/entitle/persona-bundles/cloud-candidates?cloud=' + c);
            this.cands = { ...this.cands, [c]: r.integrations || [] };
            if (r.error) this.error = r.error; }
      catch (e) { this.error = e.message; }
    },
    async pinCloud(c) {
      try { const r = await API.put('/api/entitle/persona-bundles/clouds',
                                    { cloud: c, integration_id: this.clouds[c].integration_id || '',
                                      resource: this.clouds[c].resource || '' });
            this.clouds = { ...blankEntitleClouds(), ...(r.clouds || {}) }; this.preview = {}; }
      catch (e) { this.error = e.message; }
    },
    readyCount() { return this.picked.filter(k => this.preview[k]?.ready).length; },
    async runPreview() {
      this.busy = true; this.error = '';
      try { const r = await API.post('/api/entitle/persona-bundles/preview', { keys: this.picked });
            this.preview = Object.fromEntries((r.bundles || []).map(b => [b.key, b])); }
      catch (e) { this.error = e.message; }
      finally { this.busy = false; }
    },
    async runApply() {
      const keys = this.picked.filter(k => this.preview[k]?.ready);
      if (!confirm('Create ' + keys.length + ' bundle(s) in Entitle? They become requestable immediately.')) return;
      this.busy = true; this.error = '';
      try { const r = await API.post('/api/entitle/persona-bundles/apply', { keys });
            if (r.skipped?.length) this.error = r.skipped.length + ' bundle(s) were no longer ready and were skipped.';
            this.preview = {}; this.picked = []; await this.load(); }
      catch (e) { this.error = e.message; }
      finally { this.busy = false; }
    },
    async createGroups() {
      const g = this.grp;
      const n = this.picked.length;
      if (!confirm('Create the ent-* groups for ' + n + ' selected bundle(s) in AD? Existing groups are left as they are.')) return;
      this.busy = true; this.error = ''; g.queued = ''; g.job_id = '';
      try {
        const r = await API.post('/api/entitle/persona-bundles/create-groups',
                                 { directory_id: g.directory_id, keys: this.picked, include_cloud: g.include_cloud, ou: g.ou });
        g.queued = 'Queued ' + r.groups.length + ' group(s) in ' + r.directory + ': ' +
                   r.groups.map(x => x.name).join(', ');
        g.job_id = r.job_id;
      } catch (e) { this.error = e.message; }
      finally { this.busy = false; }
    },
    async removeSet(s) {
      if (!confirm('Remove these ' + s.bundles.length + ' bundle(s) from Entitle?')) return;
      try { await API.del('/api/entitle/persona-bundles/sets/' + s.id); await this.load(); }
      catch (e) { this.error = e.message; }
    },
  };
}
