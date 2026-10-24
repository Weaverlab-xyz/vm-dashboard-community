// The Integrations tab: every integration row and its group tabs, the feature flags that
// turn them on, the preview flags, and the storage check that gates the Ansible toggle.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsIntegrations() {
  return {
    isAdmin: false,
    // Preview feature flags — populated by GET /api/setup/flags (admin only).
    previewFlags: [],
    integrations: [
      { key: 'vmware',       label: 'VMware Workstation',      group: 'hypervisors', hasConfig: false, enabled: false,
        desc: 'VM management on a Windows host. Toggle to enable.' },
      // The three BeyondTrust products gate independently — customers routinely license
      // one or two, not all three. Each key derives its own config flag via
      // _feature_to_cfg_key (password_safe -> password_safe_enabled), so renaming one
      // silently repoints its toggle at a key nothing reads.
      // Config-only: no product of its own, just the identity the four below (and
      // Workload Credentials) can authenticate with instead of a stored credential.
      { key: 'bt_platform_identity', label: 'BeyondTrust platform identity', group: 'beyondtrust', hasConfig: true, enabled: true, configOnly: true,
        desc: 'Authenticate to PRA, Password Safe, EPM for Linux, Entitle and Workload Credentials with this dashboard\'s own workload identity instead of stored tokens or client secrets. One Pathfinder Workload Identity registration covers every product it is scoped to.' },
      { key: 'password_safe', label: 'Password Safe',          group: 'beyondtrust', hasConfig: true,  enabled: false,
        desc: 'Secret and managed-account checkout, VM / database / Kubernetes onboarding, and scheduled credential rotation.' },
      { key: 'pra',          label: 'Privileged Remote Access', group: 'beyondtrust', hasConfig: true,  enabled: false,
        desc: 'Shell Jump, protocol-tunnel and Web Jump provisioning through PRA, plus the Gateway hosts that carry them.' },
      { key: 'epml',         label: 'EPM for Linux',           group: 'beyondtrust', hasConfig: true,  enabled: false,
        desc: 'Build and download EPM for Linux client packages and mint activation tokens via the BeyondTrust Pathfinder API.' },
      { key: 'portainer',    label: 'Containers',              group: 'cloud', hasConfig: true,  enabled: false,
        desc: 'Manage and view containers across all configured cloud providers (AWS ECS, Azure ACI, GCP Cloud Run), plus optional on-prem Portainer management.' },
      { key: 'ansible',      label: 'Remote Worker',           group: 'platform', hasConfig: true,  enabled: false,
        desc: 'Where config-management (Ansible) and Kubernetes (kubectl/helm) ops run — in-process or as one-shot cloud tasks (ECS / ACI / Cloud Run).' },
      { key: 'entitle',      label: 'Entitle',                 group: 'beyondtrust', hasConfig: true,  enabled: false,
        desc: 'Register built VMs & databases as Entitle integrations.' },
      // Generally available since 2026-10-14; was a preview flag before that. The key must
      // stay `workload_credentials`: _feature_to_cfg_key derives `workload_credentials_enabled`
      // from it, the key the preview toggle wrote, so a stored install keeps its state.
      { key: 'workload_credentials', label: 'Workload Credentials', group: 'beyondtrust', hasConfig: true, enabled: false,
        desc: 'Mint short-lived AWS and Azure credentials on demand instead of storing static keys, and use Workload Credentials as a secrets backend. Requires a US-region Pathfinder site with the feature enabled.' },
      { key: 'skytap',       label: 'Skytap',                  group: 'pov', hasConfig: true,  enabled: false,
        desc: 'Lab platform for customer POV environments — create one from a Skytap template. Available on a POV instance only.' },
      { key: 'pov_cloud',    label: 'POV cloud provider',      group: 'pov', hasConfig: true,  enabled: false,
        desc: 'Run POVs on one public cloud alongside Skytap — templates, scheduled suspend and the same auto-delete timer. Available on a POV instance only.' },
      { key: 'proxmox',      label: 'Proxmox VE',              group: 'hypervisors', hasConfig: true,  enabled: false,
        desc: 'On-premises VM and LXC container management via the Proxmox REST API.' },
      { key: 'vsphere',      label: 'VMware vSphere / ESXi',   group: 'hypervisors', hasConfig: true,  enabled: false,
        desc: 'Enterprise VMware VM management via the vSphere Web Services API (pyVmomi).' },
      { key: 'hyperv',       label: 'Microsoft Hyper-V',        group: 'hypervisors', hasConfig: true,  enabled: false,
        desc: 'Hyper-V VM management via WinRM/PowerShell on a Windows host.' },
      { key: 'nutanix',      label: 'Nutanix AHV',              group: 'hypervisors', hasConfig: true,  enabled: false,
        desc: 'Nutanix AHV VM management via Prism Central REST API v3.' },
      { key: 'xcpng',        label: 'XCP-ng / XenServer',       group: 'hypervisors', hasConfig: true,  enabled: false,
        desc: 'XCP-ng and XenServer VM management via the XAPI XML-RPC API.' },
      { key: 'cost_explorer', label: 'Cloud Costs',             group: 'governance', hasConfig: true,  enabled: false,
        desc: 'Month-to-date cloud spend, with an optional monthly budget alert.' },
      { key: 'admission_control', label: 'Action Guardrails',   group: 'governance', hasConfig: true,  enabled: false,
        desc: 'Pre-action policy gate (OPA): allowed regions, blocked sizes, change-freeze.' },
      { key: 'resource_expiry', label: 'Auto-delete Timer',     group: 'governance', hasConfig: true,  enabled: false,
        desc: 'Give new cloud VMs, databases and clusters an expiry, then destroy them when it passes. Starts in report-only mode.' },
      { key: 'notifications', label: 'Notifications',           group: 'governance', hasConfig: true,  enabled: false,
        desc: 'Send auto-delete warnings, job failures and budget/secret/drift alerts to Slack, Microsoft Teams, or a signed webhook. Starts in dry-run mode.' },
      { key: 'cloud_database', label: 'Databases',              group: 'cloud', hasConfig: true,  enabled: false,
        desc: 'Provision private managed databases (PostgreSQL, MySQL, SQL Server) on AWS, Azure and GCP, or register ones you already run — on-premises or in a cloud. PRA tunnel for the private ones.' },
      { key: 'k8s_management', label: 'Kubernetes',             group: 'cloud', hasConfig: true,  enabled: false,
        desc: 'Provision (EKS/AKS/GKE), register, and broker Kubernetes clusters — PRA tunnels + Entitle real-identity JIT.' },
      { key: 'cloud_functions', label: 'Cloud Functions',        group: 'cloud', hasConfig: true,  enabled: false,
        desc: 'Deploy a dashboard-authored handler as an AWS Lambda, an Azure Function App, or a GCP Cloud Run function — optionally attached to a VPC/VNet so it can reach private resources. Each gets a stable HTTPS endpoint protected by a shared bearer secret.' },
      { key: 'remote_agents', label: 'Remote Agents',           group: 'platform', hasConfig: true,  enabled: false,
        desc: 'Containerised agents inside private networks that poll out to this dashboard to discover on-premises Kubernetes clusters and databases. No inbound firewall rules. Adds an Agents page for registering and revoking them. Terminate TLS in front of the dashboard before enabling — this is the only API that serves callers outside your own network.' },
      { key: 'mcp_server',   label: 'MCP Server',                group: 'platform', hasConfig: false, enabled: false,
        desc: 'Expose read-only dashboard tools to an MCP-compatible AI client (Claude Desktop, Claude Code, Cursor) at /mcp, authenticated with a Personal Access Token. Each tool returns only what the token owner could see in the UI. Off by default; turning it on serves an endpoint that reads your estate.' },
      { key: 'vm_spend_cap', label: 'VM spend caps', group: 'governance', hasConfig: false, enabled: false,
        desc: 'Give a cloud VM a dollar cap. Spend is accrued every sweep at the provider list price and, when the cap is reached, the VM is warned about or suspended — never destroyed. The estimate excludes reservations, credits, free tier, transfer and snapshots, so it errs high, which is the safe direction for a cap. Off by default; a cap is per-VM, so turning this on accrues nothing until you set one. A cap is refused on a VM whose region has no price source, rather than stored and silently never firing.' },
      { key: 'cloud_unmanaged_discovery', label: 'Discover unmanaged cloud VMs', group: 'cloud', hasConfig: false, enabled: false,
        desc: 'Show cloud VMs this dashboard did not deploy, in a separate list on each cloud console. Discovered VMs can be started and stopped; they can never be destroyed from here. Off by default because it lists every instance in the account, subscription or project rather than the ones the dashboard deployed. A discovered VM is admin-only unless it carries a workstream tag.' },
      { key: 'multi_region', label: 'Multi-region',            group: 'cloud', hasConfig: true,  enabled: true, configOnly: true,
        desc: 'Per-region resource config sets for AWS, GCP and Azure — subnets, security groups, SSH secrets, networks. Blank fields fall back to the flat defaults.' },
      { key: 'oidc',         label: 'Single sign-on (OIDC)',   group: 'platform', hasConfig: true,  enabled: true, configOnly: true,
        desc: 'Sign in with any OpenID Connect provider — Okta, Auth0, Keycloak, Authentik, Authelia, Google Workspace, JumpCloud, Ping, GitLab. Configured from the issuer URL; endpoints are discovered automatically.' },
      { key: 'worker',       label: 'Job Worker',              group: 'platform', hasConfig: true,  enabled: true, configOnly: true,
        desc: 'How many background jobs run at once, per kind. Raise the poll-only limit if image exports and copies queue up; raise the heavy limit to provision more clusters or databases in parallel. Takes effect within seconds, no restart.' },
      { key: 'change_windows', label: 'Change Windows',        group: 'governance', hasConfig: true,  enabled: true, configOnly: true,
        desc: 'Named periods during which changes may start — "Prod Weekend, Sat 02:00–06:00 New York". Any run form can then book a job into one by name instead of starting it now. A job that misses its window is marked missed and never runs late. Optionally require a second person to approve a booked change before it fires.' },
    ],
    // The tabs of the Integrations card, in display order. Every integrations[] entry's
    // `group` must be one of these ids, or the row renders on no tab at all — pinned by
    // tests/test_settings_integrations.py.
    integrationGroups: [
      { id: 'beyondtrust', label: 'BeyondTrust' },
      { id: 'hypervisors', label: 'Hypervisors' },
      { id: 'cloud',       label: 'Cloud services' },
      { id: 'governance',  label: 'Governance & cost' },
      { id: 'platform',    label: 'Platform' },
      { id: 'pov',         label: 'POV' },
    ],
    // UI-only, remembered per browser so a reload lands back on the same tab.
    integrationTab: 'beyondtrust',

    // Storage backend prereq state — populated by GET /api/storage/backends
    // on page load (gates the Ansible enable toggle in the integrations
    // list) and refreshed when the Ansible panel opens (renders the
    // configured/active hint inside the panel body).
    storageReady: false,
    storageBackendLabel: '',
    // Mirror flag for the toggle — kept separate so the panel's hint can
    // refresh independently of the toggle state.
    ansibleEnableAllowed: false,

    async loadPreviewFlags() {
      if (!this.isAdmin) return;   // loadFeatureFlags sets isAdmin
      try {
        const data = await API.get('/api/setup/flags');
        this.previewFlags = Object.entries(data).map(([key, v]) => ({
          key, enabled: !!v.enabled, label: v.label || key, desc: v.description || '', saving: false,
          configFeature: v.config_feature || null,
        }));
      } catch (e) {
        // Non-fatal — preview section stays empty.
      }
    },

    async togglePreviewFlag(f) {
      f.saving = true;
      const newState = !f.enabled;
      try {
        await API.patch(`/api/setup/flag/${f.key}`, { enabled: newState });
        f.enabled = newState;
        this.showToast(`${f.label} ${newState ? 'enabled' : 'disabled'}.`);
      } catch (e) {
        this.showToast(`Failed to update ${f.label}: ${e.message}`, 'error');
      } finally {
        f.saving = false;
      }
    },


    async loadFeatureFlags() {
      try {
        const user = await API.get('/api/auth/me');
        this.isAdmin = user?.is_admin || false;
        if (!this.isAdmin) return;

        const features = await API.get('/api/features');
        const map = {
          vmware: features.vmware,
          password_safe: features.password_safe, pra: features.pra, epml: features.epml,
          portainer: features.portainer,
          ansible: features.ansible, entitle: features.entitle,
          workload_credentials: features.workload_credentials,
          skytap: features.skytap,
          pov_cloud: features.pov_cloud,
          proxmox: features.proxmox,
          vsphere: features.vsphere,
          hyperv: features.hyperv,
          nutanix: features.nutanix,
          xcpng: features.xcpng,
          cost_explorer: features.cost,
          admission_control: features.admission,
          resource_expiry: features.resource_expiry,
          cloud_database: features.cloud_database,
          k8s_management: features.k8s_management,
          cloud_functions: features.cloud_functions,
          // Miss an entry here and the toggle renders permanently OFF however the flag
          // is really set — silent, because nothing errors. Notifications was missing
          // one until now. Pinned by tests/test_settings_integrations.py.
          notifications: features.notifications,
          remote_agents: features.remote_agents,
          mcp_server: features.mcp_server,
          cloud_unmanaged_discovery: features.cloud_unmanaged_discovery,
          vm_spend_cap: features.vm_spend_cap,
        };
        for (const intg of this.integrations) {
          intg.enabled = !!map[intg.key];
        }
      } catch (e) {
        // Non-fatal — integrations section stays hidden
      }
    },

    async toggleIntegration(intg) {
      intg.saving = true;
      const newState = !intg.enabled;
      try {
        await API.patch(`/api/setup/feature/${intg.key}`, { enabled: newState });
        intg.enabled = newState;
        this.showToast(`${intg.label} ${newState ? 'enabled — configure credentials below' : 'disabled'}.`);
        // Auto-open the config panel when enabling a feature that needs credentials
        if (newState && intg.hasConfig) {
          await this.openPanel(intg);
        }
      } catch (e) {
        this.showToast(`Failed to update ${intg.label}: ${e.message}`, 'error');
      } finally {
        intg.saving = false;
      }
    },

    async loadStorageStatus() {
      try {
        const data = await API.get('/api/storage/backends');
        this.storageReady = !!data.any_active;
        this.ansibleEnableAllowed = this.storageReady;
        const active = (data.backends || []).find(b => b.active);
        this.storageBackendLabel = active ? active.label : '';
      } catch (e) {
        this.storageReady = false;
        this.ansibleEnableAllowed = false;
        this.storageBackendLabel = '';
      }
    },
  };
}
