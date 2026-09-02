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

So: the token becomes a managed account Password Safe rotates, and a second managed account
carries the value into PRA. Keeping the two in step is **Password Safe's job, not the
dashboard's** — registration syncs them and stops there (§4).
`services/ps_k8s_token_service.py` owns the whole feature; there is no second module and
nothing runs on a timer.

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

## 3. The rotation at registration is mandatory, because the seed cannot be taken

The Terraform provider requires a password on a managed account, and
`ps_resource_service` supplies `secrets.token_urlsafe(24)` — a placeholder. For an SSH-key
account that is harmless; here the "password" *is* the credential, so a placeholder means
Password Safe holds a value that authenticates to nothing.

The obvious fix is to **seed** the account with the token the cluster is using right now
(`initial_password`), so Password Safe, the cluster and PRA agree from the first moment.
**That is not possible for this credential.** The public REST create-managed-account path
the provider uses caps `Password` at **128 characters**, and a ServiceAccount bearer token
is a JWT of 800–1,200. Seeding one does not truncate — it fails the whole apply with
`400 "Password cannot exceed 128 characters."`, taking the managed system with it.

The cap belongs to that path alone. A plugin's rotation write-back
(`ManagedAccount_CredentialsNew_Password`) carries multi-KB values — it is how the SSH-key
plugins store 3.2 KB PEMs — so the token is perfectly *storable*, just not *seedable*.
Two different code paths into the same field, and the limits are inconsistent between
them and between client and server (`ps-cli` caps functional-account passwords at 1,000
while the API accepts 3,216). Do not infer a limit on one path from a limit on another.

So `register_managed_system` still *offers* the seed, drops it when it exceeds
`_MAX_SEED_PASSWORD_LEN`, and reports which happened as `initial_password_seeded`. For a
k8s token that is always False, which makes **rotate once** the step that puts a real
credential in the vault rather than a nicety:

- it exercises the whole path — functional-account credentials → cloud control plane →
  API server → RBAC → Secret create — at registration time instead of at 3am on the first
  scheduled rotation (same reasoning as `passwordsafe_azure_change_password_on_register`);
- and because the seed was dropped, it is *load-bearing*. `k8s_ps_token_change_on_register`
  (default on) is therefore overridden when `initial_password_seeded` is False, with a
  warning on the job. Honouring it would leave a registration that reads as complete while
  `current_token` serves the placeholder to the PRA tunnel — a credential that is wrong
  rather than missing, which is the failure mode this feature is least able to detect.

The PRA Vault subscriber is offered the same seed and drops it the same way; the
`SyncedAccounts` link is created *before* the rotation, so that one rotation populates both.

Creating the **sync link is the one fatal step**, and it happens *before* the rotation.
Completing the job with an unlinked pair would ship a token that is managed but never
delivered — precisely the hole the feature exists to close. Ordering it first also means a
failure there has changed nothing in the cluster, whereas rotating first and then failing
to link would leave PRA holding a value that was just revoked and that nothing will refresh.

**The cost of that ordering is a recoverable half-state, and recovering it is the register's
job.** `ps_token_account_id` is committed at step 2, when the managed system is created —
two steps before the link. So a fatal failure at the link leaves the column set, both
managed accounts created, and nothing syncing: a row that reads as registered beside a pair
that delivers nothing. Re-running the register therefore **reconciles the link** rather than
returning early on the column (`_reconcile_synced_link`): it reads `SyncedAccounts` and
re-POSTs the link only when the pair is genuinely unlinked. Read-then-link, because the read
needs only *Read* and a POST against a live link is not documented as idempotent; the
direction and the confirm re-read are the same as step 4's, since a swapped pair links
happily and syncs backwards. Returning early instead would re-apply the RBAC and report
success on a registration that still never reaches PRA, leaving deregister-then-register —
two managed systems destroyed and rebuilt — as the way to restore one missing reference.

**The link is not the only thing that half-state leaves broken.** Because the seed is always
dropped (§3), a run that died at the link also left the account holding the placeholder it
was *created* with, and `current_token` hands whatever is in the vault to the PRA tunnel. So
the re-register also rotates when the stored state shows neither a seed nor a completed
rotation (`seeded` / `rotated` in `ps_k8s_token_<id>`) — the test is "does the vault hold a
real credential", which is `seeded OR rotated`, not `rotated` alone: a short credential that
was genuinely seeded and deliberately left unrotated must survive a re-register untouched.
Repairing only the link would fix the plumbing around a credential that authenticates to
nothing, and the resulting registration would read as healthy.

This matters because the likeliest cause of that failure is a **403 on the link**, and the
grant is genuinely unsettled (see the prerequisites below): the operator fixes it
tenant-side and then wants a retry. The cluster is untouched throughout, so the retry is
cheap and safe. The k8s panel surfaces it as **Repair sync**, shown only while Password
Safe reports the pair unlinked — the register form itself is hidden once the row looks
registered, which is what made the half-state unreachable from the UI.

## 4. Password Safe owns the sync — the dashboard was rebuilding a primitive it already has

`POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}` makes one managed account a
**subscriber** of another, and a managed account and its subscribers always share an
identical credential. Registration calls it once, with the ServiceAccount token account as
`{id}` and the PRA Vault Token account as `{syncedAccountID}`. Every rotation from then on
is applied to the subscriber too, which runs the PRA Vault plugin's PATCH into PRA.

**This replaced a watermark reconciler, and the replacement is worth recording because the
original was a reasonable-looking mistake.** The first implementation polled
`LastChangeDate` every 15 minutes, checked the token out and wrote it onto the target
account itself. It worked, and everything it contained was there for a real reason — but
every one of those reasons was created by the decision to poll:

| The reconciler needed | Because |
|---|---|
| a per-cluster watermark, recorded pre-checkout | a rotation landing mid-pass would otherwise desync permanently |
| opaque-string date comparison | tenants emit both `…123Z` and `…+00:00` |
| exponential backoff + parking after N failures | a 403 from a missing Smart Rule never fixes itself |
| a `pending`/`yes`/`no` verification state machine | Password Safe queues change operations, so a push is not a write |
| an hourly circuit breaker | "Change Password After Release" turned every sync into a rotation |
| a singleton job type + an advisory lock + a recency guard | two passes are two checkouts and two writes of possibly different values |
| six tuning settings and a Settings panel | none of the above has a safe universal default |

All of it is gone. Not simplified — *deleted*, because the failure modes it managed only
existed while the dashboard was in the data path. The lesson generalises: before building a
reconciler against a product API, check whether the product models the relationship
directly.

Two things survive from that design, both cheap:

- **The link is confirmed by re-reading the subscriber list.** A 200 on a POST that did not
  take is indistinguishable from success at the call site, and the read costs one GET with
  read-only permission.
- **The status shown to operators is read live, never cached.** The dashboard is no longer
  a party to the sync, so anything it stored would describe registration time — and "an
  admin unlinked it in the Password Safe console" is exactly what that panel exists to
  surface.

**Why this works with two custom plugins, which the documentation does not spell out.**
The published behaviour is written for ordinary password accounts, where Password Safe
generates a policy-conformant password and pushes it outward. Here the parent *mints* a
JWT rather than accepting a generated password, so the obvious worry is that the sync
would carry a policy-generated string to the subscriber and write that into PRA — a
failure that would report success at every step while leaving PRA holding garbage.

It does not, and the reason is the mechanism rather than luck: **the sync copies whatever
is stored as the parent's password**, and the parent's plugin is what decides what gets
stored. The "Kubernetes Service Account Token" plugin ignores the password policy — it is
not minting a password — and reports the JWT it obtained from the cluster, so the JWT
*is* the stored credential and the JWT is what the subscriber receives. Confirmed by the
plugin author; recorded here because it is the single assumption the whole design rests
on, and it is not derivable from the API reference.

Worth keeping in mind for anything built on top of this: **nothing in this design detects
a wrong value, only a missing one.** The subscriber's change date moves whether or not the
value written was correct. That was equally true of the reconciler this replaced, whose
verification step confirmed the plugin's PATCH *ran* and never what it wrote.

## 5. The rotate-on-release loop

If either account's access policy enables **Change Password After Release** — or if anything
ever calls Password Safe's rotate-on-check-in endpoint — the pair rotates every time it is
released, with a real cluster rotation and a dead-credential window each time.

Synced accounts make this *worse*, and the reason is worth stating plainly: a credential
change on **either** member of a synced pair re-rotates both. So a release-triggered change
on the PRA Vault copy — an account that has nothing to do with the cluster — rotates the
real ServiceAccount token. Two defences:

1. `_checkin` issues plain `Checkin` only, and a static test asserts the rotate-on-release
   endpoint is named nowhere in the feature;
2. it is documented as an operator prerequisite, now for both accounts rather than one.

The circuit breaker that used to be the third defence went with the reconciler. It was
counting the dashboard's own pushes, and the dashboard no longer pushes.

## 6. The break window is real and cannot be closed here

In LongLived mode Password Safe revokes the old token as part of the rotation, before
anything can observe it. Password Safe applies the new value to the subscriber as part of
the same change, but change operations are queued, so there is still a window — now bounded
by Password Safe's own queue rather than by a poll interval the dashboard chose.

**Bound mode is the only lever that changes the kind of failure rather than its duration.**
Bound never revokes, so the previous token stays valid until its TTL and the window is zero.
Recommend it for any cluster whose tunnel is used interactively; keep LongLived where a
brief outage is acceptable or rotation is scheduled off-hours.

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

That access entry only exists as an API when the cluster's authentication mode includes
`API`, and EKS defaults new clusters to `CONFIG_MAP` — so the mode is a **precondition of
this whole design on EKS**, not a detail of it. The provisioning module therefore builds
clusters `API_AND_CONFIG_MAP` (`authentication_mode`, which keeps any hand-made `aws-auth`
entries working); on a `CONFIG_MAP` cluster there is no way to map the rotator at all, and
the failure surfaces only at the first rotation.

## 8. Smaller decisions worth recording

- **Password Safe is driven over REST (`ps_api_service`), not `ps-cli`.** `ps-cli` does
  expose `synced-accounts`, but `btapi_service` spawns a subprocess with its own OAuth
  handshake per call, and this feature already holds a signed-in REST session for the
  reads around it. REST also keeps the feature working in an image with no `ps-cli` binary.
- **The direction of the link is the likeliest defect in the whole feature.** Both path
  segments are plain managed-account ids, so a swapped pair links successfully and then
  syncs *backwards* — pushing the PRA Vault account's value onto the cluster's token
  account, with nothing downstream to notice. Two tests pin it: one on the URL
  `ps_api_service` builds, one on the arguments `register` passes.
- **The dashboard holds a plaintext token in exactly one place.** `checkout_credential`
  returns the value because the PRA Vault account is Terraform-provisioned and the tunnel
  path must pass it as a sensitive `TF_VAR`. Nothing else needs it — keeping the pair in
  step needs no checkout at all, which is the security dividend of §4.
- **A checked-out value is shape-guarded before use.** Password Safe can return a
  soft-failure *string* in the credential position; provisioning the tunnel with that would
  break it while reporting success. A ServiceAccount token is a JWT in both modes, so three
  dot-separated segments is a sufficient and cheap check.
- **The subscriber is bound by numeric `ManagedAccountID`, never by name**, and an account
  on the wrong platform fails closed before the link is created. PRA does not enforce
  unique vault account names, and syncing a Kubernetes bearer token to some other plugin's
  account is the one failure here that puts a secret somewhere it does not belong.
- **The mirror's managed system is named `k8s-<cluster>-pravault`, and that is not
  cosmetic.** Password Safe names a workgroup-created managed system after its *HostName*,
  and the Terraform provider's `passwordsafe_managed_account` attaches to its system **by
  name** — it takes no system id. The other two PRA Vault callers (cloud-DB, OT) put the
  appliance URL in `host_name`, so their systems all share one name; harmless while they
  share the "PRA Vault Username Password" platform, and *not* harmless here. Measured live:
  the mirror's own "PRA Vault Token" system was created, then the account was created on
  the pre-existing cloud-DB Username Password system of the same name, and the SyncedAccounts
  platform guard refused to sync a cluster bearer token into it — after registration had
  already reported the account created. The URL therefore rides `dns_name` alone here, which
  assumes the plugin reads it there (the same assumption `k8ssa` already makes for its
  address); one rotation against a tenant confirms it.
- **Removing the PRA tunnel deliberately leaves the link in place.** The plugin resolves
  its PRA Vault account by *name* (`k8s-<cluster>-sa`), so re-provisioning re-creates the
  account the link already points at and syncing resumes with no operator action. The cost
  is that a rotation landing while no tunnel exists fails the PRA half — visibly, in
  Password Safe's change log, which is the right place for it.
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
- Grant the API identity **Password Safe Account Management (Full control)** for the sync
  link, per the REST reference. `ps-cli synced-accounts -h` claims *Role Management
  (Read/Write)*, which reads like an error in the CLI help — the operation acts on managed
  accounts, not roles — but it is worth trying if the link 403s with Account Management
  already in place.
- Leave **Change Password After Release** off on **both** accounts (see §5) — under synced
  accounts a change on either one rotates the pair.
- Give the Password Safe host or Resource Broker network reachability to the cluster's API
  server. For private EKS/AKS/GKE clusters that is a real problem, and the PRA Gateway
  cannot help — Password Safe does not route through it.
