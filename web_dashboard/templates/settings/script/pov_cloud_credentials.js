// The POV panel's cloud credentials: which source each cloud uses (a stored key or secret,
// or workload identity) and removing a stored one.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsPovCloudCredentials() {
  return {
    // 'key' | 'spiffe' — which AWS credential block the POV cloud panel shows. UI state
    // only, derived on open from what is stored (see openPanel), never in panelCfg.
    povAwsSource: 'key',
    povWlcSources: null,     // read-only: /api/secrets/credential-sources' AWS row, for 'spiffe_wlc'
    // 'secret' | 'spiffe' | 'spiffe_wlc' — the Azure twin of povAwsSource, derived the same way.
    povAzureSource: 'secret',
    povWlcAzure: null,       // read-only: the same endpoint's Azure row
    // 'key' | 'spiffe' | 'spiffe_entitle' — the GCP twin, derived the same way. GCP has
    // no Workload Credentials tier, so its privileged half is an Entitle grant instead.
    povGcpSource: 'key',

    // Read back the stored lease state for each cloud. Note this does NOT authenticate:
    // it reports what the last mint recorded, which is why testWlcConnection() exists.
    // POV cloud panel → SPIFFE. Federation needs the identity AND the AWS audience on,
    // so choosing it ticks both (they are the same keys the Remote Agents panel shows).
    // It deliberately does NOT clear a stored key: that is removePovAwsStoredKey(), an
    // explicit click, because a key that silently vanished on a radio change is a
    // credential the operator can no longer see they had.
    //
    // 'spiffe_wlc' also makes the SPIFFE identity the EVERYDAY credential and leaves
    // Workload Credentials to the jobs (wlc_aws_everyday_source = federation). Leaving it
    // sets that back to the default, 'lease'. It never turns Workload Credentials' own
    // AWS switch on or off: that lives on its panel, with the secret names it needs.
    choosePovAwsSource(src) {
      this.povAwsSource = src;
      if (src === 'spiffe' || src === 'spiffe_wlc') {
        this.panelCfg.dashboard_spiffe_identity_enabled = true;
        this.panelCfg.dashboard_spiffe_aud_aws = true;
      }
      this.panelCfg.wlc_aws_everyday_source = src === 'spiffe_wlc' ? 'federation' : 'lease';
    },

    povAwsStoredKey() {
      const c = this.panelCfg || {};
      return !!c.aws_access_key_id || (c._secrets_set || []).includes('aws_secret_access_key');
    },

    // Blanks both halves of the key in the form; Save writes them. The secret goes in
    // panelSecretsTouched, or savePanelConfig leaves an untouched blank secret out of
    // the payload and the stored one survives the save.
    removePovAwsStoredKey() {
      this.panelCfg.aws_access_key_id = '';
      this.panelCfg.aws_secret_access_key = '';
      this.panelCfg._secrets_set = (this.panelCfg._secrets_set || [])
        .filter(f => f !== 'aws_secret_access_key');
      if (!this.panelSecretsTouched.includes('aws_secret_access_key')) {
        this.panelSecretsTouched.push('aws_secret_access_key');
      }
    },

    // Azure twin of choosePovAwsSource. The federated path needs the identity and the
    // Azure audience (api://AzureADTokenExchange) on; 'spiffe_wlc' also makes the SPIFFE
    // identity Azure's EVERYDAY credential (wlc_azure_everyday_source = federation).
    // Never clears the stored secret — removePovAzureStoredSecret() is the explicit click.
    choosePovAzureSource(src) {
      this.povAzureSource = src;
      if (src === 'spiffe' || src === 'spiffe_wlc') {
        this.panelCfg.dashboard_spiffe_identity_enabled = true;
        this.panelCfg.dashboard_spiffe_aud_azure = true;
      }
      this.panelCfg.wlc_azure_everyday_source = src === 'spiffe_wlc' ? 'federation' : 'lease';
    },

    // GCP twin. 'spiffe_entitle' also routes every POV write through an Entitle grant
    // (pov_gcp_provision_source = entitle). Never clears the stored key — that is
    // removePovGcpStoredKey(), an explicit click.
    choosePovGcpSource(src) {
      this.povGcpSource = src;
      if (src === 'spiffe' || src === 'spiffe_entitle') {
        this.panelCfg.dashboard_spiffe_identity_enabled = true;
      }
      this.panelCfg.pov_gcp_provision_source = src === 'spiffe_entitle' ? 'entitle' : '';
    },

    povGcpStoredKey() {
      return (this.panelCfg._secrets_set || []).includes('gcp_service_account_json')
        || !!(this.panelCfg.gcp_service_account_json || '');
    },

    // Touched, or the save keeps the stored key — and a stored key wins.
    removePovGcpStoredKey() {
      this.panelCfg.gcp_service_account_json = '';
      this.panelCfg._secrets_set = (this.panelCfg._secrets_set || [])
        .filter(f => f !== 'gcp_service_account_json');
      if (!this.panelSecretsTouched.includes('gcp_service_account_json')) {
        this.panelSecretsTouched.push('gcp_service_account_json');
      }
    },

    povAzureStoredSecret() {
      return (this.panelCfg._secrets_set || []).includes('azure_client_secret')
        || !!(this.panelCfg.azure_client_secret || '');
    },

    // Blanks the client secret only: the client id is the app the federated credential
    // lives on, and is still needed. Touched, or the save keeps the stored secret.
    removePovAzureStoredSecret() {
      this.panelCfg.azure_client_secret = '';
      this.panelCfg._secrets_set = (this.panelCfg._secrets_set || [])
        .filter(f => f !== 'azure_client_secret');
      if (!this.panelSecretsTouched.includes('azure_client_secret')) {
        this.panelSecretsTouched.push('azure_client_secret');
      }
    },
  };
}
