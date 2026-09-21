# The Certificate Lab

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need a private certificate authority to onboard certificate identities against, and want it gone again when the demo ends.

> **Preview.** Neither cloud build path has been run against a live account — the GCP CAS
> and AWS Private CA modules, the enrollment identities they mint, and the Entra publishers
> are all unexercised. Off by default; enable it under **Settings → Preview features →
> Certificate Lab**.

This page builds the lab for the Password Safe **Certificate** custom platform plugin
family, and tears it down again. Two things happen here and nothing else does: a private
certificate authority is **provisioned and destroyed**, and identities are **onboarded**
onto it as Password Safe managed accounts. Issuance itself belongs in BeyondInsight, behind
an approval — see [Why nothing is issued from here](#why-nothing-is-issued-from-here).

Read [Certificates](certificates.md) first if you have not decided on a backend or a
platform: which of the nine can sign what, and the address grammar everything here composes,
are there rather than here.

---

## A forgotten private CA is the expensive mistake

Standing the lab up by hand is the bottleneck, and its cloud half has a standing cost that
nothing else reclaims.

**A CA pool bills whether or not it ever issues a certificate.** A GCP CAS pool on the
DevOps tier is about **$20/month** plus roughly $0.30 per certificate; an AWS Private CA is
about **$400/month** standing. One nobody remembers is invisible on every page this
dashboard had before this one — which is why every CA built here is a first-class inventory
row carrying an [auto-delete timer](../auto-delete-timer.md).

The mTLS endpoint and the CI runner are **ordinary VMs** deployed through the normal cloud
pages. They already have their own timers, ref-counted NAT and Password Safe onboarding;
nothing here re-implements any of that.

---

## Prerequisites

### Password Safe

1. **BeyondInsight 26.1 or later.** This is a hard floor, not a recommendation. Before
   26.1.0.878, file secrets downloaded through the API were larger than the original and
   did not match the copy downloaded from the web console — so a PKCS#12 retrieved by a
   non-human identity on an earlier build would have been **corrupt**.
2. **The `.psplugin` imported** and the `Certificate` platform created — and, to onboard a
   subordinate CA, the **second** package too, which creates the `Subordinate CA`
   platform. They carry different plugin ids, so both install side by side and neither
   overwrites the other; upload each at **Configuration → Privileged Access Management →
   Platform Plugins → Create New Platform Plugin**. The dashboard resolves both platforms
   live by name through `GET /Platforms`, so a renamed platform just needs its new name in
   Settings (`cert_ps_platform`, `cert_ps_subca_platform`). See
   [Two packages, two platforms](certificates.md#two-packages-two-platforms).
3. **A BeyondInsight registration for the plugin to reach Secrets Safe with.** The
   dashboard builds the functional account itself; this is the one half it cannot derive
   from the CA build. One account carries **two** credentials, and there are two shapes of
   the BeyondInsight one. **The plugin reads both and prefers OAuth.**

   | | Username | Password | API key | API secret |
   |---|---|---|---|---|
   | **OAuth** (preferred) | `<ca-account>` | `<ca-secret>` | OAuth client id | OAuth client secret |
   | **Packed** (fallback) | `<ca-account>:<bi-run-as-user>` | `<ca-secret>:<bi-api-key>` | — | — |

   `ECredentialType` is a flags enum and `CredentialParameter` carries `ApiKey` and
   `ApiSecret` as fields of their own, so one account can be `Password, ApiKey` at once.
   The OAuth path removes three things: no run-as user has to exist, **nothing is packed**
   — so a CA secret *or a CA account name* may contain a colon, which the packed form
   could not express — and the plugin presents a short-lived bearer token instead of a
   long-lived static key. On the packed path both fields are split on the **last** colon,
   deliberately: a BeyondInsight username and an API registration key contain no colon,
   but a certificate authority password may contain anything at all.

   **Check which one your console supports before you configure it.** Open the functional
   account form for a managed system on the `Certificate` platform and see whether Password
   Safe offers an **API key** credential type. The SDK models it fully, but whether the
   console exposes it for a *plugin-supplied* platform is host behaviour and is
   unverified — two minutes here decides which row above you use, and the plugin works
   either way.

   In Settings → Certificate Lab:

   | Setting | For |
   |---|---|
   | `cert_ps_bi_auth` | `auto` (default), `oauth`, or `apikey`. Pin it once you have checked |
   | `cert_ps_bi_client_id` / `cert_ps_bi_client_secret` | The OAuth path. An API registration permitting the **client credentials** grant, with write access to the Secrets Safe folder. Blank falls back to the dashboard's own `pscli_client_id`/`pscli_client_secret` — same tenant by construction, but that registration administers the whole of it, and this one is handed to a plugin on a Resource Broker, so prefer a dedicated one |
   | `cert_ps_bi_api_key` / `cert_ps_bi_run_as_user` | The packed path. The run-as user falls back to `pscli_api_account_name` |

   `auto` resolves a dedicated client id first, then an explicitly set `cert_ps_bi_api_key`,
   then the inherited `pscli_*` pair. The API key outranks the inherited pair on purpose:
   an install already working on the packed path must not be moved onto an unproven one by
   an upgrade, because a functional account that authenticates as nothing onboards **green**
   and fails hours later at a rotation.

   **Building the CA creates this account** — see [Building a CA](#building-a-ca) below.
   The manual path is `cert_ps_functional_account_mode = reference`, which is the right
   setting for a CA this dashboard did not build.

   > Switching path on a CA that **already has** an account mints a second one. The two
   > shapes differ in the account name, so Password Safe sees a new object rather than a
   > duplicate, and only the new one is tracked — **Wire up Password Safe** says so, and
   > the old account has to be removed by hand once nothing is registered against it.
4. **A Secrets Safe safe** for the bundles. The dashboard creates the *folder tree* beneath
   it; it never creates the safe, which carries its own ACL.

### GCP

The sandbox setup scripts enable `privateca.googleapis.com` and grant the sandbox service
account `roles/privateca.admin` plus `roles/iam.serviceAccountAdmin` and
`roles/iam.serviceAccountKeyAdmin`. If your project was bootstrapped before this feature,
re-run `scripts/sandbox/Linux/setup-gcp.sh` (or `Setup-GcpSandbox.ps1`) — a missing API
surfaces as a `terraform apply` failure naming the service.

**The two `serviceAccount*` roles are not optional, and they were added later than the
first two.** The module creates the plugin's own *enrollment* service account and a key for
it, so a project with only `privateca.admin` builds the CA pool and then fails on
`iam.serviceAccounts.create` — an `IAM_PERMISSION_DENIED` that names neither this feature
nor the setup script. If you re-ran the script before they were added, re-run it again or
grant them by hand:

```bash
for ROLE in privateca.admin iam.serviceAccountAdmin iam.serviceAccountKeyAdmin; do gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SANDBOX_SA" --role "roles/$ROLE" --condition=None; done
```

Both the API enable and the IAM grants are eventually consistent — wait a couple of minutes
before retrying a build, or it can fail the same way once more. Where
`constraints/iam.disableServiceAccountKeyCreation` is enforced, the key grant is not enough
and the CA build cannot complete as written; that is an org policy rather than a role.

---

## Building a CA

**Workload Lab → Certificates → Build a CA.** Pick a cloud; one Terraform module per cloud does the
rest. On **GCP** that is a CAS pool on the DevOps tier, a self-signed root CA, and the
enrollment service account the plugin authenticates as. On **AWS** it is a Private CA, its
self-signed root certificate, and the IAM user the plugin authenticates as.

The cloud selector only appears when more than one module is built — the list comes from
the modules that actually exist, so a cloud can never be offered without one behind it.

### What the root may sign is decided here, once

The build form asks what the root permits beneath it, and **it cannot be changed after the
root is created**.

| Choice | The root's path length | Serves |
|---|---|---|
| End-entity certificates only *(default)* | 0 | the Certificate platform |
| Can also sign a subordinate CA | 1 | both platforms |

This is **the one prerequisite that stops a subordinate-CA build dead.** A root created to
issue leaves has a path length of zero and will refuse to sign a subordinate at all — and
it is the *certificate authority* that refuses, with a policy error naming neither this
choice nor the flag behind it. On GCP that flag is `--max-chain-length=1` at root creation
(`max_issuer_path_length` in the module); on AWS it also decides whether the enrollment
IAM policy permits a `SubordinateCACertificate` template at all.

So the dashboard records it on the CA row, shows it in the **Signs** column, and refuses a
subordinate against a leaf-only root **at the click** rather than letting it fail at the CA
hours later. Every CA built before this choice existed reads as leaf-only, which is what
those roots genuinely are.

Leaf-only stays the default: a root that permits a CA beneath it is a wider grant than a
lab needs by default, and widening it for every existing-style build would be a silent
change to what the button produces. A subordinate-capable root still issues end-entity
certificates exactly as before, so there is no downside to picking it when you intend to
demonstrate both — and demonstrating both from one CA is the clearest way to show why the
split exists, since the *same* address on the other platform issues the other kind of
thing.

### The build also creates the functional account

This is the step that used to be manual, and it had to move because **the enrollment
credential exists for exactly one moment**. Both clouds' APIs return it once — a GCP
service account key, an AWS secret access key — so it is live in the apply's outputs and
in no other place a person can reach. Recovering it by hand meant minting a *second* key.

So the build composes the account and writes it to Password Safe, in whichever of the two
shapes above is configured: on the OAuth path username `<enrollment-principal>` and
password `<enrollment-secret>` whole, with the client id and secret in the account's API
key and secret fields; on the packed one username `<enrollment-principal>:<run-as-user>`
and password `<enrollment-secret>:<api-key>`. On GCP the secret is the `private_key`
**field** out of the key JSON, PEM armour and all — never the whole file, which is the most
common way to get this wrong by hand.

It is also the only path that fits. `ps-cli` caps a functional-account password at 1,000
characters; a GCP `private_key` PEM is about 1,700. The REST API the dashboard uses
accepts 3,216.

**A failure here does not fail the build.** The CA is real and billing the moment the
apply returns, and a rebuild is not a free retry — CAS never hands a deleted pool id back.
So the row stays *available* with the error underneath it, and **Wire up Password Safe**
retries against the credential still recorded in the CA's own terraform state.

Teardown deletes the account, but only one the dashboard minted: in `reference` mode the
account is an operator's own and may be shared by every CA on the platform.

### Every id here is single-use

**CAS never gives a deleted resource id back.** Once a pool is destroyed,
`projects/<p>/locations/<l>/caPools/<id>` stays reserved permanently, and an apply that
asks for it again fails:

```
Error: Error waiting to create CaPool: Error code 3, message: Previously used CaPool ids
may not be reused. A `CaPool` for `projects/…/caPools/demo-pipeline-pool` has previously
been deleted
```

In a feature built around destroying the CA, that makes a pool id derived from the CA's
*name* alone usable exactly once — the first rebuild of `demo-pipeline`, and every rebuild
after it, is refused for good in that project and location. So the dashboard generates
`<name>-pool-<6 hex>`: the name stays the readable part of `pool=` on the address, and the
suffix is what makes a rebuild possible at all. The generated id is cut to fit CAS's
63-character cap on **both** the pool id and the `<pool>-root` CA id under it, and a free
text name is slugged (`Demo Pipeline (EU)` → `demo-pipeline-eu-…`). Those seven extra
characters come out of
[the 255-character address budget](certificates.md#the-255-character-budget-is-real).

An id supplied through the API (`pool_id` on `POST /api/cert-lab`; the build form does not
offer the field) is honoured exactly as typed, because it goes on to name the pool in every
address built against this CA — but it is validated at the click, and typing an id that has
existed here before hits the same permanent wall.

**The enrollment identity is per CA for the same reason,** one namespace up: a GCP service
account id is unique per *project* and an IAM user name per *account*, so the modules'
shared `certauth-plugin` default only ever fits one lab. The dashboard passes a per-row
`certauth-<8 hex>` instead. Without it the second CA in a project fails with
`alreadyExists` **after the pool exists**, and so does the retry after a build that got as
far as the identity and then died.

A failed build is rolled back — `terraform destroy` over the same state — before the row
goes `failed`, so a partial apply does not leave a billing pool or a live enrollment key
behind. If the rollback itself fails, the row says `MANUAL CLEANUP REQUIRED` under the
original error and **Destroy** retries the teardown.

Teardown is the part worth understanding, because **CAS resists deletion by default in three
separate ways**, and each one leaves a pool that goes on billing:

| Guard | Without it |
|---|---|
| `deletion_protection = false` | `terraform destroy` fails outright |
| `ignore_active_certificates_on_deletion = true` | a CA that has issued anything refuses to delete — which is the normal case, not the edge case |
| `skip_grace_period = true` | the deleted CA sits in a 30-day soft-delete state, and its pool cannot be deleted while it holds one |

The module sets all three. **Prove destroy before create**: apply the module, destroy it
immediately, and confirm in the console that the pool and CA are *gone* rather than pending
deletion.

AWS resists teardown differently and less: a deleted Private CA is restorable for
`permanent_deletion_time_in_days`, which **defaults to 30**. The module asks for 7, the
floor the API accepts, because this lab exists to get rid of the thing. The IAM user carries
`force_destroy` so a key or inline policy added in the console cannot wedge the destroy.
Prove destroy before create there too — confirm the CA is gone rather than merely disabled,
and that the IAM user went with it.

The CA's **chain PEM** is on the row's *Chain* button. It is a public document by
construction — it is what every client has to trust — and it is what the mTLS endpoint
playbook takes as `ca_chain_pem`.

### The timer

Every CA built here is stamped with the default auto-delete TTL at provision time, in the
provision's own transaction. `NULL` means *never*, never "inherit the default", so nothing
that already exists is retroactively armed. Extending or pinning a timer is the ordinary
**Inventory → Extend** path.

**On AWS the timer is not optional.** At ~$400/month standing against ~$20/month for a CAS
pool, a Private CA nobody remembers is a different order of mistake — so a build is
*refused* on an instance where the reaper would stamp nothing at all, which is the case when
`resource_expiry_enabled` is off or `resource_expiry_default_hours` is `0`. GCP is
deliberately not held to this: at a twentieth of the cost the same trade does not hold, and
tightening it would change behaviour somebody already relies on.

A stamped timer is necessary and not sufficient — the reaper only *deletes* when
`resource_expiry_enforce` is on and `resource_expiry_dry_run` is off. That half is reported
on the build form's **missing** list rather than refused, because arming enforcement is
something an operator may be part-way through.

A timer that runs out enqueues **exactly the job the Destroy button creates** — there is one
teardown path, exercised both ways. A *failed* teardown deliberately does **not** re-arm the
timer: a half-destroyed pool needs a human, and re-arming would retry a failing destroy on a
loop, silently.

---

## Onboarding a certificate identity

**Workload Lab → Certificates → Add identity.** The dashboard composes the address from the CA row (so
`project=`, `location=` and `pool=` can never drift from the pool that was actually built),
creates the Secrets Safe folder tree, resolves the functional account and platform, and
registers the managed system plus one managed account.

What lands in Password Safe:

| Field | Value | Why |
|---|---|---|
| Platform | `Certificate` | inherited from the functional account |
| Port | **0** | the platform does not use one. The CLI packager defaults it to 5432, inherited from the PostgreSQL plugin it was written for — that is the documented mistake |
| Network Address | the profile | the whole configuration surface |
| Timeout | 60 | read **by the plugin**, in seconds. Key generation plus a CA round trip is slower than a password change |
| Enable for API Access | **on** | `GET /ManagedAccounts` returns only accounts with `ApiEnabled`, and a CI pipeline is the whole use case |
| Change Password Using Own Credentials | **off** | a certificate identity holds no CA credential and cannot enroll for itself; the plugin reports `NotSupported`. The dashboard refuses to set it |

**One managed account per identity** — and with an Entra publisher, one per app
registration. Graph's `PATCH` replaces the whole `keyCredentials` collection, so the
publisher reads the existing entries and carries them forward; two rotations against the
same registration can each read the collection and clobber the other's key. Password Safe
serialises rotations per managed account, which is what makes that mapping safe. It is easy
to break by accident when copying a platform instance.

### Why nothing is issued from here

Registration does **not** fire a credential change, unlike the Kubernetes token path which
rotates on register to prove the whole path at once.

Issuance is meant to be gated by an approval with a reason, and that record is the first
thing the demonstration shows. Firing a rotation from the dashboard would produce a
certificate nobody approved and quietly remove the point. So the first certificate comes
from **Change Password** in BeyondInsight, and until it runs **Test password** correctly
fails — no bundle exists yet. That failure is step 1 of the demonstration, not a fault.

---

## Proving it works

Run these in order; each is cheap and fails fast.

| # | Step | What it proves |
|---|---|---|
| 1 | **Test functional account** on the managed system | The CA accepted the enrollment credential, and the action log echoes the resolved backend, lifetime, key shape, folder and secret title — the fastest way to see the address parsed the way it reads |
| 2 | **Test password** with no certificate yet | Verification correctly fails: no bundle exists |
| 3 | Submit a request with a reason, approve it | Issuance is gated by approval, with a record |
| 4 | **Change Password** | The completion message names the bundle's secret title and thumbprint — the one place the two halves are visibly linked for a human |
| 5 | **Test password** | Passes, and reports remaining validity. A real cryptographic check, not a string comparison |
| 6 | `nginx-mtls-endpoint.yml`, then `ci-fetch-cert.yml` — see [What consumes these credentials](consumers.md#the-certificate-is-spent-against-an-endpoint-that-checks-the-name) | A program retrieves both halves and the endpoint echoes `CN=svc-deploy-pipeline`. **This is the step usually skipped, and the only one that proves anything** |
| 7 | **Change Password** again, re-run the consumer | Renewal is transparent. Note the new serial |
| 8 | Break the Secrets Safe folder permission, **Change Password** | It fails — *and* step 6 still works on the previous certificate |

**Step 8 is the one to spend time on.** The plugin writes the bundle to Secrets Safe and
only *then* reports success, because Password Safe commits the new passphrase when the
action reports success. Reporting first would leave an account holding a passphrase that
opens nothing — a broken identity with no automatic recovery. Writing first means a failed
write leaves the **old** passphrase, the **old** bundle, and an identity that keeps
authenticating. Every credential-management demonstration shows the happy path; showing that
a mid-rotation failure leaves a working identity is what distinguishes this from a script.

---

## The on-premises topology

For an existing AD CS enterprise CA, the dashboard's job is narrow — the CA already exists,
so it creates the two things the plugin needs on it. Run
[`windows/adcs-pipeline-template.yml`](../../examples/playbooks/windows/adcs-pipeline-template.yml)
over WinRM against the issuing CA. It creates the template and the enrollment account, and
prints the `ca=` configuration string in the exact form the address wants.

Four template settings decide whether the plugin works at all, and the playbook sets all
four:

- **Subject supplied in the request.** Without it ADCS overrides the plugin's subject with
  the enrolling account's directory name, and the whole subject-template mechanism is
  bypassed *silently*.
- **No CA certificate manager approval.** With it every rotation returns
  `CR_DISP_UNDER_SUBMISSION` — a rotation job cannot block on a human clicking Issue.
- **A short validity.** 7 days makes renewal observable inside a demonstration. Note the
  **template decides validity**: `lifetime=` is ignored on ADCS, and the plugin does not
  pretend otherwise.
- **Client Authentication EKU only.**

The enrollment account's password is deliberately *not* set by the playbook: it belongs in
the functional account's protected field, and a password passed as an extra var lands in job
metadata and in Live Output.

Two ADCS-specific things to test, because they fail at a customer and nowhere else:

- **Impersonation works.** The CA's issued-certificates view must show the *Requester Name*
  as the enrollment account, not the Password Safe service account. If it shows the service
  account, `ImpersonateFunctionalAccount` is not taking effect and the template ACL is not
  being enforced.
- **Revoke and re-verify.** Revoke the certificate at the CA, then run **Test password**.
  Verification still *succeeds* — the plugin does not check CRLs. That is a real limitation
  to surface rather than hide.

---

## Where things live

The path table for the whole family is on
[Certificates](certificates.md#where-things-live). The pieces this page drives are
`web_dashboard/services/cert_lab_service.py` (the CA lifecycle),
`terraform/cert_ca/gcp_cas/` and `terraform/cert_ca/aws_pca/`, and
[`examples/playbooks/certificates/`](../../examples/playbooks/certificates/README.md).
