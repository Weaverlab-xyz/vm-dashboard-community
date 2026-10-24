# Onboarding a subordinate CA

> **Audience:** operator · **Profile:** `demo` · **Read this when:** the thing that will hold the credential mints its own certificates beneath it — PRA Vault issuing session certificates is the case this was built for.

> **Preview.** **No live subordinate-CA round trip has been done.** The upload format is
> settled and the plugin emits exactly the three PEM pieces PRA's account type takes, with
> the suite checking that the passphrase opens the key and that the key and certificate are
> a pair. What is untested is uploading it to a real PRA, minting a client certificate
> beneath it, and connecting — as is which PKCS#8 ciphers PRA's PEM parser accepts.

---

**Workload Lab → Certificates → Add subordinate CA**, offered only on a CA whose root can
sign one. Everything on the other two pages still applies — the same
[address grammar](certificates.md#the-address-is-the-entire-configuration-surface), the
same store, the same [two-halves split](certificates.md#what-the-plugin-does-in-one-paragraph),
and a CA [built and torn down](certificate-lab.md#building-a-ca) the same way — with one
inversion that changes what the demonstration is about.

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

## The topology is the whole design

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

## Rotation does not revoke, and the lifetime is what bounds you

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

## Name constraints are the control that makes it defensible

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
[255-character budget](certificates.md#the-255-character-budget-is-real) and the security
control pull against each other:** a permitted-DNS
list plus an email suffix can exceed 100 characters on its own, competing with the project,
location and pool names.

## What the certificate looks like, and what the form does not offer

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

Two options [the address](certificates.md#the-address-is-the-entire-configuration-surface)
normally **omits**, because the Subordinate CA package already
defaults them: `isca=true` and `bundle=PemBundle`. Restating them is harmless and reads as
documentation — it just spends budget the permitted-DNS list is competing for. An explicit
`isca=false` is a real override and survives; it makes that platform behave as the
Certificate platform does, which is legal and logged, and points at using the other
package.

## Handing the subordinate to PRA Vault

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

## The demonstration, and the step to build it around

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

## The revocation caveat that matters most here

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
