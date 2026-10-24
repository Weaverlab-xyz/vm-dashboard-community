// The panels' Test buttons: each one's running / result state and the call it makes.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsConnectionTests() {
  return {
    // OIDC discovery probe result (Test connection in the SSO panel).
    oidcTest: { running: false, ok: null, error: '', doc: null },
    wkTest: { running: false, ok: null, error: '', doc: null },
    // Which credential posture each cloud is actually on. Explicitly fetched rather
    // than polled: it is a diagnostic an operator asks for after flipping a flag.
    wlcSources: { running: false, error: '', clouds: [] },
    // Unmetered GET /session probe (Test connection in the Workload Credentials panel).
    // Distinct from wlcSources: that reports stored lease state, this authenticates live.
    wlcTest: { running: false, ok: null, error: '', message: '' },
    btIdTest: { running: false, ok: null, error: '', message: '', claims: '' },

    // Skytap "Test connection". One page of the template catalogue, which proves the
    // token AND what it can see — an account that authenticates but exposes no templates
    // cannot build a POV, so it is reported red rather than green.
    skytapTest: { running: false, ok: null, error: '', message: '' },
    povCloudTest: { running: false, ok: null, error: '', message: '' },

    // Authenticate against Workload Credentials for real. Unmetered, so it costs
    // nothing to press. Reads the SAVED config, not the form — save before testing.
    async testWlcConnection() {
      this.wlcTest = { running: true, ok: null, error: '', message: '' };
      try {
        const r = await API.post('/api/secrets/wlc/test', {});
        this.wlcTest = { running: false, ok: true, error: '', message: r.message || 'Connected.' };
      } catch (e) {
        this.wlcTest = { running: false, ok: false, error: e.message || String(e), message: '' };
      }
    },

    // Fetch the shared BeyondTrust platform identity's token and show its claims.
    // Free and local — no BeyondTrust endpoint is called. Reads the SAVED config.
    async testBtIdentity() {
      this.btIdTest = { running: true, ok: null, error: '', message: '', claims: '' };
      try {
        const r = await API.post('/api/setup/bt-platform-identity/test', {});
        this.btIdTest = { running: false, ok: true, error: '', message: r.message || 'Got a token.',
                          claims: JSON.stringify(r.claims || {}, null, 2) };
      } catch (e) {
        this.btIdTest = { running: false, ok: false, error: e.message || String(e), message: '', claims: '' };
      }
    },

    // Prove the Skytap credentials for real. Reads the SAVED config, not the form.
    //
    // NOTE the difference from testWlcConnection above: that endpoint RAISES on a bad
    // credential, so its only failure path is the catch. This one answers 200 either way
    // with the verdict in the body, because "these credentials do not work" is an answer
    // rather than a transport failure. So r.ok has to be read — treating any 200 as
    // success would paint a failed check green.
    async testSkytapConnection() {
      this.skytapTest = { running: true, ok: null, error: '', message: '' };
      try {
        const r = await API.post('/api/setup/skytap/test', {});
        this.skytapTest = {
          running: false,
          ok: !!r.ok,
          message: r.ok ? (r.message || 'Connected.') : '',
          error: r.ok ? '' : (r.message || 'The check failed.'),
        };
      } catch (e) {
        // Only reached for a transport or auth-to-the-dashboard failure (401 from
        // _require_admin), never for a bad Skytap credential.
        this.skytapTest = { running: false, ok: false, error: e.message || String(e), message: '' };
      }
    },

    // Same 200-either-way contract as testSkytapConnection above, and the same reason:
    // routed through the lab-platform registry, so it tests whichever cloud is selected
    // without this function knowing which clouds exist.
    async testPovCloudConnection() {
      this.povCloudTest = { running: true, ok: null, error: '', message: '' };
      try {
        const r = await API.post('/api/setup/pov-cloud/test', {});
        this.povCloudTest = {
          running: false,
          ok: !!r.ok,
          message: r.ok ? (r.message || 'Connected.') : '',
          error: r.ok ? '' : (r.message || 'The check failed.'),
        };
      } catch (e) {
        this.povCloudTest = { running: false, ok: false, error: e.message || String(e), message: '' };
      }
    },

    async checkWlcSources() {
      this.wlcSources = { running: true, error: '', clouds: [] };
      try {
        const r = await API.get('/api/secrets/credential-sources');
        this.wlcSources = { running: false, error: '', clouds: r.clouds || [] };
      } catch (e) {
        this.wlcSources = { running: false, error: e.message || String(e), clouds: [] };
      }
    },

    async testWorkloadIdp() {
      this.wkTest = { running: true, ok: null, error: '', doc: null };
      try {
        const doc = await API.post('/api/setup/oidc/workload-test', {});
        this.wkTest = { running: false, ok: true, error: '', doc };
      } catch (e) {
        this.wkTest = { running: false, ok: false, error: e.message || String(e), doc: null };
      }
    },

    async testOidc() {
      this.oidcTest = { running: true, ok: null, error: '', doc: null };
      try {
        const doc = await API.post('/api/setup/oidc/test', {});
        this.oidcTest = { running: false, ok: true, error: '', doc };
      } catch (e) {
        this.oidcTest = { running: false, ok: false, error: e.message || String(e), doc: null };
      }
    },
  };
}
