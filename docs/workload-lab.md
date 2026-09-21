# Workload Lab

> **Audience:** operator · **Profile:** `demo` · **Read this when:** something that is not a person needs a credential, and you want to decide which of the four mechanisms fits before reading any one of them — or you want to see something actually hold one.

> **Preview.** Every tab here is behind a preview flag and off by default. Each guide
> carries its own statement of what has and has not been proven against a live
> authority — read that before you demo the tab, not after.

**One page, five tabs, at `/workload-lab`.** A certificate, a SPIFFE identity, a
Kubernetes ServiceAccount token and a short-lived cloud credential are four answers to the
same question: *how does a machine authenticate without a secret somebody typed in once
and nobody can revoke?* They share a page because the choice between them is usually made
in one sitting, and because it turns almost entirely on facts about the consumer rather
than on preference.

**The fifth tab is not a fifth answer. It is a consumer.** The Agent tab puts a non-human
principal on a host, attested by the SPIRE tab's trust domain and authorized by a token
that expires — and it can be made answerable for the Cloud tab's lease or ask for the
Kubernetes tab's token. Until something holds one of these credentials, the other four
tabs are a description of a mechanism; the Agent tab is where the mechanism gets stopped
in front of somebody.

**There is no human in any of these workflows by design.** The consumer is a pipeline, a
broker or a cluster. That is what separates this page from Password Safe's own surfaces,
where the credential eventually reaches a person.

## The five tabs

| Tab | Guide | The credential | What governs it |
|---|---|---|---|
| **Certificates** | [Certificates](workload-lab/certificates.md) | an X.509 certificate from a private CA you stand up here | Password Safe |
| **SPIRE** | [SPIFFE and SPIRE](workload-lab/spiffe.md) | an SVID issued in a trust domain, to a workload that attested itself | Password Safe |
| **Kubernetes** | [Workload access to Kubernetes](workload-lab/kubernetes.md) | a bound ServiceAccount token for a machine *outside* the cluster | Password Safe |
| **Cloud** | [Short-lived cloud credentials](workload-lab/cloud.md) | an AWS or Azure credential minted per run and leased | Workload Credentials |
| **Agent** | [Agent Demo Cell](profiles/demo/agent-demo-cell.md) | a Personal Access Token against a deliberately narrow user — held by a worker, not a person | this dashboard's own token store |

The first four are the way in, and the fifth is what you point at them. Two of the four
carry more than one page, because two of them carry an argument that is not the tab's own:
a certificate authority has a whole lifecycle before any identity exists, and the Cloud
tab's authority is a product in its own right.

| Also under `workload-lab/` | Read this when |
|---|---|
| [The Certificate Lab](workload-lab/certificate-lab.md) | you are standing a private CA up and tearing it down — the prerequisites, the build form, the timer, and the AD CS variant for a CA you already run. |
| [Onboarding a subordinate CA](workload-lab/subordinate-ca.md) | the thing that will hold the credential mints its own certificates beneath it, so the question is rotation rather than delivery. |
| [BeyondTrust Workload Credentials](workload-lab/workload-credentials.md) | the product behind the Cloud tab — what it is, how the dashboard authenticates to it, and how to empty the database into it. |
| [Dynamic AWS and Azure credentials](workload-lab/dynamic-credentials.md) | you are moving a cloud off its standing access key, and need the lease behaviour, the IAM chain and what an issuance costs. |
| [What consumes these credentials](workload-lab/consumers.md) | somebody asks what actually spends what the lab issues. |

**All nine pages live in `docs/workload-lab/`**, so the feature is browsable without coming
through this page first. That was not true before: the guides sat among twenty-odd
unrelated pages under `integrations/`, where nothing marked them as a set.

## Which one you actually want

The tabs are not ranked, but they are not interchangeable either. Two questions settle it:

This is a question about the first four tabs. The Agent tab is not one of the options —
it is the thing doing the reaching, and which of the four it can be pointed at follows
from the same two answers.

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
BeyondTrust product. The Agent tab's is this dashboard itself — it issues a Personal
Access Token through the same hashing `api/tokens` uses, records the id, the name and the
expiry, and revoking is a column write. The invariant is "something governs it", not
"Password Safe governs it".

**And the Agent tab is held to it like the rest**, which is how it earned a tab rather
than a page: being the page's one consumer does not exempt it from naming what issues,
records and revokes the authorization its principal holds.

`tests/test_workload_lab_governance.py` pins all of this, and derives its roster by
listing `templates/workload_lab/` — so a sixth tab cannot ship until somebody records what
governs it. That is how the Agent tab arrived.

One consequence of the Cloud tab's authority worth knowing before you demo it: on AWS a
lease **cannot be revoked** at all, so the TTL is the only control there is. Azure honours
the revoke. The service reports the difference rather than swallowing it.

## Something spends each of them, and it is not hypothetical

The mirror of the invariant above, and it needs stating because the record was wrong about
it for a while. A design note argued that the lab issued four credentials and that nothing
consumed them — calling the candidates this page names, *"a pipeline, a broker or a
cluster"*, hypothetical. That was an under-reading of what had already shipped, and it has
been corrected three times since.

Each tab now has a file that spends its credential: two certificate plays that fetch both
halves and present the result to an endpoint which checks the name, two Kubernetes plays
that are the first in this repo to authenticate with something they fetched themselves, a
cloud play that uses a lease and then asserts the lease died, and a worker that reaches
Password Safe holding nothing at all.

[What consumes these credentials](workload-lab/consumers.md) is the register — which file,
what it has to hold in order to retrieve, and the cases where the answer is still *nothing
does*. It is deliberately honest in both directions: the SVID does not authenticate to
`/mcp`, the Cloud tab's credential is returned to nobody so no worker can spend it, and no
subordinate CA has ever been uploaded to a live PRA.

## Turning it on

Settings owns **three toggles** here — Certificate Lab, SPIRE Lab and Agent Demo Cell,
one per thing that is its own feature. The Kubernetes and Cloud tabs have no preview
switch of their own, deliberately: a row in Settings for each would offer an operator a
control over something they cannot already reach.

| Tab renders when | |
|---|---|
| Certificates | `cert_lab_enabled` |
| SPIRE | `spire_lab_enabled` |
| Kubernetes | `k8s_management_enabled` **and** `password_safe_enabled` |
| Cloud | `workload_credentials_enabled` |
| Agent | `agentcell_enabled` |

**The page itself is gated on any of those three.** `workload_lab_enabled` is derived —
`cert_lab_enabled OR spire_lab_enabled OR agentcell_enabled` — and it gates both the nav
link and the route, through the single reader in `services/feature_flags.py` so the two
cannot drift into a link that 404s. A flag with a Settings toggle of its own belongs in
that set, precisely so turning it on cannot leave an operator with a switch that reaches
nothing.

That has one consequence that is deliberate rather than a gap: **with all three off the
whole page 404s, so the Kubernetes and Cloud tabs are unreachable however their own flags
are set.** They are capabilities of the Workload Lab preview, not features that can
stand the page up on their own. Making them do so would mean putting non-preview flags
into the derived set, at which point the page stops resolving as all-preview and needs an
RBAC scope of its own.

The tab bar is suppressed when only one tab is on — a bar holding a single pill reads as a
rendering fault.

## Related

* [Password Safe](integrations/password-safe.md) — the authority behind three of the five
  tabs. The Cloud tab's is [Workload Credentials](workload-lab/workload-credentials.md),
  and the Agent tab's is this dashboard itself.
* [Agent Demo Cell](profiles/demo/agent-demo-cell.md) — the feature guide for the fifth
  tab: the non-human principal this lab was aligned to, and the consumer that holds
  nothing.
* [Kubernetes](kubernetes.md) — managing the clusters the Kubernetes tab acts on.
* [Permissions](permissions.md) — who may reach the page. It is all-preview today, so it
  carries no RBAC scope of its own.
* [Auto-delete Timer](auto-delete-timer.md) — the labs create real infrastructure, and the
  argument for reaping it is the same one the SPIRE guide makes.
