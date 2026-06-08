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
  a malicious administrator account.
- Misconfiguration of *your* deployment — e.g. exposing the dashboard to the
  internet without a reverse proxy/TLS, weak credentials you set, or over-broad
  cloud IAM you grant.
- Secrets you commit to your own fork or `.env`.
- Denial of service from unrealistic request volumes, or missing rate limits on
  localhost-only flows.
- Findings in third-party dependencies without demonstrated impact here — please
  report those upstream, though we still want to hear about them.

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
