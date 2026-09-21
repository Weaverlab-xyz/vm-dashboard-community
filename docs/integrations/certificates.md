# Certificates

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need a private certificate authority to onboard certificate identities against, and want it gone again when the demo ends.

> **Preview.** The plugin's shared core is covered by its own 624-assertion suite, but
> **none of its submission paths has been proven against a live authority** — the nine
> backends and the two Entra publishers can only be exercised against a real CA, cloud
> account or tenant. That is what this feature exists to make cheap. Off by default;
> enable it under **Settings → Preview features → Certificate Lab**.

This page builds the lab for the Password Safe **Certificate** custom platform plugin
family, and tears it down again. Two things happen here and nothing else does: a private
certificate authority is **provisioned and destroyed**, and identities are **onboarded**
onto it as Password Safe managed accounts. Issuance itself belongs in BeyondInsight, behind
an approval — see [Why nothing is issued from here](#why-nothing-is-issued-from-here).

It is the **Certificates** tab of the **Workload Lab** page (`/workload-lab`), alongside
[SPIRE](spiffe.md) — both labs govern identities that belong to machines, so they share a
page. Each still has its own preview toggle and its own Settings panel.

The companion docs:

- [Infrastructure as Code](../infrastructure-as-code.md) — the closed provision/destroy
  lifecycle this feature follows
- [Auto-delete Timer](../auto-delete-timer.md) — why a CA pool is exactly the thing that
  timer is for
- [Config Management](../config-management.md) — how the mTLS endpoint and the CI runner get
  configured
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

## What this page is actually for

Standing the lab up by hand is the bottleneck, and its cloud half has a standing cost that
nothing else reclaims.

**A CA pool bills whether or not it ever issues a certificate.** A GCP CAS pool on the
DevOps tier is about **$20/month** plus roughly $0.30 per certificate; an AWS Private CA is
about **$400/month** standing. A forgotten private CA is the expensive mistake, and it is
invisible on every page this dashboard had before this one — which is why every CA built
here is a first-class inventory row carrying an
[auto-delete timer](../auto-delete-timer.md).

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
   [Two packages, two platforms](#two-packages-two-platforms).
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

### Where the plugin runs, and the one constraint that decides ADCS

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
characters come out of the 255-character address budget below.

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

## Onboarding a subordinate CA

**Workload Lab → Certificates → Add subordinate CA**, offered only on a CA whose root can
sign one. Everything above still applies — the same grammar, the same store, the same
two-halves split — with one inversion that changes what the demonstration is about.

**The managed credential here is the issuer, not a certificate.** Password Safe obtains a
subordinate CA from the root, something else holds it and mints its own short-lived leaves
beneath it, and Password Safe rotates the subordinate on a schedule. One governed
credential instead of N, and **no delivery problem at all** — because nothing has to move
the leaves.

The pitch is governance rather than threat mitigation, and it collapses under the first
informed question if put the other way. A certificate authority is a privileged account
that nobody treats as one: high privilege, created once, an unknown number of copies in
unknown hands, never rotated, outliving the person who made it. That is the profile of the
shared local admin password — the exact thing PAM exists to fix. The category simply never
got pointed at PKI.

### The topology is the whole design

Get this wrong and the rotation schedule becomes an estate-wide outage schedule.

| Targets trust | Rotating the CA means | Automatable |
|---|---|---|
| the sub-CA directly | a coordinated trust-store push to every host, in lockstep | **No** |
| the root, with the sub-CA chaining to it | nothing — targets never move | **Yes** |

**Install the root chain into every target's trust store once, before anything else.** That
is what makes rotation safe, and it is the step nothing here automates — the *Chain* button
on the CA row is where that PEM comes from.

One consequence is pleasant: because leaves chain to the root, rotation is graceful for
in-flight work. There is no drain step and no break window.

### Rotation does not revoke, and the lifetime is what bounds you

The most important thing on this page. A relying party validates a leaf by walking its
chain to the root; it neither knows nor cares which subordinate was current when that leaf
was minted. So after a rotation, certificates already issued from the previous subordinate
keep working until *that subordinate's own certificate* expires — and anyone holding a copy
of the previous subordinate's key can keep minting new, perfectly valid certificates for
exactly as long.

Rotation replaces what the holder has. It takes nothing away from anyone else who may have
it. So the security bound is the subordinate's **validity period**, not the rotation
interval, and conflating the two makes the control illusory:

| Sub-CA validity | Rotating every | Concurrently valid authorities | Exposure after a key leak |
|---|---|---|---|
| 1 year | 7 days | ~52 | up to a year |
| 8 days | 7 days | 2, briefly | at most 8 days |

Issue the subordinate with a validity just longer than the rotation interval — enough
overlap that certificates from the outgoing one stay valid until the new one is in place,
and no more. Size the overlap to the longest expected session rather than picking a round
number. The dashboard cautions in the action log above 45 days rather than enforcing a rule
it has half the inputs for: the rotation interval lives in a Password Safe account policy
it cannot see. `cert_subca_default_lifetime` in Settings is where to put the value you
settle on.

**"If this key leaked, what is our exposure window?" answers *eight days, by policy*.**
That sentence is the deliverable. What the automation does is not prevent an attack — it is
what makes a short validity period sustainable, because nobody reissues a certificate
authority by hand every week.

### Name constraints are the control that makes it defensible

A vaulted CA key is not a credential to one system; it is the authority to mint an identity
for *anything* the CA may assert. Constraints turn a compromise from **mint anything** into
**mint within a bounded namespace**. They are encoded per RFC 5280 §4.2.1.10 and marked
**critical**, so a relying party that cannot interpret them rejects the certificate rather
than ignoring the boundary.

`permitdns=`, `permitemail=`, `permitip=` and `excludedns=` are on the *Add subordinate CA*
form. A permitted-IP subtree is a **network**: `10.0.0.0/8`, never `10.1.2.3/8` — the
dashboard refuses host bits, because nothing downstream can detect a subordinate
constrained to something other than what was meant.

Issuing one with no constraints at all is **allowed and warned about**, not refused:
constraints belong on the *parent* CA pool's issuance policy where the backend supports it,
so their absence here is not necessarily a mistake. Prefer the parent for two reasons — the
subordinate inherits a boundary it cannot widen, which is a stronger statement than one it
merely happens to carry; and constraints are long. **This is the one topology where the
255-character budget and the security control pull against each other:** a permitted-DNS
list plus an email suffix can exceed 100 characters on its own, competing with the project,
location and pool names.

### What the certificate looks like, and what the form does not offer

Three differences from a leaf, each deliberate, and the dashboard refuses anything that
would contradict them:

- `basicConstraints` is `CA:TRUE` with the requested path length, marked critical.
- `keyUsage` becomes `keyCertSign`, `cRLSign` and `digitalSignature`, also critical.
  `keyEncipherment` is dropped: a signing key should not also be doing TLS key exchange.
- **No extended key usage at all.** An EKU on a CA certificate constrains the whole subtree
  beneath it, and implementations disagree about whether it applies to the CA itself or to
  its issued leaves. Asserting one would silently narrow what the holder can mint and would
  surface as an unrelated validation failure at a relying party. So `eku=` is refused here
  — and `cert_default_eku`, being a config default that applies to every profile, is
  dropped from this path rather than blocking it with an option nobody chose.

`publisher=`, `tenant=`, `appid=`, `spid=`, `retain=` and the SAN options are in the
*shared* grammar because the core is shared, and they do nothing on an issuer: a publisher
exists for a relying party that pins a certificate by thumbprint, and an issuer has none.
The dashboard refuses them rather than letting them be silently dropped.

Two options the address normally **omits**, because the Subordinate CA package already
defaults them: `isca=true` and `bundle=PemBundle`. Restating them is harmless and reads as
documentation — it just spends budget the permitted-DNS list is competing for. An explicit
`isca=false` is a real override and survives; it makes that platform behave as the
Certificate platform does, which is legal and logged, and points at using the other
package.

### Handing the subordinate to PRA Vault

PRA takes **three separate PEM fields**, not one PKCS#12. In PRA, go to **Vault → Accounts
→ Add Shared Account** and choose **X.509 Parent Certificate Authority** under
*Authentication*. Its own help text explains why that is the right account type: an X.509
Parent Certificate Authority is the trust for client validation, and at least one must
exist before a client certificate can be created.

Under *Private Key Options*, **do not** choose *Generated by BeyondTrust Privileged Remote
Access*. That has PRA create the key itself — a perfectly sensible thing to do, and the
opposite of this design: a key PRA generated is one Password Safe never held and cannot
rotate. Choose the upload path.

| PRA field | What goes in it | Where it comes from |
|---|---|---|
| Private key (PEM) | the encrypted PKCS#8 private key | `key.pem` from the bundle |
| Key Passphrase | the passphrase that decrypts it | the managed account's credential |
| X.509 Certificate | the subordinate's certificate | `cert.pem` from the bundle |

`chain.pem` is the fourth artifact and does **not** go in this account — it is the trust
material uploaded alongside as PRA's additional chain trust, and installed in every
target's store.

This is why the package defaults to `bundle=PemBundle`: PRA's key field accepts PEM only,
and a PKCS#12 would have to be taken apart with `openssl` before any of it could be pasted
in. The PEM bundle emits precisely these pieces:

```bash
unzip -o subca-bundle.zip          # cert.pem  key.pem  chain.pem  fullchain.pem
```

One thing to watch, and the escape hatch for it. The key is an encrypted PKCS#8,
AES-256-CBC by default. The presence of a *Key Passphrase* field says encrypted keys are
expected, but which ciphers PRA's PEM parser accepts is not documented. If the upload is
rejected, set `pbe=Legacy`: that switches the key to 3DES with the PKCS#12 KDF, which every
OpenSSL-era parser reads. It is weaker, and it is a compatibility lever rather than a
default.

### The demonstration, and the step to build it around

1. **The CA is an inventory row.** Owner, expiry, standing cost. It is on the books, which
   no CA in the estate currently is.
2. **Its credential is governed.** Request it: approval required, reason recorded,
   retrieval audited. Contrast with *"whoever holds a copy, and we do not know who that
   is."*
3. **Rotate it.** A new subordinate is issued, the holder receives it, sessions keep
   working, **no target was touched**. PKI practitioners assume rotating an issuing CA is
   an estate-wide change; watching it not be one is the argument.
4. **Show the leaf still validates.** A certificate minted from the *previous* subordinate
   is still valid, because it chains to the root. That is the graceful-rotation property —
   and the same mechanism that means rotation does not revoke.
5. **Give the risk register a number.** Eight days, by policy, enforced by the schedule.
6. **Retire it.** Retire the managed account: Password Safe stops issuing, so the holder
   receives no replacement, and the authority expires on its own. Say **"expires"**, not
   "revoked" — see below.

Step 3 is the one to spend time on. Verify the certificate rather than the plugin's word
for it:

```bash
openssl pkcs12 -in subca.pfx -nodes -passin pass:'<passphrase>' | openssl x509 -noout -text
```

Expect `CA:TRUE, pathlen:0` critical, `Key Usage: critical` with *Certificate Sign* and
*CRL Sign* and **no** *Key Encipherment*, `X509v3 Name Constraints: critical`, and **no**
Extended Key Usage at all. Then `openssl verify -CAfile ca-chain.pem subca.pem` — if that
fails, the root's path length was wrong and nothing downstream will work.

### The revocation caveat that matters most here

Short lifetimes make *leaf* revocation close to irrelevant, which is the usual answer to
this plugin consulting neither CRLs nor OCSP. A compromised **subordinate** is a different
question, and on some CA configurations there is no answer at all.

**A GCP CAS DevOps-tier pool keeps no certificate records and publishes no CRL.** A
subordinate issued from one therefore cannot be revoked in any way a relying party will
observe: the only real remedy is rotating the root and re-establishing trust on every
target — precisely the estate-wide operation this design exists to avoid, now happening
under incident conditions. The honest options:

- **Enterprise tier** for this path, which buys certificate records and CRL publishing at
  roughly an order of magnitude more than DevOps tier's standing fee
  (`cert_gcp_cas_tier` in Settings).
- **Accept it with a very short subordinate lifetime**, so the un-revokable window is
  bounded by expiry. Defensible, and probably right for a lab — but write it down rather
  than assuming it.
- **Keep the lab and any real deployment on different tiers**, which is the likely outcome
  and should be explicit in configuration rather than discovered.

Whichever is chosen, *"cannot revoke the issuing CA"* is not a footnote.

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

### A third consumer, which holds nothing

`ci-fetch-cert.yml` proves the mechanism, and it does so with a Password Safe client id
and secret supplied to the run. The [Agent Demo Cell](../profiles/demo/agent-demo-cell.md)
is the consumer that has neither: it reaches Password Safe with a workload identity
brokered by Workload Credentials, requests the passphrase as a recorded request,
downloads the bundle with `ps-cli secrets download-secret-file` into one `0700` directory
it removes afterwards, and presents the certificate to an mTLS endpoint.

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
  [the revocation caveat](#the-revocation-caveat-that-matters-most-here).
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
