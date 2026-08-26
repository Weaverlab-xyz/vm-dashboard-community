# A POV Instance

A **second** dashboard, next to the one managing your demo estate. Same image, same code,
its own database, its own users, its own agents — and a different set of features.

It exists because of one thing: **tenancy.** A demo instance resolves its BeyondTrust
tenant from the global singletons (`bt_api_host`, `pscli_api_url`, `entitle_api_key`). A POV
instance holds a *registry* of many named tenants, because several POVs run at once and each
has its own PRA appliance and Password Safe Cloud tenant. An instance claiming both roles
would have two answers to "which tenant?" at every call site — and the wrong answer is
silent, not loud: a demo VM deploy onboarding into a customer's Password Safe, or a POV
onboarding into your demo tenant. Nothing errors, both paths "work".

So the two profiles are **mutually exclusive**, and the exclusion is enforced in code rather
than left to discipline.

---

## The gate

`install_profile` is `demo` (the default, and what every existing install already is) or
`pov`. It is set on the setup wizard's **Purpose** step, and it is a gate, not a preference.

Everything resolves through one function — `services/feature_flags.enabled()`:

```
enabled(flag) = False                         if this profile masks the flag
              = config_service.get_bool(flag) otherwise
```

That single reader is the point. `main._feature_gate` decides whether a router 404s, and
`feature_flags.flags()` decides whether the nav link and Settings toggle render. A mask
applied in only one of them gives you a nav link to a router that 404s, or a page with no
way to reach it. Both come through `enabled()`.

Two properties worth knowing:

- **The mask only ever subtracts.** A profile can refuse a feature. It can never turn on one
  that config left off.
- **An unrecognised profile resolves to `demo`.** The profile is read on the request path, so
  a typo in one config row must not take the app down — and falling back to `demo` means the
  worst case is *today's* behaviour.

### What each profile gets

| | `demo` | `pov` |
|---|---|---|
| Cloud VMs, images, Packer, promote | yes | — |
| On-prem hypervisors (Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng) | yes | — |
| Cloud databases, Kubernetes, containers, cloud functions | yes | — |
| Virtual desktops, EPM-L, cost reporting | yes | — |
| POV environments | — | yes |
| Lab platforms (Skytap) | — | yes |
| PRA, Password Safe, Entitle | yes | yes |
| Remote agents, Config Management | yes | yes |
| Auto-delete timer, notifications, secret scanning, auth/SSO | yes | yes |

The third block is deliberate: a POV instance needs PRA, Password Safe and the agent more
than a demo instance does, so those are profile-neutral rather than owned by either.

A demo-only integration on a POV instance is not merely toggled off — **Settings refuses to
enable it**, with a 409 naming the profile. Accepting the write would store a flag that
reads back as on while `enabled()` keeps returning `False`: a toggle that saves cleanly,
shows on, and does nothing. Turning something *off* is always allowed.

A POV instance also needs a **lab platform** — the thing a POV environment actually runs
on. Skytap is the first one; see [integrations/skytap.md](integrations/skytap.md) for its
prerequisites and the API token it needs. The platform registry lives in
`services/lab_platforms.py`, and `GET /api/pov/platforms` reports what each one can do so
the UI can degrade visibly rather than offering a button that fails.

---

## Standing one up

```bash
# Its own JWT key. Not a copy of the demo stack's — see below.
openssl rand -hex 32 > .jwt_secret_key.pov

# Its own env file. Start from the example and set only what a POV instance uses.
cp .env.example .env.pov

docker compose -p vmdash-pov -f docker-compose.pov.yml up -d
```

Then browse `http://localhost:8002` (the demo stack keeps 8001) and walk the wizard: create
the admin account, choose **Customer POV / POC environments** on the Purpose step, and the
four cloud credential steps disappear.

`-p vmdash-pov` matters. Without an explicit project name Compose derives one from the
directory, and both stacks would try to share networks and volume names.

### Four things that will bite otherwise

**Its `JWT_SECRET_KEY` must be its own value.** That key is the Fernet root for the
`app_config` table. Two instances sharing it is the only way their encrypted configuration
could be interchanged — and two instances with *different* keys is exactly why a `pg_dump`
restore from one into the other yields **unreadable ciphertext with no error at all**. If you
need to copy settings across, use the config-migration tool
([config-migration.md](config-migration.md)); never a database dump.

**Bring it up alone the first time.** First boot runs `init_db`, which takes a Postgres
advisory lock. Two cold instances started together can wedge on it. Start this stack, let it
finish, then start the other.

**Its agent endpoint must be reachable from inside your POV environments.** POV wiring works
by running an agent on a broker VM *inside* the customer environment, which polls outward.
That means `/api/agent` on this instance has to be reachable over public HTTPS from there.
When you host it beyond your LAN, use the gateway-sidecar split in
[cloud-hosting.md](cloud-hosting.md) so the agent endpoint is separate from the UI, and keep
the UI behind its own allowlist. A UI 404 that returns in microseconds is the allowlist
doing its job, not a broken deploy.

**Auto-delete is off, observe-only is on, and two one-hour arming clocks have not started.**
A POV is a 30-day resource, so this is the one piece of configuration a POV instance is not
useful without — and enabling it is deliberately not something the compose file can do.
Follow the rollout in [auto-delete-timer.md](auto-delete-timer.md) *before* you create a POV
you expect to be cleaned up. Note also that `resource_expiry_max_total_hours` defaults to
`720` — exactly 30 days — so a 30-day POV starts at the ceiling and cannot be extended until
you raise it.

---

## What this stack deliberately does not mount

Unlike the demo stack, neither service gets `/var/run/docker.sock` or the `runner_work`
volume. The demo stack needs the socket to launch sibling Ansible and kubectl/helm
containers. A POV instance runs Ansible **on the remote agent inside the customer
environment**, and Kubernetes is masked off entirely, so handing this stack the host's Docker
daemon would grant a great deal and buy nothing.

If you later want local Config Management runs here, the socket has to come back — and that
is a deliberate decision to make, not an omission to fix.

---

## Failure modes

**A nav link is missing and I know the flag is on.** Check the profile. `GET /api/features`
returns `install_profile`, and a masked feature reports `false` there whatever the stored
flag says. That is the mask, working.

**Settings gives me a 409 when I enable an integration.** Also the mask. The message names
the profile. That integration belongs on the other instance.

**I switched the profile and a feature did not come back.** The mask only subtracts, so
switching to `demo` stops masking — but the feature's own `*_enabled` flag still has to be
on. Check both.

**Everything is refused with "not enabled" on a fresh POV instance.** `pra_enabled` and
`password_safe_enabled` default to `True` in code but the compose file sets them explicitly;
if you wrote your own env file, set them. `configured()` is separate again: PRA and Password
Safe also need their credentials before their endpoints do anything.

**The wizard still shows cloud steps.** You are on `demo`. The Purpose step drives the step
list, so re-run `/setup` and change it — nothing else needs re-entering.

**A cloud region key appeared in a POV instance's config.** It should not. The `pov` profile
skips the cloud **writes**, not just the screens, precisely because the wizard otherwise
persists every non-secret field whether or not you filled it in — leaving
`aws_region="us-east-2"` and friends behind. If you see one, it predates the profile or was
set by hand.
