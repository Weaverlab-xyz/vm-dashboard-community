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

- **The primary key is the username**, not the source address. `get_remote_address`
  reads a value derived from `X-Forwarded-For`, and the shipped
  `TRUSTED_PROXY_HOSTS` default is `*` — so a per-address cap alone is defeated by
  rotating one header per request. A username cannot be rotated; it is the thing being
  attacked.
- A **per-address cap** rides along to catch spraying across many accounts. Its
  strength depends on pinning `TRUSTED_PROXY_HOSTS` to your proxy.
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
cap would break the UI. Missing rate limits elsewhere remain out of scope above.

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
