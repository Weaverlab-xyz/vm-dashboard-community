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
row carrying an [auto-delete timer](../../operations/auto-delete-timer.md).

The mTLS endpoint and the CI runner are **ordinary VMs** deployed through the normal cloud
pages. They already have their own timers, ref-counted NAT and Password Safe onboarding;
nothing here re-implements any of that.

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Certificate lab prerequisites](certificate-lab/prerequisites.md) | you are about to build a certificate lab and need each cloud's prerequisites in place first |
| [Building a CA](certificate-lab/building-a-ca.md) | you are building the lab's private certificate authority, root or subordinate |

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

### A third consumer, which holds nothing

`ci-fetch-cert.yml` proves the mechanism, and it does so with a Password Safe client id
and secret supplied to the run. The [Agent Demo Cell](../../profiles/demo/agent-demo-cell.md)
is the consumer that has neither: it reaches Password Safe with a workload identity
brokered by Workload Credentials, requests the passphrase as a recorded request, downloads
the bundle over `GET Secrets-Safe/Secrets/{id}/file/download`, and presents the certificate
to an mTLS endpoint.

That route rather than `ps-cli` for the reason
[password-safe.md](../../beyondtrust/password-safe.md) records: every path ps-cli offers
decodes the body to text, so a PEM bundle survives and a **PKCS#12 is corrupted rather
than refused**. The lab writes a `.pfx`, so the agent keeps the bytes itself.

It is also where the boundary below gets demonstrated rather than described. Revoke the
certificate, run the agent again, and watch it work — because nothing on that path checks.

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
[`windows/adcs-pipeline-template.yml`](../../../examples/playbooks/windows/adcs-pipeline-template.yml)
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
[`examples/playbooks/certificates/`](../../../examples/playbooks/certificates/README.md).
