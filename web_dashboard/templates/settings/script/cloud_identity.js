// Entitle's cloud-identity matrix and user JIT map: loading, showing and saving each.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsCloudIdentity() {
  return {
    // Cloud-identity JIT matrix state (Phase 3) — lives alongside the
    // Entitle integration panel since its config knobs share the same
    // operator mental model.
    ciMatrix: {
      gate_enabled: false,
      flags: { aws: false, azure: false, gcp: false },
      machine: { identity_email: '', ttl_ceiling_minutes: 60, poll_interval_ms: 400 },
      raw: '',
      count: 0,
      parseError: '',
      saving: false,
      loaded: false,
    },

    // User-JIT settings (Phase 4 of Entitle user-JIT plan)
    userJit: {
      enabled: false,
      portal_url: '',
      resource_ids_raw: '{}',
      map_count: 0,
      parseError: '',
      saving: false,
      loaded: false,
    },

    async loadCiMatrix() {
      try {
        const r = await API.get('/api/cloud-identity/matrix');
        this.ciMatrix.gate_enabled = !!r.gate_enabled;
        this.ciMatrix.flags = {
          aws: !!(r.flags?.aws), azure: !!(r.flags?.azure), gcp: !!(r.flags?.gcp),
        };
        this.ciMatrix.machine = {
          identity_email: r.machine?.identity_email || '',
          ttl_ceiling_minutes: r.machine?.ttl_ceiling_minutes ?? 60,
          poll_interval_ms: r.machine?.poll_interval_ms ?? 400,
        };
        this.ciMatrix.raw = r.matrix_raw && r.matrix_raw.trim()
          ? r.matrix_raw
          : JSON.stringify(r.matrix || {}, null, 2);
        this.ciMatrix.count = Object.keys(r.matrix || {}).length;
        this.ciMatrix.parseError = '';
        this.ciMatrix.loaded = true;
      } catch (e) {
        this.ciMatrix.parseError = 'Failed to load: ' + (e.message || e);
      }
    },

    formatCiMatrix() {
      try {
        const parsed = JSON.parse(this.ciMatrix.raw || '{}');
        this.ciMatrix.raw = JSON.stringify(parsed, null, 2);
        this.ciMatrix.count = Object.keys(parsed).length;
        this.ciMatrix.parseError = '';
      } catch (e) {
        this.ciMatrix.parseError = 'Not valid JSON: ' + e.message;
      }
    },

    async saveCiMatrix() {
      let parsed = {};
      const raw = (this.ciMatrix.raw || '').trim();
      if (raw) {
        try {
          parsed = JSON.parse(raw);
        } catch (e) {
          this.ciMatrix.parseError = 'Value must be valid JSON: ' + e.message;
          return;
        }
      }
      this.ciMatrix.parseError = '';
      this.ciMatrix.saving = true;
      try {
        const mres = await API.patch('/api/cloud-identity/matrix', { matrix: parsed });
        this.ciMatrix.count = mres.count ?? Object.keys(parsed).length;
        await API.patch('/api/cloud-identity/flags', {
          gate_enabled: this.ciMatrix.gate_enabled,
          aws_enabled: this.ciMatrix.flags.aws,
          azure_enabled: this.ciMatrix.flags.azure,
          gcp_enabled: this.ciMatrix.flags.gcp,
          machine_identity_email: this.ciMatrix.machine.identity_email || '',
          ttl_ceiling_minutes: this.ciMatrix.machine.ttl_ceiling_minutes,
          poll_interval_ms: this.ciMatrix.machine.poll_interval_ms,
        });
        this.ciMatrix.raw = JSON.stringify(parsed, null, 2);
      } catch (e) {
        this.ciMatrix.parseError = 'Save failed: ' + (e.message || e);
      } finally {
        this.ciMatrix.saving = false;
      }
    },

    async loadUserJit() {
      try {
        const r = await API.get('/api/setup/feature/entitle');
        this.userJit.enabled = !!r.entitle_user_jit_enabled;
        this.userJit.portal_url = r.entitle_request_portal_url || '';
        const raw = r.entitle_resource_ids_json && r.entitle_resource_ids_json.trim()
          ? r.entitle_resource_ids_json
          : '{}';
        this.userJit.resource_ids_raw = raw;
        try {
          this.userJit.map_count = Object.keys(JSON.parse(raw) || {}).length;
        } catch (_) {
          this.userJit.map_count = 0;
        }
        this.userJit.parseError = '';
        this.userJit.loaded = true;
      } catch (e) {
        this.userJit.parseError = 'Failed to load: ' + (e.message || e);
      }
    },

    formatUserJitMap() {
      try {
        const parsed = JSON.parse(this.userJit.resource_ids_raw || '{}');
        this.userJit.resource_ids_raw = JSON.stringify(parsed, null, 2);
        this.userJit.map_count = Object.keys(parsed).length;
        this.userJit.parseError = '';
      } catch (e) {
        this.userJit.parseError = 'Not valid JSON: ' + e.message;
      }
    },

    async saveUserJit() {
      let parsed = {};
      const raw = (this.userJit.resource_ids_raw || '').trim();
      if (raw) {
        try {
          parsed = JSON.parse(raw);
        } catch (e) {
          this.userJit.parseError = 'Value must be valid JSON: ' + e.message;
          return;
        }
      }
      this.userJit.parseError = '';
      this.userJit.saving = true;
      try {
        await API.patch('/api/setup/feature/entitle', {
          entitle_user_jit_enabled: this.userJit.enabled,
          entitle_request_portal_url: this.userJit.portal_url || '',
          entitle_resource_ids_json: raw ? JSON.stringify(parsed) : '{}',
        });
        this.userJit.map_count = Object.keys(parsed).length;
        this.userJit.resource_ids_raw = JSON.stringify(parsed, null, 2);
      } catch (e) {
        this.userJit.parseError = 'Save failed: ' + (e.message || e);
      } finally {
        this.userJit.saving = false;
      }
    },
  };
}
