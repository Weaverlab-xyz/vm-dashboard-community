# Certificates

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deciding which certificate backend and which of the two platforms an identity belongs on, and what its address has to say.

> **Preview.** The plugin's shared core is covered by its own 624-assertion suite, but
> **none of its submission paths has been proven against a live authority** — the nine
> backends and the two Entra publishers can only be exercised against a real CA, cloud
> account or tenant. That is what this feature exists to make cheap. Off by default;
> enable it under **Settings → Preview features → Certificate Lab**.

This page is the reference for the Password Safe **Certificate** custom platform plugin
family: what it is, which of the nine backends can do what, where it has to run, and the
address grammar that is its entire configuration surface. Nothing here builds anything.

It is the **Certificates** tab of the **Workload Lab** page (`/workload-lab`), alongside
[SPIRE](spiffe.md) — both labs govern identities that belong to machines, so they share a
page. Each still has its own preview toggle and its own Settings panel.

Three pages, and this is the first:

- **Certificates** *(you are here)* — the plugin, the backends, the platforms, the address
- [The Certificate Lab](certificate-lab.md) — standing a private CA up, onboarding an
  identity onto it, and tearing it all down again
- [Onboarding a subordinate CA](subordinate-ca.md) — when the holder mints its own
  certificates beneath the one it was given

The companion docs:

- [Infrastructure as Code](../infrastructure-as-code.md) — the closed provision/destroy
  lifecycle this feature follows
- [Auto-delete Timer](../auto-delete-timer.md) — why a CA pool is exactly the thing that
  timer is for
- [Config Management](../config-management.md) — how the mTLS endpoint and the CI runner get
  configured
- [What consumes these credentials](consumers.md) — the plays that spend a certificate this
  family issued
- [`examples/playbooks/certificates/`](../../examples/playbooks/certificates/README.md) — the
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

## Two packages, two platforms

The plugin ships as **two `.psplugin` packages over a shared core**, with different plugin
ids — so both install side by side and neither overwrites the other. They appear in
BeyondInsight as two **platforms**, and that is the part that matters operationally.

| | Certificate | Subordinate CA |
|---|---|---|
| Managed credential | an **end-entity certificate** | a **subordinate certificate authority** |
| Who consumes it | the identity itself — a pipeline, an agent, a service | another system, which mints its own short-lived certificates beneath it |
| Backends | all nine | only the four that can sign a subordinate |
| `isca` defaults to | `false` | `true` |
| `bundle` defaults to | `Pkcs12` | `PemBundle` |

**Which one you want.** If the thing that will present the certificate is the identity
named in it, the Certificate platform. If the thing that holds it will mint *further*
certificates — PRA Vault issuing session certificates is the case this was built for — the
other one.

**Why it is a split and not a flag.** A subordinate CA is a *delegation of issuing
authority*, not one more credential. Two platforms mean the team that may request a leaf is
not automatically the team that may request an issuer, and that separation is expressible
in Password Safe's own access model rather than by convention. The dashboard follows the
split rather than papering over it, and the refusal you get differs by platform — see
[Two refusals, and which one you get](#two-refusals-and-which-one-you-get).

They are the same code. The shared core is the overwhelming majority of it — key
generation, CSR construction, bundling, storage, verification, the configuration grammar,
every backend — so the two cannot drift apart in behaviour, and everything this page says
about configuration applies to both.

### One functional account per platform

A functional account is **platform-bound**, and a managed system inherits its functional
account's platform. So an account on `Certificate` cannot carry a managed system on
`Subordinate CA`: it onboards green and then fails every credential action. A CA serving
both therefore holds **two** functional accounts, one on each platform, carrying the same
CA credential.

The dashboard mints them at different times, on purpose:

- the **Certificate** one during the CA build, because the enrollment credential exists in
  the apply's outputs and nowhere else a person can reach;
- the **Subordinate CA** one **lazily**, on the first subordinate onboarded — a CA that
  never issues an authority should not carry an account on the platform that would. It is
  recovered from the enrollment credential still in the CA's own terraform state, so it
  needs no rebuild, which matters because CAS never hands a deleted pool id back.

Teardown deletes both, and only ones the dashboard minted. Missing the second would leave
an orphan holding a live enrollment credential after the CA it belonged to is gone.

### The nine backends

The Certificate platform's full list. The dashboard **provisions** the two cloud ones; the
rest are certificate authorities a customer already runs, so the dashboard validates their
addresses but has no build path for them.

| Backend | Names accepted | Required | Can sign a sub-CA |
|---|---|---|---|
| Microsoft ADCS | `adcs`, `ad`, `microsoft-ca` | `ca=`, `template=` | refused on policy |
| EST (RFC 7030) | `est`, `rfc7030` | `url=` | no — the protocol has no mechanism |
| EJBCA | `ejbca`, `keyfactor-ejbca` | `url=`, `ca=`, and `profile=` or `subcaprofile=` | **yes** |
| HashiCorp Vault PKI | `vault`, `vaultpki`, `hashicorp-vault` | `url=`, and `role=` for a leaf | **yes** |
| Smallstep step-ca | `stepca`, `step`, `smallstep` | `url=`, `fingerprint=` | no |
| DigiCert ONE | `digicert`, `digicert-one` | `url=`, `profile=` | no |
| Sectigo | `sectigo`, `sectigo-cm` | `url=`, `profile=` | no |
| AWS Private CA | `awspca`, `aws`, `acm-pca` | `arn=` | **yes** |
| Google Cloud CAS | `gcpcas`, `gcp`, `cas`, `google-cas` | `project=`, `location=`, `pool=` | **yes** |
| Self-signed | `selfsigned`, `self` | none | no — a new trust root, not a subordinate |

### How each of the four signs one, and the grant that follows

| Backend | How | Grant it separately? |
|---|---|---|
| `awspca` | the issuance template is switched to `SubordinateCACertificate_PathLen{0-3}`, chosen from `pathlen=`. ACM PCA **ignores the CSR's basic constraints** and builds from the template, so this is the only thing that decides whether a CA comes back | yes — restrict `acm-pca:IssueCertificate` to the subordinate template ARNs |
| `gcpcas` | the CSR's own extensions are honoured, subject to the pool's issuance policy. The parent needs `--max-chain-length=1` or higher | **no** — see below |
| `ejbca` | a CA-type certificate profile, named with `subcaprofile=` | yes — a CA-type profile is a distinct grant |
| `vaultpki` | a dedicated endpoint, `<mount>/root/sign-intermediate` — not a flag on the ordinary signing path, so the two operations cannot be confused | yes — `update` on that path alone |

Granting only that is the difference between a compromised functional account minting one
bounded authority and minting anything.

**GCP is the exception, and it changes where the boundary goes.** Issuing a subordinate
uses the *same* `privateca.certificates.create` as issuing a leaf — what differs is the CSR
and the pool's issuance policy, not the permission. So the separation has to be made with a
**dedicated CA pool** whose policy permits CA certificates, and the grant scoped to that
pool. There the boundary is the **resource rather than the verb**.

That is why the path-length choice produces its own CA row rather than being a setting that
widens an existing pool: a leaf-only pool and a subordinate-capable one being separate
resources, with separate pool-scoped enrollment identities, *is* the control.

### Two refusals, and which one you get

Which refusal an operator sees depends on which platform they are standing on, and the
difference is deliberate.

**On the Subordinate CA platform**, a backend outside the four is declined **by name**,
before any policy is consulted: it says the platform does not support that backend, lists
the four it does, and points at the Certificate platform for an end-entity certificate.
That names what to use instead, which is what someone who picked the wrong platform needs.

**On the Certificate platform**, `isca=` and the constraint options get the backend's *own
reasoning* — because that is the only platform those backends are offered on at all, so
"why not ADCS?" is answered there or nowhere:

- **ADCS.** Capable, but refused on policy. A sub-CA template that issues unattended means
  turning *off* CA certificate manager approval, which many organisations forbid outright
  — and with approval on, every rotation returns `CR_DISP_UNDER_SUBMISSION` and can never
  complete. A cloud backend's equivalent permission is IAM-scoped, reviewable, revocable
  and logged.
- **Self-signed.** A self-signed CA certificate is a new **trust root**, not a subordinate.
  Nothing above it constrains what it may assert, and every relying party would have to be
  visited to trust it and visited again to stop.
- **EST.** RFC 7030 has no mechanism for requesting a CA certificate at all — `simpleenroll`
  issues end-entity certificates and that is the whole of it. EST remains the route for
  *leaf* issuance against the same CA across a firewall.
- **step-ca, DigiCert ONE, Sectigo.** The sign endpoint issues end-entity certificates; a
  managed or public CA will not sign you a subordinate off its own hierarchy outside a
  dedicated, heavily audited programme.

On a backend that *can* sign one, `isca=true` here is legal and does nothing useful, so the
answer is the other package — putting an issuer under the access control meant for a leaf is
exactly what the split prevents.

Underneath the package's own list sits a **second gate**: the backend's own capability flag,
which **fails closed**. A backend added later is refused for subordinate issuance until it
declares the capability. That direction is deliberate — most certificate authorities cannot
sign a subordinate at all, so the unrecognised case is far more likely to be one that should
be refused.

`selfsignedtest` exists in the plugin for its own harness and is refused outright here: it
generates and persists its own CA private key **unencrypted** beside the plugin.

Three of the nine cannot revoke at all — EST has no revocation operation in RFC 7030,
step-ca's needs a credential the plugin no longer holds after issuance, and `selfsigned`
publishes nothing to revoke against. On those, **Disable Managed Account** succeeds and
says the certificate stays valid until it expires. That is a success rather than a failure:
the backend never could have done it.

---

## Where the plugin runs, and the one constraint that decides ADCS

On **Password Safe Cloud** a custom platform plugin runs on a **Resource Broker**. The
plugin needs nothing inbound — every call it makes is outbound and broker-initiated — so the
question is not inbound versus outbound. It is whether the traffic stays inside the broker's
subnet or has to cross the firewall at its edge.

| Backend | What crosses the subnet firewall | Port |
|---|---|---|
| `gcpcas` | `privateca.googleapis.com`, `oauth2.googleapis.com` (the JWT-bearer exchange that authenticates it) | 443 |
| `awspca` | `acm-pca.<region>.amazonaws.com` | 443 |
| `est`, `ejbca`, `vaultpki`, `stepca`, `digicert`, `sectigo` | the service's own host, from `url=` — administrator-supplied, so there is no fixed hostname to pre-approve | 443 |
| `selfsigned` | nothing. There is no certificate authority to reach | — |
| Entra publishers | `login.microsoftonline.com`, `graph.microsoft.com` | 443 |
| every backend | the BeyondInsight host from `biurl=`, to write the bundle | 443 |
| `adcs` | see below — this is the one that is not 443 | 135 + 49152–65535 |

**Every backend but ADCS needs nothing beyond outbound 443**, so a broker that can already
reach BeyondInsight can usually reach AWS, GCP, Entra or an HTTPS CA with no new rule. The
Certificate Lab's GCP and AWS paths are both in that group, and the self-signed backends
need no egress at all. ADCS is the single exception, and it is the next section.

`url=` is the service's **origin**, not an API path — the plugin appends its own, so a path
here yields a doubled one and a 404 at the first credential change. The dashboard refuses
one, and refuses plain `http` on everything but Vault (where a development server may
legitimately be plain): the request carries the functional account's enrollment credential
in a header. `insecure=true` exists for one real moment — a private CA's own management
endpoint presented on a certificate chaining to that very CA, which the broker does not
trust until the chain is installed — and it is logged as the footgun it is. Installing the
chain is the actual fix.

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

## What this feature does not do

Being straight about the boundary is more persuasive than eliding it.

- **No revocation checking.** The plugin consults neither CRLs nor OCSP. Short lifetimes are
  the mitigation, and that is a deliberate design position.
- **Revocation itself works, but only on six of the nine backends.** `RevokeOnDisable`
  defaults on, so **Disable Managed Account** revokes the certificate the account holds,
  and `RevokeOnRenewal` withdraws a superseded one. EST, step-ca and `selfsigned` have no
  revocation operation, so on those Disable succeeds and says the certificate stays valid
  until it expires. Worth demonstrating on a backend that can, and worth being explicit
  about on one that cannot.
- **No revocation of a subordinate CA on a DevOps-tier pool** — worse than the above,
  because the remedy is larger. See
  [the revocation caveat](subordinate-ca.md#the-revocation-caveat-that-matters-most-here).
- **No build path for six of the nine backends.** EST, EJBCA, Vault PKI, step-ca, DigiCert
  ONE and Sectigo are certificate authorities a customer already runs, so there is nothing
  for this page to provision. Their addresses are validated — the grammar, the required
  options, the `url=` shape, the per-backend authentication — but onboarding an identity
  against one means a managed system created by hand.
- **No live subordinate-CA round trip.** The upload format is settled and the plugin emits
  exactly the three PEM pieces PRA's account type takes, with the suite checking that the
  passphrase opens the key and that the key and certificate are a pair. What is untested is
  uploading it to a real PRA, minting a client certificate beneath it, and connecting — as
  is which PKCS#8 ciphers PRA's PEM parser accepts.
- **Whether the two platforms can be granted to different teams in practice.** The split is
  what makes it possible — separate platforms, separate access control, separate functional
  accounts — but the access-policy modelling is a BeyondInsight exercise this page does not
  cover. Confirm it against the customer's own role model before promising the
  separation-of-duties story.
- **No deployment.** The plugin delivers to Password Safe. Getting the certificate into an
  nginx config, a Java keystore or an IIS binding is the consumer's job —
  `ci-fetch-cert.yml` does it with a script. **Entra is the one exception**: its publisher
  does push the public half to the relying party, because Entra pins individual certificates
  and rotation would otherwise break the identity.
- **No discovery.** Certificates already deployed across the estate are invisible. The
  plugin manages what it issued.
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
| Page | `/workload-lab#certificates` → `web_dashboard/templates/workload_lab/` (`index.html` + `_certificates.html`) |
| API | `web_dashboard/api/cert_lab.py` (`/api/cert-lab/*`) |
| CA lifecycle | `web_dashboard/services/cert_lab_service.py` |
| Address + Password Safe objects | `web_dashboard/services/cert_ps_service.py` |
| Address grammar + registration | `web_dashboard/services/ps_resource_service.py` (`method="certificate"`) |
| Terraform | `terraform/cert_ca/gcp_cas/main.tf`, `terraform/cert_ca/aws_pca/main.tf` |
| Playbooks | [`examples/playbooks/certificates/`](../../examples/playbooks/certificates/README.md) |
| Tests | `tests/test_ps_certificate.py`, `tests/test_cert_lab_wiring.py`, `tests/test_cert_lab_clouds.py`, `tests/test_cert_lab_functional_account.py` |
