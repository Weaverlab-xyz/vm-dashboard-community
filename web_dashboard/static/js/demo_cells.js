// The demo cells' forms and card actions: one copy, used by the cloud pages AND the Demo hub.
//
// Each cloud page used to carry its own copy of the OT cell's logic (and GCP the network
// cell's). The three were the same code with different field names, and they had already
// drifted: the GCP copy never sent the DMZ broker fields its form collected, so a GCP cell
// with Entitle ticked deployed with no broker image. One mixin per cell, spread into the
// page component, is how the hub and the cloud pages stay the same form:
//
//   ...otCellMixin('gcp')      on /gcp, and on the hub's GCP OT panel
//   ...netCellMixin()          on /gcp#net, and on the hub's network panel
//
// The markup lives beside it, in templates/demo/_ot_cell_<cloud>.html and _net_cell.html.
//
// WHAT THE HOST MUST PROVIDE. The pickers are the host's, because a cloud page already
// loads them for its other tabs and must not load them twice. The hub provides the same
// names from /api/demo/cells/<cloud>/..., which answers with the cloud module's own
// handler -- so the shapes are identical (see api/demo.py).
//
//   every cloud  praPickers, loadPraPickers(), availableWorkgroups
//   gcp          customImages, imagesLoading, loadCustomImages(), networkOpts
//   aws          amis, loadingAmis, loadAmis(region), networkOpts,
//                loadNetworkOptsFor(region, force), region, regionCatalog
//   azure        privateImages, imagesLoading, loadPrivateImages(), networkOpts,
//                location, onLocationChange()
//
// What the host MAY provide: loadOTTunnels() -- the standalone protocol-tunnel card is the
// cloud pages' alone, because it wires real plant gear rather than a demo cell.
//
// Destroy goes through DELETE /api/demo/cells/<kind>/<id> on every page. That route checks
// the row IS a live cell and then runs the cloud's own destroy handler, broker second -- the
// order each page used to spell out for itself.

(function () {
  const OT_FORMS = {
    gcp: () => ({
      image_self_link: '', instance_name: '', machine_type: 'e2-medium',
      zone: '', subnetwork: '', workgroup: '',
      protocols: ['modbus'], plc_port: null, hmi_port: 1881,
      enable_pra: true, skip_post_deploy: false, jump_group: '', jumpoint_name: '',
      runtime: 'k3s',
      register_in_passwordsafe: true, register_in_entitle: false,
      // The plant's DMZ broker, deployed alongside the cell when Entitle is ticked.
      // 8 GB because the agent alone requests 1Gi on top of k3s.
      broker_image_self_link: '', broker_image_name: '',
      broker_machine_type: 'e2-standard-2',
    }),
    aws: () => ({
      ami_id: '', instance_name: '', instance_type: 't3.medium',
      region: '', subnet_id: '', security_group_ids: [], workgroup: '',
      protocols: ['modbus'], plc_port: null, hmi_port: 1881,
      enable_pra: true, skip_post_deploy: false, jump_group: '', jumpoint_name: '',
      runtime: 'k3s',
      register_in_passwordsafe: true, register_in_entitle: false,
      // A second bake (OT_ROLE=broker), because the deploy paths have no user-data
      // hook and the role cannot be chosen at launch.
      broker_ami_id: '', broker_ami_name: '', broker_instance_type: 't3.large',
    }),
    azure: () => ({
      image_id: '', vm_name: '', vm_size: 'Standard_B2s',
      location: '', subnet_id: '', nsg_ids: [], workgroup: '',
      protocols: ['modbus'], plc_port: null, hmi_port: 1881,
      enable_pra: true, skip_post_deploy: false, jump_group: '', jumpoint_name: '',
      runtime: 'k3s',
      register_in_passwordsafe: true, register_in_entitle: false,
      broker_image_id: '', broker_image_name: '', broker_vm_size: 'Standard_D2s_v3',
    }),
  };

  // The host's image list, per cloud, and the key that identifies one.
  const IMAGES = {
    gcp:   { list: h => h.customImages,  key: 'self_link' },
    aws:   { list: h => h.amis,          key: 'ami_id' },
    azure: { list: h => h.privateImages, key: 'resource_id' },
  };

  function nameOf(host, cloud, id) {
    const spec = IMAGES[cloud];
    const img = (spec.list(host) || []).find(i => i[spec.key] === id);
    return img ? (img.name || '') : '';
  }

  // The fields every OT payload carries, whatever the cloud.
  function commonPayload(f) {
    return {
      workgroup: f.workgroup,
      protocols: f.protocols,
      plc_port: f.protocols.length === 1 && f.protocols[0] === 'custom' ? f.plc_port : null,
      hmi_port: f.hmi_port,
      jump_group: f.jump_group || null,
      jumpoint_name: f.jumpoint_name || null,
      register_in_passwordsafe: f.register_in_passwordsafe,
      runtime: f.runtime,
      register_in_entitle: f.register_in_entitle,
    };
  }

  const OT_SUBMIT = {
    gcp: (h, f) => ['/api/ot/cell', {
      image_self_link: f.image_self_link,
      image_name: nameOf(h, 'gcp', f.image_self_link),
      instance_name: f.instance_name.trim(),
      machine_type: f.machine_type,
      zone: f.zone,
      subnetwork: f.subnetwork,
      ...commonPayload(f),
      broker_image_self_link: f.broker_image_self_link,
      broker_image_name: nameOf(h, 'gcp', f.broker_image_self_link),
      broker_machine_type: f.broker_machine_type,
    }],
    aws: (h, f) => ['/api/ot/cell/aws', {
      ami_id: f.ami_id,
      ami_name: nameOf(h, 'aws', f.ami_id),
      instance_name: f.instance_name.trim(),
      instance_type: f.instance_type,
      region: f.region || null,
      subnet_id: f.subnet_id,
      security_group_ids: f.security_group_ids,
      ...commonPayload(f),
      broker_ami_id: f.broker_ami_id,
      broker_ami_name: nameOf(h, 'aws', f.broker_ami_id),
      broker_instance_type: f.broker_instance_type,
    }],
    azure: (h, f) => ['/api/ot/cell/azure', {
      image_id: f.image_id,
      image_name: nameOf(h, 'azure', f.image_id),
      vm_name: f.vm_name.trim(),
      vm_size: f.vm_size,
      location: f.location || '',
      subnet_id: f.subnet_id,
      nsg_ids: f.nsg_ids,
      ...commonPayload(f),
      broker_image_id: f.broker_image_id,
      broker_image_name: nameOf(h, 'azure', f.broker_image_id),
      broker_vm_size: f.broker_vm_size,
    }],
  };

  window.otCellMixin = function (cloud) {
    if (!OT_FORMS[cloud]) throw new Error('otCellMixin: unknown cloud ' + cloud);
    return {
      otCloud:         cloud,
      otLoaded:        false,
      otPresets:       [],
      otCells:         [],
      otError:         '',
      otSubmitting:    false,
      otShowAllImages: false,
      otForm:          OT_FORMS[cloud](),

      async loadOTData() {
        this.otLoaded = true;
        this.loadPraPickers();
        if (cloud === 'gcp') {
          if (!(this.customImages || []).length && !this.imagesLoading) this.loadCustomImages();
        } else if (cloud === 'aws') {
          if (!this.otForm.region) this.otForm.region = this.region || '';
          this.onOTRegionChange();
        } else {
          if (!this.otForm.location) this.otForm.location = this.location || '';
          if (!(this.privateImages || []).length && !this.imagesLoading) this.loadPrivateImages();
        }
        try {
          const r = await API.get('/api/ot/presets');
          this.otPresets = r.presets || [];
        } catch (_) { this.otPresets = []; }
        this.loadOTCells();
        if (typeof this.loadOTTunnels === 'function') this.loadOTTunnels();
      },

      otImages() {
        const imgs = IMAGES[cloud].list(this) || [];
        return this.otShowAllImages ? imgs : imgs.filter(i => {
          // The role is a bake-time env var the image does not carry, so the name is all
          // there is. A broker baked from ot-sim-debian.sh is easily named ot-sim-broker:
          // keep it out of the cell picker, where it would deploy a cell with no PLCs.
          const n = (i.name || '').toLowerCase();
          return n.includes('ot-sim') && !n.includes('broker');
        });
      },

      // The DMZ broker is a different bake: k3s and the Entitle agent's chart, and no
      // simulators -- a DMZ host that answers Modbus is a lie about where it sits.
      otBrokerImages() {
        const imgs = IMAGES[cloud].list(this) || [];
        return this.otShowAllImages ? imgs
          : imgs.filter(i => (i.name || '').toLowerCase().includes('broker'));
      },

      // AWS: subnets, SGs and AMIs are region-scoped -- refetch on change, then default to
      // the sandbox private subnet + VM SG, which is where an air-gapped cell belongs.
      async onOTRegionChange() {
        this.otForm.subnet_id = '';
        this.otForm.security_group_ids = [];
        await Promise.all([
          this.loadNetworkOptsFor(this.otForm.region, true),
          this.loadAmis(this.otForm.region),
        ]);
        const o = this.networkOpts || {};
        if (!this.otForm.subnet_id && o.default_subnet_id) this.otForm.subnet_id = o.default_subnet_id;
        if (!this.otForm.security_group_ids.length && o.default_security_group_id) {
          this.otForm.security_group_ids = [o.default_security_group_id];
        }
      },

      // Azure: the page runs one location for every tab, so the cell's picker moves it
      // rather than keeping a second, diverging copy of the options.
      async onOTLocationChange() {
        this.otForm.subnet_id = '';
        this.otForm.nsg_ids = [];
        this.location = this.otForm.location;
        await this.onLocationChange();
      },

      toggleOTSG(event, sgId) {
        const ids = this.otForm.security_group_ids;
        this.otForm.security_group_ids = event.target.checked
          ? [...ids, sgId] : ids.filter(id => id !== sgId);
      },

      toggleOTNSG(event, nsgId) {
        const ids = this.otForm.nsg_ids;
        this.otForm.nsg_ids = event.target.checked
          ? [...ids, nsgId] : ids.filter(id => id !== nsgId);
      },

      // Only the endpoints the baked image actually serves. The standalone tunnel card
      // keeps offering every preset, since it points at real gear.
      cellPresets() {
        return (this.otPresets || []).filter(p => p.cell);
      },

      // The fieldbus protocols a PLC speaks, apart from the cell's own platform endpoints
      // (its k3s API): listing "Kubernetes API" as a PLC protocol would be untrue.
      cellPlcPresets() {
        return this.cellPresets().filter(p => p.plc);
      },

      // Only the k3s runtime serves these; on a docker-baked cell they would be tunnels
      // to a port with no listener, which the server refuses anyway.
      cellPlatformPresets() {
        if (this.otForm.runtime === 'docker') return [];
        return this.cellPresets().filter(p => !p.plc);
      },

      // Switching to docker drops any platform preset already chosen, so the form can
      // never submit a combination the server is about to reject.
      onOTRuntimeChange() {
        if (this.otForm.runtime !== 'docker') return;
        const platform = this.cellPresets().filter(p => !p.plc).map(p => p.key);
        this.otForm.protocols = this.otForm.protocols.filter(p => !platform.includes(p));
      },

      toggleOTProtocol(event, key) {
        const ps = this.otForm.protocols;
        this.otForm.protocols = event.target.checked ? [...ps, key] : ps.filter(p => p !== key);
      },

      async loadOTCells() {
        try {
          const r = await API.get('/api/ot/cells?cloud=' + cloud);
          this.otCells = r.cells || [];
        } catch (e) {
          this.otError = e.message || 'Failed to load cells';
        }
      },

      async submitOTCell() {
        this.otError = '';
        this.otSubmitting = true;
        try {
          const [url, body] = OT_SUBMIT[cloud](this, this.otForm);
          const res = await API.post(url, body);
          window.location.href = '/jobs/' + res.job_id;
        } catch (e) {
          this.otError = e.message || 'Deploy failed';
        } finally {
          this.otSubmitting = false;
        }
      },

      // The same egress check the agent install runs once, on demand: "can this host
      // reach anything else?", asked from the pod where the agent sits. Installs nothing.
      async probeOTEgress(c) {
        c.probing = true;
        try {
          const r = await API.post(`/api/ot/cell/${encodeURIComponent(c.vm_job_id)}/probe`, {});
          window.location.href = '/jobs/' + r.job_id;
        } catch (e) {
          c.probing = false;
          toast(e.message || 'Could not queue the egress probe', 'error');
        }
      },

      async rewireOTCell(c) {
        try {
          const r = await API.post(`/api/ot/cell/${encodeURIComponent(c.vm_job_id)}/rewire`, {});
          window.location.href = '/jobs/' + r.job_id;
        } catch (e) {
          toast(e.message || 'Re-wire failed', 'error');
        }
      },

      async destroyOTCell(c) {
        const withBroker = c.broker_instance_name
          ? ` Its DMZ broker ${c.broker_instance_name} goes too, along with the plant's own Entitle agent token.` : '';
        if (!confirm(`Destroy OT cell ${c.instance_name}? This removes the VM, its PRA jump items (Web Jump, tunnel, Shell Jump), the PRA Vault checkout account and any Password Safe registration.${withBroker}`)) return;
        try {
          await API.delete(`/api/demo/cells/ot/${encodeURIComponent(c.vm_job_id)}`);
          toast('Destroy started — the cell disappears once the job completes.', 'success');
        } catch (e) {
          toast(e.message || 'Destroy failed', 'error');
        }
      },

      // Retire a failed cell's record. The server refuses if the VM is still there, and
      // asks for confirmation (force) when it cannot tell either way.
      async clearOTCell(c, force) {
        if (!force && !confirm(`Clear the record of failed cell ${c.instance_name}? The VM is checked first — this only retires the card, it does not destroy anything.`)) return;
        try {
          const q = force ? '?force=true' : '';
          await API.delete(`/api/ot/cell/${encodeURIComponent(c.vm_job_id)}${q}`);
          toast('Cleared.', 'success');
          this.loadOTCells();
        } catch (e) {
          const msg = e.message || 'Clear failed';
          if (!force && /force=true/.test(msg) && confirm(msg + '\n\nClear the record anyway?')) {
            return this.clearOTCell(c, true);
          }
          toast(msg, 'error');
        }
      },
    };
  };

  // The network (VyOS) cell. GCP only. Host provides the GCP names listed above.
  window.netCellMixin = function () {
    return {
      netLoaded:        false,
      netCells:         [],
      netError:         '',
      netSubmitting:    false,
      netShowAllImages: false,
      netForm: {
        image_self_link: '', image_name: '', instance_name: '',
        // e2-small, not the OT cell's e2-medium: VyOS routes and filters, it does not
        // run a Kubernetes control plane and four simulators.
        machine_type: 'e2-small',
        zone: '', subnetwork: '', workgroup: '',
        vyos_release: '1.4', ruleset: 'BLOCKLIST',
        register_in_passwordsafe: true,
        enable_pra: true, skip_post_deploy: false, jump_group: '', jumpoint_name: '',
      },

      async loadNetData() {
        this.netLoaded = true;
        if (!(this.customImages || []).length && !this.imagesLoading) this.loadCustomImages();
        this.loadPraPickers();
        this.loadNetCells();
      },

      // Matched on 'vyos' rather than the exact bake name: the operator imports the
      // source image under a name of their own. The API refuses a non-VyOS image anyway.
      netImages() {
        const imgs = this.customImages || [];
        return this.netShowAllImages ? imgs
          : imgs.filter(i => (i.name || '').toLowerCase().includes('vyos'));
      },

      async loadNetCells() {
        try {
          const r = await API.get('/api/netcell/cells');
          this.netCells = r.cells || [];
        } catch (e) {
          this.netError = e.message || 'Failed to load cells';
        }
      },

      async deployNetCell() {
        this.netError = '';
        this.netSubmitting = true;
        try {
          const r = await API.post('/api/netcell/cell', {
            ...this.netForm,
            image_name: nameOf(this, 'gcp', this.netForm.image_self_link),
          });
          // The notes carry the caveats the form cannot: which onboarding method was
          // forced, and that this is checkout rather than rotation.
          toast((r.notes || []).length ? r.notes.join(' ') : (r.message || 'Deploying…'), 'success');
          this.netForm.instance_name = '';
          this.loadNetCells();
          if (typeof this.loadJobs === 'function') this.loadJobs();
        } catch (e) {
          this.netError = e.message || 'Deploy failed';
        } finally {
          this.netSubmitting = false;
        }
      },

      async destroyNetCell(c) {
        if (!confirm(`Destroy network cell ${c.instance_name}? This removes the VM, its Shell Jump and any Password Safe registration.`)) return;
        try {
          await API.delete(`/api/demo/cells/net/${encodeURIComponent(c.job_id)}`);
          toast('Destroy started — the cell disappears once the job completes.', 'success');
        } catch (e) {
          toast(e.message || 'Destroy failed', 'error');
        }
      },
    };
  };

  // ── The Demo hub's hosts ─────────────────────────────────────────────────────
  //
  // On a cloud page the host is the page itself. On the hub each panel is its own small
  // component that provides the same names, read from /api/demo/cells/<cloud>/..., which
  // runs the cloud module's own handler -- so a Demo Presenter, who holds no cloud scope,
  // gets the pickers without the cloud's read routes being opened to them.
  function demoCellHost(cloud) {
    const base = '/api/demo/cells/' + cloud;
    return {
      praPickers: { configured: false, jump_groups: [], gateways: [], loaded: false },
      availableWorkgroups: [],
      networkOpts: {},
      imagesLoading: false,
      loadError: '',

      async loadPraPickers() {
        if (this.praPickers.loaded) return;
        try {
          const r = await API.get('/api/pra/pickers');
          if (r) this.praPickers = { configured: !!r.configured, jump_groups: r.jump_groups || [],
                                     gateways: r.jumpoints || [], loaded: true };
        } catch (_) { /* non-fatal: the pickers fall back to "(configured default)" */ }
      },

      async loadWorkgroups() {
        try { this.availableWorkgroups = (await API.get('/api/groups/workgroups')) || []; }
        catch (_) { this.availableWorkgroups = []; }
      },

      async hostGet(path) {
        try {
          return await API.get(base + path);
        } catch (e) {
          this.loadError = e.message || 'Could not load the form options';
          return null;
        }
      },

      // GCP
      customImages: [],
      async loadCustomImages() {
        this.imagesLoading = true;
        const r = await this.hostGet('/custom-images');
        this.customImages = (r && r.images) || [];
        this.imagesLoading = false;
      },

      // AWS
      amis: [], loadingAmis: false, region: '', regionCatalog: [],
      async loadAmis(region = '') {
        this.loadingAmis = true;
        const r = await this.hostGet('/amis' + (region ? '?region=' + encodeURIComponent(region) : ''));
        this.amis = (r && r.amis) || [];
        this.loadingAmis = false;
      },
      async loadNetworkOptsFor(region) {
        const r = await this.hostGet('/network-options' + (region ? '?region=' + encodeURIComponent(region) : ''));
        this.networkOpts = r || {};
      },

      // Azure
      privateImages: [], location: '',
      async loadPrivateImages() {
        this.imagesLoading = true;
        const loc = this.location ? '?location=' + encodeURIComponent(this.location) : '';
        const r = await this.hostGet('/images' + loc);
        this.privateImages = (r && r.images) || [];
        this.imagesLoading = false;
      },
      async onLocationChange() {
        await Promise.all([this.loadNetworkOpts(), this.loadPrivateImages()]);
      },

      // GCP and Azure: one call, no region argument (GCP's zone picker reads it as is).
      async loadNetworkOpts() {
        const loc = cloud === 'azure' && this.location
          ? '?location=' + encodeURIComponent(this.location) : '';
        const r = await this.hostGet('/network-options' + loc);
        this.networkOpts = r || {};
        if (cloud === 'azure' && !this.location && r) {
          this.location = r.location || (r.locations && r.locations[0]) || '';
        }
      },

      async hostInit() {
        this.loadWorkgroups();
        if (cloud === 'aws') {
          try {
            const r = await API.get('/api/regions?cloud=aws');
            if (r && r.default) this.region = r.default;
            if (r && r.regions) this.regionCatalog = r.regions;
          } catch (_) { /* the region picker falls back to the configured default */ }
        } else {
          await this.loadNetworkOpts();
        }
      },
    };
  }

  // x-data="demoOTPanel('aws')" -- one OT cell form on the hub.
  window.demoOTPanel = function (cloud) {
    return {
      ...demoCellHost(cloud),
      ...window.otCellMixin(cloud),
      async init() {
        await this.hostInit();
        this.loadOTData();
      },
    };
  };

  // x-data="demoNetPanel()" -- the network cell form on the hub.
  window.demoNetPanel = function () {
    return {
      ...demoCellHost('gcp'),
      ...window.netCellMixin(),
      async init() {
        this.loadWorkgroups();
        this.loadNetData();
      },
    };
  };
})();
