# Short-lived cloud credentials for a workload

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a workload needs AWS or Azure access and today it has a long-lived access key in a CI secret store.

**Workload Lab → Cloud.** A short-lived AWS or Azure credential, minted on demand by
BeyondTrust **Workload Credentials** against a dynamic secret and handed out as a lease.

This is the tab aimed at the most common non-human credential there is.

---

## The credential this replaces

Ask how a build server reaches AWS and the answer is almost always an access key pasted into
a CI secret store. It has three properties nobody chose:

| | |
|---|---|
| **No expiry** | it works until somebody remembers to rotate it |
| **No revocation in practice** | rotating means finding every consumer first, so it does not happen |
| **No record of retrieval** | the secret store logs who *edited* it, not which build *read* it |

The other three Workload Lab tabs replace a certificate, a SPIFFE identity and a Kubernetes
token. This one replaces that key, and the mechanism is the same one the dashboard already
uses for its own cloud calls — so unlike the SPIRE path, the engine here is
**live-validated** (confirmed against a real site, 2026-08-21, with the recorded payload in
`tests/test_workload_credentials.py`).

## What it does

Registering an identity here records that **this workload draws from that dynamic
secret**. (It is not the same object as a **Workload Identity** registered in Pathfinder,
which is an OIDC trust — see Boundaries.) It mints nothing: issuance is metered, so the first credential appears when
somebody presses **Issue** or a consumer asks Workload Credentials directly.

A mint returns a lease — a credential plus an id and an expiry:

| Cloud | What comes back | Revocable? |
|---|---|---|
| **AWS** | `access_key_id`, `secret_access_key`, `session_token` (an assumed-role triple) | **No** |
| **Azure** | `client_id`, `client_secret`, `tenant_id` (service-principal credentials) | **Yes** |

## The scope is not set in the dashboard

**This is the honest difference from the sibling tabs and it is worth understanding before a
demonstration.** The Kubernetes tab chooses a RoleBinding and the Certificate tab chooses a
profile, so the dashboard decides what the identity may do. Here the **dynamic secret's own
definition in Workload Credentials** decides — which role is assumed, which subscription,
which permissions. The dashboard names the secret and cannot widen or narrow it.

That means the interesting scoping conversation happens in WC, not here, and a narrow dynamic
secret is what makes this demonstration worth anything. A dynamic secret that assumes an
administrator role produces a short-lived skeleton key, which is the same mistake as a vaulted
`cluster-admin` token.

## Revocation is asymmetric — say this out loud

**Azure leases can be released early. AWS leases cannot.** STS will not withdraw a credential
it has already signed, so `revoke` is refused with `lease_not_revocable`.

The consequence is the opposite of what people assume: **on AWS the TTL is the only control
there is, which makes a short one matter more, not less.** The dashboard refuses an AWS revoke
at the click rather than running a job that would appear to succeed — the underlying client
swallows the provider's refusal, so a success there would be a lie about a live credential.

Deleting an identity is not a revoke either. What it reliably stops is **minting**: the
identity can no longer draw another credential, which is the thing that would otherwise
continue indefinitely and keep billing. An outstanding AWS lease lives out its TTL regardless.

## Issuance is metered

Workload Credentials bills per issuance. Three consequences the design follows from:

- **Registering mints nothing.** It is inert until someone asks for a credential.
- **The issue count is on the row.** It is a cost figure as much as an audit one, and a number
  climbing on an identity nobody is using is the signature of a consumer stuck in a retry loop.
- **The job is never retried speculatively.** If the count ever climbs faster than the button
  is pressed, the job retry path is the first place to look.

This is also why the lab does **not** reuse the dashboard's own credential store. That store
(`workload_credential_lease`) is a singleton per `(cloud, purpose)` holding the credential
*this application* uses, wrapped in a memo, a lock and a billing backoff. Minting into it from
the lab would overwrite a credential the dashboard may be mid-deployment with — and bill for
doing so.

## Proving it works

One consumer play ships with this: `examples/playbooks/cloud/ci-run-with-dynamic-creds.yml`.

| # | Step | What it proves |
|---|---|---|
| 1 | Register an identity | it exists, nothing minted, nothing billed |
| 2 | Issue | a credential exists that did not a minute ago, with a lease and an expiry |
| 3 | Run the play | a program authenticates with it; no long-lived key on the host |
| 4 | Re-run with `wait_for_expiry=true` | **refused** — nothing revoked, nobody rotated |
| 5 | Azure: Revoke, then re-run | **refused immediately** — the one path here with a kill switch |
| 6 | AWS: attempt Revoke | **refused by the dashboard** — `lease_not_revocable`. The honest limit |
| 7 | Delete the identity, ask for another | minting stops. The outstanding lease still lives out its TTL |

**Steps 4, 5, 6 and 7 are the ones that prove something.** Step 4 is written as an *asserted
task inside the play*, and the wait is real rather than simulated — a play that faked the
clock would prove it can print a failure message, not that the credential died.

### Two traps the play encodes

**The runner's own cloud credentials are blanked.** A dashboard runner may already carry
`AWS_ACCESS_KEY_ID` in its environment — that is how every other cloud play here
authenticates. Left set, the CLI falls back to them: every task passes, the expiry assertion
never fires, and the play reports a successful demonstration of a mechanism it never used.

**`failed_when: false`, never `ignore_errors`.** The post-expiry call has to fail so the next
task can judge *why*. `ignore_errors` would also swallow a missing CLI or a broken proxy, and
the assertion would pass on the wrong error. The assertion checks both that the call failed
**and** that the message names an expiry or an auth failure.

## Boundaries

**Workload Credentials authenticates whoever can mint, not the workload.** Anyone holding the
token that mints *is* the workload, as far as this mechanism can tell. That is the same
limitation the Kubernetes tab has with its Password Safe client, and precisely the axis the
[SPIRE](spiffe.md) path wins on — there the workload attests itself and nothing is held at all.

**The chain still bottoms out somewhere — but no longer here.** For the *consumer* being
demonstrated it is a Workload Credentials token in that workload's environment: this mechanism
moves the problem to something short-lived, audited and metered, it does not make it vanish.
For the **dashboard's own** calls it can now bottom out in nothing at all, because Pathfinder
will trust a registered **Workload Identity** — see
[Cloud hosting → No PAT](cloud-hosting.md#no-pat-authenticate-to-pathfinder-with-an-entra-workload-identity).
That is worth saying out loud in a demonstration: the same move is available to the consumer,
and what closes the gap is a registration in Pathfinder rather than a better secret store.

**AWS caps a role-chained credential at one hour.** A longer TTL request comes back clamped, so
the page shows the provider's own expiry rather than what was asked for.

## The timer

An identity can carry an auto-delete expiry, and it appears on **Inventory** as kind
**Workload Cloud Credential**. The argument differs from the sibling tabs' and is sharper
*because* the credentials are short-lived: reaping does not stop a live lease — on AWS nothing
can — so what the reap ends is the identity's **ability to mint another**. An identity left
behind keeps drawing a fresh credential every time its consumer asks, each issuance billed,
indefinitely.

## What is not built

- **Federated trust for the demonstrated workload.** WC *does* accept an OIDC trust
  relationship: a **Workload Identity** registered in Pathfinder (GitHub Actions, Azure Entra
  ID, or a Custom IDP with explicit claim conditions) lets a workload present an attested
  token — a GitHub Actions OIDC token, an Entra identity token, in principle a SPIFFE
  JWT-SVID — and hold nothing. The dashboard now authenticates that way for its **own** calls.
  What is not built is doing it *for the identity this tab registers*: the row still names a
  dynamic secret, and a consumer that wants credentials with nothing held needs its own
  registration, made by hand in Pathfinder. Registration is a GUI action with no customer API,
  so this tab cannot create one on an operator's behalf — it could only tell them what to
  type.
- **Per-identity TTL enforcement.** The request is passed through and the provider decides.
- **Reading a credential back.** No endpoint returns one, by design — see the boundaries above.
