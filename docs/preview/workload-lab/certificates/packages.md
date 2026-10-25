# The certificate plugin's two packages

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are installing the certificate plugin and need to know which package fits which platform.

Part of [Certificates](../certificates.md).

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

## One functional account per platform

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

## The nine backends

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

## How each of the four signs one, and the grant that follows

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

## Two refusals, and which one you get

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
