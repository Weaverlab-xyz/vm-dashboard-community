// The Auto-delete Timer, Containers and Job Worker panels' live status, and the actions
// they offer on it.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsOperations() {
  return {
    portainerFirewall: null, // same for the managed Portainer node (Containers panel)
    portainerFirewallBusy: false, // …and the one thing this panel can DO to it
    expirySweep: null,       // last auto-delete sweep summary (Auto-delete Timer panel)
    expirySweeping: false,
    workerStatus: null,      // what the job worker resolved its limits to (Job Worker panel)
    workerStatusLoading: false,

    expirySweepSummary() {
      const s = this.expirySweep;
      if (!s) return 'No sweep report yet.';
      if (s.never_run) return 'Never run — the first pass happens on the next sweep interval.';
      if (s.corrupted) return 'The stored report could not be read.';
      if (s.skipped) return 'Last pass skipped: ' + s.skipped;
      const lines = [
        `${s.started_at} · scanned ${s.scanned}, past expiry ${s.due}, destroyed ${s.reaped}`
        + (s.failed ? `, ${s.failed} failed` : '')
        + (s.deleting ? '' : ' (report only — nothing deleted)')
        + (s.capped ? ' · CAPPED' : ''),
      ];
      if (!s.armed) lines.push('The feature is still arming — nothing can be deleted yet.');
      else if (!s.deleting && s.enforce_since) lines.push(
        'Deletion was enabled at ' + s.enforce_since + ' and is still arming.');
      for (const t of (s.targets || []).slice(0, 5)) {
        lines.push(`  ${t.kind}/${t.cloud} ${t.name} — ${Math.floor(t.overdue_s / 60)}m overdue`);
      }
      if ((s.targets || []).length > 5) lines.push(`  …and ${s.targets.length - 5} more`);
      return lines.join('\n');
    },

    async runExpirySweep() {
      this.expirySweeping = true;
      try {
        const r = await API.post('/api/expiry/sweep', {});
        toast(r.message || 'Sweep queued.', r.ok ? 'success' : 'info');
        // The pass runs in the worker, so the report lands a moment later. Re-read once
        // rather than polling — the operator can reopen the panel for a fresh look.
        if (r.ok) {
          setTimeout(async () => {
            try { this.expirySweep = await API.get('/api/expiry/last-sweep'); } catch (e) {}
          }, 4000);
        }
      } catch (e) {
        toast('Could not queue a sweep: ' + e.message, 'error');
      } finally {
        this.expirySweeping = false;
      }
    },

    // The breakdown above is read-only, and for a long time so was this panel's whole
    // relationship with the firewall: it could show you that the node admits an address
    // the dashboard no longer egresses from, and offer nothing to do about it short of
    // a redeploy. This is the deploy's own "Configuring firewall" step on demand.

    async reapplyPortainerFirewall() {
      this.portainerFirewallBusy = true;
      try {
        const data = await API.post('/api/containers/portainer/node/firewall', {});
        if (!data) return;   // 401: API.post logs out and answers null
        // The response IS the breakdown, so the box re-renders from the state that now
        // exists in the cloud rather than the one it was opened with.
        this.portainerFirewall = data;
        const bits = [];
        if ((data.added || []).length) bits.push('now allowing ' + summarizeCidrs(data.added));
        if ((data.removed || []).length) bits.push('dropped ' + summarizeCidrs(data.removed));
        // Fail-closed is the contract, so an empty set is a real outcome, not an error
        // — but it means nothing can reach the node, which no "re-applied" can imply.
        if (!data.opened) {
          toast('Ingress re-applied, but the allow-list is empty — the node is now reachable by nobody.', 'error');
        } else {
          toast(bits.length ? 'Ingress re-applied: ' + bits.join('; ') + '.'
                            : 'Ingress re-applied — no change.',
                data.changed ? 'success' : 'info');
        }
      } catch (e) {
        toast('Could not re-apply the node ingress: ' + e.message, 'error');
      } finally {
        this.portainerFirewallBusy = false;
      }
    },

    // Read-only. The worker publishes what it actually resolved, because the numbers
    // saved above are clamped twice — to the code ceilings, then to what the worker's DB
    // pool can serve. Without this an operator who raises a limit and sees no change has
    // no way to find out why short of reading container logs.
    async loadWorkerStatus() {
      this.workerStatusLoading = true;
      try {
        this.workerStatus = await API.get('/api/worker/status');
      } catch (e) {
        this.workerStatus = { error: e.message };
      } finally {
        this.workerStatusLoading = false;
      }
    },

    workerStatusSummary() {
      const s = this.workerStatus;
      if (!s) return 'Loading…';
      if (s.error) return 'Could not read the worker status: ' + s.error;
      if (s.never_run) return 'No worker has reported yet. If jobs are running, the worker '
        + 'is on an older image; otherwise check that it is deployed and can reach the database.';
      if (s.corrupted) return 'The stored status could not be read.';

      const eff = s.effective || {};
      const conf = s.configured || {};
      const lines = [
        `heavy ${eff.heavy}   mixed ${eff.medium}   poll-only ${eff.light}   total ${eff.total}`,
      ];
      // Only worth showing when they disagree — that gap IS the diagnosis.
      const diffs = ['heavy', 'medium', 'light', 'total']
        .filter(k => conf[k] !== undefined && conf[k] !== eff[k])
        .map(k => `${k} ${conf[k]}→${eff[k]}`);
      if (diffs.length) lines.push('reduced: ' + diffs.join(', '));
      if (s.clamp_reason) lines.push('why: ' + s.clamp_reason);
      lines.push(`${s.in_flight ?? 0} job(s) in flight when reported · ${s.executor_threads} threads`
        + (s.pool_capacity ? ` · pool of ${s.pool_capacity}` : ''));
      lines.push(`reported by ${s.hostname} at ${s.published_at}`);
      return lines.join('\n');
    },
  };
}
