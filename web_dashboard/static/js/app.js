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
            window.location.href = '/login';
        },

        hasWorkgroup(wg) {
            return this.workgroups.includes(wg);
        }
    });
});

// ── API helper ────────────────────────────────────────────────────────────────
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

    get:    (path)        => API.request('GET',    path),
    post:   (path, body)  => API.request('POST',   path, body),
    put:    (path, body)  => API.request('PUT',    path, body),
    patch:  (path, body)  => API.request('PATCH',  path, body),
    del:    (path)        => API.request('DELETE', path),
    delete: (path)        => API.request('DELETE', path),  // alias — some templates use API.delete
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
window.afterDeploy = function (resp, opts) {
    const o = opts || {};
    const say = o.notify || ((m, t) => toast(m, t || 'success'));
    if (resp && resp.batch_id) {
        const n = resp.count || (resp.job_ids || []).length;
        say((o.label || 'Deployment') + ': ' + n + ' instance' + (n !== 1 ? 's' : '') + ' queued',
            'success');
        setTimeout(() => {
            window.location.href = '/jobs?batch_id=' + encodeURIComponent(resp.batch_id);
        }, 400);
        return true;
    }
    return false;
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

// Responsive nav: swap the inline link row for a hamburger + dropdown when
// the links can't fit alongside the brand and user menu. Measures overflow
// directly instead of using a fixed Tailwind breakpoint, so it stays correct
// as feature flags add or remove nav items per install.
function responsiveNav() {
    return {
        compact: false,
        mobileNav: false,
        moreOpen: false,
        moreCount: 0,
        _measuring: false,
        _pinned: false,
        _t: null,

        // Persona nav pinning. CURATION ONLY: every link that was in the row is still
        // reachable — the ones the persona did not pin move into the "More" menu, and the
        // flyout drawer keeps the complete list in shipped order regardless.
        //
        // This exists to BUY nav width, not spend it. base.html's x-ref="navRow" spans the
        // brand, the links AND the user menu, and the row folds entirely into the drawer
        // the moment it overflows (61px of headroom at 1280px — tests/test_profile_theme
        // pins the brand side of that budget). Six pinned links plus one "More" button is
        // materially narrower than the twenty-link row, so a persona strictly reduces fold
        // pressure rather than adding to it.
        //
        // Scoped to $refs.navInline, never document: both renders of _nav_links.html are in
        // the DOM at once and a document-wide selector would scramble the drawer.
        //
        // Runs ONCE (_pinned). It is a DOM move, not a render, so repeating it on every
        // resize would re-walk an already-pinned row for nothing.
        applyPins() {
            if (this._pinned) return;
            const inline = this.$refs.navInline;
            const menu = this.$refs.navMore;
            if (!inline || !menu) return;
            this._pinned = true;

            const pins = (inline.dataset.navPins || '')
                .split(',').map(s => s.trim()).filter(Boolean);
            // Neutral: no pins, no menu, DOM untouched.
            if (!pins.length) return;

            const links = Array.from(inline.querySelectorAll('a[data-nav]'));
            const byId = new Map(links.map(a => [a.dataset.nav, a]));

            // Hoist the pinned links to the front, in the order the persona named them.
            // insertBefore on a node already in the parent MOVES it, so this reorders
            // without cloning — cloning would drop the Alpine x-show bindings that hide
            // the admin-only links from non-admins.
            let cursor = null;
            for (const id of pins) {
                const el = byId.get(id);
                if (!el) continue;              // a pin for a link this instance lacks
                inline.insertBefore(el, cursor ? cursor.nextSibling : inline.firstChild);
                cursor = el;
            }

            // Everything unpinned goes to the overflow menu. `w-full` so the links fill the
            // vertical popover; the drawer already proves these classes read fine stacked.
            for (const a of links) {
                if (pins.includes(a.dataset.nav)) continue;
                a.classList.add('w-full');
                menu.appendChild(a);
            }
        },

        // Open/close the overflow popover, anchoring it before it is shown.
        toggleMore() {
            this.moreOpen = !this.moreOpen;
            if (this.moreOpen) this.positionMore();
        },

        // Anchor the popover's right edge to the More button's right edge.
        //
        // The popover cannot live inside the button's wrapper: base.html's x-ref="navRow"
        // is `overflow-hidden` — which is what makes the `scrollWidth > clientWidth` fold
        // read meaningful — and a panel hanging below a 64px row is a descendant of that
        // clip. Measured in a browser: the panel opened at its full 132px and 122px of it
        // were clipped away, leaving a same-colour sliver on the nav. z-index does not
        // help; overflow clipping is not a stacking question.
        //
        // So the panel is a child of <nav> instead, and this restores the `right-0` it lost
        // by moving. Rects rather than offsetLeft: the difference of two viewport-relative
        // rects is scroll-independent and does not care which ancestor is the offsetParent.
        //
        // The host is read as menu.parentElement, NOT as $el. $el is per-expression: called
        // from the button's own @click it resolves to the BUTTON, so `$el.right - btn.right`
        // was 0 and this bailed every single time the user pressed the thing — the exact
        // symptom it exists to fix, moved one layer down. parentElement is <nav>, which is
        // the `relative` containing block the `right` offset is resolved against, and it is
        // the same node no matter which element the caller was evaluated on.
        positionMore() {
            const btn = this.$refs.moreBtn;
            const menu = this.$refs.navMore;
            if (!btn || !menu || !menu.parentElement) return;
            // Folded (or pre-layout): the button has no box, and trusting a zero rect
            // would fling the panel to the far left. It is x-show'd off anyway.
            if (!btn.offsetWidth) return;
            const host = menu.parentElement.getBoundingClientRect();
            const b = btn.getBoundingClientRect();
            const right = host.right - b.right;
            // Negative means the button is currently sitting outside the nav: measure()
            // forces the inline row visible before it reads the fold, so mid-measure an
            // overflowing row really does push this button past the nav's right edge.
            // Clamping that to 0 would PERSIST a wrong anchor (the panel is x-show'd off
            // while compact, so nothing would reveal it until the window grew again).
            // Leave the last good value alone instead; the next open recomputes.
            if (right <= 0) return;
            menu.style.right = right + 'px';
        },

        // How many overflow links are actually reachable. Counted rather than assumed,
        // because most unpinned links are admin-only (`x-show="$store.auth.isAdmin"`) and a
        // non-admin would otherwise get a "More" button opening an empty popover.
        //
        // The test is the element's OWN computed display, which is independent of its
        // ancestors'. That distinction is the whole reason this is not a one-liner: the menu
        // itself is `x-show="moreOpen"`, so at the moment this runs it is display:none, and
        // an `offsetParent !== null` check therefore returns 0 for EVERY link — the button
        // would never appear and the overflow links would be reachable only from the drawer.
        // Measured in a browser with Alpine: inside a hidden menu, a genuinely visible link
        // has offsetParent === null and offsetWidth === 0 but computed display 'inline',
        // while one its own x-show has hidden computes to 'none'.
        countMore() {
            const menu = this.$refs.navMore;
            if (!menu) { this.moreCount = 0; return; }
            this.moreCount = Array.from(menu.querySelectorAll('a[data-nav]'))
                .filter(a => getComputedStyle(a).display !== 'none').length;
        },

        init() {
            const measure = () => {
                if (this._measuring || this.mobileNav) return;
                this._measuring = true;
                // Force the inline layout so we can read the row's natural
                // vs available width. One-frame flash on resize is fine.
                this.compact = false;
                // Before the read, not after: the fold decision has to see the PINNED
                // width. Measuring first would decide on the twenty-link width and throw
                // away the entire width benefit for that page load.
                this.applyPins();
                this.$nextTick(() => {
                    const row = this.$refs.navRow;
                    this.countMore();
                    // Re-anchor here too, not just on open: this is the one moment the
                    // inline row is forced visible, so it is the only place a resize
                    // underneath an already-open popover can re-measure the button.
                    this.positionMore();
                    if (row) {
                        this.compact = row.scrollWidth > row.clientWidth + 1;
                    }
                    this._measuring = false;
                });
            };
            const onResize = () => {
                clearTimeout(this._t);
                this._t = setTimeout(measure, 50);
            };
            window.addEventListener('resize', onResize);
            // The nav is x-show=isLoggedIn, so it has zero size until login;
            // re-measure when the token changes to catch that transition.
            this.$watch('$store.auth.token', () => this.$nextTick(measure));
            this.$nextTick(measure);
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

