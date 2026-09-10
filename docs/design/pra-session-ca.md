# Design: managing PRA's session-issuing CA with Password Safe

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are making Password Safe the issuer and rotator of the CA that PRA Vault uses to mint session certificates, or deciding whether to.

**The Password Safe half is now built. Nothing on this side of it is.** The Certificate
plugin issues a subordinate CA on either cloud backend — `isca=true`, with `pathlen=` and
name constraints — and refuses it on ADCS and self-signed, for the reasons §7 gives. It is
documented in **`Beekeeper-Certificate.docx`** ("Issuing a subordinate CA instead of a
leaf"), with a lab procedure in **`Beekeeper-Certificate-TestCase.docx`** ("Topology D — a
subordinate CA for PRA session certificates"). Both were written against this note, so the
reasoning below and the plugin's own reasoning are one argument; where they diverge now,
the plugin is the fact and this note has been corrected to match.

What remains unbuilt is everything between Password Safe and PRA. Open question 1 — what
PRA Vault's CA account accepts — **is now answered from the product UI**, and §9 records
it: a PEM key, a passphrase and a certificate, as three fields on an **X.509 Parent
Certificate Authority** shared account. That is better news than this note expected,
because PRA splits the credential the same way Password Safe does — and it makes
`bundle=PemBundle` mandatory here rather than optional. What it leaves behind is a narrower
set of problems: the plugin's PEM bundle is a **zip**, the chain has no field on that form,
PRA's accepted PKCS#8 ciphers are undocumented, and the dashboard's own onboarding
validator refuses every new option (§4). So issuing and rotating a governed subordinate is
real and testable today; an end-to-end session is not.

Facts about the PRA Vault side are still marked where they come from the product rather
than from anything this repo has exercised.

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

§9 now shows what the account form looks like, and it carries **one** certificate field —
so the additional trust is presumably a second *X.509 Parent Certificate Authority* entry
holding the root, certificate and no key. That is a more specific guess than this paragraph
started with, and it is still a guess. Nothing below this line is safe until it is
confirmed; it remains open question 2.

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

`terraform/cert_ca/gcp_cas/main.tf`'s `ca_chain_pem` output already emits exactly what
targets need to trust:

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

The plugin can now ask for a subordinate, so this is no longer a hypothetical narrowing —
it is the reason a sub-CA request against the lab CA fails today. It fails at CAS rather
than in the plugin, which is the right place for it to fail, but the error names an
issuance policy rather than a missing flag, so it reads as a permissions problem.

**The answer is a second pool, not a wider lab root.**
`Beekeeper-Certificate-TestCase.docx` §6.2 builds `demo-subca-pool` with
`--max-chain-length=1` beside the existing one and leaves the leaf-issuing root alone.
That is better than flipping this line: the comment above it stays true, the mTLS endpoint
demo keeps a root that provably cannot sign a CA, and the two use cases stop sharing a
blast radius. So what the module needs is a `max_issuer_path_length` *variable* defaulting
to `0`, not an edit — and that change has not been made.

## 4. The vocabulary now exists in the plugin and is missing in the dashboard

The gap this section described has moved rather than closed, and it is worth being precise
about which side it is on now.

**The plugin has the grammar.** `isca=true` switches issuance from a leaf to a subordinate,
`pathlen=` (default `0`) bounds what may appear beneath it, and `permitdns=`,
`permitemail=`, `permitip=` and `excludedns=` carry name constraints. `permitip=` takes a
network address rather than a host — `10.0.0.0/8`, not `10.1.2.3/8` — which is the kind of
thing that would otherwise be discovered against a live CA.

**The refusal this section used to describe is now ours.** `_CERT_COMMON_KEYS`
(`ps_resource_service.py:337-344`) is still a closed set of leaf-shaped options, and an
unrecognised key is *refused* rather than passed through
(`ps_resource_service.py:424-433`) — deliberately, because the plugin only warns on a typo
and carries on with a default. So the dashboard cannot onboard a sub-CA managed system at
all today: `isca=true` fails validation as "not a recognised option", naming the alias
table. That guard is right and should stay; widening it is a deliberate change, not a
loosening.

**The shape of that change is known exactly, and one detail is not obvious.** The six keys
belong in `_CERT_COMMON_KEYS`, with an explicit refusal when the backend is `adcs`,
`selfsigned` or `selfsignedtest`, mirroring the plugin's own refusals (§7).
`_CERT_BACKEND_KEYS` cannot express this: it maps each key to exactly *one* owning backend
and rejects on `owner != backend`, so it has no way to say "either cloud backend". Reaching
for it would refuse `isca=` on `gcpcas` or on `awspca`, whichever was not named.

`selfsignedtest` still generates its own CA, and the dashboard still refuses that backend
outright because it "generates and persists its own CA private key UNENCRYPTED beside the
plugin" (`ps_resource_service.py:414-420`). The plugin now also refuses `isca=true` on it —
and on `selfsigned` — for a different and better reason (§7). Do not reach for either.

**And the 255-character address is a real ceiling here.** Every value rides the managed
system's Network Address because `appsettings.json` ships inside the `.psplugin`
(`_MAX_MANAGED_SYSTEM_ADDRESS = 255`, `ps_resource_service.py:114`). A `gcpcas` profile with
`project`, `location` and `pool` already runs to ~120 characters before anything else.

The tension: **the control that makes this design defensible is name constraints (§5), and
name constraints are long.** A `permitdns=` subtree list plus a `permitemail=` suffix can
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

**The arithmetic, now that there is a worked address to measure.**
`Beekeeper-Certificate-TestCase.docx` §6.3 fits a `gcpcas` sub-CA profile with one
permitted DNS suffix into **198 characters** — and that counts a `<project>` placeholder, so
a real project id plus two more suffixes of the same length lands past 250. Note what is
consuming it: `bundle=PemBundle` is mandatory on this topology (§9), and it is 17 characters
that cannot be dropped as a default. The ceiling is reachable rather than theoretical, and
it is reached by adding exactly the control §5 wants. §6.6 of that document makes "add a
second and third `permitdns=` and watch the length" a test step for this reason.

**And there is now a third escape, with a catch.** SDK 26.2 added
`ManagedSystem_Attributes` and `ManagedAccount_Attributes`; the plugin reads any attribute
prefixed `cert:` as the option after the prefix, so `cert:permitdns` costs nothing against
the address at all — attributes beat the field on the same object, and a profile can live
entirely in them with the Network Address left empty. Two caveats keep this from closing
the question. It is **off by default behind an `appsettings.json` switch**, which is the one
setting the plugin cannot take from a Password Safe field — so on Password Safe **Cloud**
this path is unavailable and the address remains the only surface. And whether the host
populates attributes for a plugin at all is unverified against a live BeyondInsight; if
they arrive empty, nothing here changes.

## 5. Name constraints are what make an automated sub-CA defensible

A vaulted CA key is not a credential to one system. It is the authority to **mint an
identity for anything the CA is permitted to assert**. Automating its issuance and rotation
raises the value of that vault account considerably, and "it's in a vault" is not by itself
an answer to that.

Name constraints turn a compromise from *mint anything* into *mint within a bounded
namespace*, which is the difference between an incident and a catastrophe. They are the
single control that most changes the risk posture here.

**The plugin now encodes them**, per RFC 5280 §4.2.1.10 and marked **critical** — so a
relying party that cannot interpret the extension refuses the certificate rather than
ignoring the boundary, which is the only version of this control worth having. Issuing a
subordinate with no constraints at all is permitted but logged as a warning.

**The CAS module still has no `name_constraints` block, and that is still where they
belong.** §4's recommendation — put them on the parent's issuance policy, where the
subordinate inherits a boundary it cannot widen and they cost nothing against the address
budget — is now also the plugin's own recommendation, for the same two reasons. The address
options are the fallback for a lab that wants to exercise the path, which is exactly how
`Beekeeper-Certificate-TestCase.docx` §6.4 uses the single `permitdns=` in its example.

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

**The plugin carries this rule as a caution rather than an enforcement, correctly.** It
cannot see the rotation schedule — Password Safe holds that, and the plugin is handed one
action at a time — so it warns on any subordinate issued for more than 45 days instead of
enforcing a ratio it has half the inputs for. The pairing therefore stays an operator
decision, and `lifetime=8d` against a 7-day account policy is the configuration
`Beekeeper-Certificate-TestCase.docx` §6.3 and §6.5 pin down for the lab. That is a
recorded answer for the lab rather than for a customer; question 6 stays open for the
latter, where the overlap has to be sized to the longest expected session.

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

**This is now the plugin's implemented behaviour, and it refuses in two directions.**
`isca=true` on `adcs` is refused with the approval-flag reasoning above. `isca=true` on
`selfsigned` and `selfsignedtest` is refused for a different and equally deliberate reason:
a self-signed CA certificate is a **new trust root**, not a subordinate — nothing above it
constrains what it may assert, and every relying party would have to be visited to trust it
and visited again to stop. Both refusals are policy positions rather than gaps, and both
are worth being able to explain, because a customer will ask about each.

**One AWS mechanic matters to the dashboard change in §4.** ACM PCA ignores the
basic-constraints and key-usage extensions in a submitted CSR and builds the certificate
from its template instead, so `isca=true` selects
`arn:aws:acm-pca:::template/SubordinateCACertificate_PathLen{N}/V1` from `pathlen=`. The
plugin reports a `templatearn=` that contradicts `isca=` rather than resolving it silently
— and it has to, because without that check a request for a CA returns a perfectly valid
*end-entity* certificate that fails at the relying party as an untrusted issuer, a long way
from the cause. Whatever validation the dashboard grows should mirror that check rather than
leave it to the plugin, since the dashboard is where the address is still editable.

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

## 9. What PRA Vault accepts — answered, and the split survives after all

Today a leaf is split on purpose: the managed account holds the PKCS#12 passphrase, Secrets
Safe holds the bundle, and both halves are governed. The docs are explicit that the folder
ACL and the account's access policy are both live controls and *"the weaker of the two is
your real access boundary"* ([certificates.md:47-49](../certificates.md)).

For a leaf that is a sound design and a good demonstration. For a **CA signing key** the
same sentence stops being a caution and becomes a finding: a folder-permission mistake would
expose the issuer. The instinct was therefore that the sub-CA must reach PRA Vault as one
object and never be parked half-and-half across two ACL domains on the way.

**The PRA side is now known, and it does not work the way that instinct assumed.** In
**Vault → Accounts → Add Shared Account**, Authentication offers **X.509 Parent Certificate
Authority**, and that account type takes three values:

| PRA Vault field | What goes in it | Where it comes from |
|---|---|---|
| the private-key box — *"Only PEM encoding is valid"* | the encrypted PKCS#8 key | the plugin's bundle |
| **Key Passphrase** | the passphrase that opens it | the **managed account's** credential |
| **X.509 Certificate** (required) | the subordinate's own certificate | the plugin's bundle |

So **PRA is PEM, not PKCS#12**, and `bundle=PemBundle` is therefore *mandatory* on this
topology rather than merely preferable — the key field states "Only PEM encoding is valid",
and a PKCS#12 would have to be taken apart with `openssl` before any of it could be pasted
in. `PemBundle` emits a zip of `cert.pem`, `key.pem`, `chain.pem` and `fullchain.pem`, and
those map onto that form field for field: `key.pem` and `cert.pem` into the two boxes,
`chain.pem` into neither.

**One cipher question could still stop the upload, and there is a lever for it.** `key.pem`
is an encrypted PKCS#8, AES-256-CBC by default. The presence of a Key Passphrase field says
encrypted keys are expected, but which ciphers PRA's PEM parser accepts is not documented.
If the upload is rejected, `pbe=Legacy` switches the PEM key to 3DES with the PKCS#12 KDF,
which every OpenSSL-era parser reads — weaker, and a compatibility lever rather than a
default. Worth knowing before a demo, because the failure would look like a malformed key.

The plugin's own suite now covers this format specifically — that the zip carries all four
files, that the passphrase opens the key and a wrong one does not, and that the key and
certificate are a pair by comparing public keys rather than assuming. So the artifacts are
proven to be the right shape; only the round trip into PRA is not.

**And PRA splits the credential the same way Password Safe does.** The passphrase is a
first-class field on the PRA side too, separate from the key it opens. This section
previously warned that a PRA side accepting only two separate halves "would invalidate this
section and probably the feature" — the opposite turned out to be true. The split is not a
compromise forced by `ECredentialType` having no certificate type; it is the shape both
products already use, and it is what makes §10's sync primitive applicable at all.

**Note the Private Key Options radio, because one choice ends this design.** "Generated by
BeyondTrust Privileged Remote Access" means PRA mints the CA key itself and it never
leaves — at which point Password Safe has nothing to issue and no credential to govern.
This design requires the other option, where the key is supplied. Worth stating explicitly
because the generated option is the one a PRA administrator would reach for by habit.

### What is left, and it is narrower than the original unknown

- **The bundle is a zip.** Secrets Safe holds `cert.pem` and the key *inside* an archive, so
  nothing can hand either to PRA as-is. A delivery step has to fetch the secret, unpack it
  and write two PEM values. That puts something in the data path, which §10 is precisely a
  warning about, and it is the part still unbuilt.
- **There is no chain field on that form.** `cert.pem` has a home; `chain.pem` does not. The
  tooltip says a parent CA is "utilized as a trust for Client Validation and Authentication"
  and that at least one must exist before an X.509 Client Certificate can be created, which
  reads as though the root is added as its own parent-CA entry — certificate only, no key —
  to supply the anchor. **That is the mechanism §1 rests on and it is still unverified.**
  Open question 2 is now sharper rather than answered: not "does PRA accept an additional
  trust" but "is a second X.509 Parent Certificate Authority account how you supply it, and
  does a leaf minted beneath the subordinate then validate to the root a target trusts."

## 10. Reuse the sync primitive — the lesson from k8s tokens generalises

`ps_api_service.link_synced_account` (`ps_api_service.py:979`) already does the delivery:
`POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}` makes one managed account a
subscriber of another, and **Password Safe owns the propagation from then on**. It carries
the k8s bearer token into PRA Vault today.

**But it carries one credential, and a sub-CA is three values.** This section used to say it
"would carry a CA unchanged"; §9 shows that is wrong. What a synced account propagates is
the managed account's *password* — here the passphrase, which is the one of the three values
Password Safe models as a credential at all. The certificate and key PEMs are a Secrets Safe
file secret, and no synced-account link reaches them. So the primitive covers one field of
three, and the rest is new work rather than reuse.

**Which makes the ordering a correctness argument, not a detail.** Every rotation mints a
new subordinate *and* a new passphrase. If the passphrase syncs to PRA automatically while
the PEMs are delivered by hand or on a different schedule, PRA ends up holding the previous
key encrypted under a passphrase that no longer matches it — and the sub-CA stops working
until someone notices. **Partial automation here is worse than none**, and the fix is the
same shape as the plugin's own write-first rule (§ "Why the write ordering is the
correctness argument" in `Beekeeper-Certificate.docx`): write the two PEMs into PRA first,
and let the passphrase land last. Any implementation that syncs the passphrase before it can
deliver the key has built the failure rather than avoided it.

**And one thing has to be established before the link can be made at all.** Whether an
**X.509 Parent Certificate Authority** account in PRA Vault is a valid synced-account
subscriber is unknown, and `expect_subscriber_platform` fails closed — so an unrecognised
platform name is refused, which is the guard working correctly but is also a hard stop until
the name is known. Establish it against a live PRA before assuming this path exists.

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

### The narrow security claim that does survive — and what it is really worth

§6's rule (validity period, not rotation interval) is what connects that section to this
one, and the connection is easy to overstate in both directions.

**The sound version of the argument** is mechanical: there is no CRL, so the only bound on a
leaked subordinate is its validity period; a validity short enough to matter is days; nobody
reissues a certificate authority by hand every week, so it quietly stops happening. **The
automation is therefore not the control — it is what makes the control sustainable.** That
survives scrutiny in a way "we rotate, so the old one goes away" never did.

**It does not fix the premise, though.** It bounds the exposure window from a leaked key
that, given the isolation above, has nowhere to be presented. Tightening a window on an
attack that cannot currently be executed is not a reason to build anything.

**Where it does real work is drift.** The isolation assumption lives in Terraform, not in
anything the database itself enforces, and `VALID_REGISTER_CLOUDS` exists precisely so
instances can be registered that were never provisioned under those rules. When that
happens the isolation silently stops being true and nobody re-runs this analysis. A short
validity bound is the control still standing when the assumption it depended on has lapsed.
That argument needs no belief that PRA will be compromised — only that architectures drift.

**And its best use is to give the governance story a number.** "If the issuing CA key
leaked, what is our exposure window?" answers *indefinite*, or "we would have to go look up
what we set," in almost every organisation. Here it answers **eight days, by policy,
enforced by the rotation schedule** — a figure that is auditable, controlled, and reportable.

The number is only worth as much as the automation under it, which is the honest caveat
while the delivery half is unbuilt. Password Safe rotates the subordinate on schedule
today; nothing yet puts the new one into PRA. Until §10's ordering problem is solved, eight
days is the design's claim rather than the deployment's, and it should be presented that way.

That is a risk-register artifact rather than a preventive control, which makes it governance
after all: governance with something quantitative under it instead of an assertion about
hygiene. Pitch it that way round. "We can state our worst-case exposure window and we
control it" is a stronger sentence than anything in the threat-model framing, and it is
also true.

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

## Open questions — 1 answered, 6 partly; answer the rest before writing code

1. ~~**What does PRA Vault's CA account accept?**~~ **Answered** — an *X.509 Parent
   Certificate Authority* shared account, taking a PEM private key, a **Key Passphrase**
   and the certificate as three separate fields. PEM only, so `bundle=PemBundle` is
   mandatory. §9 has the mapping and the problems it leaves.
2. **Is a second parent-CA account how the root chain is supplied, and does a leaf then
   validate?** Sharpened by the answer to 1 rather than settled: that form has no chain
   field, so the root presumably goes in as its own parent-CA entry, certificate and no
   key. This is still the load-bearing assumption of §1 and still deserves one confirmed
   round trip — mint a leaf beneath the subordinate and verify it against the root a target
   trusts — before anything is built.
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
   the rotation cadence. **Answered for the lab** — `lifetime=8d` against a 7-day account
   policy — and open for a customer, where the overlap has to be sized to the longest
   expected session rather than picked round.
7. **Does PRA re-read the CA mid-session, or only at session launch?** Decides whether a
   rotation landing mid-session is genuinely invisible or merely usually invisible.
8. **Which tier** for a non-lab deployment, given §6.
9. **Can an X.509 Parent Certificate Authority account be a synced-account subscriber, and
   under what platform name?** §10 cannot use the product's own propagation primitive
   without this, and `expect_subscriber_platform` fails closed, so a guess is refused
   rather than half-working.
10. **Does the host populate `ManagedSystem_Attributes` for a plugin action?** If it does,
    §4's address ceiling stops constraining the name constraints in §5. If they arrive
    empty the address is the only surface, and on Password Safe Cloud it is regardless.
11. **Which PKCS#8 ciphers does PRA's PEM parser accept?** The plugin's default is
    AES-256-CBC and `pbe=Legacy` drops it to 3DES. Cheap to answer — one upload — and worth
    answering before a demo, because a refused key looks like a malformed one (§9).

## Operator prerequisites this dashboard cannot automate

- Install the **root** chain (`ca_chain_pem`) into every target's trust store. This is the
  step that makes rotation safe, and it happens once, outside anything here.
- Upload the chain as PRA Vault's additional trust alongside the signing CA — on current
  evidence a second **X.509 Parent Certificate Authority** shared account holding the root's
  certificate with no private key (§9, unverified).
- On the signing CA's own account, choose the Private Key Options radio that **supplies** the
  key. "Generated by BeyondTrust Privileged Remote Access" is the habitual choice and it
  ends this design — a key PRA generates is a key Password Safe never issued and cannot
  govern (§9).
- Grant the API identity the same Password Safe roles the k8s path needs for
  `SyncedAccounts` — Account Management (Full control), plus a Smart Rule containing both
  accounts. There is no Smart Rule API; this is out-of-band and it is the failure every
  Password Safe path in this repo hits first.
- Leave **Change Password After Release** off on both accounts. Under synced accounts a
  change on *either* member rotates the pair — which here means re-issuing the CA.
