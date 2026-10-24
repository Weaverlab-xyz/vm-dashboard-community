// The Account tab: the signed-in user's profile, MFA state and security keys (WebAuthn
// registration and removal), and the MCP endpoint they connect a client to.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsAccount() {
  return {
    credentials: [],
    loading: true,
    mfaRequired: false,
    authProvider: 'Local',
    registering: false,
    namingKey: false,
    deviceName: '',
    mcpUrl: window.location.origin + '/mcp',
    _pendingAttestation: null,
    _pendingChallengeToken: null,

    async loadProfile() {
      try {
        const user = await API.get('/api/auth/me');
        if (user) {
          this.mfaRequired = user.mfa_required || false;
          // Every provider the login paths write; anything else (local, sso_pending) is a
          // password login. A two-way test here once labelled every OIDC user "Local".
          this.authProvider = ({ azure_ad: 'Sign in with Microsoft (Entra ID)',
                                 oidc: 'Single sign-on (OIDC)' })[user.auth_provider]
                              || 'Local (username/password)';
        }
      } catch (e) {
        // non-critical
      }
    },

    async loadCredentials() {
      this.loading = true;
      try {
        const data = await API.get('/api/mfa/credentials');
        this.credentials = data || [];
      } catch (e) {
        this.showToast('Failed to load security keys: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    async startRegistration() {
      if (!window.PublicKeyCredential) {
        this.showToast('Your browser does not support WebAuthn / security keys.', 'error');
        return;
      }
      this.registering = true;
      this.namingKey = false;
      this.toast = '';

      try {
        // 1. Get creation options from server
        const { challenge_token, options } = await API.post('/api/mfa/register/begin', {});
        this._pendingChallengeToken = challenge_token;

        // 2. Invoke browser WebAuthn
        const creationOptions = WebAuthnHelper.prepareCreationOptions(options);
        let credential;
        try {
          credential = await navigator.credentials.create({ publicKey: creationOptions });
        } catch (e) {
          if (e.name === 'NotAllowedError') {
            this.showToast('Key registration was cancelled or timed out.', 'error');
          } else {
            this.showToast('Registration error: ' + e.message, 'error');
          }
          this.registering = false;
          return;
        }

        // 3. Prompt for a name before sending to server
        this._pendingAttestation = WebAuthnHelper.serializeCredential(credential);
        this.namingKey = true;
        this.deviceName = '';
      } catch (e) {
        this.showToast('Failed to start registration: ' + e.message, 'error');
        this.registering = false;
      }
    },

    async completeRegistration() {
      if (!this._pendingAttestation || !this._pendingChallengeToken) return;

      try {
        const result = await API.post('/api/mfa/register/complete', {
          challenge_token: this._pendingChallengeToken,
          device_name: this.deviceName || 'Security Key',
          attestation_response: this._pendingAttestation,
        });

        this.showToast('Security key registered successfully!', 'success');
        this.mfaRequired = true;
        await this.loadCredentials();
      } catch (e) {
        this.showToast('Registration failed: ' + e.message, 'error');
      } finally {
        this.cancelRegistration();
      }
    },

    cancelRegistration() {
      this.registering = false;
      this.namingKey = false;
      this.deviceName = '';
      this._pendingAttestation = null;
      this._pendingChallengeToken = null;
    },

    async deleteCredential(id) {
      if (!confirm('Remove this security key? You will need to re-register it to use it again.')) return;

      const token = Alpine.store('auth').token;
      const resp = await fetch('/api/mfa/credentials/' + id, {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + token },
      });

      if (resp.status === 204) {
        this.showToast('Security key removed.', 'success');
        await this.loadCredentials();
        if (this.credentials.length === 0) this.mfaRequired = false;
      } else {
        const data = await resp.json().catch(() => ({}));
        this.showToast(data.detail || 'Failed to remove key.', 'error');
      }
    },
  };
}
