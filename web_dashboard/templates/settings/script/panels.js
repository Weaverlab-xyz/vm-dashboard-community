// The slide-over panel an integration's Configure button opens: which panel is open, its
// config and secrets, loading and saving it, and the read-only facts some panels show
// beside the form. The panels themselves are templates/settings/panels/<key>.html.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsPanels() {
  return {
    panel: null,
    // Layout state inside the open flyout: which of its tabs is showing (a long panel sets
    // its first tab from its own tab bar's x-init) and which collapsible sections are open.
    // Not in the URL fragment, which already belongs to settingsTab.
    panelTab: '',
    panelOpen: {},
    panelLoading: false,
    panelSaving: false,
    panelError: '',
    panelCfg: {},
    // Masked fields the operator typed into this time the panel was open. The server
    // sends secrets back empty, so this is the only thing that tells a deliberately
    // cleared box apart from one that was never touched.
    panelSecretsTouched: [],
    entitleAgentToken: '',   // read-only display of the auto-minted Entitle agent token name
    agentAudience: null,     // read-only view of the pinned agent signing audience (Remote Agents panel)
    oauthRedirectUris: null, // read-only: the SSO callbacks the server will send (OIDC panel)
    spiffeIdentity: null,    // read-only status of the dashboard's own SPIFFE token files (SPIFFE and SPIRE panel)
    spiffeIssuerCheck: { running: false, result: null, error: '' },
    agentAudienceResetting: false,
    rancherFirewall: null,   // read-only breakdown of the Rancher node firewall allow-list (k8s panel)

    // The OIDC callback to register. An edited-but-unsaved UI base URL previews what
    // saving it will produce (same normalisation as services/public_url.normalize);
    // otherwise it is exactly what the server sends.
    oidcCallbackUri() {
      const path = '/api/auth/oauth/oidc/callback';
      let typed = (this.panelCfg?.ui_base_url || '').trim();
      const saved = this.oauthRedirectUris?.ui_base_url || '';
      if (typed) {
        if (!typed.includes('://')) typed = 'https://' + typed;
        const [scheme, rest] = [typed.split('://')[0], typed.split('://').slice(1).join('://')];
        const host = rest.split('/')[0];
        const origin = host ? `${scheme.toLowerCase()}://${host}` : '';
        if (origin && origin !== saved) return origin + path;
      }
      return this.oauthRedirectUris?.oidc || (window.location.origin + path);
    },

    async openPanel(intg) {
      this.panel = intg;
      this.panelCfg = {};
      this.panelSecretsTouched = [];
      this.panelTab = '';
      this.panelOpen = {};
      this.wlcTest = { running: false, ok: null, error: '', message: '' };
      this.btIdTest = { running: false, ok: null, error: '', message: '', claims: '' };
      this.skytapTest = { running: false, ok: null, error: '', message: '' };
      this.povCloudTest = { running: false, ok: null, error: '', message: '' };
      this.panelError = '';
      this.panelLoading = true;
      try {
        this.panelCfg = await API.get(`/api/setup/feature/${intg.key}`);
        // Remove the 'enabled' key — that's controlled by the toggle, not the form
        delete this.panelCfg.enabled;
        if (intg.key === 'entitle') {
          // Entitle's API base is REGIONAL — api.us / api.ca / the unprefixed one are
          // separate deployments, and every one of them answers, so a wrong region is
          // accepted here and surfaces later as a tenant holding none of your resources.
          // Prefilled with the US region to match EntitleFeatureConfig's default; if your
          // tenant is elsewhere, change it. Kept in sync with that default by
          // tests/test_bt_tenants.py rather than by anyone remembering.
          if (!this.panelCfg.entitle_api_url) {
            this.panelCfg.entitle_api_url = 'https://api.us.entitle.io/v1';
          }
          // The agent token name is auto-minted server-side — show it read-only and keep it
          // OUT of the save payload so a stale open panel can't blank the minted value.
          this.entitleAgentToken = this.panelCfg.entitle_agent_token_name || '';
          delete this.panelCfg.entitle_agent_token_name;
        }
        // Ansible requires a configured + active storage backend before enable.
        if (intg.key === 'ansible') {
          await this.loadStorageStatus();
          this.normalizeRunnerBackends();
        }
        // Phase 3: lazy-load the cloud-identity matrix when the Entitle
        // panel opens so the textarea + flag toggles are pre-populated.
        if (intg.key === 'entitle' && !this.ciMatrix.loaded) {
          await this.loadCiMatrix();
        }
        // Phase 4: same lazy-load for the user-JIT subsection.
        if (intg.key === 'entitle' && !this.userJit.loaded) {
          await this.loadUserJit();
        }
        // Multi-region: lazy-load all three clouds' per-region config sets.
        if (intg.key === 'multi_region') {
          await Promise.all(['aws', 'gcp', 'azure'].map(c => this.loadRegionSets(c)));
        }
        // Kubernetes: lazy-load the read-only Rancher firewall breakdown so the
        // operator can see which sources are auto-whitelisted (clusters + Gateway).
        if (intg.key === 'k8s_management') {
          this.rancherFirewall = null;
          try {
            this.rancherFirewall = await API.get('/api/containers/rancher/firewall');
          } catch (e) { /* node/GCP not configured yet — panel just omits the box */ }
        }
        // Password Safe: the GCP cloud-run channel's audience is no longer a single
        // config value — a deployed per-region service beats it — so the panel has to
        // show what each region actually resolves to, or the box lies about three
        // regions out of four.
        if (intg.key === 'password_safe') {
          this.dbops = null;
          await this.loadDbops();
        }
        // Remote agents: the pinned signing audience is write-once and was previously
        // invisible, so an audience pinned to the wrong hostname looked identical to a
        // revoked agent. Read-only — the panel never puts it in the save payload.
        // POV cloud provider: which AWS credential the panel shows is DERIVED from what
        // is stored, never saved as its own setting — a stored key wins over federation
        // in aws_service._aws_kwargs, so a separate "mode" value could only ever disagree
        // with what the calls actually use.
        if (intg.key === 'pov_cloud') {
          const c = this.panelCfg || {};
          const storedKey = !!c.aws_access_key_id
            || (c._secrets_set || []).includes('aws_secret_access_key');
          const federated = !storedKey && c.dashboard_spiffe_identity_enabled
                            && c.dashboard_spiffe_aud_aws;
          this.povAwsSource = !federated ? 'key'
            : (c.wlc_aws_everyday_source === 'federation' ? 'spiffe_wlc' : 'spiffe');
          this.spiffeIssuerCheck = { running: false, result: null, error: '' };
          // Azure: the client id stays either way — with SPIFFE it names the app whose
          // federated credential trusts the dashboard — so only a stored SECRET decides.
          const storedAzureSecret = (c._secrets_set || []).includes('azure_client_secret');
          const azureFederated = !storedAzureSecret && c.dashboard_spiffe_identity_enabled
                                 && c.dashboard_spiffe_aud_azure;
          this.povAzureSource = !azureFederated ? 'secret'
            : (c.wlc_azure_everyday_source === 'federation' ? 'spiffe_wlc' : 'spiffe');
          // GCP: a stored key wins; the SPIFFE path needs the identity and a workload
          // identity provider (a resource name, not a checkbox).
          const storedGcpKey = (c._secrets_set || []).includes('gcp_service_account_json');
          const gcpFederated = !storedGcpKey && c.dashboard_spiffe_identity_enabled
                               && !!(c.dashboard_spiffe_aud_gcp || '').trim();
          this.povGcpSource = !gcpFederated ? 'key'
            : (c.pov_gcp_provision_source === 'entitle' ? 'spiffe_entitle' : 'spiffe');
          this.povWlcSources = null;
          this.povWlcAzure = null;
          try {
            const r = await API.get('/api/secrets/credential-sources');
            this.povWlcSources = (r.clouds || []).find(x => x.cloud === 'aws') || null;
            this.povWlcAzure = (r.clouds || []).find(x => x.cloud === 'azure') || null;
          } catch (e) { /* read-only status; admin-only, and the panel still saves without it */ }
          this.spiffeIdentity = null;
          try {
            this.spiffeIdentity = await API.get('/api/spiffe-identity');
          } catch (e) { /* read-only status; the panel still saves without it */ }
        }
        if (intg.key === 'oidc') {
          this.oauthRedirectUris = null;
          try {
            this.oauthRedirectUris = await API.get('/api/auth/oauth/redirect-uris');
          } catch (e) { /* oidcCallbackUri() falls back to the browser origin */ }
        }
        if (intg.key === 'remote_agents') {
          this.agentAudience = null;
          try {
            this.agentAudience = await API.get('/api/agents/audience');
          } catch (e) { /* the router is feature-gated; off means 404, and this panel is where you turn it on */ }
        }
        if (intg.key === 'spiffe') {
          this.spiffeIssuerCheck = { running: false, result: null, error: '' };
          this.spiffeIdentity = null;
          try {
            this.spiffeIdentity = await API.get('/api/spiffe-identity');
          } catch (e) { /* read-only status; the panel still saves without it */ }
        }
        // Containers: same read-only breakdown for the managed Portainer node, so the
        // operator can see whether the node is actually reachable and from where.
        if (intg.key === 'portainer') {
          this.portainerFirewall = null;
          try {
            this.portainerFirewall = await API.get('/api/containers/portainer/node/firewall');
          } catch (e) { /* node/GCP not configured yet — panel just omits the box */ }
        }
        // Auto-delete: the last sweep report is the only place an operator can see what
        // the feature would actually do, so it loads with the panel.
        if (intg.key === 'resource_expiry') {
          this.expirySweep = null;
          try {
            this.expirySweep = await API.get('/api/expiry/last-sweep');
          } catch (e) { /* never run yet — the summary line says so */ }
        }
        // Job Worker: the saved limits are clamped before use, so the panel is misleading
        // without the worker's own readout of what it resolved them to.
        if (intg.key === 'worker') {
          this.workerStatus = null;
          await this.loadWorkerStatus();
        }
      } catch (e) {
        this.panelError = 'Failed to load configuration: ' + e.message;
      } finally {
        this.panelLoading = false;
      }
    },

    // Notification endpoints and change windows used to be methods here; they are
    // notificationEndpoints() / changeWindowsEditor() in static/js/app.js now, shared with
    // the pages that non-admins holding those scopes use.

    // `tab` lands a cross-link on one tab of a tabbed panel. openPanel clears panelTab
    // before its first await, so setting it straight after survives the reset, and the
    // panel's tab bar only defaults it when it is still empty.
    openPanelByKey(key, tab = '') {
      const intg = this.integrations.find(i => i.key === key);
      if (!intg) return;
      this.openPanel(intg);
      this.panelTab = tab;
    },

    // Per-cloud runner backends fall back to the legacy global ansible_runner /
    // k8s_runner. On load, pre-fill each blank per-cloud key from the global
    // mapped to its matching cloud (e.g. global 'aci' → Azure='aci', others
    // 'local') so the selects reflect the effective config; the operator can
    // then set each cloud explicitly. Saved values are always explicit per-cloud.
    normalizeRunnerBackends() {
      const c = this.panelCfg;
      const svcByCloud = { aws: 'ecs', azure: 'aci', gcp: 'gcp' };
      for (const runner of ['ansible_runner', 'k8s_runner']) {
        const globalMode = c[runner] || 'local';
        for (const cloud of ['aws', 'azure', 'gcp']) {
          const key = `${runner}_${cloud}`;
          if (!c[key]) c[key] = (globalMode === svcByCloud[cloud]) ? globalMode : 'local';
        }
      }
    },

    // Records which masked field an edit landed in. Delegated from the panel body so a
    // new secret input is covered automatically instead of needing its own handler; the
    // field name comes off x-model, which Alpine leaves on the element.
    noteSecretEdit(ev) {
      const el = ev.target;
      if (!el || el.type !== 'password') return;
      const model = el.getAttribute('x-model') || '';
      // Only panelCfg-bound inputs: the notification editor binds notif.draft.secret and
      // owns its own keep/clear contract.
      if (!model.startsWith('panelCfg.')) return;
      const field = model.slice('panelCfg.'.length);
      if (field && !this.panelSecretsTouched.includes(field)) {
        this.panelSecretsTouched.push(field);
      }
    },

    async checkSpiffeIssuer() {
      this.spiffeIssuerCheck = { running: true, result: null, error: '' };
      try {
        const result = await API.post('/api/spiffe-identity/check-issuer', {});
        this.spiffeIssuerCheck = { running: false, result, error: '' };
      } catch (e) {
        this.spiffeIssuerCheck = { running: false, result: null, error: e.message };
      }
    },

    // Reset the pinned agent signing audience. Destructive in a way that is not obvious
    // from the button — it does not touch any agent row, but every enrolled agent starts
    // failing authentication — so the count goes in the confirm text.
    async resetAgentAudience() {
      const a = this.agentAudience || {};
      const n = a.agents_enrolled || 0;
      const affected = n
        ? `\n\n${n} enrolled agent(s) will stop authenticating and must be re-enrolled with a fresh code.`
        : '';
      if (!confirm(`Reset the signing audience?\n\nIt is pinned to ${a.pinned}. The next enrolment code will pin it again — from Public base URL if you have set one, otherwise from the origin your browser is on.${affected}`)) return;
      this.agentAudienceResetting = true;
      try {
        const r = await API.delete('/api/agents/audience');
        this.agentAudience = r;
        this.showToast(r.detail || 'Signing audience reset.');
      } catch (e) {
        this.showToast('Could not reset the signing audience: ' + e.message, 'error');
      } finally {
        this.agentAudienceResetting = false;
      }
    },

    async savePanelConfig() {
      if (!this.panel) return;
      this.panelSaving = true;
      this.panelError = '';
      try {
        const body = { enabled: this.panel.enabled, ...this.panelCfg };
        // Describe-the-secrets metadata, not config values.
        delete body._secret_fields;
        delete body._secrets_set;
        // A non-empty masked box can only have come from typing, so count it as edited
        // even if noteSecretEdit missed it. That keeps the case this whole contract
        // exists for — "I pasted a new token and Save did nothing" — working without
        // depending on the edit handler being able to read x-model off the element.
        const secretFields = this.panelCfg._secret_fields || [];
        const typed = secretFields.filter(f => (this.panelCfg[f] || '') !== '');
        const edited = [...new Set([...this.panelSecretsTouched, ...typed])];
        // Leave every unedited masked field out of the payload entirely, so a blank box
        // the operator never visited cannot overwrite a stored credential. Sending
        // _secrets_touched tells the server this panel is touch-aware, which is what
        // lets a box the operator DID clear still erase the value.
        for (const f of secretFields) {
          if (!edited.includes(f)) delete body[f];
        }
        body._secrets_touched = edited;
        await API.patch(`/api/setup/feature/${this.panel.key}`, body);
        this.showToast(`${this.panel.label} configuration saved.`);
        this.panel = null;
      } catch (e) {
        this.panelError = 'Save failed: ' + e.message;
      } finally {
        this.panelSaving = false;
      }
    },
  };
}
