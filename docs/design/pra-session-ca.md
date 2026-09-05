# Design: managing PRA's session-issuing CA with Password Safe

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are considering making Password Safe the issuer and rotator of the CA that PRA Vault uses to mint session certificates.

**Nothing described here is built.** This is the reasoning behind a proposal, recorded
before the code so the PKI owners can refuse it on the merits rather than after a sprint.
Facts about the PRA Vault side are marked where they come from the product rather than from
anything this repo has exercised.

**The case for building it is governance, not threat mitigation** — §11 says so plainly,
including why the security argument is weak here and what the simpler stopping point is
(a hand-issued subordinate, in "Why not simply upload the root?" below). Read both before
pitching this to anyone.

**Scope: cloud CA backends, against services whose trust store the operator controls.** ADCS
is excluded on policy (§7). For databases: **self-managed PostgreSQL and MySQL now, MongoDB
when the `sra` provider ships a tunnel resource** — MongoDB being the best authentication fit
of the three and the only one that may reach a managed service (Atlas). SQL Server is out
permanently, having no client-certificate login type at all. Two external dependencies are
carried rather than fatal; both are in §8.

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

### Why not simply upload the root to PRA?

The first question anyone asks, and it deserves a real answer rather than an appeal to
PKI convention.

**Practically, it may not be available.** Issuing requires the CA's *private key*, and a
GCP CAS root's key lives in Google's HSM and is not exportable. `ca_chain_pem` (§2) gives
the public certificates, which is all the trust-store side needs — it is not what PRA
would need to mint. Uploading a root would mean generating one *outside* CAS specifically
so it could be exported, trading HSM protection on the anchor for convenience. Confirm
against the CA in question before assuming either way.

**Conceptually, the root is what every target has decided to believe**, and that decision
cannot be un-made without visiting every target. A root in PRA is unbounded (it can mint
anything), unrotatable (rotating means touching every trust store) and effectively
unrevocable, sitting online in a session-brokering appliance for a decade. A subordinate is
bounded, replaceable and short-lived, and the root goes back to doing one job.

**But there are three options here, not two, and the middle one is real:**

| | Root in PRA | Sub-CA issued by hand | Sub-CA issued + rotated by Password Safe |
|---|---|---|---|
| Root exposure | total | contained | contained |
| Rotate without touching targets | no | yes, manually | yes, automatically |
| Practical sub-CA lifetime | n/a | ~a year | days to weeks |
| Signing-key lifecycle audited | no | no | yes |
| Moving parts | fewest | few | most |

**Most of the containment is in the middle column** — issue a subordinate by hand, upload
it, leave the root alone. An afternoon's work, no plugin capability, none of the open
questions below. Anyone who only needs the security property should stop there, and this
note should not be read as arguing otherwise.

What the third column adds is that **rotation becomes cheap enough to do often**, and that
matters here for one specific reason: §6 establishes that a DevOps-tier pool cannot revoke
a subordinate at all. A short lifetime is then the *only* available mitigation, and short
lifetimes are only practical when rotation is automated. That is the narrow, honest case
for the automation — not that it prevents an attack, but that it shrinks a window nothing
else can close. The broader case for building it is §11, and it is not a security case.

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

**The same mechanism means rotation does not revoke, and this is the most important thing
on this page to get right.** A target validates a leaf by walking its chain to the root it
trusts; it neither knows nor cares which subordinate was current when the leaf was minted.
So after a rotation:

- certificates already issued from the previous subordinate **keep working** until that
  subordinate's own certificate expires; and
- anyone holding a **copy of the previous subordinate's key** can keep minting new,
  perfectly valid certificates for exactly as long.

Rotation replaces what PRA *has*. It takes nothing away from anyone else who may have it.
The k8s token analogy breaks down precisely here — that rotation revokes, this one does
not. §6 is where the consequence lands.

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

### The rule that makes the short-lifetime option actually work

Because rotation does not revoke (§1), **the security bound is the subordinate's validity
period, not the rotation interval.** These are easy to conflate and the difference is the
whole mitigation:

| Sub-CA validity | Rotation every | Concurrently valid authorities | Exposure after a key leak |
|---|---|---|---|
| 1 year | 7 days | ~52 | up to a year |
| 8 days | 7 days | 2 (briefly) | at most 8 days |

Rotating often while issuing long-lived subordinates buys **nothing**. It just accumulates
valid authorities, each of which can mint anything in scope until its own expiry, and each
of which is a copy someone might have taken.

So the rule is: **issue the subordinate with a validity just longer than the rotation
interval** — enough overlap that leaves from the outgoing one stay valid until the new one
is in place, and no more. Rotate at day 7 of an 8-day subordinate, and the old authority
dies on its own a day later. That is what turns "we rotate" into an actual bound, and it is
the answer open question 6 is really asking for.

Note the interaction with §1's graceful-rotation property: the overlap is what keeps
in-flight sessions working. Too little and rotation breaks live sessions; too much and the
bound loosens. The overlap should be sized to the longest expected session, not picked
round.

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

## 8. Databases: three candidate engines, and the constraints are outside this design

**Engine scope is PostgreSQL, MySQL and MongoDB** — the first two available now, MongoDB
held up by tooling rather than capability. SQL Server is excluded permanently and Oracle is
excluded for now; neither is a gap to close later:

| Engine | PRA tunnel | Backend TLS | x.509 client auth |
|---|---|---|---|
| postgres | `sra_postgresql_tunnel_jump` | **cleartext** | yes |
| mysql | `sra_my_sql_tunnel_jump` | **cleartext** | yes |
| mongodb | PRA has one; **no `sra` provider resource** | required by the auth mechanism | **native — the subject DN *is* the identity** |
| sqlserver | `sra_protocol_tunnel_jump` (`mssql`) | TDS-aware, does its own | **none — no client-cert login type** |
| oracle | `sra_protocol_tunnel_jump` (`tcp`) | raw TCP | only via an Oracle Wallet |

From [databases.md:92-97](../databases.md) and `terraform_pra_service._DB_TUNNEL_RESOURCE`.
SQL Server authenticates by SQL login, Windows auth or Entra; its TLS is for encryption,
never identity, so no certificate this design issues could ever log into it. Oracle's
certificate path runs through a Wallet — a different container and a different delivery
problem, not this one.

### MongoDB is the best fit on authentication and the only one blocked on tooling

Worth separating from the rest of the table, because its exclusion is not about capability
and could lift without anything changing in PRA's tunnel behaviour.

**On the authentication side it is the strongest candidate here, not a marginal one.**
`MONGODB-X509` maps the certificate's **subject DN** to a user in the `$external` database:
the certificate *is* the identity, natively, rather than a credential checked alongside one.
That is precisely what this design produces. And because MongoDB requires TLS for x.509
authentication, the cleartext problem that blocks Postgres and MySQL cannot arise by
construction — if PRA's MongoDB tunnel authenticates by certificate at all, it is already
doing backend TLS. (Product behaviour, not verified here.)

**It is excluded for a provisioning reason.** PRA has a MongoDB tunnel, but the
`beyondtrust/sra` Terraform provider ships no resource for it, and this repo brokers DB
tunnels with the provider and **never `btapi`** — stated in both
`cloud_database_service.py:18-20` and `database.py:1342-1343`, which is why
`VALID_ENGINES` is `{postgres, mysql, sqlserver, oracle}`. So MongoDB waits on a provider
resource, not on a product capability and not on anything in this design.

**It may also be the one engine where a *managed* service works.** MongoDB Atlas supports
self-managed X.509 authentication with an operator-supplied CA — unlike RDS, Cloud SQL and
Flexible Server, which accept none (below). If that holds, Atlas is the only managed cloud
database a PRA-held subordinate could chain into, which would make MongoDB the strongest
target overall rather than a deferred one. Product knowledge, unverified here, and worth
confirming before it is relied on.

**The blocker is a missing option on two provider resources, not an architectural
impossibility.** It is worth being precise about this, because the obvious reading of the
table is wrong in a way that would kill the feature for no reason.

Today the Postgres and MySQL jumps proxy the **cleartext** wire protocol, and all three
clouds disable transport security to accommodate them: `rds.force_ssl=0`
(`terraform/db_postgres/main.tf:85-91`), `ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED`
(`db_gcp_postgres/main.tf:96`), `require_secure_transport=OFF`
(`db_azure_postgres/main.tf:110-120`).

It does **not** follow that credential injection and session recording require cleartext.
A TLS-terminating proxy sees the plaintext protocol *inside* the TLS session, and PRA
already does exactly that on two of its four tunnel types:

- `tunnel_type=mssql` is TDS-aware and negotiates its own backend TLS — injecting and
  recording the whole time, against databases (Azure SQL) that cannot be set to cleartext
  at all (`terraform/db_azure_sqlserver/main.tf:77-80`);
- `tunnel_type=k8s` takes `url` + `ca_certificates` (`terraform_pra_service.py:1459`), so
  the tunnel does TLS to the backend and verifies it against supplied CA material.

So the gap is that `sra_postgresql_tunnel_jump` and `sra_my_sql_tunnel_jump` expose no
backend-TLS option — a product feature gap with in-product precedent, and the right thing to
ask BeyondTrust for. Not a reason to abandon the design.

**What the working shape needs is a certificate in two places.** In the TLS-terminating
proxy model the Gateway is a *client* of the database, so:

| Where | What it needs | Who issues it |
|---|---|---|
| the Gateway host | a **client** certificate to present to the DB | PRA, minted per session from the vaulted sub-CA |
| the database | a **server** certificate, plus our root in `ssl_ca` | the DB's own; the root comes from `ca_chain_pem` |

That is this design working as intended rather than a workaround: the short-lived leaf PRA
mints *is* the Gateway's client certificate, the DB validates it against the root it already
trusts, and §1's rotation safety carries over unchanged.

A raw `tunnel_type=tcp` forward is the other way to get a certificate onto the wire — the
client negotiates TLS end to end — but a raw forwarder sees no protocol, so it injects no
credential and records no session content. `_DB_TUNNEL_RESOURCE` hardcodes the dedicated
resources for both engines, so it is a code change either way. Mentioned because someone
will propose it; it trades away the reason for using PRA.

**Managed services are blocked separately, and that one is real.** `aws_db_instance`,
`google_sql_database_instance` and `azurerm_postgresql_flexible_server` do not accept an
operator-supplied CA for verifying client certificates — Cloud SQL issues client certificates
from its own per-instance CA, and RDS and Flexible Server expose no such setting (product
behaviour, not verified here). So even over a TLS-capable tunnel a PRA-held subordinate would
have nothing to chain into on a managed instance.

**Which leaves self-managed PostgreSQL and MySQL on a VM as the near-term target** — where
`pg_hba.conf`, `ssl_ca` and `REQUIRE X509` are the operator's to set. That is the same
property the mTLS endpoint pattern relies on
(`examples/playbooks/certificates/nginx-mtls-endpoint.yml`, already fed by `ca_chain_pem`):
the design works where the trust store is ours to configure. MongoDB, if its provider
resource lands, is the better target and possibly the only one that reaches a managed
service.

So the database scope is **certificate authentication to self-managed PostgreSQL and MySQL
now, MongoDB when the provider resource exists**, carrying two independent dependencies —
backend TLS plus a client certificate on the two dedicated tunnel resources, and an
`sra` MongoDB tunnel resource. Both are bounded external asks rather than design flaws, and
§1-§7 and §9-§11 stand independently of when either lands.

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

## 11. What this is actually for — a governance story, not a threat model

The reason to build this is **not** that it stops an attack, and pitching it that way
collapses under the first informed question. Recording the honest version here so nobody
has to rediscover it in front of a customer.

**Why the security case is weak.** A PRA compromise is unlikely, and reaching a database
session through PRA already passes MFA, an approval workflow and session audit. More
decisively, these databases are unreachable by any other route —
`terraform/db_postgres/main.tf` makes `publicly_accessible = false` load-bearing, with the
PRA tunnel as the only path in. So even a stolen subordinate key yields a certificate with
nowhere to present it. The compensating controls are real and the network isolation closes
the bypass those controls would otherwise miss.

What *would* change that: a database reachable another way (a peered network, a bastion, or
an on-prem instance registered rather than provisioned — `VALID_REGISTER_CLOUDS` is
deliberately wider than `VALID_CLOUDS`), the same CA being used beyond DB sessions, or an
auditor asking whether the issuing CA can be revoked, which has a compliance answer
independent of likelihood.

### The reframe that carries the story

**A certificate authority is a privileged account that nobody treats as one.** Look at the
profile of a signing key: it grants high privilege, is created once and never touched, has
an unknown number of copies in unknown hands, does not meaningfully expire, sits in no
vault, is never rotated or checked out, and outlives the person who made it.

That is the profile of the shared local admin password — the exact thing privileged access
management exists to fix. The category simply never got pointed at PKI. The pitch works
because it does not ask anyone to accept a new premise: they already believe static, shared,
unrotated privileged credentials are unacceptable. It shows them one they already hold,
somewhere they had not looked.

### What the arrangement establishes

- **"Who can issue?" becomes answerable.** Today the honest answer is "whoever holds a copy,
  and we do not know who that is." After, it is a managed account with an access policy and
  a checkout record. Answerability is a governance property regardless of whether anyone
  ever abuses the key.
- **The authority gets a lifecycle** — issued, held, rotated, retired, each with a date and
  an actor. CAs conventionally have none; they are created and forgotten.
- **Separation of duties, with the handoff recorded.** The root stays with the PKI team in
  the HSM; the subordinate that exercises it day to day lives in PRA; Password Safe brokers
  between them. No team holds both halves.
- **Least privilege applied to an issuer** — name constraints (§5) say which identities this
  authority may assert at all. Most CAs are unconstrained by default.
- **Time-bounded rather than standing authority** — the JIT argument, applied to an issuer
  instead of a session. But the bound is the subordinate's **validity period**, not the
  rotation interval; §6 has the rule, and getting it wrong makes the property illusory.
- **Retiring the authority is an access decision — but not an instant one.** Retire the
  managed account and Password Safe stops issuing, so PRA receives no replacement. The
  subordinate it already holds keeps working until it expires, and so does any copy of it
  (§1). Immediate revocation needs a CRL the targets actually consult, and §6 records that
  a DevOps-tier pool publishes none. This is still better than a conventional deployment,
  where withdrawing a CA is a change programme — but it is expiry-driven, and saying
  "revoked" in front of a customer would be wrong.
- **It closes the top of the audit chain.** PRA answers *who connected*. Nothing today
  answers *who authorised that identity to exist*. Those are different questions and only
  the first is currently covered.

### The demonstration

1. The CA is an inventory row — owner, expiry, standing cost. It is on the books.
2. Its credential is a managed account: approval to check out, retrieval recorded.
3. **Rotate it.** A new subordinate, sessions keep working, no target touched. This is the
   moment worth building the demo around — PKI practitioners assume rotating an issuing CA
   means an estate-wide change, and watching it not be one is the argument.
4. A live session whose certificate chains back to that governed credential.
5. Retire it, and the access is gone — the answer conventional PKI gives badly.

### Where the pitch is weak

- It addresses a problem most organisations do not feel yet, which is a slower sale than an
  incident narrative.
- The payoff is posture, not prevention. Do not let it drift back toward implying otherwise;
  that is what fails under scrutiny.
- It presumes the audience cares about PKI hygiene, which many do not until an auditor asks.
  The qualifying question is roughly "have you had a certificate-management finding?"

It lands best with regulated customers, anyone carrying such a finding, and anyone whose
mTLS build quietly multiplied their CA count without assigning an owner to any of them.

**The precedent already shipped.** [k8s ServiceAccount token
rotation](k8s-sa-token-rotation.md) is this same story with a different credential — a
bearer token nobody rotated, made into a managed account with Password Safe owning the
sync. "We already do this for tokens; the CA is the next unmanaged authority" is a much
easier opening than introducing the pattern cold.

## Open questions — answer these before writing code

1. **What does PRA Vault's CA account accept?** PKCS#12, or PEM key plus chain? How is the
   passphrase supplied? §9 depends entirely on this.
2. **Can PRA be pointed at a subordinate whose root is installed separately on targets?**
   Reported yes, via the additional chain upload — this is the load-bearing assumption of
   §1 and deserves one confirmed round trip before anything is built.
3. **Will `sra_postgresql_tunnel_jump` / `sra_my_sql_tunnel_jump` gain backend TLS with a
   client certificate?** The database half of §8 waits on this. Precedent exists inside the
   product (`mssql` terminates TLS, `k8s` takes `ca_certificates`), so this is a feature
   request with a worked example rather than a novel ask — and it is worth putting to
   BeyondTrust on its own merits, since a cleartext Gateway→DB hop is a finding for
   plenty of customers who will never use this design.
4. **Will the `beyondtrust/sra` provider ship a MongoDB tunnel resource?** MongoDB is the
   best authentication fit in §8 and is held up only by this — the repo brokers DB tunnels
   with the provider and never `btapi`. Ask alongside question 3; they go to the same team.
5. **Does MongoDB Atlas's self-managed X.509 accept our subordinate's chain?** If so it is
   the only managed cloud database this design reaches, which would change the §8 conclusion
   from "self-managed only" to "self-managed, plus Atlas".
6. **What sub-CA lifetime, against what leaf lifetime?** Drives §6's un-revokable window and
   the rotation cadence.
7. **Does PRA re-read the CA mid-session, or only at session launch?** Decides whether a
   rotation landing mid-session is genuinely invisible or merely usually invisible.
8. **Which tier** for a non-lab deployment, given §6.

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
