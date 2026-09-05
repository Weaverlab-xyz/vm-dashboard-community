# Design: managing PRA's session-issuing CA with Password Safe

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are considering making Password Safe the issuer and rotator of the CA that PRA Vault uses to mint session certificates.

**Nothing described here is built.** This is the reasoning behind a proposal, recorded
before the code so the PKI owners can refuse it on the merits rather than after a sprint.
Facts about the PRA Vault side are marked where they come from the product rather than from
anything this repo has exercised.

**Scope: cloud CA backends, against services whose trust store the operator controls.** ADCS
is excluded on policy (§7) and cloud *database* session access — the use case this looks
like it should serve — is excluded on mechanism (§8). Read §8 before proposing it again;
the reason it fails is not visible from either product's documentation.

## The problem, and the inversion that makes it tractable

PRA Vault can hold a certificate authority and issue **short-lived x.509 certificates from
it at session launch**, with the rest of the chain uploaded alongside it as additional
trust. That changes what is worth managing.

The instinct, coming from [Certificates](../certificates.md), is to treat a leaf as the
managed credential: Password Safe issues it, Secrets Safe holds the bundle, something
delivers it into a session. For PRA that is wrong twice over. PRA does not need a leaf —
it mints its own. And the delivery problem that would dominate the work
(`examples/playbooks/certificates/ci-fetch-cert.yml` generalised to every session) exists
only because we were moving the wrong artifact.

The credential worth managing is the **issuer**. And today it is the one credential in this
whole picture that nothing rotates, nothing audits and nothing can revoke — while the
Certificate plugin governs leaves that PRA never asked for.

So: Password Safe issues a **subordinate CA**, PRA Vault holds it, PRA mints session leaves
beneath it, and Password Safe rotates the subordinate on a schedule. One governed
credential instead of N, and the leaves become short-lived enough that the plugin's
documented absence of CRL and OCSP checking stops mattering for them.

## 1. Rotating a CA is not rotating a token, and the chain topology is what decides

This is the whole design. Everything else is plumbing.

A bearer token rotation affects one consumer. A CA rotation affects **every relying party
that pinned it**, simultaneously. Get the topology wrong and the rotation schedule becomes
an estate-wide outage schedule.

Two arrangements, and only one of them can be automated:

| Targets trust | Rotating the CA in PRA means | Automatable |
|---|---|---|
| the **sub-CA** directly | a coordinated trust-store push to every host, in lockstep with the rotation | No |
| the **root**, with the sub-CA chaining to it | nothing — targets never move | Yes |

So the required shape is:

```
root CA (long-lived)  ─────────────►  target trust stores   (installed once)
     │                                        ▲
     └── signs sub-CA ──► PRA Vault ──► session leaf ───────┘
                              ▲                 (chains: leaf → sub-CA → root)
                     Password Safe rotates
```

**This works only because PRA accepts an additional trust in the chain alongside the signing
CA** — reported from the product, not verified here. Without it, PRA would have to be the
trust anchor itself, targets would pin whatever PRA holds, and the top row of that table
would be the only available arrangement. That single capability is what moves this from
unbuildable to buildable, and it is the first thing to re-confirm if any of this is revived
later.

A pleasant consequence worth stating, because it is not obvious: **rotation is graceful for
in-flight sessions.** Leaves already minted chain to the root, which has not changed, so
they stay valid until their own short expiry. There is no drain step and no break window —
which is the opposite of [k8s token rotation §6](k8s-sa-token-rotation.md), where the
revoke is the point.

## 2. The trust anchor is already an output — this is a second consumer, not new work

`terraform/cert_ca/gcp_cas/main.tf:221-224` already emits exactly what targets need to
trust:

```hcl
output "ca_chain_pem" {
  value = join("\n", google_privateca_certificate_authority.this.pem_ca_certificates)
  description = "The CA chain the mTLS endpoint must trust — feed this to the nginx endpoint playbook as ssl_client_certificate"
}
```

Today it goes into nginx's `ssl_client_certificate` for the mTLS demo endpoint. It is the
same artifact PRA needs as its uploaded chain trust, and `publish_ca_cert = true` is already
set (`main.tf:119`). Nothing here needs inventing.

## 3. The lab CA currently forbids a subordinate, on purpose

`main.tf:153-156`:

```hcl
ca_options {
  is_ca = true
  # One level: this root signs leaf certificates directly, and nothing below it
  # may itself be a CA.
  max_issuer_path_length = 0
}
```

A one-line change to `1`, but it was a deliberate narrowing and the comment above it becomes
false, so it should be changed with intent rather than silently. The lab CA was scoped to
issue leaves because that is all the Certificate plugin could ask for; widening it is the
first structural admission that PRA is a different kind of consumer.

## 4. The plugin has no CA vocabulary, and the address budget fights the fix

Two gaps, and the second is the awkward one.

**No CA semantics anywhere.** `_CERT_COMMON_KEYS` (`ps_resource_service.py:337-344`) is a
closed set of leaf-shaped options, and an unrecognised key is *refused* rather than passed
through (`ps_resource_service.py:426-433`) — deliberately, because the plugin only warns on
a typo and carries on with a default. That guard is right and should stay; it just means
`isca=` and name constraints are new grammar, not free-form additions.

The plugin can already generate a CA — `selfsignedtest` does — but that path is refused
outright because it "generates and persists its own CA private key UNENCRYPTED beside the
plugin" (`ps_resource_service.py:414-420`). The capability half-exists; the key handling is
harness-grade. Do not reach for it.

**And the 255-character address is a real ceiling here.** Every value rides the managed
system's Network Address because `appsettings.json` ships inside the `.psplugin`
(`_MAX_MANAGED_SYSTEM_ADDRESS = 255`, `ps_resource_service.py:114`). A `gcpcas` profile with
`project`, `location` and `pool` already runs to ~120 characters before anything else.

The tension: **the control that makes this design defensible is name constraints (§5), and
name constraints are long.** A permitted-DNS-subtree list plus a permitted-UPN suffix can
exceed 100 characters on its own. So the safest configuration is the one least likely to
fit, and the failure mode is the one [Certificates](../certificates.md) already warns about
— a truncated address loses whatever sat at its end and reads as *absent* rather than as
damage.

That is a genuine design problem, not a detail to solve during implementation. Options, in
the order worth trying: put the constraints on the **parent** CA where they are inherited
and cost the address nothing; carry them on the managed **account** name after a `?`, where
per-identity values already go; or accept that a constrained sub-CA needs its own managed
system per constraint set. The first is almost certainly right and removes the tension
entirely — CAS supports name constraints at the pool's issuance policy, so the sub-CA
inherits a boundary it cannot widen.

## 5. Name constraints are what make an automated sub-CA defensible

A vaulted CA key is not a credential to one system. It is the authority to **mint an
identity for anything the CA is permitted to assert**. Automating its issuance and rotation
raises the value of that vault account considerably, and "it's in a vault" is not by itself
an answer to that.

Name constraints turn a compromise from *mint anything* into *mint within a bounded
namespace*, which is the difference between an incident and a catastrophe. They are the
single control that most changes the risk posture here, and the CAS module has no
`name_constraints` block today.

Pair them with a short sub-CA lifetime and a parent the automation cannot reach. The
subordinate should be the only thing Password Safe can issue; the root's own key should be
somewhere this dashboard has no path to.

## 6. The DevOps tier cannot revoke a subordinate — and that is the sharp edge

Short-lived leaves make leaf revocation close to irrelevant, which is the usual answer to
the plugin consulting neither CRLs nor OCSP.

**A compromised sub-CA is a different question**, and the lab's cost model has already
answered it in a way nobody chose for this use case. From `main.tf:118`:

> A DevOps-tier pool keeps no certificate records and cannot publish a CRL anyway.

So a sub-CA issued from the lab's DevOps-tier pool **cannot be revoked**. If its key leaks,
the only remedy is to rotate the *root* and re-establish trust on every target — precisely
the estate-wide operation §1 was designed to avoid, now happening under incident conditions
rather than on a schedule.

The honest options:

- **Enterprise tier for this path.** Buys certificate records and CRL publishing. The module
  header notes it is "an order of magnitude" more than the DevOps tier's ~$20/month, which
  is a real change to a feature whose entire justification is that a forgotten CA is the
  expensive mistake.
- **Accept it, with a very short sub-CA lifetime.** If the subordinate lives days rather
  than months, the un-revokable window is bounded by its own expiry. This is defensible and
  probably right for a lab, and it must be *written down* rather than assumed.
- **Keep the lab and any real deployment on different tiers**, which is the likely outcome
  and should be explicit in config rather than discovered.

Whichever is chosen, it belongs in the docs as a stated limitation, in the register of
[Certificates § What this feature does not do](../certificates.md). "Cannot revoke the
issuing CA" is not a footnote.

## 7. ADCS is out of scope, and the approval requirement is why

The Certificate plugin requires `msPKI-Enrollment-Flag = 0` — **no CA certificate manager
approval** — because otherwise every rotation returns `CR_DISP_UNDER_SUBMISSION` and a
rotation job cannot block on a human clicking Issue. The ADCS playbook sets it
(`examples/playbooks/windows/adcs-pipeline-template.yml:89-90`) and
[Certificates](../certificates.md) explains why.

For a client-auth leaf that constraint is a mild operational annoyance. For a **subordinate
CA, issued unattended, on a rotation schedule**, it is the thing most PKI teams gate
absolutely — a sub-CA is a delegation of the authority itself, and "no human approves it" is
a hard no in a lot of organisations regardless of how good the surrounding controls are.

The cloud backends do not have this problem: a GCP CAS `certificateRequester` binding or an
AWS PCA `IssueCertificate` permission is IAM-scoped, reviewable, revocable and logged, which
is a defensible story in a way "we turned off approval on the sub-CA template" is not.

**So this design covers cloud CA backends only.** Shipping ADCS support would mostly produce
a feature customers are forbidden to enable, and building it to be refused is worse than not
building it.

## 8. Cloud database session access is the one place this cannot land

The obvious first use case — PRA mints a short-lived certificate that authenticates a
**cloud database** session — is blocked twice over, and the two blockers are independent.
This section exists because it is the use case everyone proposes first, and the reason it
fails is not visible from either product's documentation.

**The engine matrix inverts perfectly.** From [databases.md:92-97](../databases.md) and
`terraform_pra_service._DB_TUNNEL_RESOURCE`:

| Engine | PRA tunnel | Backend TLS | x.509 client auth |
|---|---|---|---|
| postgres | `sra_postgresql_tunnel_jump` | **cleartext** | yes |
| mysql | `sra_my_sql_tunnel_jump` | **cleartext** | yes |
| sqlserver | `sra_protocol_tunnel_jump` (`mssql`) | TDS-aware, does its own | **none — no client-cert login type** |
| oracle | `sra_protocol_tunnel_jump` (`tcp`) | raw TCP | only via Oracle Wallet |

The two engines that support certificate authentication are reached over a tunnel that
proxies the **cleartext** wire protocol — that is how PRA injects the Vault credential and
records the session, and the dedicated jumps have no backend-TLS option. All three clouds
disable transport security to accommodate it: `rds.force_ssl=0`
(`terraform/db_postgres/main.tf:85-91`), `ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED`
(`db_gcp_postgres/main.tf:96`), `require_secure_transport=OFF`
(`db_azure_postgres/main.tf:110-120`).

**A client certificate is a TLS handshake artifact.** With no TLS on the jumpoint→DB hop
there is no handshake in which to present one. This is not a configuration gap that could be
closed — the cleartext proxy *is* the credential-injection and session-recording mechanism.

And the one engine whose tunnel does negotiate backend TLS, SQL Server, has no
client-certificate login type at all: it authenticates by SQL login, Windows auth or Entra.
Its TLS is for encryption, never identity.

**The second blocker stands even if the first were solved.** These modules provision
*managed* services — `aws_db_instance`, `google_sql_database_instance`,
`azurerm_postgresql_flexible_server` — and managed database services do not accept an
operator-supplied CA for verifying client certificates. Cloud SQL issues client certificates
from its own CA; RDS and Flexible Server expose no client-verification CA setting. A PRA-held
subordinate would have nothing to chain into.

So the ground this design actually stands on is **services whose trust store the operator
controls** — the mTLS endpoint pattern (`examples/playbooks/certificates/nginx-mtls-endpoint.yml`,
already fed by `ca_chain_pem`), and self-managed engines on a VM. A cloud-database variant
would need a different tunnel primitive from PRA, not a different certificate.

## 9. The credential has to land whole, and the split model does not survive the promotion

Today a leaf is split on purpose: the managed account holds the PKCS#12 passphrase, Secrets
Safe holds the bundle, and both halves are governed. The docs are explicit that the folder
ACL and the account's access policy are both live controls and *"the weaker of the two is
your real access boundary"* ([certificates.md:47-49](../certificates.md)).

For a leaf that is a sound design and a good demonstration. For a **CA signing key** the
same sentence stops being a caution and becomes a finding: a folder-permission mistake would
expose the issuer. So the sub-CA must reach PRA Vault as one object — key, certificate and
chain together — and never be parked half-and-half across two ACL domains on the way.

Whether that is possible depends on what PRA Vault's CA account accepts (PKCS#12? PEM key
plus chain? how is the passphrase handled?). **That is the single unknown that most shapes
the implementation**, and it should be answered before any code is written, because a PRA
side that only accepts the two halves separately would invalidate this section and probably
the feature.

## 10. Reuse the sync primitive — the lesson from k8s tokens generalises

`ps_api_service.link_synced_account` (`ps_api_service.py:979`) already does the delivery:
`POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}` makes one managed account a
subscriber of another, and **Password Safe owns the propagation from then on**. It carries
the k8s bearer token into PRA Vault today and would carry a CA unchanged.

Two of its properties matter more here than there:

- **Direction is unguarded by the API.** Both path segments are plain account ids, so a
  swapped pair links happily and syncs *backwards*. Pin it with tests, as the k8s path does.
- **`expect_subscriber_platform` fails closed.** Linking a CA signing key to an account
  managed by some other plugin is the one failure that puts a secret somewhere it does not
  belong, and the guard already exists.

[k8s-sa-token-rotation.md §4](k8s-sa-token-rotation.md) records a watermark reconciler that
was deleted wholesale once the product's own primitive was found. **Do not rebuild it here.**
Every failure mode that reconciler managed was created by the dashboard being in the data
path, and the same would be true again.

## Open questions — answer these before writing code

1. **What does PRA Vault's CA account accept?** PKCS#12, or PEM key plus chain? How is the
   passphrase supplied? §8 depends entirely on this.
2. **Can PRA be pointed at a subordinate whose root is installed separately on targets?**
   Reported yes, via the additional chain upload — this is the load-bearing assumption of
   §1 and deserves one confirmed round trip before anything is built.
3. **What sub-CA lifetime, against what leaf lifetime?** Drives §6's un-revokable window and
   the rotation cadence.
4. **Does PRA re-read the CA mid-session, or only at session launch?** Decides whether a
   rotation landing mid-session is genuinely invisible or merely usually invisible.
5. **Which tier** for a non-lab deployment, given §6.

## Operator prerequisites this dashboard cannot automate

- Install the **root** chain (`ca_chain_pem`) into every target's trust store. This is the
  step that makes rotation safe, and it happens once, outside anything here.
- Upload the chain as PRA Vault's additional trust alongside the signing CA.
- Grant the API identity the same Password Safe roles the k8s path needs for
  `SyncedAccounts` — Account Management (Full control), plus a Smart Rule containing both
  accounts. There is no Smart Rule API; this is out-of-band and it is the failure every
  Password Safe path in this repo hits first.
- Leave **Change Password After Release** off on both accounts. Under synced accounts a
  change on *either* member rotates the pair — which here means re-issuing the CA.
