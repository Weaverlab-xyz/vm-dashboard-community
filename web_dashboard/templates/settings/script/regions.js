// Regions: the admission allow-list picker and the Multi-region panel's per-cloud sets.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsRegions() {
  return {
    // Cross-cloud per-region config sets — the Multi-region panel. One state block
    // per cloud, each loaded from / saved to /api/setup/regions/{cloud}. The generic
    // editor markup is driven by the server-provided fields + fallbacks.
    regionSets: {
      aws:   { defaultRegion: '', fields: [], fallbacks: {}, regions: {}, newRegion: '', loaded: false, saving: false, error: '' },
      gcp:   { defaultRegion: '', fields: [], fallbacks: {}, regions: {}, newRegion: '', loaded: false, saving: false, error: '' },
      azure: { defaultRegion: '', fields: [], fallbacks: {}, regions: {}, newRegion: '', loaded: false, saving: false, error: '' },
    },

    // Region catalog per cloud, for the admission allow-list picker.
    regionCatalog: { aws: [], gcp: [], azure: [], oci: [] },

    // admission_allowed_regions is stored as a comma-separated string (the gate
    // also accepts a JSON array). These helpers read/write that string so the
    // chips UI and the raw text input stay in sync.
    allowedRegionList() {
      return (this.panelCfg.admission_allowed_regions || '')
        .split(',').map(s => s.trim()).filter(Boolean);
    },

    addAllowedRegion(id) {
      const r = (id || '').trim();
      if (!r) return;
      const cur = this.allowedRegionList();
      if (cur.includes(r)) return;
      this.panelCfg.admission_allowed_regions = [...cur, r].join(', ');
    },

    removeAllowedRegion(id) {
      this.panelCfg.admission_allowed_regions =
        this.allowedRegionList().filter(r => r !== id).join(', ');
    },

    // Catalog grouped by cloud, minus regions already on the allow-list.
    regionCatalogGroups() {
      const chosen = new Set(this.allowedRegionList());
      return Object.entries(this.regionCatalog)
        .map(([cloud, regions]) => ({
          cloud,
          regions: (regions || []).filter(r => !chosen.has(r.id)),
        }))
        .filter(g => g.regions.length > 0);
    },

    async loadRegionCatalog() {
      for (const cloud of Object.keys(this.regionCatalog)) {
        try {
          const r = await API.get(`/api/regions?cloud=${cloud}`);
          this.regionCatalog[cloud] = r?.regions || [];
        } catch (e) { this.regionCatalog[cloud] = []; }
      }
    },

    async loadRegionSets(cloud) {
      const s = this.regionSets[cloud];
      try {
        const r = await API.get(`/api/setup/regions/${cloud}`);
        s.defaultRegion = r.default_region || '';
        s.fields = r.fields || [];
        s.fallbacks = r.fallbacks || {};
        const regions = {};
        for (const [loc, cfg] of Object.entries(r.regions || {})) {
          const entry = {};
          for (const f of s.fields) entry[f] = (cfg && cfg[f]) || '';
          regions[loc] = entry;
        }
        s.regions = regions;
        s.error = '';
        s.loaded = true;
      } catch (e) {
        s.error = 'Failed to load region sets: ' + (e.message || e);
        s.loaded = true;
      }
    },
    addRegion(cloud) {
      const s = this.regionSets[cloud];
      const loc = (s.newRegion || '').trim().toLowerCase().replace(/\s+/g, '');
      if (!loc) return;
      if (!s.regions[loc]) {
        const entry = {};
        for (const f of s.fields) entry[f] = '';
        s.regions[loc] = entry;
      }
      s.newRegion = '';
    },
    removeRegion(cloud, loc) {
      delete this.regionSets[cloud].regions[loc];
    },
    async saveRegionSets(cloud) {
      const s = this.regionSets[cloud];
      s.saving = true;
      s.error = '';
      try {
        await API.put(`/api/setup/regions/${cloud}`, { regions: s.regions });
        this.showToast(`${cloud.toUpperCase()} region sets saved.`);
        // Reload so blank-only regions the server dropped disappear from the editor.
        await this.loadRegionSets(cloud);
      } catch (e) {
        s.error = 'Save failed: ' + (e.message || e);
      } finally {
        s.saving = false;
      }
    },
  };
}
