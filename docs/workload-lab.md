# Workload Lab

> **Audience:** operator · **Profile:** `demo` · **Read this when:** something that is not a person needs a credential, and you want to decide which of the four mechanisms fits before reading any one of them.

> **Preview.** Every tab here is behind a preview flag and off by default. Each guide
> carries its own statement of what has and has not been proven against a live
> authority — read that before you demo the tab, not after.

**One page, four tabs, at `/workload-lab`.** A certificate, a SPIFFE identity, a
Kubernetes ServiceAccount token and a short-lived cloud credential are four answers to the
same question: *how does a machine authenticate without a secret somebody typed in once
and nobody can revoke?* They share a page because the choice between them is usually made
in one sitting, and because it turns almost entirely on facts about the consumer rather
than on preference.

**There is no human in any of these workflows by design.** The consumer is a pipeline, a
broker or a cluster. That is what separates this page from Password Safe's own surfaces,
where the credential eventually reaches a person.

## The four tabs

| Tab | Guide | The credential | What governs it |
|---|---|---|---|
| **Certificates** | [Certificates](integrations/certificates.md) | an X.509 certificate from a private CA you stand up here | Password Safe |
| **SPIRE** | [SPIFFE and SPIRE](integrations/spiffe.md) | an SVID issued in a trust domain, to a workload that attested itself | Password Safe |
| **Kubernetes** | [Workload access to Kubernetes](integrations/workload-kubernetes.md) | a bound ServiceAccount token for a machine *outside* the cluster | Password Safe |
| **Cloud** | [Short-lived cloud credentials](integrations/workload-cloud.md) | an AWS or Azure credential minted per run and leased | Workload Credentials |

All four live under [`integrations/`](integrations/README.md), with the rest of the pages
about systems the dashboard talks to.

## Which one you actually want

The tabs are not ranked, but they are not interchangeable either. Two questions settle it:

**What is the consumer reaching?** A cluster API server wants **Kubernetes** or **SPIRE**;
an AWS or Azure API wants **Cloud**; anything that completes a TLS handshake — a
pipeline, an appliance, an mTLS service mesh — wants **Certificates**.

**Who runs the thing it is reaching?** This is the one that surprises people. The SPIFFE
route into a cluster needs `--authentication-config`, a kube-apiserver flag, so it reaches
a self-managed k3s and **none of EKS, AKS or GKE**. On a managed cluster the Kubernetes
tab is the only one of the two you can have. Both guides cross-link for this reason.

Where a workload can attest itself, **SPIRE is the stronger answer** and the other tabs
say so: nothing is held anywhere, so there is no vault read to authenticate. The rest of
the page exists because most workloads cannot.

## Every tab governs what it creates

This is the invariant of the whole feature, and it is worth stating on the hub because it
is the difference between a lab and a demo of somebody else's technology. A tab that
stands up an identity and hands you a list of values to paste has demonstrated SPIRE, or
ADCS, or STS — not BeyondTrust. So for each of the four:

* **something ISSUES the credential** — a write against an authority, not a read-only
  list of strings;
* **the row RECORDS what was issued**, which is what makes it a governance record rather
  than a button;
* **the identity can be REMOVED**, so nothing is created that cannot be cleaned up;
* and **no tab writes a credential onto its own row** — the dashboard is not the vault.

**The authority is not always Password Safe.** Three tabs vault their credential there;
the Cloud tab's is minted and held by **Workload Credentials**, which has its own issuance
audit, its own leases and its own per-issuance billing. Governed, by a different
BeyondTrust product. The invariant is "something governs it", not "Password Safe governs
it".

`tests/test_workload_lab_governance.py` pins all of this, and derives its roster by
listing `templates/workload_lab/` — so a fifth tab cannot ship until somebody records what
governs it.

One consequence of the Cloud tab's authority worth knowing before you demo it: on AWS a
lease **cannot be revoked** at all, so the TTL is the only control there is. Azure honours
the revoke. The service reports the difference rather than swallowing it.

## Turning it on

Settings owns **exactly two toggles** here, one per lab — Certificate Lab and SPIRE Lab.
The other two tabs have no preview switch of their own, deliberately: a third and fourth
row in Settings would offer an operator a control over something they cannot already
reach.

| Tab renders when | |
|---|---|
| Certificates | `cert_lab_enabled` |
| SPIRE | `spire_lab_enabled` |
| Kubernetes | `k8s_management_enabled` **and** `password_safe_enabled` |
| Cloud | `workload_credentials_enabled` |

**The page itself is gated on either lab flag.** `workload_lab_enabled` is derived —
`cert_lab_enabled OR spire_lab_enabled` — and it gates both the nav link and the route,
through the single reader in `services/feature_flags.py` so the two cannot drift into a
link that 404s.

That has one consequence that is deliberate rather than a gap: **with both lab flags off
the whole page 404s, so the Kubernetes and Cloud tabs are unreachable however their own
flags are set.** They are capabilities of the Workload Lab preview, not features that can
stand the page up on their own. Making them do so would mean putting non-preview flags
into the derived set, at which point the page stops resolving as all-preview and needs an
RBAC scope of its own.

The tab bar is suppressed when only one tab is on — a bar holding a single pill reads as a
rendering fault.

## Related

* [Workload Credentials](integrations/workload-credentials.md) — the product behind the
  Cloud tab, and the only authority here that is not Password Safe.
* [Password Safe](integrations/password-safe.md) — the authority behind the other three.
* [Kubernetes](kubernetes.md) — managing the clusters the Kubernetes tab acts on.
* [Permissions](permissions.md) — who may reach the page. It is all-preview today, so it
  carries no RBAC scope of its own.
* [Auto-delete Timer](auto-delete-timer.md) — the labs create real infrastructure, and the
  argument for reaping it is the same one the SPIRE guide makes.
