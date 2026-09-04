# Certificates

> **Preview.** The plugin's shared core is covered by its own 307-assertion suite, but
> **none of its four submission paths has been proven against a live authority** — ADCS,
> AWS Private CA, GCP CAS and the two Entra publishers can only be exercised against a
> real CA, cloud account or tenant. That is what this feature exists to make cheap. Off by
> default; enable it under **Settings → Preview features → Certificate Lab**.

This page builds the lab for the Password Safe **Certificate** custom platform plugin, and
tears it down again. Two things happen here and nothing else does: a private certificate
authority is **provisioned and destroyed**, and certificate identities are **onboarded**
onto it as Password Safe managed accounts. Issuance itself belongs in BeyondInsight, behind
an approval — see [Why nothing is issued from here](#why-nothing-is-issued-from-here).

The companion docs:

- [Infrastructure as Code](infrastructure-as-code.md) — the closed provision/destroy
  lifecycle this feature follows
- [Auto-delete Timer](auto-delete-timer.md) — why a CA pool is exactly the thing that
  timer is for
- [Config Management](config-management.md) — how the mTLS endpoint and the CI runner get
  configured
- [`examples/playbooks/certificates/`](../examples/playbooks/certificates/README.md) — the
  endpoint and consumer playbooks

---

## What the plugin does, in one paragraph

Password Safe **does not become a certificate authority**. An external CA signs and remains
the authority. The plugin generates the keypair in its own process, sends only a PKCS#10
certificate signing request, and never transmits the private key — so the CA never sees it
either. What Password Safe becomes is a **registrar and broker**: it requests certificates,
holds them, governs retrieval, and adds no depth to the certificate chain.

The result is split across two objects, and **both halves are needed and both are
governed**:

| Half | Where it lives |
|---|---|
| The PKCS#12 **passphrase** | the managed account's credential |
| The **bundle** that passphrase opens | a Secrets Safe file secret |

Retrieving one without the other yields nothing usable. So the Secrets Safe folder's ACL
and the managed account's access policy are **both** live controls — set them to the same
population, or the weaker of the two is your real access boundary.

---

## What this page is actually for

Standing the lab up by hand is the bottleneck, and its cloud half has a standing cost that
nothing else reclaims.

**A CA pool bills whether or not it ever issues a certificate.** A GCP CAS pool on the
DevOps tier is about **$20/month** plus roughly $0.30 per certificate; an AWS Private CA is
about **$400/month** standing. A forgotten private CA is the expensive mistake, and it is
invisible on every page this dashboard had before this one — which is why every CA built
here is a first-class inventory row carrying an
[auto-delete timer](auto-delete-timer.md).

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
2. **The `.psplugin` imported** and the `Certificate` platform created. The dashboard
   resolves the platform live by name through `GET /Platforms`, so a renamed platform just
   needs its new name in Settings.
3. **A functional account on that platform.** One account carries **two** credentials, both
   fields split on the **last** colon:

   | Field | Form |
   |---|---|
   | Username | `<ca-account>:<bi-run-as-user>` |
   | Password | `<ca-secret>:<bi-api-key>` |

   Splitting from the right is deliberate: a BeyondInsight username and an API registration
   key contain no colon, but a certificate authority password may contain anything at all.
4. **A Secrets Safe safe** for the bundles. The dashboard creates the *folder tree* beneath
   it; it never creates the safe, which carries its own ACL.

### Where the plugin runs, and the one constraint that decides ADCS

On **Password Safe Cloud** a custom platform plugin runs on a **Resource Broker**. The
plugin needs nothing inbound — every call it makes is outbound and broker-initiated — so the
question is not inbound versus outbound. It is whether the traffic stays inside the broker's
subnet or has to cross the firewall at its edge.

| Backend | What crosses the subnet firewall | Port |
|---|---|---|
| `gcpcas` | `privateca.googleapis.com`, `oauth2.googleapis.com` (the JWT-bearer exchange that authenticates it) | 443 |
| `awspca` | `acm-pca.<region>.amazonaws.com` | 443 |
| Entra publishers | `login.microsoftonline.com`, `graph.microsoft.com` | 443 |
| every backend | the BeyondInsight host from `biurl=`, to write the bundle | 443 |
| `adcs` | see below — this is the one that is not 443 | 135 + 49152–65535 |

Four of the five backends need nothing beyond outbound 443, so **a broker that can already
reach BeyondInsight can usually reach GCP, AWS and Entra with no new rule**. The Certificate
Lab's GCP path is in that group.

**ADCS is the exception, and it is a subnet question.** `ICertRequest3` is DCOM: the broker
opens TCP 135 to the CA's endpoint mapper, and the CA answers on a *dynamic high port* in
49152–65535. Between servers on a subnet, 135 is normally already open alongside 139, 445
and 3389, so none of that traffic reaches the edge firewall and enrollment simply works.
Across that firewall it is a different request entirely — 135 plus a 16,000-port range
through the perimeter, which no network team grants.

So the prerequisite is **put the Resource Broker on the CA's subnet**, not merely in the same
zone. Microsoft's Certificate Enrollment Web Service exists to turn enrollment into an
outbound-443 protocol and would remove the constraint; this plugin does not implement it.
Where the CA is segmented away from every candidate broker, that gap is the blocker and no
configuration works around it.

This is also the easiest constraint here to miss, because **a single-subnet lab satisfies it
by accident** — the problem first appears at a customer whose CA is segmented.

### GCP

The sandbox setup scripts enable `privateca.googleapis.com` and grant the sandbox service
account the CA Service roles. If your project was bootstrapped before this feature, re-run
`scripts/sandbox/Linux/setup-gcp.sh` (or `Setup-GcpSandbox.ps1`) — a missing API surfaces
as a `terraform apply` failure naming the service.

---

## The address is the entire configuration surface

`appsettings.json` ships **inside** the `.psplugin`, so its values are global to every
managed system, cannot be changed without repackaging, and on Password Safe Cloud cannot be
reached at all. Every value therefore rides the managed system's **Network Address**, which
is not a hostname but the whole certificate profile:

```
gcpcas?project=bt-se-lab&location=us-central1&pool=demo-pool&lifetime=24h&key=ecdsa-p256&biurl=https://tenant&folder=PKI/Pipelines&owner=1
```

That is what makes one installed plugin able to serve an ADCS template, a cloud CA pool and
a self-signed Entra credential at the same time, each configured by whoever owns that
managed system.

### The 255-character budget is real

Password Safe's address column is **255 characters**, and real profiles get close: the ADCS
example in the plugin's test case is **231**, and its own earlier draft was **269** — over
the limit without looking excessive, before anyone typed a real CA name or a Cloud tenant
URL. Dropping `secret=` (the default title written out longhand) and shortening `folder=`
bought those 38 characters back.

**The failure is silent.** An address trimmed to fit loses whatever sat at its end, and a
truncated `&owner=1` reads as an *absent* owner rather than as damage — so the first symptom
is a rotation failing on something with no obvious connection to length.

So the *Add identity* form shows a live character count, and the dashboard **refuses** an
over-long address naming the overage and what to drop. The plugin checks too and warns at or
over the limit, so this is the earlier of two guards rather than the only one — but a warning
in an action log arrives after the managed system already exists. Three levers, in the order
worth trying:

1. **Drop anything already at its default.** `secret=cert/{system}/{account}` (31
   characters), `retain=1`, `subject=CN={AccountName}`, `warn=25`, `key=rsa3072`,
   `pbe=Aes256`, `store=SecretsSafe` and `wait=30` all cost characters to say what the
   plugin would have done anyway. On AWS, `region=` is the same thing — it is the ARN's own
   fourth field.
2. **Move per-identity values onto the managed account name**, after a `?` —
   `svc-deploy-pipeline?dns=deploy.corp.example.com&lifetime=7d`. `dns=`, `ip=`, `subject=`,
   `lifetime=`, `eku=` and `key=` are all accepted there, where they cost nothing from this
   budget and stop forcing a managed system per SAN set.
3. **Shorten `folder=`.** `Certificates/Pipelines` → `Certs/Pipelines` buys 7 and loses
   nothing: a folder is addressed by path, not read as prose.

The most expensive single field is an AWS `arn=` at **101 characters** with a real account id
and CA id — which is why an AWS profile in particular wants its subject on the managed
account. If every option is genuinely doing work, the profile needs splitting across two
managed systems; a managed system is already the unit that pins one CA and one template, so
a profile too large for one address is usually two profiles.

### A mistyped option is refused, not ignored

The plugin logs an unrecognised option as a **warning** and carries on with a default. That
is the failure this dashboard refuses instead: `lifetim=30d` has no effect at all and leaves
a real certificate issued against a validity nobody chose, hours later, on a schedule. The
dashboard validates the whole grammar at registration — unknown backends, missing required
options, backend-scoped options on the wrong backend, out-of-range values, and a publisher
without its target id.

---

## Building a CA

**Certificate Lab → Build a CA.** One Terraform module creates a CAS pool on the DevOps
tier, a self-signed root CA, and the enrollment service account the plugin authenticates as.

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

The CA's **chain PEM** is on the row's *Chain* button. It is a public document by
construction — it is what every client has to trust — and it is what the mTLS endpoint
playbook takes as `ca_chain_pem`.

### The timer

Every CA built here is stamped with the default auto-delete TTL at provision time, in the
provision's own transaction. `NULL` means *never*, never "inherit the default", so nothing
that already exists is retroactively armed. Extending or pinning a timer is the ordinary
**Inventory → Extend** path.

A timer that runs out enqueues **exactly the job the Destroy button creates** — there is one
teardown path, exercised both ways. A *failed* teardown deliberately does **not** re-arm the
timer: a half-destroyed pool needs a human, and re-arming would retry a failing destroy on a
loop, silently.

---

## Onboarding a certificate identity

**Certificate Lab → Add identity.** The dashboard composes the address from the CA row (so
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
| 6 | `nginx-mtls-endpoint.yml`, then `ci-fetch-cert.yml` | A program retrieves both halves and the endpoint echoes `CN=svc-deploy-pipeline`. **This is the step usually skipped, and the only one that proves anything** |
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
[`windows/adcs-pipeline-template.yml`](../examples/playbooks/windows/adcs-pipeline-template.yml)
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

## What this feature does not do

Being straight about the boundary is more persuasive than eliding it.

- **No revocation checking.** The plugin consults neither CRLs nor OCSP. Short lifetimes are
  the mitigation, and that is a deliberate design position.
- **No deployment.** The plugin delivers to Password Safe. Getting the certificate into an
  nginx config, a Java keystore or an IIS binding is the consumer's job —
  `ci-fetch-cert.yml` does it with a script. **Entra is the one exception**: its publisher
  does push the public half to the relying party, because Entra pins individual certificates
  and rotation would otherwise break the identity.
- **No discovery.** Certificates already deployed across the estate are invisible. The
  plugin manages what it issued.
- **AWS Private CA is not provisioned here.** The Terraform module would be a near-copy of
  the CAS one, but at ~$400/month standing it should be created for a demonstration and
  destroyed immediately after. The plugin's `awspca` backend works against a CA you build
  yourself — the address grammar is validated the same way.
- **Entra app registrations are not created here.** The dashboard consumes app registrations
  but has no create-app-registration path. The Password Safe half is unchanged —
  `selfsigned?publisher=entraapp&…` is just another address — but the two registrations, the
  `Application.ReadWrite.OwnedBy` grant, and the publisher's service principal being added
  as an **owner of the target app** are manual. That last step is the one that costs an
  afternoon: without it every patch returns `Authorization_RequestDenied` while the
  permissions page looks entirely correct.

---

## Where things live

| | |
|---|---|
| Page | `/cert-lab` → `web_dashboard/templates/cert_lab/index.html` |
| API | `web_dashboard/api/cert_lab.py` (`/api/cert-lab/*`) |
| CA lifecycle | `web_dashboard/services/cert_lab_service.py` |
| Address + Password Safe objects | `web_dashboard/services/cert_ps_service.py` |
| Address grammar + registration | `web_dashboard/services/ps_resource_service.py` (`method="certificate"`) |
| Terraform | `terraform/cert_ca/gcp_cas/main.tf` |
| Playbooks | [`examples/playbooks/certificates/`](../examples/playbooks/certificates/README.md) |
| Tests | `tests/test_ps_certificate.py`, `tests/test_cert_lab_wiring.py` |
