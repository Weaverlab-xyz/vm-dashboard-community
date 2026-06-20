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

        get isLoggedIn() {
            return !!this.token;
        },

        login(token, username, workgroups, isAdmin = false) {
            this.token = token;
            this.username = username;
            this.workgroups = workgroups;
            this.isAdmin = isAdmin;
            localStorage.setItem('vm_cli_token', token);
            localStorage.setItem('vm_cli_username', username);
            localStorage.setItem('vm_cli_workgroups', JSON.stringify(workgroups));
            localStorage.setItem('vm_cli_is_admin', isAdmin ? 'true' : 'false');
        },

        logout() {
            this.token = null;
            this.username = null;
            this.workgroups = [];
            this.isAdmin = false;
            localStorage.removeItem('vm_cli_token');
            localStorage.removeItem('vm_cli_username');
            localStorage.removeItem('vm_cli_workgroups');
            localStorage.removeItem('vm_cli_is_admin');
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

// ── WebSocket job tracker ─────────────────────────────────────────────────────
class JobTracker {
    constructor(jobId, callbacks = {}) {
        this.jobId = jobId;
        this.callbacks = callbacks;
        this.ws = null;
    }

    connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${protocol}//${location.host}/api/ws/jobs/${this.jobId}`);

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
        pending:   'bg-yellow-100 text-yellow-800',
        running:   'bg-blue-100 text-blue-800',
        completed: 'bg-green-100 text-green-800',
        failed:    'bg-red-100 text-red-800',
        cancelled: 'bg-gray-100 text-gray-800',
    };
    return map[status] || 'bg-gray-100 text-gray-600';
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

function requireAuth() {
    if (!Alpine.store('auth').isLoggedIn) {
        window.location.href = '/login';
    }
}

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

