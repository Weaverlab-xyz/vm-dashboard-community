# Security Policy

Thanks for helping keep the **Infrastructure Management Dashboard (Community
Edition)** and the people who run it safe. We take security issues seriously and
appreciate responsible disclosure.

## Reporting a vulnerability

**Please do not open a public issue, pull request, or Discussion for a security
vulnerability** — a public report exposes the problem to everyone before a fix
is available.

Instead, report it privately through GitHub's private vulnerability reporting:

➡️ **[Open a private security advisory](https://github.com/Weaverlab-xyz/vm-dashboard-community/security/advisories/new)**
&nbsp;&nbsp;(Repository → **Security** tab → **Report a vulnerability**)

This opens a private channel visible only to the maintainers and you. If you
cannot use GitHub advisories for some reason, open a minimal public issue titled
"Security contact request" (with **no** details) and a maintainer will reach out
with a private channel.

Please include as much of the following as you can:

- A description of the issue and its impact.
- The affected component and version (release tag or commit SHA).
- Step-by-step reproduction — a proof-of-concept, request/response, or config.
- Relevant logs, screenshots, or sample payloads.
- Your assessment of severity and, if you have one, a suggested fix.

## What to expect

This is a community project maintained on a best-effort basis. As a rough guide:

- **Acknowledgement** of your report within ~5 business days.
- **Initial assessment / triage** within ~10 business days.
- **Fix and coordinated disclosure** on a timeline driven by severity and
  complexity. We'll keep you updated and are happy to credit you (with your
  permission) when the advisory is published.

We follow **coordinated disclosure**: please give us a reasonable window to ship
a fix before publishing any write-up.

## Supported versions

Security fixes are applied to the latest released version and `main`.

| Version                 | Supported            |
| ----------------------- | -------------------- |
| Latest release / `main` | ✅                   |
| Older tagged releases   | ❌ — please upgrade  |

## Scope and threat model

This is **self-hosted** software: you run it in your own environment with your
own cloud credentials, secrets, and infrastructure. An authenticated
administrator is *intended* to perform powerful operations — provisioning and
managing VMs and cloud resources, building images, and retrieving secrets
through configured vaults. That power is by design and is **not** itself a
vulnerability.

**In scope** — please report:

- Authentication or authorization bypass; privilege escalation; cross-user or
  cross-workgroup access (IDOR).
- Remote code or command injection (the dashboard invokes shell, PowerShell,
  Terraform, and Packer).
- Server-side request forgery (SSRF), path traversal, or unsafe file handling
  (ISO/OVA/image paths, uploads).
- Leakage of secrets or credentials — e.g. into logs, API responses, or error
  messages.
- Cross-site scripting (XSS), CSRF, or session/JWT handling flaws in the web
  UI or API.
- Insecure defaults shipped in this repository.

**Out of scope** — generally not vulnerabilities:

- Issues that require an already-compromised host, root/admin on the server, or
  a malicious administrator account — **except across the remote-agent boundary,
  see below**.
- Misconfiguration of *your* deployment — e.g. exposing the dashboard to the
  internet without a reverse proxy/TLS, weak credentials you set, or over-broad
  cloud IAM you grant.
- Secrets you commit to your own fork or `.env`.
- Denial of service from unrealistic request volumes, or missing rate limits on
  localhost-only flows. (Password login is throttled — see below. A bypass of
  *that* is in scope.)
- Findings in third-party dependencies without demonstrated impact here — please
  report those upstream, though we still want to hear about them.

### Login throttling

`POST /api/auth/login` is throttled by `web_dashboard/services/login_guard.py`. Worth
knowing how it is keyed, because the obvious assumption is wrong:

- **The primary key is the username**, not the source address. A username cannot be
  rotated; it is the thing being attacked. The address is a secondary key, because an
  address is only as trustworthy as the proxy configuration behind it.
- A **per-address cap** rides along to catch spraying across many accounts. It is
  meaningful only because `TRUSTED_PROXY_HOSTS` now defaults to loopback rather than
  `*` — see below. If you set it back to `*`, this half becomes decorative: a client
  can then declare its own address and rotate it per request.
- Counters live in the database, so they hold across both gunicorn workers and survive
  a restart. An in-process counter would give an attacker double the allowance.
- It is a **sliding window, not a lockout** — deliberately. A lockout would hand
  anyone who knows a username a denial-of-service against that account. The block
  lifts on its own and `Retry-After` says exactly when.
- The 429 is identical whether the account exists, does not exist, or is real and being
  guessed at, so the throttle is not a user-enumeration oracle.
- The same budget covers the FIDO2 second factor and `webauthn/login/begin`, so a
  stolen password does not buy unlimited attempts at the second factor.

Tunable through the config store (`app_config`), all optional:

| Key | Default | Meaning |
|---|---|---|
| `login_throttle_enabled` | `true` | Master switch; fails *closed* if unreadable |
| `login_max_attempts` | `10` | Failures per username per window; `0` disables |
| `login_max_attempts_per_ip` | `50` | Failures per address per window; `0` disables |
| `login_window_minutes` | `15` | The sliding window |
| `login_retention_minutes` | `60` | How long failure rows are kept for forensics |

Note that **no other endpoint is rate limited.** The SlowAPI limiter in `main.py` is
constructed but inert — `SlowAPIMiddleware` is not added, because a blanket per-address
cap would break the UI, which fires many API calls per page load. Missing rate limits
elsewhere remain out of scope above.

### SSO and WebAuthn ceremony state

FIDO2/WebAuthn challenges and the OAuth/OIDC CSRF `state` (with its PKCE verifier) are
held in the `ephemeral_state` table, deleted on read, and expire in 120 and 300 seconds
respectively.

In the database for the same reason as the login counters above, and it was the same
mistake: this was a process-local dict guarded by a `threading.Lock`, which is the right
guard for the wrong hazard. The app runs `gunicorn -w 2`, so the lock made the dict safe
against one worker's threads while giving the two worker **processes** a private copy
each. Both ceremonies span two requests — FIDO2 begin/complete, OAuth login/callback —
and nothing pins a browser to a worker, so the second leg reached a process with no
record of the state about half the time. That surfaced as `/login?error=invalid_state`
or "Invalid or expired FIDO2 challenge" on a correct credential, and it got worse with
each added replica. It was an availability bug rather than a bypass — the closed
direction, never the open one — but it made SSO and MFA unreliable enough to discourage
turning them on, which is its own security cost.

**Consumption is a delete, and the delete's rowcount is the lock**, the same portable
atomic claim the job queue uses. Only the caller whose `DELETE` matched a row may act on
what it read, so a replayed `state` cannot be accepted twice even by two workers racing
on it. An expired row is consumed rather than left behind: expiry is a rejection, not a
retry. Expired rows are also swept on the write path, so an abandoned ceremony — a
closed SSO tab, a cancelled touch prompt — no longer accumulates, which the dict did for
the life of the worker.

These rows are opaque, single-use and short-lived; they hold no credential and no token.

### Reverse proxies, forwarded headers, and the public URL

`TRUSTED_PROXY_HOSTS` decides which peers may set `X-Forwarded-For` and
`X-Forwarded-Proto`. It **defaults to `127.0.0.1`**, matching uvicorn's own default. A
wildcard would let any client that can reach the socket declare its own source address,
and the per-address half of the login throttle keys off exactly that value.

**Behind a reverse proxy, set it to the proxy's literal IP.** It must be a literal:
uvicorn 0.27 compares strings and understands neither hostnames nor CIDR (CIDR arrived
in 0.31), so a service name or a subnet silently never matches. `docker-compose.agent.yml`
shows the pattern — a fixed subnet and a static address on the proxy container.

Getting it wrong is **not silent**. When a forwarded header arrives from an untrusted
peer the dashboard logs, once per peer:

> Ignoring X-Forwarded-* from 172.29.7.2: it is not in trusted_proxy_hosts (127.0.0.1).
> If 172.29.7.2 is your reverse proxy, set TRUSTED_PROXY_HOSTS=172.29.7.2 …

**Set `PUBLIC_BASE_URL` too** (e.g. `https://dash.example.com`). The OAuth callback URIs
and the remote-agent signing audience used to be derived from `request.url.scheme`,
which is only `https` because the proxy headers were trusted — so proxy trust and OAuth
correctness were the same failure. Stating the origin once decouples them: the callback
is right whether or not the proxy is listed, which matters because a redirect-URI
mismatch is rejected by the identity provider with no useful diagnostic.

| Key | Default | Meaning |
|---|---|---|
| `TRUSTED_PROXY_HOSTS` | `127.0.0.1` | Literal peer IPs allowed to set `X-Forwarded-*`, comma-separated |
| `PUBLIC_BASE_URL` | *(derived)* | The absolute origin this dashboard is reached at |

### The remote-agent trust boundary

Everything above assumes one trust domain: you run the dashboard, it holds your
credentials, and an administrator abusing it is abusing resources they already own.

[Remote agents](docs/remote-agents.md) break that assumption, so they get their own
rule. An agent is a container running inside a private network — possibly one whose
owner is not the dashboard's operator — and it is, structurally, a
dashboard-controlled execution endpoint on that network. **Across this boundary the
dashboard is untrusted.** Compromise of the dashboard, or of its database, must not
be sufficient to execute anything on the far side.

**In scope** for the agent, in addition to everything above:

- Any way to make an agent act on a job envelope that the dashboard's signing key did
  not sign — including forging, replaying, or substituting one.
- Any way to get an agent to reach a host or port outside its `policy.yaml`, including
  via DNS rebinding, redirect following, or a path that skips the resolved-IP check.
- Any protocol field that can carry executable content — a command, a script, a
  fetchable URL, a filename — into an agent.
- Any way for one enrolled agent to lease, read, log to, or complete another agent's
  job.
- Any way for an agent credential to be replayable: a captured request that succeeds
  twice, outside its timestamp window, or against a different method, path, body or
  audience than it was signed for.
- Any way a dashboard-side API can modify, disable, or misreport an agent's local
  policy file.

Reports in these categories are wanted **even though they require a compromised or
malicious dashboard**, which is exactly the exemption that does not apply here.

For background on how the dashboard handles credentials and secrets, see
[docs/secrets-management.md](docs/secrets-management.md).

## Safe harbor

We consider good-faith security research conducted in line with this policy to
be authorized. We will not pursue action against researchers who:

- Make a good-faith effort to avoid privacy violations, data destruction, and
  service disruption.
- Test only against their **own** deployments — never against other users'
  instances or infrastructure.
- Give us a reasonable time to remediate before public disclosure.

Thank you for helping keep the community safe.
