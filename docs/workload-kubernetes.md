# Workload access to Kubernetes, brokered by Password Safe

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a machine outside a Kubernetes cluster needs to reach its API server, and the cluster is managed (EKS/AKS/GKE) so the SPIFFE path cannot be configured at all.

**Workload Lab → Kubernetes.** A short-lived ServiceAccount token for a machine *outside*
the cluster — a CI build, a fleet scan — minted by the API server, rotated and audited by
Password Safe, retrieved once per run.

This is the companion to [SPIFFE and SPIRE](spiffe.md), not a replacement for it, and the
difference between them is the first thing to understand because it decides which one you
can actually have.

---

## The question this answers

**How does a machine outside the cluster authenticate to it?**

A pod already has a good answer, and every managed cloud builds on it: a projected
ServiceAccount token — short-lived, audience-bound, rotated by the kubelet. IRSA, GKE
Workload Identity and AKS Workload Identity are all that mechanism with a cloud-IAM
exchange bolted on.

A machine outside the cluster has none of it, and the options are worse than most people
expect:

| Option | What is wrong with it |
|---|---|
| A long-lived kubeconfig in the CI system | No expiry, no revocation, and **no record that anyone read it** |
| A client certificate | Kubernetes **cannot revoke certificates at all** — there is no CRL check |
| OIDC / `--authentication-config` | Unavailable on EKS, AKS and GKE. See [SPIFFE and SPIRE](spiffe.md) |
| A token Secret in the cluster | Since 1.24 Kubernetes no longer auto-creates them, and one created by hand never expires |

So "how does our build server talk to the cluster?" is a live question for most customers,
and the usual answer in the wild is the first row.

## What this does instead

Onboarding one identity does four things, in this order:

1. creates a **ServiceAccount** in the cluster and binds it (see the profiles below);
2. applies the **rotator RBAC**, so Password Safe's functional account can mint tokens —
   in Bound mode that is `serviceaccounts/token` **create** and no access to Secrets at all;
3. creates a Password Safe **managed system and account** on the "Kubernetes Service
   Account Token" plugin;
4. **rotates once**, which is what puts a credential that authenticates into the vault.

Step 4 is not optional and the page tells you when it has not happened. A bearer token is
800-1200 characters and Password Safe's create API caps a password at 128, so the account
is *always* created holding a placeholder. A row that stopped after step 3 looks onboarded
and serves something that authenticates to nothing; the Workload Lab marks it
**"placeholder — never rotated"**.

## The two profiles

**The profile is the demonstration, not the token.** A vaulted token is only interesting if
it is scoped, and neither of these is `cluster-admin`:

| Profile | Consumer | Binding | What it proves |
|---|---|---|---|
| **Deployer** | a CI build running a deploy | ClusterRole `edit` via a **RoleBinding** in one namespace | the canonical case — and it is **refused** in a second namespace |
| **Reader** | a fleet inventory, posture or CMDB scan | ClusterRole `view` via a **ClusterRoleBinding** | read-all that **cannot read Secrets** — `view` omits them upstream |

Both bind an **upstream default ClusterRole** rather than a Role written here, deliberately.
A hand-written Role would be this feature's opinion about what a CI build needs, and it
would have to be re-audited every time Kubernetes grew a resource type. `edit` and `view`
are what every cluster already agrees those words mean, and `view`'s omission of Secrets is
an upstream property rather than a claim this repo makes.

### Why not reuse the existing `ps-token` path?

The [Kubernetes](kubernetes.md#access--identity) page already onboards a ServiceAccount
token as a managed account, and it is the right tool for what it does: it serves a human's
brokered PRA session, so it uses a **cluster-admin** ServiceAccount. A *vaulted*
cluster-admin token is a vaulted skeleton key — it demonstrates nothing about scoping,
because there is no scope. It also has room for exactly one registration per cluster, and a
Deployer plus a Reader on one cluster is two identities.

One thing the Workload Lab deliberately does **not** do is call that path's `register()`.
Its seeding step applies a ClusterRoleBinding to `cluster-admin` in order to read a
"current token" as a seed — which is then discarded anyway, because of the 128-character
cap above. The seed costs a cluster-admin binding and buys nothing, so this skips it.

## Proving it works

Two consumer playbooks ship with this, and they are the part that actually proves
something:

* `examples/playbooks/k8s/ci-deploy-with-ps-token.yml`
* `examples/playbooks/k8s/ci-read-with-ps-token.yml`

They are the **first** Kubernetes plays in this repo that authenticate with something they
fetched themselves. The other four take the kubeconfig the dashboard injects and supply
nothing, which is convenient and is exactly the property these remove.

| # | Step | What it proves |
|---|---|---|
| 1 | Onboard a Deployer | the account exists, `ApiEnabled` is on, nothing has been retrieved yet |
| 2 | Run the deploy play | a program fetches the token and applies a Deployment |
| 3 | Point it at another namespace | **Forbidden** — the token is namespace-scoped |
| 4 | Onboard a Reader, run the read play | cluster-wide reads work; **reading a Secret is Forbidden** |
| 5 | Rotate, re-run | the consumer never notices; the audit trail shows the build's reason |
| 6 | Break the Secrets Safe permission | retrieval fails — **and a token already held keeps working until its TTL** |
| 7 | Delete and recreate the ServiceAccount | every token ever issued dies. The only hard kill switch |

**Steps 3, 4, 6 and 7 are the ones that prove something.** Steps 3 and 4 are written as
*asserted tasks inside the plays*, not as runbook steps, and that distinction is the point:
a step in a runbook gets skipped, and an assertion does not. If the refusals do not refuse,
the plays fail.

### Two traps the plays encode

**The injected kubeconfig has to be cleared.** When the dashboard runs a play against a
Kubernetes target it injects a cluster-admin kubeconfig as `K8S_AUTH_KUBECONFIG` and
`KUBECONFIG`. Left set, `kubernetes.core` can authenticate with *those* instead of the
retrieved token — every task passes, the refusals do not refuse, and the play reports a
successful demonstration of nothing at all. Both plays blank them in an `environment:`
block.

**`failed_when: false`, never `ignore_errors`.** The refusal tasks have to fail so the next
task can judge *why*. `ignore_errors` would also swallow an unreachable API server, and the
assertion would then pass on a connection error rather than on a 403. Each refusal
assertion checks both that the request failed **and** that it failed with a 403 — because a
wrong API server or an expired token also fails, and a check that only asserted "it failed"
would report a passing demonstration on a typo.

## Boundaries — state these plainly

**In Bound mode, rotation does not revoke.** A token already retrieved lives out its TTL
whatever Password Safe does next. So rotation is *hygiene* and deleting the ServiceAccount
is *containment* — every token the plugin has ever issued is bound to that account's `uid`
and dies with it. `AppSettings:Kubernetes:OldSecretRetentionMinutes` is tenant-side and
cannot be read from here, so "rotation revokes" is something to document and never to
assert (the same wording [the rotation design note](design/k8s-sa-token-rotation.md) already
uses). The tab says so on every Rotate.

**The vault authenticates whoever can retrieve, not the workload.** Anyone who can retrieve
*is* the workload, as far as this mechanism can tell. That is precisely the axis the SPIRE
path wins on and this one does not — there, the workload attests itself and the credential
rests nowhere. Both are on the same page for that reason.

**The credential chain bottoms out in a Password Safe OAuth client** living in the runner's
environment. That is the same uncomfortable property the External Secrets Operator path has
with a cluster Secret: something has to hold the first credential. This mechanism moves the
problem to a place with an audit trail and a rotation schedule; it does not make it vanish.

**600 seconds is the floor.** That is the TokenRequest API's own minimum, not a choice made
here — a cluster silently caps anything lower, so the address builder clamps to it.

## The auto-delete timer

A workload identity can carry an expiry, and the argument is the one
[SPIFFE and SPIRE](spiffe.md) already makes for a trust domain: there is no billable
resource here at all, so cost is not the reason. **A forgotten workload token keeps
authenticating**, and this row is the only page in the dashboard it appears on. The reap
deletes the ServiceAccount, which is the hard kill switch above.

It appears on **Inventory** as kind **Workload K8s Token**, alongside the
cluster it lives in — which has its own separate timer, because reaping the identity must
leave the cluster running.

## Compared against the SPIFFE path

The full table is in [SPIFFE and SPIRE](spiffe.md#what-it-is-being-compared-against). The
short version:

| | This (Bound SA token via Password Safe) | JWT-SVID over the Workload API |
|---|---|---|
| Where the credential rests | Password Safe | **Nowhere** — process memory, for minutes |
| Who can replay it | Anyone who can retrieve it | A caller on the attested workload's own machine |
| Works on EKS / AKS / GKE | **Yes** | No — needs a self-managed API server |
| Governed as an inventory row | Yes | No — there is no credential to govern |
| Scoped by | The profile's RoleBinding / ClusterRoleBinding | The same RBAC, keyed on the SPIFFE ID |

Neither row is the winner everywhere, which is why the Workload Lab shows both and the two
tabs link to each other.

## What is not built

* **No audience scoping.** Bound ServiceAccount tokens take an audience and nothing here
  sets one yet. Until it does, a token is accepted by anything that trusts the cluster's
  own issuer.
* **No Operator/incident profile.** Two profiles cover the two consumers worth
  demonstrating; a break-glass one is a different conversation about approval.
* **Nothing here replaces the standing cluster-admin kubeconfig the dashboard itself uses**
  for registered clusters ([Kubernetes](kubernetes.md)). That is the strongest engineering
  case for this mechanism and it deserves its own change — it alters how every existing
  cluster run authenticates.
