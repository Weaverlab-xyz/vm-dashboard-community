# Demo and POV profiles

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding which kind of instance to install, or you cannot find a feature the docs say exists.

Every install picks one of two purposes on the setup wizard's **Purpose** step, and the
choice is a gate rather than a preference. Almost everything else in `docs/` describes a
capability; this folder describes which of the two instances that capability belongs to.

| | I am… | Start here |
|---|---|---|
| **demo** | running my own estate — deploying cloud VMs, building images, standing up clusters and databases, showing them to people | [Demo profile](demo/README.md) |
| **pov** | running customer proof-of-value environments, each against the customer's own BeyondTrust tenant | [A POV Instance](../pov-instance.md) |

If you are not sure, you are on `demo`: it is the default, and what every existing install
already is.

## The gate

`install_profile` is `demo` or `pov`, and the two are **mutually exclusive**. The exclusion
is enforced in code rather than left to discipline, because of one thing: **tenancy.** A
demo instance resolves its BeyondTrust tenant from the global singletons. A POV instance
holds a *registry* of many named tenants, because several POVs run at once and each has its
own PRA appliance and Password Safe Cloud tenant. An instance claiming both roles would
have two answers to "which tenant?" at every call site — and the wrong answer is silent,
not loud: a demo VM deploy onboarding into a customer's Password Safe, or a POV onboarding
into your demo tenant. Nothing errors, both paths "work".

Two properties worth knowing:

- **The mask only ever subtracts.** A profile can refuse a feature. It can never turn on one
  that config left off.
- **An unrecognised profile resolves to `demo`.** The profile is read on the request path, so
  a typo in one config row must not take the app down — and falling back to `demo` means the
  worst case is *today's* behaviour.

For the full mechanism, the per-feature matrix and what a POV instance does instead, see
[A POV Instance](../pov-instance.md).

## What this folder does not cover

The capability docs at the docs root are written once and shared: Cloud VMs, Databases,
Kubernetes, Image Management and the rest describe what the dashboard does, and each one's
header block names the profile it applies to. This folder covers only what is *specific* to
running a demo estate or a customer POV.
