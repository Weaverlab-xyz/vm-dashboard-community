# Demo and POV profiles

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding which kind of instance to install, or you cannot find a feature the docs say exists.

Every install picks one of two purposes on the setup wizard's **Purpose** step, and the
choice is a gate rather than a preference. Almost everything else in `docs/` describes a
capability; this folder describes which of the two instances that capability belongs to.

| | I am… | Start here |
|---|---|---|
| **demo** | running my own estate — deploying cloud VMs, building images, standing up clusters and databases, whether to operate them or to show them to people | [Demo profile](demo/README.md) |
| **pov** | running customer proof-of-value environments, each against the customer's own BeyondTrust tenant | [POV profile](pov/README.md) |

If you are not sure, you are on `demo`: it is the default, and what every existing install
already is.

**The name is narrower than the profile.** `demo` was named for the first thing it was used
for, and it has never meant the work is pretend: it deploys real VMs into real cloud
accounts, onboards real credentials and rotates them on a real schedule. What it actually
names is a *tenancy shape* — one BeyondTrust tenant, resolved from the global singletons —
and somebody administering the infrastructure they depend on has exactly that shape. So a
community operator running their own environment wants `demo`, not a third profile: a
profile that masked features by purpose rather than by tenancy would be a preference, and
[the gate](#the-gate) below is the argument for why it must not be one.

Why they are exclusive is **tenancy.** A demo instance resolves its BeyondTrust tenant from
the global singletons (`bt_api_host`, `pscli_api_url`, `entitle_api_key`). A POV instance
holds a *registry* of many named tenants, because several POVs run at once and each has its
own PRA appliance and Password Safe Cloud tenant. An instance claiming both roles would
have two answers to "which tenant?" at every call site — and the wrong answer is silent,
not loud: a demo VM deploy onboarding into a customer's Password Safe, or a POV onboarding
into your demo tenant. Nothing errors, both paths "work".

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
| The AWS / Azure / GCP / OCI consoles and Images | yes | — |
| POV environments | — | yes |
| Lab platforms (Skytap) | — | yes |
| PRA, Password Safe, Entitle | yes | yes |
| Remote agents, Config Management, Storage | yes | yes |
| Auto-delete timer, notifications, secret scanning, auth/SSO | yes | yes |

The third block is deliberate: a POV instance needs PRA, Password Safe and the agent more
than a demo instance does, so those are profile-neutral rather than owned by either.

The cloud-console row is the one entry that is **not** a feature flag. Those five pages
gate on credential *presence*, so there was no flag for the mask to subtract and all five
kept rendering on a POV instance — pointing at pages the wizard guarantees can never hold
data. They resolve through `feature_flags.profile_page_allowed` and `main._profile_page_gate`
instead, which obey the same rule as the flag gate: one reader for the nav link and the
route, or you get a link to a 404.

**Storage stays**, and it is load-bearing rather than an oversight. Config Management is
gated on there being an active storage backend, so without one a POV instance cannot run
a playbook at all — and it has no cloud to put one in. The
[agent-brokered filesystem backend](../storage-management.md#remote-filesystem--unc-via-agent)
is the answer: a POV already runs an agent inside the customer's environment, and that
agent can reach a share the dashboard cannot.

A demo-only integration on a POV instance is not merely toggled off — **Settings refuses to
enable it**, with a 409 naming the profile. Accepting the write would store a flag that
reads back as on while `enabled()` keeps returning `False`: a toggle that saves cleanly,
shows on, and does nothing. Turning something *off* is always allowed.

## Failure modes

**A nav link is missing and I know the flag is on.** Check the profile. `GET /api/features`
returns `install_profile`, and a masked feature reports `false` there whatever the stored
flag says. That is the mask, working.

**Settings gives me a 409 when I enable an integration.** Also the mask. The message names
the profile. That integration belongs on the other instance.

**I switched the profile and a feature did not come back.** The mask only subtracts, so
switching to `demo` stops masking — but the feature's own `*_enabled` flag still has to be
on. Check both.

## What this folder does not cover

The capability docs at the [docs root](../README.md) are written once and shared: Cloud
VMs, Databases, Kubernetes, Image Management and the rest describe what the dashboard
does, and each one's
header block names the profile it applies to. This folder covers only what is *specific* to
running a demo estate or a customer POV.
