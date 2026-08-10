# Design: rotating the k8s ServiceAccount token with Password Safe

Why the k8s token-rotation feature is shaped the way it is. Four of these are things a
future reader cannot recover from the code, because they are facts about the Password
Safe plugin or about Kubernetes, not about this repo.

## The problem

A cluster's PRA k8s tunnel can inject a ServiceAccount bearer token at session launch
(`POST /clusters/{id}/tunnel` with `vault_inject`). The dashboard mints that token once,
stores it as a PRA Vault `opaque_token` account, and never touches it again. There was no
rotation, refresh or re-vault path anywhere — and on GKE the mint falls back to
`kubectl create token --duration=24h`, so the vaulted credential silently stopped working
after a day with no expiry record and no way to refresh short of tearing the tunnel down.

Two Password Safe custom plugins close the gap:

| Plugin | Does |
|---|---|
| **Kubernetes Service Account Token** | Verifies and rotates k8s SA tokens on EKS, AKS, GKE and generic/on-prem clusters |
| **PRA Vault Token** | Writes whatever value Password Safe supplies into a PRA Vault `opaque_token` account (`PATCH /api/config/v1/vault/account/{id}`) |

So: the token becomes a managed account Password Safe rotates, and a second managed
account mirrors each rotation into the PRA Vault copy. `services/ps_k8s_token_service.py`
owns registration; `services/k8s_token_sync.py` owns the mirroring.

## 1. The rotation sweep is label-scoped — which inverts the obvious risk

The plugin's rotation creates a new token Secret, stores it, then deletes the Secrets it
snapshotted at the start. That snapshot comes from a **label selector**:

```
beyondtrust.com/managed-by=password-safe,beyondtrust.com/service-account=<sa>
```

The dashboard's `<sa>-token` Secret (`k8s_service._entitle_k8s_rbac_manifest`) carries the
`kubernetes.io/service-account.name` **annotation** and no labels, so it is never in that
set.

The intuitive worry — "rotation will delete the dashboard's Secret and break PRA" — is
therefore **wrong**. The real consequence is quieter and worse: co-existence is stable, so
rotation *stops revoking anything*. `<sa>-token` becomes a permanent, unrotatable,
unaudited **cluster-admin** bearer token, and the single property that makes LongLived
mode worth choosing is defeated by a Secret the dashboard left behind.

Hence registration deletes it — and only after the PRA Vault account carries a
Password-Safe-issued token, because until then that Secret holds the credential live
brokered sessions are using. Gated by `k8s_ps_token_delete_legacy_secret` (default on);
turning it off leaves a working system with a named residual risk, which the job result
says out loud.

One more consequence: `AppSettings:Kubernetes:OldSecretRetentionMinutes > 0` disables the
sweep entirely. That is a tenant-side plugin setting the dashboard cannot read, so
"rotation revokes" is something to document, never to assert.

## 2. Every token is bound to the ServiceAccount's uid

The plugin asserts the new token's `uid` claim against the live ServiceAccount, so
**deleting and recreating the SA invalidates every token Password Safe has ever issued**
and leaves the managed account pointing at an object that no longer exists.

`deregister_pra_tunnel` deletes the whole `pra-access` namespace to revoke the injected
token. That is right when the dashboard owns the token and wrong when Password Safe does,
so it now skips the revoke while `ps_token_account_id` is set and logs why. Retiring a
managed ServiceAccount goes through `DELETE /clusters/{id}/ps-token`, which off-boards the
managed systems first.

The same function also clears the sync watermark. Without that, re-provisioning a tunnel
mints a fresh token and a fresh PRA Vault account while the stale watermark suppresses the
first sync — PRA holding one token, Password Safe another, and nothing reconciling them.

## 3. Seed *and* rotate at registration

The Terraform provider requires a password on a managed account, and
`ps_resource_service` supplies `secrets.token_urlsafe(24)` — a placeholder. For an SSH-key
account that is harmless; here the "password" *is* the credential, so a placeholder means
Password Safe holds a value that authenticates to nothing.

Registration therefore does both:

1. **seed** the account with the token the cluster is using right now
   (`initial_password`), so Password Safe, the cluster and PRA agree from the first
   moment; and
2. **rotate once** (`k8s_ps_token_change_on_register`, default on), which exercises the
   whole path — functional-account credentials → cloud control plane → API server → RBAC →
   Secret create — at registration time instead of at 3am on the first scheduled rotation.
   Same reasoning as `passwordsafe_azure_change_password_on_register`.

Mirroring the rotated token into PRA is the one **fatal** step. Completing the job there
would ship a token that is managed but not mirrored — precisely the hole the feature
exists to close.

## 4. The sync is a watermark reconciler, not an event pipeline

Password Safe owns the schedule and offers no webhook, so the dashboard polls
`LastChangeDate` (cheap — no credential request, no checkout) and pushes when it moves.
Making the persisted state a description of *what PRA is known to hold* rather than a log
of what happened is what makes missed rotations, lost state, double rotations and a
rotation landing mid-sync all fall out correctly with no queue.

**Two details are load-bearing:**

- **The date is compared as an opaque string.** Tenants emit both `…123Z` and `…+00:00`; a
  parse that fails either never fires (nothing ever syncs) or fires every pass (a checkout
  and a credential write every interval, forever).
- **The recorded watermark is the date read *before* the checkout.** If Password Safe
  rotates between the read and the push, we push token *A* while recording date *A* — the
  next pass sees the newer date, re-pushes, and self-corrects at the cost of one wasted
  checkout. Recording a post-checkout re-read would store date *B* having pushed token
  *A*: a silent, permanent desync, because every later pass then agrees nothing changed.

Verification is free. Password Safe is the only party that knows whether the plugin's
PATCH ran, and its change date on the *target* account is the receipt — so the next pass's
read of that account verifies the previous push with no extra call. A fresh push records
`pending` (Password Safe queues change operations, so "accepted, not yet reflected" is
normal); a target date that has not moved by the next pass becomes `no` and charges
backoff.

A failure never advances the watermark. That is the difference between "retries next pass"
and "silently stale forever".

## 5. The rotate-on-release loop

If the token account's access policy enables **Change Password After Release** — or if
anything ever calls Password Safe's rotate-on-check-in endpoint — every sync triggers
another rotation: an endless rotate → sync → rotate loop with a real cluster rotation and
a dead-credential window every pass. Three defences, all present:

1. `_checkin` issues plain `Checkin` only, and a static test asserts the rotate-on-release
   endpoint is named nowhere in the feature;
2. a per-cluster circuit breaker (`k8s_token_sync_max_per_hour`, default 4) trips and names
   the access policy as the likely cause;
3. it is documented as an operator prerequisite.

## 6. The break window is real and cannot be closed here

In LongLived mode Password Safe revokes the old token as part of the rotation, before
anything can observe it. So PRA holds a dead credential for up to one sync interval:

- expected ≈ `interval / 2` (≈ 7.5 min at the default 15)
- worst ≈ `interval + pass duration` (≈ 16 min)

The dashboard can shorten that window, not remove it. **`;bound` is the only lever that
changes the kind of failure rather than its duration** — Bound mode never revokes, so the
previous token stays valid until its TTL and the window is zero as long as the interval is
well under the TTL. Recommend it for any cluster whose tunnel is used interactively; keep
LongLived where a brief outage is acceptable or rotation is scheduled off-hours.

## 7. Why the rotator RBAC subject comes from config

The plugin's functional account is a **cloud IAM identity**, and the in-cluster
ClusterRoleBinding subject is that identity as Kubernetes sees it. The dashboard only knows
the functional account's *name* — its credentials live in Password Safe — and the mapping
is mostly not derivable:

| Cloud | Subject | Derivable? |
|---|---|---|
| GKE | the service account's email | **Yes** — it is the functional account's own name |
| AKS | the service principal's **object id** (`oid`) | No — a different GUID from the client id in the username; needs a Graph lookup |
| EKS | the username the access entry maps the principal to | No — and the principal ARN cannot be recovered from an access key id without the secret |
| Generic | a bootstrap ServiceAccount | Partly — the dashboard creates it |

So each cloud names its own config key and a missing one is reported by name. A wrong
subject fails as an opaque 401/403 at the first rotation, not at bind time, which is why
the ClusterRole is applied unconditionally but the binding only when a subject is known —
Password Safe's own **Verify Functional Account** then names every missing verb and prints
the ClusterRole to apply, and logs the AKS object id on every run.

For EKS the dashboard also creates the access entry when
`k8s_ps_rotator_eks_principal_arn` is set. Without it the binding's `User` subject matches
nothing and the API server 401s — invisible from inside the cluster. It never edits the
`aws-auth` ConfigMap: a bad edit there can lock every principal out.

## 8. Smaller decisions worth recording

- **The credential is checked out over REST (`ps_api_service`), not `ps-cli`.** The poll
  needs one signed-in session for every cluster; `btapi_service` spawns a subprocess with
  its own OAuth handshake per call. It also keeps the feature working in an image with no
  `ps-cli` binary, and is why the sync pass qualifies for the LIGHT worker tier.
- **The plaintext stays inside one function.** `rotate_pra_vault_token` checks out, pushes
  and checks in in a single frame and returns a 12-hex digest — never the value. Job
  metadata is served by the jobs API and the MCP job tool, so a credential that escapes
  the module escapes to a lot of places. `checkout_credential` is the one documented
  exception: the PRA Vault account is Terraform-provisioned, so the tunnel path must hold
  the value long enough to pass it as a sensitive `TF_VAR`.
- **A checked-out value is shape-guarded before any write.** Password Safe can return a
  soft-failure *string* in the credential position; mirroring that into PRA's vault would
  break the tunnel while reporting success. A ServiceAccount token is a JWT in both modes,
  so three dot-separated segments is a sufficient and cheap check.
- **The PRA target is bound by numeric `ManagedAccountID`, never by name**, and a resolved
  account on the wrong platform fails closed. PRA does not enforce unique vault account
  names, and writing a Kubernetes bearer token into some other plugin's account is the one
  failure here that puts a secret somewhere it does not belong.
- **The sync must never go through Terraform.** `terraform_pra_service._scrub_tf_state`
  redacts `token` fail-closed, so an apply over the stored tunnel state would push the
  redaction sentinel into PRA.
- **OKE and on-prem share the generic `k8s;` path.** The plugin has no OCI provider, so a
  fourth cloud branch would buy nothing.

## Operator prerequisites the dashboard cannot automate

- Import both `.psplugin` packages and create their platforms.
- Create the per-cloud functional accounts (the dashboard references them by name and
  never holds a cloud secret).
- Grant the API identity **Requestor** plus an access policy granting **View** on a Smart
  Rule containing both managed accounts. There is no Smart Rule API — in this repo or in
  Password Safe's public API — so this is out-of-band, and it is the failure every
  Password Safe consumption path here has hit first.
- Leave **Change Password After Release** off on the token account (see §5).
- Give the Password Safe host or Resource Broker network reachability to the cluster's API
  server. For private EKS/AKS/GKE clusters that is a real problem, and the PRA Gateway
  cannot help — Password Safe does not route through it.
