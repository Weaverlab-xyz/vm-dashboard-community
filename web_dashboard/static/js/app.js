/**
 * Infrastructure Management Dashboard - Alpine.js global store and utilities
 */

// ── Auth store ────────────────────────────────────────────────────────────────
document.addEventListener('alpine:init', () => {
    Alpine.store('auth', {
        token: localStorage.getItem('vm_cli_token') || null,
        username: localStorage.getItem('vm_cli_username') || null,
        workgroups: JSON.parse(localStorage.getItem('vm_cli_workgroups') || '[]'),
        isAdmin: localStorage.getItem('vm_cli_is_admin') === 'true',
        // A POV accessor: a prospect's ephemeral login, bound to one POV environment.
        // Held here ONLY so requireAuth can send them to their own page instead of a
        // dashboard that would refuse every call on it. It is not what confines them —
        // localStorage is editable by whoever holds it, and the real control is the path
        // allowlist in api/auth.get_current_user, which consults nothing the client sends.
        accessorEnvId: localStorage.getItem('vm_cli_accessor_env') || '',

        get isAccessor() {
            return !!this.accessorEnvId;
        },

        get isLoggedIn() {
            return !!this.token;
        },

        login(token, username, workgroups, isAdmin = false, accessorEnvId = '') {
            this.token = token;
            this.username = username;
            this.workgroups = workgroups;
            this.isAdmin = isAdmin;
            this.accessorEnvId = accessorEnvId || '';
            localStorage.setItem('vm_cli_token', token);
            localStorage.setItem('vm_cli_username', username);
            localStorage.setItem('vm_cli_workgroups', JSON.stringify(workgroups));
            localStorage.setItem('vm_cli_is_admin', isAdmin ? 'true' : 'false');
            localStorage.setItem('vm_cli_accessor_env', accessorEnvId || '');
        },

        logout() {
            this.token = null;
            this.username = null;
            this.workgroups = [];
            this.isAdmin = false;
            this.accessorEnvId = '';
            localStorage.removeItem('vm_cli_token');
            localStorage.removeItem('vm_cli_username');
            localStorage.removeItem('vm_cli_workgroups');
            localStorage.removeItem('vm_cli_is_admin');
            localStorage.removeItem('vm_cli_accessor_env');
            // Both persona cookies, not just the assigned one. These are per-BROWSER and
            // the next person to log in here would otherwise inherit this user's focus --
            // harmless (a persona grants nothing) but baffling, and it would make an
            // assignment look broken on any shared machine.
            window.clearPersonaCookies();
            window.location.href = '/login';
        },

        hasWorkgroup(wg) {
            return this.workgroups.includes(wg);
        }
    });
});

// ── API helper ────────────────────────────────────────────────────────────────
// ── Assigned persona ────────────────────────────────────────────────────────────
// The focus assigned to a user or their OIDC group, cached where the SERVER can read it.
//
// An HTML page load carries no identity in this app -- the token lives in localStorage and
// only rides /api/* XHR as a Bearer header -- so services/personas.resolve cannot ask who
// is asking. /api/auth/me can, and both login paths already call it for is_admin. This
// writes its answer into a cookie the nav's own server-side render then reads.
//
// A cookie rather than localStorage precisely BECAUSE the server has to see it. That is
// only acceptable while a persona cannot gate anything: the worst a forged value achieves
// is reordering your own nav. The day a persona can hide a page, this is wrong.
function setAssignedPersona(u) {
    const key = (u && u.persona) || '';
    const src = (u && u.persona_source) || '';
    // Written on every login, or CLEARED -- never merely left alone. An admin removing an
    // assignment has to actually take effect, and a stale cookie would outlive it.
    if (key && (src === 'user' || src === 'group')) {
        document.cookie = 'persona_assigned=' + encodeURIComponent(src + ':' + key)
                        + '; path=/; max-age=31536000; samesite=lax';
    } else {
        document.cookie = 'persona_assigned=; path=/; max-age=0; samesite=lax';
    }
}
window.setAssignedPersona = setAssignedPersona;

function clearPersonaCookies() {
    document.cookie = 'persona_assigned=; path=/; max-age=0; samesite=lax';
    document.cookie = 'persona=; path=/; max-age=0; samesite=lax';
}
window.clearPersonaCookies = clearPersonaCookies;

window.API = {
    async request(method, path, body = null, extraHeaders = {}) {
        const token = Alpine.store('auth').token;
        const opts = {
            method,
            headers: {
                'Content-Type': 'application/json',
                ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
                ...extraHeaders,
            },
        };
        if (body) opts.body = JSON.stringify(body);

        const resp = await fetch(path, opts);

        if (resp.status === 401) {
            Alpine.store('auth').logout();
            return null;
        }

        if (resp.status === 202) {
            // Accepted — e.g. a provision/decommission returning {ok, job_id, ...}.
            return await resp.json().catch(() => ({}));
        }

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            const detail = err.detail;
            const message = typeof detail === 'string'
                ? detail
                : (detail && detail.message) || `HTTP ${resp.status}`;
            const e = new Error(message);
            // Entitle user-JIT Phase 4: expose request_access_url + missing
            // scope/level on the Error so callers can render a deep link.
            if (detail && typeof detail === 'object') {
                if (detail.code)               e.code             = detail.code;
                if (detail.request_access_url) e.requestAccessUrl = detail.request_access_url;
                if (detail.missing_scope)      e.missingScope     = detail.missing_scope;
                if (detail.missing_level)      e.missingLevel     = detail.missing_level;
            }
            // Hand the Error to toast() out of band, because no call site does: all
            // ~142 of them pass a STRING built from it (`toast(e.message, 'error')`,
            // `toast('Deploy failed: ' + e.message, 'error')`), which drops
            // requestAccessUrl one hop before the renderer. base.html's toast()
            // re-attaches the link from here — see the adoption rules there. Nulled
            // on a failure without a link so the stash always reflects the most
            // recent one. Pinned end to end in tests/toast_request_access_check.js.
            window.__lastApiError = e.requestAccessUrl ? { error: e, at: Date.now() } : null;
            throw e;
        }

        return resp.json();
    },

    // Send a Blob as the request body, unencoded. For the chunked-upload lane on
    // /storage: `request()` JSON.stringifies its body, and a 8 MiB slice through
    // JSON.stringify + base64 is the allocation the chunked lane exists to avoid.
    //
    // Not a bare fetch() at the call site, and that is the point of it living here: a
    // fetch() without this Authorization header is an ANONYMOUS request, which reads as a
    // 401 on a page the user is plainly logged into.
    async sendBlob(method, path, blob, extraHeaders = {}) {
        const token = Alpine.store('auth').token;
        const resp = await fetch(path, {
            method,
            headers: {
                'Content-Type': 'application/octet-stream',
                ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
                ...extraHeaders,
            },
            body: blob,
        });
        if (resp.status === 401) {
            Alpine.store('auth').logout();
            return null;
        }
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            const detail = err.detail;
            throw new Error(typeof detail === 'string'
                ? detail
                : (detail && detail.message) || `HTTP ${resp.status}`);
        }
        return resp.json();
    },

    get:    (path)        => API.request('GET',    path),
    post:   (path, body)  => API.request('POST',   path, body),
    put:    (path, body)  => API.request('PUT',    path, body),
    patch:  (path, body)  => API.request('PATCH',  path, body),
    del:    (path)        => API.request('DELETE', path),
    delete: (path)        => API.request('DELETE', path),  // alias — some templates use API.delete
    putBlob: (path, blob, headers) => API.sendBlob('PUT', path, blob, headers),
};

// ── Reusable secret picker ────────────────────────────────────────────────────
// Spread into any Alpine page component (`...secretPickerState()`), call
// `loadSecretBackends()` once (e.g. in init), and render the picker with the
// `secret_picker` Jinja macro (templates/partials/secret_picker.html). The macro
// stores a transient backend id on `<obj>.<backend_field>` and the composed
// reference string (e.g. `aws_sm://dashboard/foo`) on `<obj>.<ref_field>`, which
// the deploy request sends; the backend resolves it via
// config_service.resolve_reference() with the per-cloud config as the fallback.
window.secretPickerState = function () {
    return {
        // Only the external backends produce resolvable references; the
        // database backend stores the value inline (not a ref), so it's omitted.
        secretPrefix: { aws_sm: 'aws_sm://', azure_kv: 'azure_kv://', gcp_sm: 'gcp_sm://', bt_secrets_safe: 'bt_safe://' },
        secretBackends: [],
        secretItems: {},        // backend id → [{name, ref, description}]
        secretItemsLoading: {}, // backend id → bool

        async loadSecretBackends() {
            try {
                const all = await API.get('/api/secrets/backends');
                this.secretBackends = (all || []).filter(b => this.secretPrefix[b.id]);
            } catch (e) {
                this.secretBackends = [];
            }
        },

        async loadSecretItems(backend) {
            if (!backend || !this.secretPrefix[backend]) return;
            this.secretItemsLoading[backend] = true;
            try {
                const r = await API.get(`/api/secrets/items?backend=${encodeURIComponent(backend)}`);
                this.secretItems[backend] = (r && r.items) || [];
            } catch (e) {
                this.secretItems[backend] = [];
            } finally {
                this.secretItemsLoading[backend] = false;
            }
        },

        composeSecretRef(backend, ref) {
            if (!backend || !ref) return '';
            return (this.secretPrefix[backend] || '') + ref;
        },
    };
};

// ── Deploy count / auto-numbered names ────────────────────────────────────────
// Every cloud deploy form takes a Count; the server expands the base name into a
// numbered series and returns the names it used. These helpers only PREVIEW that
// expansion — services/vm_naming.py is authoritative, and the fixtures both sides
// must agree on live in tests/test_vm_naming.py and tests/template_helpers_check.js.

// Must match MAX_DEPLOY_COUNT in services/vm_naming.py, or the form lets through a 422.
window.DEPLOY_COUNT_MAX = 20;

// The length the EXPANDED name must fit in, per provider. Mirrors vm_naming._LIMITS.
//   aws / oci  255  tag value / display name; effectively unbounded for real names
//   azure       15  NOT the 64-char ARM limit — azure_service derives the in-guest
//                   hostname as vm_name[:15], so a series that only differs past
//                   character 15 gives two VMs the same hostname
//   gcp         63  RFC1035
window.NAME_LIMITS = { aws: 255, azure: 15, gcp: 63, oci: 255 };

// Spread into a page component (`...deployNameState()`) to get the Count ceiling and
// the name preview.
window.deployNameState = function () {
    return {
        countMax: window.DEPLOY_COUNT_MAX,

        // ("web", 3, 63) -> ["web-01","web-02","web-03"]; count <= 1 -> ["web"].
        // The base is trimmed so base+suffix fits `limit` — never the suffix, which is
        // what keeps the series unique at Azure's 15 characters.
        nameSeries(base, count, limit, opts) {
            const o = opts || {};
            const cap = limit || 255;
            const n = Math.max(1, Math.min(parseInt(count, 10) || 1, window.DEPLOY_COUNT_MAX));
            let b = String(base || '').trim();
            if (o.lower) b = b.toLowerCase();
            if (n === 1) return [b];
            const width = Math.max(2, String(n).length);
            const stem = b.slice(0, Math.max(1, cap - width - 1)).replace(/[-.]+$/, '');
            return Array.from({ length: n },
                (_, i) => stem + '-' + String(i + 1).padStart(width, '0'));
        },

        // Render-ready preview. `truncated` drives the amber styling.
        namePreview(base, count, limit, opts) {
            const names = this.nameSeries(base, count, limit, opts);
            const n = names.length;
            if (!String(base || '').trim() || n <= 1) {
                return { names: names, text: '', truncated: false };
            }
            const width = Math.max(2, String(n).length);
            const stemLen = names[0].length - width - 1;
            const truncated = String(base).trim().length > stemLen;
            const text = n <= 4
                ? 'will create: ' + names.join(', ')
                : 'will create: ' + names.slice(0, 3).join(', ') + ' … ' + names[n - 1]
                  + ' (' + n + ' total)';
            return { names: names, text: text, truncated: truncated };
        },
    };
};

// Deploy endpoints return either a single job ({job_id, …}) or a batch
// ({batch_id, count, …}). A batch lands on the /jobs rollup, which already polls,
// counts failures and is bookmarkable.
//
// Returns false when there is no batch_id, so each caller keeps its existing
// single-job path verbatim — that is what makes count == 1 a zero-risk change, and it
// lets the front end ship before or after the server.
//
// `unit` names what was queued ('instance' by default, 'VM' for a bulk power op) and
// `message` replaces the composed sentence outright — bulk power needs that because it
// has to name the VMs it could NOT queue, and a count alone would let the number
// quietly disagree with the selection. Both are optional, so every existing caller is
// unchanged.
window.afterDeploy = function (resp, opts) {
    const o = opts || {};
    const say = o.notify || ((m, t) => toast(m, t || 'success'));
    if (resp && resp.batch_id) {
        const n = resp.count || (resp.job_ids || []).length;
        const unit = o.unit || 'instance';
        say(o.message
            || (o.label || 'Deployment') + ': ' + n + ' ' + unit + (n !== 1 ? 's' : '')
               + ' queued',
            o.type || 'success');
        setTimeout(() => {
            window.location.href = '/jobs?batch_id=' + encodeURIComponent(resp.batch_id);
        }, 400);
        return true;
    }
    return false;
};

// ── Reusable bulk power toolbar ───────────────────────────────────────────────
//
// Spread into an on-prem VM page component (`...bulkPowerState()`) and render with the
// `bulk_power_buttons` Jinja macro (templates/partials/bulk_power_toolbar.html). The
// page keeps its own selection state — `selectedVmIds` — and supplies four seams,
// because the six pages genuinely disagree about all four:
//
//   bulkPowerUrl            '/api/<kind>/power/bulk'
//   _bulkPowerRows()        the currently visible rows (filteredVms, filteredResources…)
//   _bulkPowerTarget(vm)    the per-VM payload — the SAME object shape the row's own
//                           powerOp already POSTs, so bulk cannot address a VM
//                           differently from the button beside it
//   _bulkPowerRunning(vm)   true / false / null, where null means "not known"
//
// It also uses the page's existing `_vmKey(vm)` (must match the server's
// `_override_key`), its `showToast` if it has one, and its `guestToolsMaybeReady` and
// `canOp` when present. Nothing here is a getter: tests/template_helpers_check.js
// extracts helpers by the literal `name(args) {` shape and cannot see one.
window.bulkPowerState = function () {
    return {
        bulkPowerBusy: false,
        // Which op is in flight, so only the button that was pressed says so. All four
        // are disabled regardless — a second bulk op while the first is still being
        // accepted would send the same selection twice.
        bulkPowerOp: '',

        // Which ops a graceful shutdown has to consult the guest agent for. Kept here
        // rather than in the macro so a page that gains a guest-tools gate does not
        // also have to remember the toolbar.
        bulkGuestOps: ['shutdown', 'reboot'],

        // The op the toolbar may send. Falls back to ALLOWED when the page has no
        // `canOp` — templates/nutanix/index.html has no agent path and therefore no
        // canOp/agentOps at all, and `!canOp(op)` there would be an unbound name, which
        // Alpine fails silently on.
        bulkOpAllowed(op) {
            return typeof this.canOp === 'function' ? this.canOp(op) : true;
        },

        bulkOpTitle(op) {
            return typeof this.opTitle === 'function' ? this.opTitle(op) : '';
        },

        // Is this VM a candidate for `op`, from the state the table has already drawn?
        //
        // UNKNOWN IS NOT ABSENT, and this is the rule worth keeping: a row synced by an
        // agent may carry no power state at all, and a VM we are unsure about is
        // ELIGIBLE, never skipped. A skip is silent — the operator selected a machine
        // and nothing happened to it — whereas a job that should not have run fails
        // with the hypervisor's own message saying so. Same reasoning as
        // guestToolsMaybeReady: the page must not refuse on the strength of a field it
        // never measured.
        bulkPowerEligible(vm, op) {
            const running = this._bulkPowerRunning(vm);
            if (op === 'start') return running !== true;
            if (running === false) return false;
            if (this.bulkGuestOps.includes(op)
                && typeof this.guestToolsMaybeReady === 'function'
                && !this.guestToolsMaybeReady(vm)) {
                return false;
            }
            return true;
        },

        // What pressing `op` would actually do. Pure, and unit-tested in
        // tests/template_helpers_check.js — the eligibility arithmetic is the one piece
        // of this the operator sees before committing.
        bulkPowerPlan(op) {
            const chosen = new Set((this.selectedVmIds || []).map(String));
            const rows = (this._bulkPowerRows() || [])
                .filter(vm => chosen.has(String(this._vmKey(vm))));
            const eligible = rows.filter(vm => this.bulkPowerEligible(vm, op));
            return {
                targets: eligible.map(vm => this._bulkPowerTarget(vm)),
                total: chosen.size,
                skipped: rows.length - eligible.length,
            };
        },

        // Named per op, because "are you sure" tells the operator nothing they did not
        // already know. Each string says what the guest is or is not asked, which is
        // the difference between these buttons.
        bulkPowerConfirm(op, plan) {
            const n = plan.targets.length;
            const vms = n + ' VM' + (n !== 1 ? 's' : '');
            const skip = plan.skipped
                ? ' (' + plan.skipped + ' of the ' + plan.total
                  + ' selected cannot take this and will be skipped)'
                : '';
            const q = {
                shutdown: 'Shut down ' + vms + '? Each guest is asked to shut down; one '
                        + 'with no guest agent running will not answer.',
                stop: 'Force off ' + vms + '? This cuts the virtual power on all of '
                    + 'them — the guests are not asked, and unsaved work is lost.',
                restart: 'Hard-restart ' + vms + '? This is a power cut and back on '
                       + 'again — the guests are not asked.',
                reset: 'Reset ' + vms + '? This is a hard reset — the guests are not '
                     + 'asked.',
                hard_reboot: 'Force reboot ' + vms + '? The guests are not asked.',
                reboot: 'Reboot ' + vms + '? Each guest is asked to reboot.',
            }[op];
            // `start` returns falsy on purpose: it is not destructive, and the row's own
            // Start button does not confirm either. A dialog on the safe op is what
            // teaches an operator to dismiss the dialog on the unsafe one.
            return q ? q + skip : '';
        },

        async submitBulkPower(op) {
            if (this.bulkPowerBusy) return;
            const say = (m, t) => (typeof this.showToast === 'function'
                ? this.showToast(m, t) : toast(m, t));

            const plan = this.bulkPowerPlan(op);
            if (plan.targets.length === 0) {
                // No request, and no dialog. The selection is real but this op has
                // nothing to do with it, and a 400 from the server would say the same
                // thing far less clearly.
                say('None of the ' + plan.total + ' selected VMs can be sent ' + op
                    + ' right now — check their current state.', 'error');
                return;
            }
            const question = this.bulkPowerConfirm(op, plan);
            if (question && !confirm(question)) return;

            this.bulkPowerBusy = true;
            this.bulkPowerOp = op;
            try {
                const resp = await API.post(this.bulkPowerUrl,
                                            { op: op, targets: plan.targets });
                const failed = resp.failed || [];
                let message = 'Queued ' + resp.count + ' job'
                            + (resp.count !== 1 ? 's' : '');
                if (failed.length) {
                    // Named, not counted. Each of these carries its own reason — an
                    // offline agent, an op with no verb for this product, a workgroup
                    // this user cannot reach — and collapsing them to a number is how
                    // one connection's missing grant becomes "some of them didn't work".
                    message += '; ' + failed.length + ' could not run: '
                             + failed.map(f => f.name + ' (' + f.error + ')').join('; ');
                } else if (plan.skipped) {
                    message += '; ' + plan.skipped + ' skipped';
                }
                this.selectedVmIds = [];
                this.selectAll = false;
                // Lands on the batch, the way a single power op lands on its job —
                // otherwise the only reference to N jobs disappears with the toast.
                // afterDeploy returns false when there is no batch_id, which for this
                // endpoint should not happen; say so rather than clearing the selection
                // and going quiet, which would read as nothing having been queued.
                if (!window.afterDeploy(resp, {
                        unit: 'VM', message: message,
                        type: failed.length ? 'error' : 'success',
                        notify: say,
                    })) {
                    say(message + ' — but the response carried no batch id, so there is '
                        + 'no rollup to link to. Check the Jobs page.', 'error');
                }
            } catch (e) {
                say('Bulk ' + op + ' failed: ' + (e.message || e), 'error');
            } finally {
                this.bulkPowerBusy = false;
                this.bulkPowerOp = '';
            }
        },
    };
};

// ── WebSocket job tracker ─────────────────────────────────────────────────────
class JobTracker {
    constructor(jobId, callbacks = {}) {
        this.jobId = jobId;
        this.callbacks = callbacks;
        this.ws = null;
    }

    connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        // The browser WebSocket API cannot set an Authorization header, and a token in
        // the query string would be logged by every proxy on the path. The subprotocol
        // list is the one client-settable header that is neither, so the token rides
        // there and the server echoes `vmdash.bearer` back on accept.
        const token = localStorage.getItem('vm_cli_token');
        const url = `${protocol}//${location.host}/api/ws/jobs/${this.jobId}`;
        this.ws = token
            ? new WebSocket(url, ['vmdash.bearer', token])
            : new WebSocket(url);

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (this.callbacks.onMessage) this.callbacks.onMessage(data);
            if (data.status === 'completed' && this.callbacks.onComplete) {
                this.callbacks.onComplete(data);
            }
            if (data.status === 'failed' && this.callbacks.onFailed) {
                this.callbacks.onFailed(data);
            }
        };

        this.ws.onerror = (e) => {
            if (this.callbacks.onError) this.callbacks.onError(e);
        };

        this.ws.onclose = () => {
            if (this.callbacks.onClose) this.callbacks.onClose();
        };
    }

    close() {
        if (this.ws) this.ws.close();
    }
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function statusBadge(status) {
    const map = {
        // queued = a child row a parent job will drive; the runner never claims it.
        queued:    'bg-yellow-50 text-yellow-700',
        pending:   'bg-yellow-100 text-yellow-800',
        running:   'bg-blue-100 text-blue-800',
        completed: 'bg-green-100 text-green-800',
        failed:    'bg-red-100 text-red-800',
        cancelled: 'bg-gray-100 text-gray-800',
    };
    return map[status] || 'bg-gray-100 text-gray-600';
}

// Display name for a PERMISSION_SCOPES key. The keys are persisted in user/group
// permission JSON (and bootstrap_entitle_groups.py turns them into Entitle group names),
// so a scope whose display name has drifted from its key gets an entry here rather than
// a rename. Anything unmapped falls back to the old behaviour: underscores to spaces,
// capitalized by CSS.
function permissionScopeLabel(scope) {
    const map = {
        cloud_database: 'Databases',
        config_mgmt:    'Configuration',
        k8s:            'Kubernetes',
        vms:            'VMs',
        aws:            'AWS',
        azure:          'Azure',
        gcp:            'GCP',
        oci:            'OCI',
    };
    return map[scope] || String(scope || '').replace(/_/g, ' ');
}

function formatDuration(seconds) {
    if (seconds == null) return '–';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}m ${s}s`;
}

function timeAgo(isoStr) {
    if (!isoStr) return '–';
    // Server stores datetime.utcnow() without timezone info — treat as UTC
    const utcStr = /Z$|[+-]\d{2}:\d{2}$/.test(isoStr) ? isoStr : isoStr + 'Z';
    const ms = Date.now() - new Date(utcStr).getTime();
    const s = Math.floor(ms / 1000);
    if (s < 0) return 'just now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return new Date(utcStr).toLocaleDateString();
}

// Forward-looking sibling of timeAgo, for the auto-delete timer. timeAgo cannot be
// reused: it computes (now - t) and collapses every negative result to 'just now', so a
// future timestamp — which is what an expiry always is — would render "just now" on every
// unexpired resource.
//
// Returns 'overdue' once the moment has passed, so the caller doesn't have to
// distinguish "expiring" from "expired" by re-parsing the date. Granularity stops at
// minutes: seconds churn on every tick and read as false precision on a multi-day timer.
function timeUntil(isoStr) {
    if (!isoStr) return 'never';
    // Server stores datetime.utcnow() without timezone info — treat as UTC
    const utcStr = /Z$|[+-]\d{2}:\d{2}$/.test(isoStr) ? isoStr : isoStr + 'Z';
    const t = new Date(utcStr).getTime();
    if (isNaN(t)) return '–';
    const s = Math.floor((t - Date.now()) / 1000);
    if (s <= 0) return 'overdue';
    if (s < 3600) return `in ${Math.max(1, Math.floor(s / 60))}m`;
    if (s < 172800) return `in ${Math.floor(s / 3600)}h`;   // < 48h → hours
    return `in ${Math.floor(s / 86400)}d`;
}

// Absolute UTC form of a timestamp, for the tooltip behind a relative label. An operator
// about to extend or delete something needs the actual deadline, not "in 6h".
function utcStamp(isoStr) {
    if (!isoStr) return '';
    const utcStr = /Z$|[+-]\d{2}:\d{2}$/.test(isoStr) ? isoStr : isoStr + 'Z';
    const d = new Date(utcStr);
    if (isNaN(d.getTime())) return '';
    return d.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

// The accessor's own page. One constant, so the redirect below and the page itself can
// never disagree about where an accessor lives.
const ACCESSOR_HOME = '/pov/access';

function requireAuth() {
    const auth = Alpine.store('auth');
    if (!auth.isLoggedIn) {
        window.location.href = '/login';
        return;
    }
    // A POV accessor on any other page would render a shell whose every API call comes
    // back 403 — technically safe and completely baffling. Send them home instead.
    //
    // A CONVENIENCE, not a control. It reads localStorage, which the holder can edit, and
    // it runs in their browser. Nothing here is load-bearing: api/auth.get_current_user
    // refuses an accessor on every path but its own, server-side, whatever this does.
    if (auth.isAccessor && window.location.pathname !== ACCESSOR_HOME) {
        window.location.href = ACCESSOR_HOME;
    }
}

// The nav: one bar, one drawer, at every width.
//
// This used to fold between an inline link row and the drawer, measuring the row's
// scrollWidth on every resize to decide which. The measurement that retired that is in
// tests/test_persona_nav — a fully-enabled admin instance overflows a 1280px viewport by
// 1290px, so the inline row was already folded away for the configuration most people
// run. What is left is the model that was always the complete one.
function responsiveNav() {
    return {
        mobileNav: false,
        _pinned: false,

        // Persona nav pinning. CURATION ONLY, and now structurally so: the links are
        // REORDERED within the one list that exists. Nothing is moved to a second
        // container that could hide it, and nothing is removed, so "a persona can never
        // make a page unreachable" needs no escape hatch to be true.
        //
        // Scoped to $refs.navDrawer rather than document: the drawer is the only render of
        // _nav_links.html today, but the docs shell and any future second render would
        // both be scrambled by a document-wide selector, and that failure is silent.
        //
        // Runs ONCE (_pinned). It is a DOM move, not a render.
        applyPins() {
            if (this._pinned) return;
            const list = this.$refs.navDrawer;
            if (!list) return;
            this._pinned = true;

            const pins = (list.dataset.navPins || '')
                .split(',').map(s => s.trim()).filter(Boolean);
            // Neutral: no pins, DOM untouched.
            if (!pins.length) return;

            const byId = new Map(Array.from(list.querySelectorAll('a[data-nav]'))
                .map(a => [a.dataset.nav, a]));

            // Hoist the pinned links to the top, in the order the persona named them.
            // insertBefore on a node already in the parent MOVES it, so this reorders
            // without cloning — cloning would drop the Alpine x-show bindings that hide
            // the admin-only links from non-admins.
            let cursor = null;
            for (const id of pins) {
                const el = byId.get(id);
                if (!el) continue;              // a pin for a link this instance lacks
                list.insertBefore(el, cursor ? cursor.nextSibling : list.firstChild);
                cursor = el;
            }

            // A rule between the persona's links and the rest, so the reorder reads as a
            // choice rather than as a scrambled list. Only when something actually moved:
            // a pin set naming nothing this instance has must not leave a divider at the
            // top of an untouched list. createElement, not innerHTML — the siblings are
            // live Alpine-bound nodes.
            if (cursor) {
                list.insertBefore(document.createElement('hr'), cursor.nextSibling);
            }
        },

        init() {
            // Scroll lock while the flyout is open. A drawer that lets the page scroll
            // underneath it costs the user their place: they swipe to reach a link near
            // the bottom of a ~28-item list, the swipe lands on the backdrop or runs past
            // the end of the list, and the page behind moves instead. They dismiss the
            // menu and are somewhere they did not choose.
            //
            // `overflow: hidden` on <body> rather than `position: fixed`: html's overflow
            // is `visible`, so body's propagates to the viewport and the page locks in
            // place WITHOUT the scroll position being reset, which the position:fixed
            // version of this trick famously loses. The two chaining paths that overflow
            // alone does not close are handled in the template — `overscroll-contain` on
            // the link list, `touch-none` on the backdrop.
            this.$watch('mobileNav', open => {
                document.body.classList.toggle('overflow-hidden', open);
            });
            // $nextTick, not a bare call: a component's init() runs BEFORE Alpine walks
            // its children, so $refs.navDrawer is still undefined here and applyPins()
            // returns having done nothing — silently, leaving the pins in the attribute
            // and the list in shipped order.
            this.$nextTick(() => this.applyPins());
        },
    };
}
window.responsiveNav = responsiveNav;

// ── WebAuthn / FIDO2 helper ────────────────────────────────────────────────────
// window assignment ensures inline template scripts can access it regardless of scope
window.WebAuthnHelper = {
    /** Decode a base64url string to Uint8Array */
    decodeChallenge(b64url) {
        const padding = '='.repeat((4 - b64url.length % 4) % 4);
        const b64 = b64url.replace(/-/g, '+').replace(/_/g, '/') + padding;
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        return bytes;
    },

    /** Encode an ArrayBuffer or Uint8Array to base64url string */
    encodeBuffer(buf) {
        const bytes = buf instanceof ArrayBuffer ? new Uint8Array(buf) : buf;
        let binary = '';
        for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
        return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
    },

    /**
     * Convert server-sent PublicKeyCredentialCreationOptions (JSON with base64url bytes)
     * into the format navigator.credentials.create() expects.
     */
    prepareCreationOptions(opts) {
        const o = JSON.parse(JSON.stringify(opts));  // deep clone
        if (o.challenge) o.challenge = this.decodeChallenge(o.challenge);
        if (o.user && o.user.id) o.user.id = this.decodeChallenge(o.user.id);
        if (o.excludeCredentials) {
            o.excludeCredentials = o.excludeCredentials.map(c => ({
                ...c,
                id: this.decodeChallenge(c.id),
            }));
        }
        return o;
    },

    /**
     * Convert server-sent PublicKeyCredentialRequestOptions (JSON with base64url bytes)
     * into the format navigator.credentials.get() expects.
     */
    prepareRequestOptions(opts) {
        const o = JSON.parse(JSON.stringify(opts));
        if (o.challenge) o.challenge = this.decodeChallenge(o.challenge);
        if (o.allowCredentials) {
            o.allowCredentials = o.allowCredentials.map(c => ({
                ...c,
                id: this.decodeChallenge(c.id),
            }));
        }
        return o;
    },

    /**
     * Serialize a PublicKeyCredential returned by the browser into a plain JSON
     * object suitable for sending to the server.
     */
    serializeCredential(cred) {
        const obj = {
            id: cred.id,
            rawId: this.encodeBuffer(cred.rawId),
            type: cred.type,
        };
        const r = cred.response;
        if (r.attestationObject !== undefined) {
            // Registration response
            obj.response = {
                clientDataJSON: this.encodeBuffer(r.clientDataJSON),
                attestationObject: this.encodeBuffer(r.attestationObject),
            };
        } else {
            // Authentication response
            obj.response = {
                clientDataJSON: this.encodeBuffer(r.clientDataJSON),
                authenticatorData: this.encodeBuffer(r.authenticatorData),
                signature: this.encodeBuffer(r.signature),
                userHandle: r.userHandle ? this.encodeBuffer(r.userHandle) : null,
            };
        }
        return obj;
    },
};

