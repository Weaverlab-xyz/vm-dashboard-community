// The Password Safe panel's DB-Ops services on the GCP cloud-run channel.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsDbops() {
  return {
    // Password Safe panel: the per-region DB-Ops services and the audience each one
    // resolves to. Read-only — never merged into panelCfg, so a stale open panel
    // cannot write a computed audience back over the operator's override.
    dbops: null,
    dbopsLoading: false,
    dbopsBusy: '',

    async loadDbops() {
      this.dbopsLoading = true;
      try {
        this.dbops = await API.get('/api/databases/dbops/status');
      } catch (e) {
        // The Databases router is feature-gated, so "off" is a 403 here. Leave the box
        // saying "not loaded" rather than throwing a toast at someone who has simply
        // not turned the feature on yet.
        this.dbops = null;
      } finally {
        this.dbopsLoading = false;
      }
    },

    async deployDbops(region) {
      if (!confirm(
        `Deploy the DB-Ops service to ${region}?\n\n` +
        'It is a Cloud Run service inside the VPC, deployed with a warm instance that ' +
        'bills continuously (a few dollars a month). Only the invoker service accounts ' +
        'configured here will be able to call it.')) return;
      this.dbopsBusy = region;
      try {
        const r = await API.post('/api/databases/dbops/deploy', { region });
        this.showToast(`Deploying the DB-Ops service in ${region} — job ${r.job_id}.`);
        await this.loadDbops();
      } catch (e) {
        this.showToast('Could not deploy the DB-Ops service: ' + e.message, 'error');
      } finally {
        this.dbopsBusy = '';
      }
    },

    async syncDbopsInvokers(region) {
      this.dbopsBusy = region;
      try {
        const r = await API.post('/api/databases/dbops/invokers', { region });
        this.showToast(r.changed
          ? `Applying ${r.invokers} invoker(s) to the ${region} DB-Ops service — job ${r.job_id}.`
          : `The ${region} DB-Ops service already holds exactly those invokers — nothing to do.`);
        await this.loadDbops();
      } catch (e) {
        this.showToast('Could not sync the DB-Ops invokers: ' + e.message, 'error');
      } finally {
        this.dbopsBusy = '';
      }
    },
  };
}
